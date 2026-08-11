#!/usr/bin/env python3
"""Stage-3 autonomous link-recovery experiment.

Unlike stage3_link_recovery.py's local_repair/central_repair/two_loop modes
(where the harness itself calls a pre-written install_alternate() after a
fixed or polled delay), the repair decision here belongs entirely to
daim_link_agent.py, an independent background process started before the
fault is injected. This script only starts the diamond topology, starts the
agent, starts the ping, brings s1-s2 down for real via Mininet, waits, and
then merges the agent's own timestamped JSON event log (its detection and
repair-install times) with the ping loss result. No repair function is
called from this script.
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
RAW = ROOT / "results/network/stage3_autonomous_agent_raw.csv"
REPETITIONS = 5


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
        ping = h1.popen(
            ["ping", "-c", "80", "-i", "0.02", "-W", "1", "10.0.0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.3)

        failure_ns = time.perf_counter_ns()
        net.configLinkStatus("s1", "s2", "down")

        # Read ping's and the agent's output concurrently instead of
        # blocking on ping.communicate() first: a blocking wait on Mininet's
        # host-namespace ping process here was observed to starve the
        # concurrent read of the agent's pipe, so the agent's repair events
        # were never drained even though the agent itself completed the
        # repair in milliseconds.
        ping_chunks = []
        ping_done = False
        deadline = time.time() + 20
        while time.time() < deadline and not (ping_done and ping.poll() is not None):
            ready, _, _ = select.select([ping.stdout, agent.stdout], [], [], 0.2)
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
        result = parse_ping("".join(ping_chunks))

        read_agent_events(agent, time.time() + 5, events)

        down_events = [e for e in events if e.get("event") == "link_down_detected"]
        repair_events = [e for e in events if e.get("event") == "repair_installed"]
        failed_events = [e for e in events if e.get("event") == "repair_failed"]

        detection_ns = down_events[0]["ns"] if down_events else None
        repair_start_ns = repair_events[0]["repair_start_ns"] if repair_events else None
        repair_end_ns = repair_events[0]["repair_end_ns"] if repair_events else None

        result.update({
            "evidence_level": "measured_emulation_autonomous_agent",
            "mode": "autonomous_agent",
            "repetition": repetition,
            "failure": "s1-s2 link down",
            "failure_to_detection_us": (detection_ns - failure_ns) / 1000.0 if detection_ns else None,
            "failure_to_repair_start_us": (repair_start_ns - failure_ns) / 1000.0 if repair_start_ns else None,
            "repair_action_us": (repair_end_ns - repair_start_ns) / 1000.0 if repair_start_ns and repair_end_ns else None,
            "repaired_path": ",".join(repair_events[0]["path"]) if repair_events else None,
            "repair_failed": bool(failed_events),
            "agent_event_count": len(events),
        })
        return result
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
    for repetition in range(1, REPETITIONS + 1):
        print(f"stage3-autonomous repetition={repetition}", flush=True)
        row = run_one(repetition)
        print(json.dumps(row), flush=True)
        rows.append(row)

    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {RAW}")

    missing_detection = sum(1 for r in rows if r["failure_to_detection_us"] is None)
    missing_repair = sum(1 for r in rows if not r["repaired_path"])
    print(f"missing_detection={missing_detection} missing_repair={missing_repair}")
    if missing_detection or missing_repair:
        print("stage3_autonomous_verification=FAIL")
        sys.exit(1)
    print("stage3_autonomous_verification=PASS")


if __name__ == "__main__":
    main()
