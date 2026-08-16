"""Formal baseline 2 of 2 (Section 10): a real controller-driven recovery
path, using the os_ken OpenFlow framework (the same one already used for
Paper 1's Stage-2 baseline controllers in this repository) against the
identical diamond topology and s1-s2 fault injection every single-host live
experiment in this evidence set uses.

Unlike daim_link_agent.py (an independent client process watching OVSDB
Interface.link_state via a push-based `ovsdb-client monitor` subscription,
computing its own BFS reroute, and issuing flow-mod calls through the local
daim_ovs_flow adapter), this is the CLASSIC centralized-SDN-controller model:
the switches' OpenFlow control channel itself is the only path for both
detection (an OFPT_PORT_STATUS message from the switch when the link's local
carrier state changes) and repair (OFPT_FLOW_MOD messages pushed back down
that same channel, confirmed via an OFPT_BARRIER_REQUEST/REPLY round trip --
the OpenFlow analogue of the agent's own read-back-confirmed flow
installation). This measures the CONTROL-CHANNEL round-trip cost (PortStatus
notification -> compute -> FlowMod -> BarrierReply) as a point of comparison
against the agent's own OVSDB-push + local Python BFS + local adapter-exec
cost, on the same topology and fault.

Path recomputation here is deliberately a HARDCODED two-path swap (primary
s1-s2-s4, alternate s1-s3-s4), not a general BFS -- this is a baseline
representative of a scripted/hardcoded controller reaction, not a
reimplementation of daim_link_agent.py's own generic algorithm.
"""
import json
import time

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3

# Explicit dpid->name map, matching the dpid= values the harness assigns
# each switch in DiamondTopo (1=s1, 2=s2, 3=s3, 4=s4).
S1, S2, S3, S4 = 1, 2, 3, 4

# Port numbers, matching daim_link_agent.py's own TOPOLOGY dict for the
# diamond exactly (s1: h1=1,s2=2,s3=3; s2: s1=1,s4=2; s3: s1=1,s4=2;
# s4: s2=1,s3=2,h2=3).
S1_TO_H1, S1_TO_S2, S1_TO_S3 = 1, 2, 3
S2_TO_S1, S2_TO_S4 = 1, 2
S3_TO_S1, S3_TO_S4 = 1, 2
S4_TO_S2, S4_TO_S3, S4_TO_H2 = 1, 2, 3


def log(event, **fields):
    record = {"ts": time.time(), "event": event}
    record.update(fields)
    print(json.dumps(record), flush=True)


class RecoveryBaselineController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}
        self.repaired = False
        self._port_down_ns = None
        log("controller_started")

    def _flow_mod(self, dp, priority, in_port, out_port, cmd=None):
        p, o = dp.ofproto_parser, dp.ofproto
        match = p.OFPMatch(in_port=in_port)
        actions = [p.OFPActionOutput(out_port)]
        inst = [p.OFPInstructionActions(o.OFPIT_APPLY_ACTIONS, actions)]
        kwargs = dict(datapath=dp, priority=priority, match=match, instructions=inst)
        if cmd is not None:
            kwargs["command"] = cmd
        dp.send_msg(p.OFPFlowMod(**kwargs))

    def _delete_flow(self, dp, in_port):
        p, o = dp.ofproto_parser, dp.ofproto
        match = p.OFPMatch(in_port=in_port)
        dp.send_msg(p.OFPFlowMod(
            datapath=dp, command=o.OFPFC_DELETE, out_port=o.OFPP_ANY,
            out_group=o.OFPG_ANY, match=match,
        ))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def features(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        if dp.id == S1:
            self._flow_mod(dp, 100, S1_TO_H1, S1_TO_S2)
            self._flow_mod(dp, 100, S1_TO_S2, S1_TO_H1)
        elif dp.id == S2:
            self._flow_mod(dp, 100, S2_TO_S1, S2_TO_S4)
            self._flow_mod(dp, 100, S2_TO_S4, S2_TO_S1)
        elif dp.id == S4:
            self._flow_mod(dp, 100, S4_TO_S2, S4_TO_H2)
            self._flow_mod(dp, 100, S4_TO_H2, S4_TO_S2)
        log("switch_connected", dpid=dp.id)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status(self, ev):
        msg = ev.msg
        dp = msg.datapath
        port_no = msg.desc.port_no
        is_down = bool(msg.desc.state & dp.ofproto.OFPPS_LINK_DOWN)
        if not (dp.id == S1 and port_no == S1_TO_S2 and is_down):
            return
        if self.repaired:
            return
        self.repaired = True
        detect_ns = time.perf_counter_ns()
        log("link_down_detected", dpid=dp.id, port=port_no, ns=detect_ns)

        s1, s2, s4 = self.dps.get(S1), self.dps.get(S2), self.dps.get(S4)
        s3 = self.dps.get(S3)
        if not (s1 and s4 and s3):
            log("repair_failed", reason="missing_datapath")
            return

        # Reroute s1<->h1 traffic via s3 instead of s2; wire s3's pass-
        # through flows (previously unused, primary path never touched
        # s3); reroute s4<->h2 traffic to arrive via s3 instead of s2.
        self._delete_flow(s1, S1_TO_H1)
        self._flow_mod(s1, 100, S1_TO_H1, S1_TO_S3)
        self._flow_mod(s1, 100, S1_TO_S3, S1_TO_H1)
        self._flow_mod(s3, 100, S3_TO_S1, S3_TO_S4)
        self._flow_mod(s3, 100, S3_TO_S4, S3_TO_S1)
        self._delete_flow(s4, S4_TO_S2)
        self._flow_mod(s4, 100, S4_TO_H2, S4_TO_S3)
        self._flow_mod(s4, 100, S4_TO_S3, S4_TO_H2)

        # A Barrier Request/Reply round trip on s1 is the OpenFlow-native
        # way to confirm every FlowMod sent before it has actually been
        # applied by the switch -- the control-channel analogue of the
        # agent's own read-back-confirmed apply_flow() contract.
        p = s1.ofproto_parser
        s1.send_msg(p.OFPBarrierRequest(s1))
        self._barrier_pending_since_ns = detect_ns

    @set_ev_cls(ofp_event.EventOFPBarrierReply, MAIN_DISPATCHER)
    def barrier_reply(self, ev):
        if getattr(self, "_barrier_pending_since_ns", None) is None:
            return
        end_ns = time.perf_counter_ns()
        start_ns = self._barrier_pending_since_ns
        self._barrier_pending_since_ns = None
        log("repair_installed", repair_start_ns=start_ns, repair_end_ns=end_ns,
            path=["s1", "s3", "s4"])
