#!/usr/bin/env python3
"""Live-network reproduction of the startup/restart scenario the initial-
snapshot fix (daim_link_agent.py::read_initial_snapshot) targets: the
s1-s2 link is brought down *before* the agent process is even started, so
the only way the agent can ever learn about it is from OVSDB's initial
table snapshot -- no "new"-actioned transition will ever arrive for an
interface that was already down when the monitor subscription began. This
is evidence-gate item 1 (startup-state synchronization) from
Submission_Manuscript.md Section 4.4/8.3.

Unlike stage3_holddown_flapping.py, this script does not exercise any
transition at all after agent start -- the entire point is to confirm the
agent's *very first* action (installing its initial path) already accounts
for a link that was down before it ever connected to OVSDB.
"""
import json
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
RESULT = ROOT / "results/network/stage3_startup_already_down_result.json"


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


def main():
    setLogLevel("warning")
    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(topo=DiamondTopo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    agent = None
    result = {"evidence_level": "measured_emulation_startup_already_down"}
    try:
        net.start()
        # Bring the s1-s2 link down BEFORE the agent ever starts, so the
        # only way it can find out is the initial OVSDB snapshot.
        net.configLinkStatus("s1", "s2", "down")
        time.sleep(0.5)

        agent = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        events = []
        started = read_agent_events(agent, time.time() + 10, events, stop_event_name="agent_started")
        result["agent_started_seen"] = started
        result["events"] = events

        started_event = next((e for e in events if e.get("event") == "agent_started"), None)
        result["initial_path"] = started_event.get("initial_path") if started_event else None
        result["down_edges_at_startup"] = started_event.get("down_edges") if started_event else None
        result["fatal_events"] = [e for e in events if e.get("event") == "fatal"]

        # The real proof: does traffic actually flow via the alternate path
        # from the very first ping, with no repair event ever needed?
        h1, h2 = net.get("h1", "h2")
        ping = h1.cmd("ping -c 20 -i 0.05 -W 1 10.0.0.2")
        result["ping_output_tail"] = ping[-300:]
        result["ping_had_loss"] = "0% packet loss" not in ping

        down_edges_as_sets = [set(e) for e in (result["down_edges_at_startup"] or [])]
        result["correct"] = (
            started
            and result["initial_path"] == ["s1", "s3", "s4"]
            and down_edges_as_sets == [{"s1", "s2"}]
            and not result["fatal_events"]
        )
    finally:
        if agent and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
        net.stop()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"wrote {RESULT}")
    print("CORRECT" if result["correct"] else "INCORRECT -- startup bug still present")
    sys.exit(0 if result["correct"] else 1)


if __name__ == "__main__":
    main()
