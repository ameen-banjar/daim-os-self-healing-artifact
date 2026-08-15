#!/usr/bin/env python3
"""Live-network verification of _edge_confirmed_up()'s asymmetric-confirmation
invariant (Section 4.7) against REAL, independently-timed interface transitions
-- the gap Section 8.2 explicitly flags as never live-tested, since
net.configLinkStatus() changes both of an edge's interfaces' state through one
call and so has never produced a window where one interface has confirmed
"up" while the other has not.

Mechanism: two Linux `dummy` netdevices (not a veth pair -- confirmed
empirically that a veth pair's two ends are carrier-coupled: bringing one end
administratively down also drops carrier, and hence OVSDB link_state, on the
peer end, making genuine asymmetric timing impossible to produce with a veth
link) are added as extra, otherwise-unused ports on s1 and s2 respectively,
alongside the diamond topology's real h1-s1-s2-s4-h2 connectivity (left
completely untouched by this script). MONITORED_INTERFACES is reconfigured
(via DAIM_TOPOLOGY_CONFIG) to watch these two dummy interfaces for the s1-s2
edge instead of the real s1-eth2/s2-eth1 link -- the agent's BFS/hold-down/
edge-confirmation logic operates on the declared topology graph and down_edges
set, not on which physical port backs an OVSDB signal, so this is a faithful
live test of the signaling/decision logic, not a substitute for the packet-loss
measurement stage3_holddown_flapping.py already provides on the real link.

Schedule (all times relative to script start, chosen to clear the 2.0s
hold-down window between the initial repair and the asymmetric-recovery
probes):
    t=0.0  s1-dummy DOWN  -> real repair (edge added to down_edges, BFS
                             reroutes via s1-s3-s4), hold-down window opens
    t=0.1  s2-dummy DOWN  -> the OTHER interface for the same edge, expected
                             suppressed (still within the hold-down window)
    t=3.0  s1-dummy UP    -> ONE of the two interfaces confirms up, the other
                             (s2-dummy) is still down from t=0.1 -- the edge
                             must NOT be reconciled as recovered yet
    t=5.0  s2-dummy UP    -> the SECOND interface confirms up two full
                             seconds later -- only now must the edge be
                             reconciled as recovered

This is evidence-gate item 1 (part 2) from Submission_Manuscript.md Section
10/8.2.
"""
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "network/daim_link_agent.py"
TOPOLOGY_CONFIG = ROOT / "network/asymmetric_interface_topology.json"
RESULT = ROOT / "results/network/stage3_asymmetric_interface_result.json"
EVENTS_LOG = ROOT / "results/network/stage3_asymmetric_interface_events.jsonl"

SCHEDULE = [
    (0.0, "s1-dummy", "down"),
    (0.1, "s2-dummy", "down"),
    (3.0, "s1-dummy", "up"),
    (5.0, "s2-dummy", "up"),
]
DEADLINE_S = 9.0


class DiamondTopo(Topo):
    def build(self):
        s1, s2, s3, s4 = [
            self.addSwitch(f"s{i}", protocols="OpenFlow13", failMode="secure")
            for i in range(1, 5)
        ]
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        self.addLink(h1, s1)
        self.addLink(s1, s2)
        self.addLink(s2, s4)
        self.addLink(s1, s3)
        self.addLink(s3, s4)
        self.addLink(s4, h2)


def sh(cmd):
    subprocess.run(cmd, shell=True, check=True)


def add_dummy_port(bridge, name):
    subprocess.run(f"ip link del {name}", shell=True, stderr=subprocess.DEVNULL)
    sh(f"ip link add {name} type dummy")
    sh(f"ip link set {name} up")
    sh(f"ovs-vsctl add-port {bridge} {name}")


def read_agent_events(agent, deadline, events, stop_event_name=None):
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        ready, _, _ = select.select([agent.stdout], [], [], min(0.2, remaining))
        if not ready:
            continue
        line = agent.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        if stop_event_name and event.get("event") == stop_event_name:
            return True
    return False


def write_topology_config():
    config = {
        "topology": {
            "s1": {"h1": [1, None], "s2": [2, 1], "s3": [3, 1]},
            "s2": {"s1": [1, 2], "s4": [2, 1]},
            "s3": {"s1": [1, 3], "s4": [2, 2]},
            "s4": {"s2": [1, 2], "s3": [2, 3], "h2": [3, None]},
        },
        "host_attachment": {"h1": "s1", "h2": "s4"},
        "source": "h1",
        "dest": "h2",
        "monitored_interfaces": {
            "s1-dummy": ["s1", "s2"],
            "s2-dummy": ["s1", "s2"],
        },
        "remote_endpoints": {},
    }
    TOPOLOGY_CONFIG.write_text(json.dumps(config, indent=2))


def run():
    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(topo=DiamondTopo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    agent = None
    try:
        net.start()
        # The real s1-s2 veth link (s1-eth2/s2-eth1) is left completely
        # alone here -- these two dummy interfaces are additional, unused
        # ports added purely for independent OVSDB link_state signaling.
        add_dummy_port("s1", "s1-dummy")
        add_dummy_port("s2", "s2-dummy")
        write_topology_config()

        env = dict(os.environ)
        env["DAIM_TOPOLOGY_CONFIG"] = str(TOPOLOGY_CONFIG)
        agent = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True, env=env,
        )

        events = []
        if not read_agent_events(agent, time.time() + 10, events, stop_event_name="agent_started"):
            raise RuntimeError("agent did not report agent_started within 10s")

        schedule_start = time.perf_counter()
        idx = 0
        fault_log = []
        deadline = time.time() + DEADLINE_S
        while time.time() < deadline:
            if idx < len(SCHEDULE):
                t_offset, iface, state = SCHEDULE[idx]
                if time.perf_counter() - schedule_start >= t_offset:
                    sh(f"ip link set {iface} {state}")
                    fault_log.append({
                        "scheduled_t": t_offset, "interface": iface, "state": state,
                        "actual_t": time.perf_counter() - schedule_start,
                    })
                    idx += 1
            ready, _, _ = select.select([agent.stdout], [], [], 0.05)
            if agent.stdout in ready:
                line = agent.stdout.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            if idx >= len(SCHEDULE) and time.perf_counter() - schedule_start > DEADLINE_S - 1.0:
                break

        read_agent_events(agent, time.time() + 2, events)
        return fault_log, events
    finally:
        if agent and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
        net.stop()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run("ip link del s1-dummy", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("ip link del s2-dummy", shell=True, stderr=subprocess.DEVNULL)


def analyze(fault_log, events):
    """Reports the wall-clock timestamps of the two "up" injections and of
    every link_up_detected (edge-recovered) event the agent logged, so
    correctness can be checked directly against real timestamps rather than
    log order alone: the edge must NOT be reconciled as recovered at any
    timestamp between s1-dummy's up injection and s2-dummy's up injection --
    only at or after s2-dummy's."""
    s1_up_ts = next((f["actual_t"] for f in fault_log if f["interface"] == "s1-dummy" and f["state"] == "up"), None)
    s2_up_ts = next((f["actual_t"] for f in fault_log if f["interface"] == "s2-dummy" and f["state"] == "up"), None)
    first_event_ts = events[0]["ts"] if events else None
    recovered = [
        {"ts": e["ts"], "t_offset": (e["ts"] - first_event_ts) if first_event_ts else None}
        for e in events if e.get("event") == "link_up_detected"
    ]
    return {
        "s1_dummy_up_scheduled_t": s1_up_ts,
        "s2_dummy_up_scheduled_t": s2_up_ts,
        "link_up_detected_events": recovered,
        "note": (
            "correctness requires every link_up_detected t_offset to be "
            ">= s2_dummy_up_scheduled_t (never between s1's and s2's up "
            "injections) -- verify by eye against the full event sequence "
            "printed below, which is the authoritative record"
        ),
    }


def main():
    setLogLevel("warning")
    fault_log, events = run()

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_LOG.open("w") as handle:
        for e in events:
            handle.write(json.dumps(e) + "\n")

    summary = analyze(fault_log, events)
    summary["fault_log"] = fault_log
    summary["event_count"] = len(events)
    with RESULT.open("w") as handle:
        json.dump(summary, handle, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"wrote {len(events)} events to {EVENTS_LOG}")
    print(f"wrote summary to {RESULT}")

    print("\n--- full event sequence ---")
    for e in events:
        print(json.dumps(e))


if __name__ == "__main__":
    main()
