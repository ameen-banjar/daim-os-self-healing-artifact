#!/usr/bin/env python3
"""Formal baseline 2 of 2 (Section 10): drives osken_recovery_baseline_
controller.py against the identical diamond topology and s1-s2 fault
injection every single-host live experiment in this evidence set uses,
merging the controller's own JSON event log with a concurrent ping's loss
result -- the same protocol shape as stage3_autonomous_agent.py, with the
os_ken controller (a real OpenFlow control channel, PortStatus-driven
detection, FlowMod+BarrierReply-confirmed repair) in place of
daim_link_agent.py.

The controller is launched as the unprivileged `ubuntu` user (its pip
packages are user-installed, not visible to root) via `sudo -u ubuntu`,
while this harness itself needs root for Mininet -- both live in the same
Python process, no shell backgrounding involved.
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
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

ROOT = Path(__file__).resolve().parents[1]
NETWORK_DIR = ROOT / "network"
RAW = ROOT / "results/network/stage3_baseline_controller_driven_raw.csv"
REPETITIONS = 5
OSKEN_UNIX_USER = "ubuntu"


class DiamondTopo(Topo):
    """Identical wiring/ports to every other diamond topology in this
    evidence set, with explicit dpid=1..4 so the controller can address
    switches by a known, stable identifier (os_ken's own DPID, not a
    Mininet-internal name)."""
    def build(self):
        s1 = self.addSwitch("s1", protocols="OpenFlow13", dpid="0000000000000001")
        s2 = self.addSwitch("s2", protocols="OpenFlow13", dpid="0000000000000002")
        s3 = self.addSwitch("s3", protocols="OpenFlow13", dpid="0000000000000003")
        s4 = self.addSwitch("s4", protocols="OpenFlow13", dpid="0000000000000004")
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


def read_events(proc, deadline, events, stop_event_name=None):
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        ready, _, _ = select.select([proc.stdout], [], [], min(0.2, remaining))
        if not ready:
            continue
        line = proc.stdout.readline()
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
    controller_proc = None
    net = None
    try:
        controller_proc = subprocess.Popen(
            ["sudo", "-u", OSKEN_UNIX_USER, "-H", "bash", "-c",
             f"export PATH=$PATH:/home/{OSKEN_UNIX_USER}/.local/bin && "
             f"cd {NETWORK_DIR} && "
             "python3 osken_launcher.py osken_recovery_baseline_controller"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        events = []
        if not read_events(controller_proc, time.time() + 10, events, stop_event_name="controller_started"):
            raise RuntimeError("controller did not report controller_started within 10s")

        net = Mininet(
            topo=DiamondTopo(),
            controller=lambda name: RemoteController(name, ip="127.0.0.1", port=6653),
            switch=OVSSwitch, link=TCLink, autoSetMacs=True,
        )
        net.start()
        # Give the controller time to see all 4 switch-connect events and
        # install every primary-path flow before any traffic starts.
        deadline = time.time() + 10
        while time.time() < deadline and sum(1 for e in events if e.get("event") == "switch_connected") < 4:
            read_events(controller_proc, time.time() + 0.5, events)
        time.sleep(0.5)

        h1, h2 = net.get("h1", "h2")
        ping = h1.popen(
            ["ping", "-c", "80", "-i", "0.02", "-W", "1", "10.0.0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.3)

        failure_ns = time.perf_counter_ns()
        net.configLinkStatus("s1", "s2", "down")

        ping_chunks = []
        ping_done = False
        deadline = time.time() + 20
        while time.time() < deadline and not (ping_done and ping.poll() is not None):
            ready, _, _ = select.select([ping.stdout, controller_proc.stdout], [], [], 0.2)
            if ping.stdout in ready:
                line = ping.stdout.readline()
                if line:
                    ping_chunks.append(line)
                else:
                    ping_done = True
            if controller_proc.stdout in ready:
                line = controller_proc.stdout.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        result = parse_ping("".join(ping_chunks))

        read_events(controller_proc, time.time() + 3, events)

        down_events = [e for e in events if e.get("event") == "link_down_detected"]
        repair_events = [e for e in events if e.get("event") == "repair_installed"]

        detection_ns = down_events[0].get("ns") if down_events else None
        repair_start_ns = repair_events[0]["repair_start_ns"] if repair_events else None
        repair_end_ns = repair_events[0]["repair_end_ns"] if repair_events else None

        result.update({
            "evidence_level": "measured_emulation_controller_driven_baseline",
            "baseline": "controller_driven",
            "repetition": repetition,
            "failure_to_detection_us": (detection_ns - failure_ns) / 1000.0 if detection_ns else None,
            "repair_action_us": (repair_end_ns - repair_start_ns) / 1000.0 if repair_start_ns and repair_end_ns else None,
            "repaired_path": ",".join(repair_events[0]["path"]) if repair_events else None,
            "controller_event_count": len(events),
        })
        return result
    finally:
        if net:
            net.stop()
        if controller_proc and controller_proc.poll() is None:
            subprocess.run(["sudo", "pkill", "-f", "osken_launcher"], check=False)
            try:
                controller_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                controller_proc.kill()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    setLogLevel("warning")
    rows = []
    for repetition in range(1, REPETITIONS + 1):
        print(f"stage3-baseline-controller-driven repetition={repetition}", flush=True)
        row = run_one(repetition)
        print(json.dumps(row), flush=True)
        rows.append(row)

    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {RAW}")

    missing = sum(1 for r in rows if not r["repaired_path"])
    print(f"missing_repair={missing}")
    if missing:
        print("stage3_baseline_controller_driven_verification=FAIL")
        sys.exit(1)
    print("stage3_baseline_controller_driven_verification=PASS")


if __name__ == "__main__":
    main()
