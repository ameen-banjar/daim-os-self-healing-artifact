#!/usr/bin/env python3
"""Formal baseline 1 of 2 (Section 10): a real OpenFlow13 fast-failover-group
configuration, measured on the identical diamond topology and s1-s2 fault
injection every other single-host live experiment in this evidence set uses
-- but with NO controller and NO daim_link_agent.py process at all. Pure
dataplane: a group with two buckets (primary watch_port=2 to s2, backup
watch_port=3 to s3) is pre-installed on s1 before the fault, and OVS itself
switches to the live bucket the instant the watched port's carrier drops.

An empirical smoke test (run manually against a bare Mininet diamond before
writing this harness) found a real, structural limitation: a fast-failover
group only reacts to the LOCAL switch's own watched port going down. s1's
group correctly redirects h1->h2 traffic to s3 the instant s1's port to s2
drops (confirmed via `ovs-ofctl dump-group-stats` showing the backup
bucket's packet count increasing). But h2->h1 return traffic is forwarded by
s4 via a STATIC flow (in_port=3 -> output to s2), and s4's own
directly-attached ports (to s2, to s3) never change state for this fault --
only s1's (and s2's) port to EACH OTHER goes down, which s4 cannot observe.
A symmetric group at s4 would not help either: it would watch s4's own port
to s2, which never goes down for this specific fault. This is not a
topology-design artefact of this diamond specifically -- it is the textbook
scope of OpenFlow fast failover (and IP Fast Reroute generally): local
link/port protection, not network-wide reconvergence. Both directions are
measured and reported honestly below rather than only measuring the
direction that happens to recover.

Forward-direction delivery is observed independently of whether replies
return, via tcpdump on h2 capturing incoming ICMP echo-REQUESTS (not
relying on h1's own ping accounting, which would conflate forward delivery
with reply delivery). Reverse-direction delivery reuses a plain, already
zero-loss-proven `ping` (Section 7.1's own established pattern).
"""
import csv
import json
import re
import select
import subprocess
import time
from pathlib import Path

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results/network/stage3_baseline_fast_failover_raw.csv"
REPETITIONS = 5
PING_INTERVAL_S = 0.02
PING_COUNT = 250


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
    subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)


def install_fast_failover_baseline():
    """Group only at s1 (h1->h2 forward direction). No group at s4 -- as
    documented above, it would never react to this fault, so installing one
    would be theatre, not protection; the reverse direction's outcome is
    measured and reported as a real limitation instead."""
    sh("ovs-ofctl -O OpenFlow13 add-group s1 "
       "group_id=1,type=ff,bucket=watch_port:2,actions=output:2,"
       "bucket=watch_port:3,actions=output:3")
    sh("ovs-ofctl -O OpenFlow13 add-flow s1 in_port=1,actions=group:1")
    sh("ovs-ofctl -O OpenFlow13 add-flow s1 in_port=2,actions=output:1")
    sh("ovs-ofctl -O OpenFlow13 add-flow s1 in_port=3,actions=output:1")
    sh("ovs-ofctl -O OpenFlow13 add-flow s2 in_port=1,actions=output:2")
    sh("ovs-ofctl -O OpenFlow13 add-flow s2 in_port=2,actions=output:1")
    sh("ovs-ofctl -O OpenFlow13 add-flow s3 in_port=1,actions=output:2")
    sh("ovs-ofctl -O OpenFlow13 add-flow s3 in_port=2,actions=output:1")
    sh("ovs-ofctl -O OpenFlow13 add-flow s4 in_port=1,actions=output:3")
    sh("ovs-ofctl -O OpenFlow13 add-flow s4 in_port=2,actions=output:3")
    sh("ovs-ofctl -O OpenFlow13 add-flow s4 in_port=3,actions=output:1")


SEQ_RE = re.compile(r"^(\d+\.\d+) IP 10\.0\.0\.1 > 10\.0\.0\.2: ICMP echo request.*seq (\d+)")


def parse_tcpdump_seqs(text):
    """seq -> arrival wall-clock timestamp, from `tcpdump -tt -n` output."""
    out = {}
    for line in text.splitlines():
        m = SEQ_RE.match(line.strip())
        if m:
            out[int(m.group(2))] = float(m.group(1))
    return out


def parse_ping(output):
    sent = re.search(r"(\d+) packets transmitted", output)
    received = re.search(r"(\d+) received", output)
    loss = re.search(r"([0-9.]+)% packet loss", output)
    return {
        "packets_sent": int(sent.group(1)) if sent else 0,
        "packets_received": int(received.group(1)) if received else 0,
        "packet_loss_pct": float(loss.group(1)) if loss else 100.0,
    }


def longest_missing_run(sent_seqs, received_seqs):
    longest = 0
    current = 0
    for s in sorted(sent_seqs):
        if s not in received_seqs:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def run_one(repetition):
    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(topo=DiamondTopo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    tcpdump_proc = None
    try:
        net.start()
        install_fast_failover_baseline()
        h1, h2 = net.get("h1", "h2")

        dump_log = f"/tmp/ff_tcpdump_{repetition}.log"
        h2_intf = h2.defaultIntf().name
        tcpdump_proc = h2.popen(
            ["tcpdump", "-i", h2_intf, "-tt", "-n", "icmp"],
            stdout=open(dump_log, "w"), stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)

        ping = h1.popen(
            ["ping", "-c", str(PING_COUNT), "-i", str(PING_INTERVAL_S), "-W", "1", "10.0.0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(1.0)
        net.configLinkStatus("s1", "s2", "down")

        # Drain ping's stdout via select rather than a blocking
        # .communicate(), matching every other harness in this evidence set
        # (e.g. stage3_autonomous_agent.py) -- a plain blocking call on a
        # Mininet host.popen() pipe has been unreliable in this environment.
        deadline = time.time() + 25
        while time.time() < deadline and ping.poll() is None:
            ready, _, _ = select.select([ping.stdout], [], [], 0.2)
            if ping.stdout in ready:
                ping.stdout.readline()

        tcpdump_proc.terminate()
        try:
            tcpdump_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tcpdump_proc.kill()
        tcpdump_proc = None
        time.sleep(0.3)

        arrivals = parse_tcpdump_seqs(Path(dump_log).read_text())
        all_seqs = set(range(1, PING_COUNT + 1))
        received_seqs = set(arrivals)
        longest_run = longest_missing_run(all_seqs, received_seqs)
        outage_lower_ms = max(0, (longest_run - 1) * PING_INTERVAL_S * 1000)
        outage_upper_ms = (longest_run + 1) * PING_INTERVAL_S * 1000

        # Reverse direction: plain, already zero-loss-proven ping pattern.
        reverse_ping = h2.cmd("ping -c 20 -i 0.05 -W 1 10.0.0.1")
        reverse_result = parse_ping(reverse_ping)

        result = {
            "baseline": "fast_failover_group",
            "repetition": repetition,
            "forward_echo_requests_sent": PING_COUNT,
            "forward_echo_requests_arrived_at_h2": len(received_seqs),
            "forward_missing_count": PING_COUNT - len(received_seqs),
            "forward_longest_missing_run": longest_run,
            "forward_outage_bound_lower_ms": outage_lower_ms,
            "forward_outage_bound_upper_ms": outage_upper_ms,
            "forward_direction_recovered": len(received_seqs) > PING_COUNT * 0.5,
            "reverse_ping_packets_sent": reverse_result["packets_sent"],
            "reverse_ping_packets_received": reverse_result["packets_received"],
            "reverse_ping_packet_loss_pct": reverse_result["packet_loss_pct"],
            "reverse_direction_recovered": reverse_result["packet_loss_pct"] < 50.0,
        }
        return result
    finally:
        if tcpdump_proc is not None and tcpdump_proc.poll() is None:
            tcpdump_proc.terminate()
        net.stop()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    setLogLevel("warning")
    rows = []
    for repetition in range(1, REPETITIONS + 1):
        print(f"stage3-baseline-fast-failover repetition={repetition}", flush=True)
        row = run_one(repetition)
        print(json.dumps(row), flush=True)
        rows.append(row)

    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {RAW}")

    fwd_ok = sum(1 for r in rows if r["forward_direction_recovered"])
    rev_ok = sum(1 for r in rows if r["reverse_direction_recovered"])
    print(f"forward_direction_recovered: {fwd_ok}/{len(rows)}")
    print(f"reverse_direction_recovered: {rev_ok}/{len(rows)}")


if __name__ == "__main__":
    main()
