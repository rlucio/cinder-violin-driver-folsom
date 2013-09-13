# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Violin Memory, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
Violin Memory iSCSI Driver for Openstack Cinder (Folsom release)

Uses Violin REST API via XG Tools to manage a vSHARE licensed
memory gateway to provide network block-storage services.

---

Each allocated lun is configured as writable with a 512b blocksize.

*** Note that The fields for zero'ing the lun or performing thin
provisioning are not supported by vSHARE at this time. ***

Each new volume/lun is exported to a new iSCSI target specifically
made for it. The idea is that this allows CHAP authentication to be
managed independently on a per-volume basis.  The export is then
configured to use a specific initiator group (igroup) that has been
pre-configured for use by Nova/Cinder hosts.

When an initiator has been chosen to connect to one of the available
luns (eg via 'nova volume-attach'), it will be added to the configured
igroup allowing it to see the export.  It will also be given any
target location and authentication information needed to connect to
the chosen lun.
"""

import time
import random

from cinder import exception
from cinder import flags
from cinder.openstack.common import log as logging
from cinder.openstack.common import cfg
from cinder.volume.driver import ISCSIDriver

# TODO: add version checking for xgtools libraries
#
from vxg.core.session import XGSession
from vxg.core.node import XGNode
import vxg

LOG = logging.getLogger(__name__)

violin_opts = [
    cfg.StrOpt('gateway_vip',
               default='',
               help='IP address or hostname of the v6000 master VIP'),
    cfg.StrOpt('gateway_mga',
               default='',
               help='IP address or hostname of mg-a'),
    cfg.StrOpt('gateway_mgb',
               default='',
               help='IP address or hostname of mg-b'),
    cfg.StrOpt('gateway_user',
               default='admin',
               help='User name for connecting to the Memory Gateway'),
    cfg.StrOpt('gateway_password',
               default='',
               help='User name for connecting to the Memory Gateway'),
    cfg.IntOpt('gateway_iscsi_port',
               default=3260,
               help='IP port to use for iSCSI targets'),
    cfg.StrOpt('gateway_iscsi_target_prefix',
               default='iqn.2004-02.com.vmem:',
               help='prefix for iscsi volumes'),
    cfg.StrOpt('gateway_iscsi_igroup_name',
               default='openstack',
               help='name of igroup for initiators'),
]

FLAGS = flags.FLAGS
FLAGS.register_opts(violin_opts)

class InvalidBackendConfig(exception.CinderException):
    message = _("Volume backend config is invalid: %(reason)s")

class ViolinDriver(ISCSIDriver):
    """ Executes commands relating to Violin Memory Arrays """

    def __init__(self, *args, **kwargs):
        super(ViolinDriver, self).__init__(*args, **kwargs)
        self.session_start_time = 0
        self.session_timeout = 900
        self.array_info = []
        self.vmem_vip=None
        self.vmem_mga=None
        self.vmem_mgb=None
        self.container=""
        self.device_id=""

    def do_setup(self, context):
        """ Any initialization the driver does while starting """
        if not FLAGS.gateway_vip:
            raise exception.InvalidInput(reason=_(
                    'Gateway VIP is not set'))

        if not FLAGS.gateway_mga:
            raise exception.InvalidInput(reason=_(
                    'Gateway IP for mg-a is not set'))

        if not FLAGS.gateway_mgb:
            raise exception.InvalidInput(reason=_(
                    'Gateway IP for mg-b is not set'))

        self.vmem_vip = vxg.open(FLAGS.gateway_vip, FLAGS.gateway_user,
                                 FLAGS.gateway_password)

        self.vmem_mga = vxg.open(FLAGS.gateway_mga, FLAGS.gateway_user,
                                 FLAGS.gateway_password)

        self.vmem_mgb = vxg.open(FLAGS.gateway_mgb, FLAGS.gateway_user,
                                 FLAGS.gateway_password)

        self.gateway_iscsi_ip_addresses_mga = self._get_active_iscsi_ips(self.vmem_mga)
        for ip in self.gateway_iscsi_ip_addresses_mga:
            self.array_info.append({ "node": FLAGS.gateway_mga,
                                     "addr": ip,
                                     "conn": self.vmem_mga })

        self.gateway_iscsi_ip_addresses_mgb = self._get_active_iscsi_ips(self.vmem_mgb)
        for ip in self.gateway_iscsi_ip_addresses_mgb:
            self.array_info.append({ "node": FLAGS.gateway_mgb,
                                     "addr": ip,
                                     "conn": self.vmem_mgb })

        vip = self.vmem_vip.basic

        ret_dict = vip.get_node_values("/vshare/state/local/container/*")
        if ret_dict:
            self.container = ret_dict.items()[0][1]

        ret_dict = vip.get_node_values(
            "/media/state/array/%s/chassis/system/dev_id" % self.container)
        if ret_dict:
            self.device_id = ret_dict.items()[0][1]

        ret_dict = vip.get_node_values("/wsm/inactivity_timeout")
        if ret_dict:
            self.timeout = ret_dict.items()[0][1]

    def check_for_setup_error(self):
        """Returns an error if prerequisites aren't met"""
        vip = self.vmem_vip.basic

        if len(self.container) == 0:
            raise InvalidBackendConfig(reason=_('container is missing'))

        if len(self.device_id) == 0:
            raise InvalidBackendConfig(reason=_('device ID is missing'))

        bn = "/vshare/config/iscsi/enable"
        resp = vip.get_node_values(bn)
        if resp[bn] != True:
            raise InvalidBackendConfig(reason=_('iSCSI is not enabled'))

        bn = "/vshare/config/igroup/%s" % FLAGS.gateway_iscsi_igroup_name
        resp = vip.get_node_values(bn)
        if len(resp.keys()) == 0:
            raise InvalidBackendConfig(reason=_('igroup is missing'))

        if len(self.gateway_iscsi_ip_addresses_mga) == 0:
            raise InvalidBackendConfig(reason=_(
                    'no available iSCSI IPs on mga'))

        if len(self.gateway_iscsi_ip_addresses_mgb) == 0:
            raise InvalidBackendConfig(reason=_(
                    'no available iSCSI IPs on mgb'))

    def create_volume(self, volume):
        """ Creates a volume """
        self._login()
        self._create_lun(volume)

    def create_volume_from_snapshot(self, volume, snapshot):
        """ Creates a volume from a snapshot """
        raise NotImplementedError()

    def delete_volume(self, volume):
        """ Deletes a volume """
        self._login()
        self._delete_lun(volume)

    def create_snapshot(self, snapshot):
        """ Creates a snapshot from an existing volume """
        raise NotImplementedError()

    def delete_snapshot(self, snapshot):
        """ Deletes a snapshot """
        raise NotImplementedError()

    def ensure_export(self, context, volume):
        """Synchronously checks and re-exports volumes at cinder start time """
        # NYI
        pass

    def create_export(self, context, volume):
        """ Exports the volume """
        self._login()
        vol = self._get_short_name(volume['name'])
        tgt = self._create_iscsi_target(volume)
        lun = self._export_lun(volume)
        self.vmem_vip.basic.save_config()

        iqn = "%s%s:%s" % (FLAGS.gateway_iscsi_target_prefix, tgt['node'], vol)
        provider_data = { 'provider_location': self._iscsi_location(
                tgt['addr'], FLAGS.iscsi_port, iqn, lun) }
        return provider_data

    def remove_export(self, context, volume):
        """ Removes an export for a logical volume """
        self._login()
        self._unexport_lun(volume)
        self._delete_iscsi_target(volume)
        self.vmem_vip.basic.save_config()

    def check_for_export(self, context, volume_id):
        """Make sure volume is exported."""
        # NYI
        pass

    def initialize_connection(self, volume, connector):
        """Initializes the connection (target<-->initiator) """
        self._login()
        self._add_igroup_member(connector)
        self.vmem_vip.basic.save_config()
        return super(ViolinDriver, self).initialize_connection(volume, connector)

    def terminate_connection(self, volume, connector):
        """ Terminates the connection (target<-->initiator) """
        super(ViolinDriver, self).terminate_connection(volume, connector)
        self._login()
        self._remove_igroup_member(connector)
        self.vmem_vip.basic.save_config()

    def _create_lun(self, volume):
        """
        Creates a new lun.

        The equivalent CLI command is "lun create container
        <container_name> name <lun_name> size <gb>"

        Arguments:
            volume -- volume object provided by the Manager
        """
        v = self.vmem_vip

        LOG.info(_("[VMEM] Creating lun %s (%d GB)"),
                 volume['name'], volume['size'])

        # using the defaults for other fields: (container, name, size,
        # quantity, nozero, thin, readonly, startnum, blksize)
        #
        for i in range(3):
            self._wait_for_lockstate()
            resp = v.lun.create_lun(self.container, volume['name'],
                                    volume['size'], 1, "0", "0", "w", 1, 512)
            if resp['code'] == 0 and not 'try again later' in resp['message']:
                break

        if resp['code'] != 0 or 'try again later' in resp['message']:
            raise exception.Error(_('Failed to create LUN: %d, %s')
                                  % (resp['code'], resp['message']))

    def _delete_lun(self, volume):
        """
        Deletes a lun.

        The equivalent CLI command is "no lun create container
        <container_name> name <lun_name>"

        Arguments:
            volume -- volume object provided by the Manager
        """
        v = self.vmem_vip

        LOG.info(_("[VMEM] Deleting lun %s"), volume['name'])

        for i in range(3):
            self._wait_for_lockstate()
            resp = v.lun.bulk_delete_luns(self.container, volume['name'])
            if resp['code'] == 0 and not 'try again later' in resp['message']:
                break

        if resp['code'] != 0 or 'try again later' in resp['message']:
            raise exception.Error(_('Failed to delete LUN: %d, %s')
                                  % (resp['code'], resp['message']))

    def _create_iscsi_target(self, volume):
        """
        Creates a new target for use in exporting a lun

        Openstack does not yet support multipathing. We still create
        HA targets but we pick a single random target for the
        Openstack infrastructure to use.  This at least allows us to
        evenly distribute LUN connections across the storage cluster.
        The equivalent CLI commands are "iscsi target create
        <target_name>" and "iscsi target bind <target_name> to
        <ip_of_mg_eth_intf>".

        Arguments:
            volume -- volume object provided by the Manager

        Returns:
            reference to randomly selected target object
        """
        v = self.vmem_vip
        target_name = self._get_short_name(volume['name'])

        LOG.info(_("[VMEM] Creating iscsi target %s"), target_name)

        resp = v.iscsi.create_iscsi_target(target_name)

        if resp['code'] != 0:
            raise exception.Error(_('Failed to create iscsi target: %d, %s')
                                  % (resp['code'], resp['message']))

        resp = self.vmem_mga.iscsi.bind_ip_to_target(
            target_name, self.gateway_iscsi_ip_addresses_mga)

        if resp['code'] != 0:
            raise exception.Error(_("Failed to bind iSCSI targets: %d, %s")
                                  % (resp['code'], resp['message']))

        resp = self.vmem_mgb.iscsi.bind_ip_to_target(
            target_name, self.gateway_iscsi_ip_addresses_mgb)

        if resp['code'] != 0:
            raise exception.Error(_("Failed to bind iSCSI targets: %d, %s")
                                  % (resp['code'], resp['message']))

        return self.array_info[random.randint(0, len(self.array_info)-1)]

    def _delete_iscsi_target(self, volume):
        """
        Deletes the iscsi target for a lun

        iSCSI targets must be deleted from each gateway separately.
        The CLI equivalent is "no iscsi target create <target_name>".

        Arguments:
            volume -- volume object provided by the Manager
        """
        v = self.vmem_vip
        target_name = self._get_short_name(volume['name'])

        LOG.info(_("[VMEM] Deleting iscsi target for %s"), target_name)

        resp = self.vmem_mga.iscsi.unbind_ip_from_target(
            target_name, self.gateway_iscsi_ip_addresses_mga)

        if resp['code'] != 0:
            raise exception.Error(_("Failed to unbind iSCSI targets: %d, %s")
                                  % (resp['code'], resp['message']))

        resp = self.vmem_mgb.iscsi.unbind_ip_from_target(
            target_name, self.gateway_iscsi_ip_addresses_mgb)

        if resp['code'] != 0:
            raise exception.Error(_("Failed to unbind iSCSI targets: %d, %s")
                                  % (resp['code'], resp['message']))

        resp = v.iscsi.delete_iscsi_target(target_name)

        if resp['code'] != 0:
            raise exception.Error(_('Failed to delete iSCSI target: %d, %s')
                                  % (resp['code'], resp['message']))

    def _export_lun(self, volume):
        """
        Generates the export configuration for the given volume

        The equivalent CLI command is "lun export container
        <container_name> name <lun_name>"

        Arguments:
            volume -- volume object provided by the Manager

        Returns:
            lun_id -- the LUN ID assigned by the backend
        """
        v = self.vmem_vip
        target_name = self._get_short_name(volume['name'])

        LOG.info(_("[VMEM] Exporting lun %s"), volume['name'])

        resp = v.lun.export_lun(self.container, volume['name'], target_name,
                                FLAGS.gateway_iscsi_igroup_name, -1)

        if resp['code'] != 0:
            raise exception.Error(_('LUN export failed: %d, %s')
                                  % (resp['code'], resp['message']))

        self._wait_for_exportstate(volume['name'], True)

        lun_id = self._get_lun_id(self.container, volume['name'], target_name,
                                  FLAGS.gateway_iscsi_igroup_name)

        return lun_id

    def _unexport_lun(self, volume):
        """
        Removes the export configuration for the given volume.

        The equivalent CLI command is "no lun export container
        <container_name> name <lun_name>"

        Arguments:
            volume -- volume object provided by the Manager
        """
        v = self.vmem_vip

        LOG.info(_("[VMEM] Unexporting lun %s"), volume['name'])

        resp = v.lun.unexport_lun(self.container, volume['name'],
                                  "all", "all", -1)

        if resp['code'] != 0:
            raise exception.Error(_("LUN unexport failed: %d, %s")
                                  % (resp['code'], resp['message']))

        self._wait_for_exportstate(volume['name'], False)

    def _add_igroup_member(self, connector):
        """
        Add an initiator to the openstack igroup so it can see exports.

        The equivalent CLI command is "igroup addto name <igroup_name>
        initiators <initiator_name>"

        Arguments:
            connector -- connector object provided by the Manager
        """
        v = self.vmem_vip

        LOG.info(_("[VMEM] Adding initiator %s to igroup"),
                 connector['initiator'])

        resp = v.igroup.add_initiators(FLAGS.gateway_iscsi_igroup_name,
                                       connector['initiator'])

        if resp['code'] != 0:
            raise exception.Error(_('Failed to add igroup member: %d, %s')
                                  % (resp['code'], resp['message']))

    def _remove_igroup_member(self, connector):
        """
        Removes an initiator to the openstack igroup.

        The equivalent CLI command is "no igroup addto name
        <igroup_name> initiators <initiator_name>".

        Arguments:
            connector -- connector object passed from the manager
        """
        v = self.vmem_vip

        # do not remove the initiator from the igroup if it still has
        # any active sessions on the backend
        #
        ids = v.basic.get_node_values('/vshare/state/global/*')

        for i in ids:
            bn = "/vshare/state/global/%d/target/iscsi/**" % ids[i]
            iscsi_targets = v.basic.get_node_values(bn)

            for t in iscsi_targets:
                if iscsi_targets[t] == connector['initiator']:
                    return

        LOG.info(_("[VMEM] Removing initiator %s from igroup"),
                 connector['initiator'])

        resp = v.igroup.delete_initiators(FLAGS.gateway_iscsi_igroup_name,
                                          connector['initiator'])

        if resp['code'] != 0 and resp['code'] != 14036:
            # -code 14036, message 'Igroup <igroup> doesn't include
            #  initiator <initiator>'
            #
            raise exception.Error(_('Failed to remove igroup member: %s, %s')
                                  % (resp['code'], resp['message']))

    def _login(self, force=False):
        """
        Get new api creds from the backend, only if needed.

        Arguments:
            force -- re-login on all sessions regardless of last login time

        Returns:
           True if sessions were refreshed, false otherwise.
        """
        now = time.time()
        if abs(now - self.session_start_time) >= self.session_timeout or \
                force == True:
            self.vmem_vip.basic.login()
            self.vmem_mga.basic.login()
            self.vmem_mgb.basic.login()
            self.session_start_time = now
            return True
        return False

    def _get_lun_id(self, container_name, volume_name, target_name, igroup_name):
        """
        Queries the gateway to find the lun id for the exported volume.

        Arguments:
            container_name -- backend array flash container name
            volume_name    -- LUN to query
            target_name    -- iSCSI target associated with the LUN
            igroup_name    -- igroup associated with the LUN

        Returns:
            LUN ID for the exported lun as an integer.
        """
        vip = self.vmem_vip.basic
        bn = "/vshare/config/export/container/%s/lun/%s/target/%s/initiator/%s/lun_id" \
            % (container_name, volume_name, target_name, igroup_name)
        resp = vip.get_node_values(bn)
        return resp[bn]

    def _get_short_name(self, volume_name):
        """
        Creates a vSHARE-compatible iSCSI target name.

        The Folsom-style volume names are prefix(7) + uuid(36), which
        is too long for vSHARE for target names.  To keep things
        simple we can just truncate the name to 32 chars.

        Arguments:
            volume_name -- name of volume/lun

        Returns:
            Shortened volume name as a string.
        """
        return volume_name[:32]

    def _iscsi_location(self, ip, port, iqn, lun=None):
        """
        Create a properly formatted provider_location string.

        Arguments:
            ip          -- iSCSI target IP address
            port        -- iSCSI target service port
            iqn         -- iSCSI target IQN
            lun         -- ID of the exported LUN

        Returns:
            provider_location as a formatted string.
        """
        # the main driver.py _get_iscsi_properties() function has
        # broken field parsing for the location string made here.  We
        # work around this by putting a blank space for the third
        # field
        #
        return "%s:%s,%s%s %s" % (ip, port, " ", iqn, lun)

    def _wait_for_exportstate(self, volume_name, state=False):
        """
        Polls volume's export configuration root.

        XG sets/queries following a request to create or delete a
        lun export may fail on the backend if vshared is still
        processing the export action.  We can check whether it is
        done by polling the export binding for a lun to
        ensure it is created or deleted.

        Arguments:
            volume_name -- name of volume to be polled
            state       -- True to poll for existence, False for lack of

        Returns:
            True if the export state was eventually found, false otherwise.
        """
        status = False
        vip = self.vmem_vip.basic

        bn = "/vshare/config/export/container/%s/lun/%s" \
            % (self.container, volume_name)

        for i in range(30):
            resp = vip.get_node_values(bn)
            if state and len(resp.keys()):
                status = True
                break
            elif (not state) and (not len(resp.keys())):
                break
            else:
                time.sleep(1)
        return status

    def _wait_for_lockstate(self):
        """
        Polls configured backend LVM lock.

        Lun deletion will fail on the backend if vshared is still busy
        deleting a lun from a previous request.  We can check whether
        it is 'ready' by polling the LVM lockstate for each gateway.
        """
        vip = self.vmem_vip.basic

        opts1 = [ XGNode('container', 'string', self.container),
                  XGNode('port', 'uint8', 1),
                  XGNode('dev_id', 'string', self.device_id) ]

        opts2 = [ XGNode('container', 'string', self.container),
                  XGNode('port', 'uint8', 2),
                  XGNode('dev_id', 'string', self.device_id) ]

        for i in range(30):
            resp1 = vip.perform_action('/vshare/actions/vlock/lockstate', opts1)
            resp2 = vip.perform_action('/vshare/actions/vlock/lockstate', opts2)
            if resp1['message'][0] == '0' and resp2['message'][0]:
                break
            else:
                time.sleep(1)

    def _get_active_iscsi_ips(self, mg_conn):
        """
        Get a list of gateway IP addresses that can be used for iSCSI.

        Arguments:
            mg_conn -- active XG connection to one of the gateways

        Returns:
            active_gw_iscsi_ips -- list of IP addresses
        """
        active_gw_iscsi_ips = []
        interfaces_to_skip = [ 'lo', 'vlan10', 'eth1', 'eth2', 'eth3' ]

        bn = "/net/interface/config/*"
        intf_list = mg_conn.basic.get_node_values(bn)

        for i in intf_list:
            do_skip = False

            for s in interfaces_to_skip:
                if intf_list[i] == s:
                    do_skip = True
                    break

            if not do_skip:
                bn1 = "/net/interface/state/%s/addr/ipv4/1/ip" % intf_list[i]
                bn2 = "/net/interface/state/%s/flags/link_up" % intf_list[i]
                resp = mg_conn.basic.get_node_values([bn1, bn2])

                if len(resp.keys()) == 2 and resp[bn2] == True:
                    active_gw_iscsi_ips.append(resp[bn1])

        return active_gw_iscsi_ips
