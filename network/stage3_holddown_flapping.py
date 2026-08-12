#!/usr/bin/env python3
"""Live-network reproduction of the hold-down flapping-link scenario that
test_daim_link_agent.py::test_holddown_suppresses_flapping only exercises at
the logic level (synthetic events, fake clock). This script drives the same
FLAP_EVENTS schedule -- down at t=0.0, up at 0.1, down at 0.2, up at 0.5,
down at 1.9, up at 2.5, down at 2.6 -- against a *real* Mininet/OVS s1-s2
link using net.configLinkStatus, with the real daim_link_agent.py reacting
to real OVSDB notifications, and records every event the agent actually
logs. This is evidence-gate item 1 from Submission_Manuscript.md Section 10.

Unlike stage3_autonomous_agent.py (single failure, 5 repetitions), this
script runs the full flap schedule once per repetition and reports the
sequence of agent actions observed, so it can be compared directly against
FLAP_EVENTS's predicted sequence:
    ["repair", "suppressed", "suppressed", "suppressed",
     "suppressed", "recovered", "noop"]
"""
import csv
import json
import re
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
RAW = ROOT / "results/network/stage3_holddown_flapping_raw.csv"
EVENTS_LOG = ROOT / "results/network/stage3_holddown_flapping_events.jsonl"
REPETITIONS = 5

# Same schedule as test_daim_link_agent.py's FLAP_EVENTS, driven against a
# real link instead of a synthetic event list.
FLAP_SCHEDULE = [
    (0.0, "down"),
    (0.1, "up"),
    (0.2, "down"),
    (0.5, "up"),
    (1.9, "down"),
    (2.5, "up"),
    (2.6, "down"),
]
EXPECTED_ACTIONS = [
    "repair", "suppressed", "suppressed", "suppressed",
    "suppressed", "recovered", "noop",
]


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


def parse_ping(output):
    sent = re.search(r"(\d+) packets transmitted", output)
    received = re.search(r"(\d+) received", output)
    loss = re.search(r"([0-9.]+)% packet loss", output)
    return {
        "packets_sent": int(sent.group(1)) if sent else 0,
        "packets_received": int(received.group(1)) if received else 0,
        "packet_loss_pct": float(loss.group(1)) if loss else 100.0,
    }


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


ACTION_FOR_EVENT = {
    "repair_installed": "repair",
    "transition_suppressed": "suppressed",
    "link_up_detected": "recovered",
    "repair_failed": "repair_failed",
}


def run_one(repetition):
    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(topo=DiamondTopo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    agent = None
    try:
        net.start()
        agent = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )

        events = []
        if not read_agent_events(agent, time.time() + 10, events, stop_event_name="agent_started"):
            raise RuntimeError("agent did not report agent_started within 10s")

        h1, h2 = net.get("h1", "h2")
        # 200 packets at 20ms = 4s of probe traffic, comfortably covering the
        # full ~2.9s flap schedule below plus settling time.
        ping = h1.popen(
            ["ping", "-c", "200", "-i", "0.02", "-W", "1", "10.0.0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.3)

        schedule_start = time.perf_counter()
        flap_log = []
        idx = 0
        deadline = time.time() + 20
        ping_chunks = []
        ping_done = False
        while time.time() < deadline and not (ping_done and ping.poll() is not None):
            # Fire the next scheduled transition once its time has arrived.
            if idx < len(FLAP_SCHEDULE):
                t_offset, state = FLAP_SCHEDULE[idx]
                if time.perf_counter() - schedule_start >= t_offset:
                    net.configLinkStatus("s1", "s2", state)
                    flap_log.append({
                        "scheduled_t": t_offset, "state": state,
                        "actual_t": time.perf_counter() - schedule_start,
                    })
                    idx += 1
            ready, _, _ = select.select([ping.stdout, agent.stdout], [], [], 0.05)
            if ping.stdout in ready:
                line = ping.stdout.readline()
                if line:
                    ping_chunks.append(line)
                else:
                    ping_done = True
            if agent.stdout in ready:
                line = agent.stdout.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            if idx >= len(FLAP_SCHEDULE) and time.perf_counter() - schedule_start > 4.5 and ping_done:
                break
        result = parse_ping("".join(ping_chunks))

        read_agent_events(agent, time.time() + 3, events)

        observed_actions = [
            ACTION_FOR_EVENT[e["event"]] for e in events
            if e.get("event") in ACTION_FOR_EVENT
        ]

        result.update({
            "evidence_level": "measured_emulation_holddown_flapping",
            "mode": "autonomous_agent_flapping",
            "repetition": repetition,
            "observed_action_sequence": ";".join(observed_actions),
            "matches_predicted_sequence": observed_actions == EXPECTED_ACTIONS,
            "agent_event_count": len(events),
            "flap_log": json.dumps(flap_log),
        })
        return result, events
    finally:
        if agent and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
        net.stop()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    setLogLevel("warning")
    rows = []
    all_events = []
    for repetition in range(1, REPETITIONS + 1):
        print(f"stage3-holddown-flapping repetition={repetition}", flush=True)
        row, events = run_one(repetition)
        print(json.dumps(row), flush=True)
        rows.append(row)
        all_events.append({"repetition": repetition, "events": events})

    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {RAW}")

    with EVENTS_LOG.open("w") as handle:
        for rep in all_events:
            handle.write(json.dumps(rep) + "\n")
    print(f"wrote full event logs to {EVENTS_LOG}")

    matches = sum(1 for r in rows if r["matches_predicted_sequence"])
    print(f"repetitions matching predicted action sequence: {matches}/{len(rows)}")
    for r in rows:
        print(f"  rep {r['repetition']}: {r['observed_action_sequence']}"
              f" (matches={r['matches_predicted_sequence']})")


if __name__ == "__main__":
    main()
