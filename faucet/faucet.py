"""RyuApp shim between Ryu and Valve."""

# Copyright (C) 2013 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2015 Brad Cowie, Christopher Lorier and Joe Stringer.
# Copyright (C) 2015 Research and Education Advanced Network New Zealand Ltd.
# Copyright (C) 2015--2017 The Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import random
import signal

import ipaddress
import json

from config_parser import dp_parser
from config_parser_util import config_file_hash
from valve_util import btos, dpid_log, get_logger, kill_on_exception, get_sys_prefix
from valve import valve_factory
import faucet_api
import faucet_metrics
import valve_of


from ryu.base import app_manager
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.controller import dpset
from ryu.controller import event
from ryu.controller import ofp_event
from ryu.lib import hub
from ryu.lib.packet import ethernet
from ryu.lib.packet import packet
from ryu.lib.packet import vlan as ryu_vlan
from ryu.ofproto import ether
from ryu.services.protocols.bgp.bgpspeaker import BGPSpeaker


class EventFaucetReconfigure(event.EventBase):
    """Event used to trigger FAUCET reconfiguration."""
    pass


class EventFaucetResolveGateways(event.EventBase):
    """Event used to trigger gateway re/resolution."""
    pass


class EventFaucetHostExpire(event.EventBase):
    """Event used to trigger expiration of host state in controller."""
    pass


class EventFaucetMetricUpdate(event.EventBase):
    """Event used to trigger update of metrics."""
    pass


class EventFaucetAPIRegistered(event.EventBase):
    """Event used to notify that the API is registered with Faucet."""
    pass


class Faucet(app_manager.RyuApp):
    """A RyuApp that implements an L2/L3 learning VLAN switch.

    Valve provides the switch implementation; this is a shim for the Ryu
    event handling framework to interface with Valve.
    """
    OFP_VERSIONS = valve_of.OFP_VERSIONS
    _CONTEXTS = {
        'dpset': dpset.DPSet,
        'faucet_api': faucet_api.FaucetAPI
        }
    logname = 'faucet'
    exc_logname = logname + '.exception'

    def __init__(self, *args, **kwargs):
        super(Faucet, self).__init__(*args, **kwargs)

        # There doesnt seem to be a sensible method of getting command line
        # options into ryu apps. Instead I am using the environment variable
        # FAUCET_CONFIG to allow this to be set, if it is not set it will
        # default to valve.yaml
        sysprefix = get_sys_prefix()
        self.config_file = os.getenv(
            'FAUCET_CONFIG', sysprefix + '/etc/ryu/faucet/faucet.yaml')
        self.logfile = os.getenv(
            'FAUCET_LOG', sysprefix + '/var/log/ryu/faucet/faucet.log')
        self.exc_logfile = os.getenv(
            'FAUCET_EXCEPTION_LOG',
            sysprefix + '/var/log/ryu/faucet/faucet_exception.log')

        # Set the signal handler for reloading config file
        signal.signal(signal.SIGHUP, self.signal_handler)

        # Create dpset object for querying Ryu's DPSet application
        self.dpset = kwargs['dpset']

        # Setup logging
        self.logger = get_logger(
            self.logname, self.logfile, logging.DEBUG, 0)
        # Set up separate logging for exceptions
        self.exc_logger = get_logger(
            self.exc_logname, self.exc_logfile, logging.DEBUG, 1)

        # TODO: metrics instance can be passed to Valves also,
        # for DP specific instrumentation.
        prom_port = int(os.getenv('FAUCET_PROMETHEUS_PORT', '9244'))
        self.metrics = faucet_metrics.FaucetMetrics(prom_port)

        # Set up a valve object for each datapath
        self.valves = {}
        self.config_hashes, valve_dps = dp_parser(
            self.config_file, self.logname)
        for valve_dp in valve_dps:
            # pylint: disable=no-member
            valve_cl = valve_factory(valve_dp)
            if valve_cl is None:
                self.logger.error(
                    'Hardware type not supported for DP: %s', valve_dp.name)
            else:
                valve = valve_cl(valve_dp, self.logname)
                self.valves[valve_dp.dp_id] = valve
                valve.update_config_metrics(self.metrics)

        self.gateway_resolve_request_thread = hub.spawn(
            self.gateway_resolve_request)
        self.host_expire_request_thread = hub.spawn(
            self.host_expire_request)
        self.metric_update_request_thread = hub.spawn(
            self.metric_update_request)

        self.dp_bgp_speakers = {}
        self._reset_bgp()

        # Register to API
        api = kwargs['faucet_api']
        api._register(self)
        self.send_event_to_observers(EventFaucetAPIRegistered())

    def _bgp_update_metrics(self):
        """Update BGP metrics."""
        for dp_id, bgp_speakers in list(self.dp_bgp_speakers.items()):
            for vlan, bgp_speaker in list(bgp_speakers.items()):
                neighbor_states = list(json.loads(bgp_speaker.neighbor_state_get()).items())
                for neighbor, neighbor_state in neighbor_states:
                    # pylint: disable=no-member
                    self.metrics.bgp_neighbor_uptime_seconds.labels(
                        dpid=hex(dp_id), vlan=vlan.vid, neighbor=neighbor).set(
                            neighbor_state['info']['uptime'])
                    # pylint: disable=no-member
                    self.metrics.bgp_neighbor_routes.labels(
                        dpid=hex(dp_id), vlan=vlan.vid, neighbor=neighbor, ipv='4').set(
                            len(vlan.ipv4_routes))
                    # pylint: disable=no-member
                    self.metrics.bgp_neighbor_routes.labels(
                        dpid=hex(dp_id), vlan=vlan.vid, neighbor=neighbor, ipv='6').set(
                            len(vlan.ipv6_routes))

    def _bgp_route_handler(self, path_change, vlan):
        """Handle a BGP change event.

        Args:
            path_change (ryu.services.protocols.bgp.bgpspeaker.EventPrefix): path change
            vlan (vlan): Valve VLAN this path change was received for.
        """
        prefix = ipaddress.ip_network(btos(path_change.prefix))
        nexthop = ipaddress.ip_address(btos(path_change.nexthop))
        withdraw = path_change.is_withdraw
        flowmods = []
        valve = self.valves[vlan.dp_id]
        ryudp = self.dpset.get(valve.dp.dp_id)
        if vlan.is_faucet_vip(nexthop):
            self.logger.error(
                'BGP nexthop %s for prefix %s cannot be us',
                nexthop, prefix)
            return
        if not vlan.ip_in_vip_subnet(nexthop):
            self.logger.error(
                'BGP nexthop %s for prefix %s is not a connected network',
                nexthop, prefix)
            return

        if withdraw:
            self.logger.info(
                'BGP withdraw %s nexthop %s', prefix, nexthop)
            flowmods = valve.del_route(vlan, prefix)
        else:
            self.logger.info(
                'BGP add %s nexthop %s', prefix, nexthop)
            flowmods = valve.add_route(vlan, nexthop, prefix)
        if flowmods:
            self._send_flow_msgs(ryudp, flowmods)

    def _create_bgp_speaker_for_vlan(self, vlan):
        """Set up BGP speaker for an individual VLAN if required.

        Args:
            vlan (vlan): VLAN associated with this speaker.
        Returns:
            ryu.services.protocols.bgp.bgpspeaker.BGPSpeaker: BGP speaker.
        """
        handler = lambda x: self._bgp_route_handler(x, vlan)
        bgp_speaker = BGPSpeaker(
            as_number=vlan.bgp_as,
            router_id=vlan.bgp_routerid,
            bgp_server_port=vlan.bgp_port,
            best_path_change_handler=handler)
        for faucet_vip in vlan.faucet_vips:
            bgp_speaker.prefix_add(
                prefix=str(faucet_vip), next_hop=str(faucet_vip.ip))
        for route_table in (vlan.ipv4_routes, vlan.ipv6_routes):
            for ip_dst, ip_gw in list(route_table.items()):
                bgp_speaker.prefix_add(
                    prefix=str(ip_dst), next_hop=str(ip_gw))
        for bgp_neighbor_address in vlan.bgp_neighbor_addresses:
            bgp_speaker.neighbor_add(
                address=bgp_neighbor_address,
                remote_as=vlan.bgp_neighbor_as,
                local_address=vlan.bgp_local_address,
                enable_ipv4=True,
                enable_ipv6=True)
        return bgp_speaker

    def _reset_bgp(self):
        """Set up a BGP speaker for every VLAN that requires it."""
        # TODO: port status changes should cause us to withdraw a route.
        # TODO: configurable behavior - withdraw routes if peer goes down.
        for dp_id, valve in list(self.valves.items()):
            if dp_id not in self.dp_bgp_speakers:
                self.dp_bgp_speakers[dp_id] = {}
            bgp_speakers = self.dp_bgp_speakers[dp_id]
            for bgp_speaker in list(bgp_speakers.values()):
                bgp_speaker.shutdown()
            for vlan in list(valve.dp.vlans.values()):
                if vlan.bgp_as:
                    bgp_speakers[vlan] = self._create_bgp_speaker_for_vlan(vlan)

    def gateway_resolve_request(self):
        """Trigger gateway/nexthop re/resolution."""
        while True:
            self.send_event('Faucet', EventFaucetResolveGateways())
            hub.sleep(2 + random.randint(0, 2))

    def host_expire_request(self):
        """Trigger expiration of host state in controller."""
        while True:
            self.send_event('Faucet', EventFaucetHostExpire())
            hub.sleep(5 + random.randint(0, 2))

    def metric_update_request(self):
        while True:
            self.send_event('Faucet', EventFaucetMetricUpdate())
            hub.sleep(5 + random.randint(0, 2))

    def _send_flow_msgs(self, ryu_dp, flow_msgs):
        """Send OpenFlow messages to a connected datapath.

        Args:
            ryu_db (ryu.controller.controller.Datapath): datapath.
            flow_msgs (list): OpenFlow messages to send.
        """
        dp_id = ryu_dp.id
        if dp_id not in self.valves:
            self.logger.error('send_flow_msgs: unknown %s', dpid_log(dp_id))
            return
        valve = self.valves[dp_id]
        reordered_flow_msgs = valve.valve_flowreorder(flow_msgs)
        valve.ofchannel_log(reordered_flow_msgs)
        for flow_msg in reordered_flow_msgs:
            # pylint: disable=no-member
            self.metrics.of_flowmsgs_sent.labels(
                dpid=hex(dp_id)).inc()
            flow_msg.datapath = ryu_dp
            ryu_dp.send_msg(flow_msg)

    # pylint: disable=unused-argument
    def signal_handler(self, sigid, frame):
        """Handle any received signals.

        Args:
            sigid (int): signal to handle.
            frame (frame): stack frame.
        """
        if sigid == signal.SIGHUP:
            self.send_event('Faucet', EventFaucetReconfigure())

    def _config_changed(self, new_config_file):
        """Return True if configuration has changed.

        Args:
            new_config_file (str): name, possibly new, of FAUCET config file.
        Returns:
            bool: True if the file, or any file it includes, has changed.
        """
        if new_config_file != self.config_file:
            return True
        for config_file, config_hash in list(self.config_hashes.items()):
            config_file_exists = os.path.isfile(config_file)
            # Config file not loaded but exists = reload.
            if config_hash is None and config_file_exists:
                return True
            # Config file loaded but no longer exists = reload.
            if config_hash and not config_file_exists:
                return True
            # Config file hash has changed = reload.
            new_config_hash = config_file_hash(config_file)
            if new_config_hash != config_hash:
                return True
        return False

    @set_ev_cls(EventFaucetReconfigure, MAIN_DISPATCHER)
    def reload_config(self, ryu_event):
        """Handle a request to reload configuration.

        Args:
            ryu_event (ryu.controller.event.EventReplyBase): triggering event.
        """
        new_config_file = os.getenv('FAUCET_CONFIG', self.config_file)
        if self._config_changed(new_config_file):
            self.config_file = new_config_file
            self.config_hashes, new_dps = dp_parser(
                new_config_file, self.logname)
            for new_dp in new_dps:
                valve = self.valves[new_dp.dp_id]
                flowmods = valve.reload_config(new_dp)
                ryudp = self.dpset.get(new_dp.dp_id)
                if ryudp is not None:
                    self._send_flow_msgs(ryudp, flowmods)
                self._reset_bgp()
                valve.update_config_metrics(self.metrics)
        else:
            self.logger.info('configuration is unchanged, not reloading')
        # pylint: disable=no-member
        self.metrics.faucet_config_reload_requests.inc()

    @set_ev_cls(EventFaucetResolveGateways, MAIN_DISPATCHER)
    def resolve_gateways(self, ryu_event):
        """Handle a request to re/resolve gateways.

        Args:
            ryu_event (ryu.controller.event.EventReplyBase): triggering event.
        """
        for dp_id, valve in list(self.valves.items()):
            flowmods = valve.resolve_gateways()
            if flowmods:
                ryudp = self.dpset.get(dp_id)
                self._send_flow_msgs(ryudp, flowmods)

    @set_ev_cls(EventFaucetHostExpire, MAIN_DISPATCHER)
    def host_expire(self, ryu_event):
        """Handle a request expire host state in the controller.

        Args:
            ryu_event (ryu.controller.event.EventReplyBase): triggering event.
        """
        for valve in list(self.valves.values()):
            valve.host_expire()
            valve.update_metrics(self.metrics)

    @set_ev_cls(EventFaucetMetricUpdate, MAIN_DISPATCHER)
    def metric_update(self, ryu_event):
        """Handle a request to update metrics in the controller.

        Args:
            ryu_event (ryu.controller.event.EventReplyBase): triggering event.
        """
        self._bgp_update_metrics()

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER) # pylint: disable=no-member
    @kill_on_exception(exc_logname)
    def _packet_in_handler(self, ryu_event):
        """Handle a packet in event from the dataplane.

        Args:
            ryu_event (ryu.controller.event.EventReplyBase): packet in message.
        """
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id

        if not dp_id in self.valves:
            self.logger.error('_packet_in_handler: unknown %s', dpid_log(dp_id))
            return

        valve = self.valves[dp_id]
        valve.ofchannel_log([msg])

        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocols(ethernet.ethernet)[0]
        eth_type = eth_pkt.ethertype

        # Packet ins, can only come when a VLAN header has already been pushed
        # (ie. when we have progressed past the VLAN table). This gaurantees
        # a VLAN header will always be present, so we know which VLAN the packet
        # belongs to.
        if eth_type == ether.ETH_TYPE_8021Q:
            # tagged packet
            vlan_proto = pkt.get_protocols(ryu_vlan.vlan)[0]
            vlan_vid = vlan_proto.vid
        else:
            return

        in_port = msg.match['in_port']
         # pylint: disable=no-member
        self.metrics.of_packet_ins.labels(
            dpid=hex(dp_id)).inc()
        flowmods = valve.rcv_packet(
            dp_id, self.valves, in_port, vlan_vid, pkt)
        self._send_flow_msgs(ryu_dp, flowmods)
        valve.update_metrics(self.metrics)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER) # pylint: disable=no-member
    @kill_on_exception(exc_logname)
    def _error_handler(self, ryu_event):
        """Handle an OFPError from a datapath.

        Args:
            ryu_event (ryu.controller.ofp_event.EventOFPErrorMsg): trigger
        """
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id
        if dp_id in self.valves:
            # pylint: disable=no-member
            self.metrics.of_errors.labels(
                dpid=hex(dp_id)).inc()
            self.valves[dp_id].ofchannel_log([msg])
            self.logger.error('Got OFError: %s', msg)
        else:
            self.logger.error('_error_handler: unknown %s', dpid_log(dp_id))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER) # pylint: disable=no-member
    def handler_features(self, ryu_event):
        """Handle receiving a switch features message from a datapath.

        Args:
            ryu_event (ryu.controller.ofp_event.EventOFPStateChange): trigger.
        """
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id
        if dp_id in self.valves:
            flowmods = self.valves[dp_id].switch_features(dp_id, msg)
            self._send_flow_msgs(ryu_dp, flowmods)
        else:
            self.logger.error('handler_features: unknown %s', dpid_log(dp_id))

    @set_ev_cls(dpset.EventDP, dpset.DPSET_EV_DISPATCHER)
    @kill_on_exception(exc_logname)
    def handler_connect_or_disconnect(self, ryu_event):
        """Handle connection or disconnection of a datapath.

        Args:
            ryu_event (ryu.controller.dpset.EventDP): trigger.
        """
        ryu_dp = ryu_event.dp
        dp_id = ryu_dp.id

        # Datapath down message
        if not ryu_event.enter:
            if dp_id in self.valves:
                # pylint: disable=no-member
                self.metrics.of_dp_disconnections.labels(
                    dpid=hex(dp_id)).inc()
                self.logger.debug('%s disconnected', dpid_log(dp_id))
                self.valves[dp_id].datapath_disconnect(dp_id)
            else:
                self.logger.error(
                    'handler_connect_or_disconnect: unknown %s', dpid_log(dp_id))
            return

        # pylint: disable=no-member
        self.metrics.of_dp_connections.labels(
            dpid=hex(dp_id)).inc()
        self.logger.debug('%s connected', dpid_log(dp_id))
        self.handler_datapath(ryu_dp)

    @set_ev_cls(dpset.EventDPReconnected, dpset.DPSET_EV_DISPATCHER)
    @kill_on_exception(exc_logname)
    def handler_reconnect(self, ryu_event):
        """Handle reconnection of a datapath.

        Args:
            ryu_event (ryu.controller.dpset.EventDPReconnected): trigger.
        """
        ryu_dp = ryu_event.dp
        self.logger.debug('%s reconnected', dpid_log(ryu_dp.id))
        self.handler_datapath(ryu_dp)

    def handler_datapath(self, ryu_dp):
        """Handle any/all re/dis/connection of a datapath.

        Args:
            ryu_dp (ryu.controller.controller.Datapath): datapath.
        """
        dp_id = ryu_dp.id
        if dp_id in self.valves:
            discovered_up_port_nums = [
                port.port_no for port in list(ryu_dp.ports.values()) if port.state == 0]
            flowmods = self.valves[dp_id].datapath_connect(
                dp_id, discovered_up_port_nums)
            self._send_flow_msgs(ryu_dp, flowmods)
        else:
            self.logger.error('handler_datapath: unknown %s', dpid_log(dp_id))

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER) # pylint: disable=no-member
    @kill_on_exception(exc_logname)
    def port_status_handler(self, ryu_event):
        """Handle a port status change event.

        Args:
            ryu_event (ryu.controller.ofp_event.EventOFPPortStatus): trigger.
        """
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id
        ofp = msg.datapath.ofproto
        reason = msg.reason
        port_no = msg.desc.port_no

        if dp_id not in self.valves:
            self.logger.error(
                'port_status_handler: unknown %s', dpid_log(dp_id))
            return

        valve = self.valves[dp_id]
        flowmods = []
        if reason == ofp.OFPPR_ADD:
            flowmods = valve.port_add(dp_id, port_no)
        elif reason == ofp.OFPPR_DELETE:
            flowmods = valve.port_delete(dp_id, port_no)
        elif reason == ofp.OFPPR_MODIFY:
            port_down = msg.desc.state & ofp.OFPPS_LINK_DOWN
            if port_down:
                flowmods = valve.port_delete(dp_id, port_no)
            else:
                flowmods = valve.port_add(dp_id, port_no)
        else:
            self.logger.warning('Unhandled port status %s for port %u',
                                reason, port_no)

        self._send_flow_msgs(ryu_dp, flowmods)

    def get_config(self):
        config = {}
        for valve in list(self.valves.values()):
            valve_conf = valve.get_config_dict()
            for k in ('dps', 'acls', 'vlans'):
                config.setdefault(k, {})
                config[k].update(valve_conf[k])
        return config

    def get_tables(self, dp_id):
        return self.valves[dp_id].dp.get_tables()
