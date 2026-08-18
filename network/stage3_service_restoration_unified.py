#!/usr/bin/env python3
"""Unified data-plane service-restoration metric across all three mechanisms
measured in this evidence set (autonomous agent, fast-failover group,
controller-driven recovery), replacing the earlier, construct-invalid
comparison that pitted the agent's own `repair_action_us` (a control-plane,
Flow-Mod-completion interval, as Section 6.3 already defines it) against the
fast-failover baseline's ping-derived outage BOUND (a data-plane
observation) as if they were the same endpoint. They are not: this harness
measures the SAME thing -- real packet-level data-plane restoration time,
observed identically for all three mechanisms -- and keeps each mechanism's
own control-plane phase decomposition as separate, clearly-labelled columns,
not conflated with the data-plane number.

Method: two independent, continuous ICMP probes run for the whole
repetition -- h1->h2 and h2->h1 -- at a 5 ms interval (4x finer than the
20 ms interval used elsewhere in this evidence set, chosen and verified
empirically to sustain 100% delivery on this Mininet/OVS testbed under
normal conditions before being adopted here). Each direction's arrivals are
captured independently via `tcpdump` on the RECEIVING host, observing
incoming ICMP echo-requests directly (not relying on replies, exactly as
the fast-failover baseline's own forward-direction measurement already
does) -- so forward and reverse restoration are measured as two genuinely
separate observations, not inferred from a round-trip. For each direction:
`outage_duration_ms = first_good_after_fault - last_good_before_fault`, a
real bound from actual observed arrivals, not a formula-derived range. A
direction that never sees another arrival within the measurement window is
recorded as `recovered=False`, `outage_duration_ms=None` -- exactly the
fast-failover baseline's own reverse-direction finding, now measured the
same way as every other mechanism's result rather than inferred
differently.

Each mechanism's own control-plane phase timestamps (already emitted by
its own existing implementation -- daim_link_agent.py's
bfs_*/stage_*/commit_*/repair_* fields, osken_recovery_baseline_
controller.py's link_down_detected/repair_installed events; the
fast-failover group has no software control plane at all, so these columns
are `None` for it, honestly) are read from that mechanism's own log
unchanged, not re-derived -- these numbers are NOT recomputed here, only
matched up against the new, unified data-plane restoration number for
comparison.
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
AGENT = NETWORK_DIR / "daim_link_agent.py"
RESULTS = ROOT / "results/network"
RAW = RESULTS / "stage3_service_restoration_unified_raw.csv"
EVENTS_LOG = RESULTS / "stage3_service_restoration_unified_events.jsonl"

PROBE_INTERVAL_S = 0.005
PROBE_COUNT = 700  # 3.5s of probing per direction
PRE_FAULT_S = 1.0
OSKEN_UNIX_USER = "ubuntu"


class DiamondTopo(Topo):
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


def sh(cmd):
    subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)


def install_fast_failover_flows():
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
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
        if stop_event_name and events and events[-1].get("event") == stop_event_name:
            return True
    return False


SEQ_RE = re.compile(r"^(\d+\.\d+) IP (\S+) > (\S+): ICMP echo request.*seq (\d+)")


def parse_tcpdump_arrivals(text):
    """seq -> arrival wall-clock timestamp."""
    out = {}
    for line in text.splitlines():
        m = SEQ_RE.match(line.strip())
        if m:
            out[int(m.group(4))] = float(m.group(1))
    return out


def measure_direction(net, sender_name, receiver_name, sender_ip, dest_ip, dump_log_path):
    """Captures ONLY echo-requests genuinely arriving from `sender_ip` --
    a plain `icmp` BPF filter on the receiving host's interface also
    captures that SAME host's own outgoing probe traffic for the other,
    concurrently-running direction (both directions share physical
    interfaces on a 2-host topology), which would otherwise be
    misidentified as "arrivals" for this direction. Confirmed empirically:
    an earlier version without this src/dst filter reported the
    fast-failover baseline's reverse direction as recovering in ~1-3 ms,
    contradicting the already-established (and re-confirmed after this
    fix) 0/51 finding -- traced to exactly this capture conflation."""
    sender = net.get(sender_name)
    receiver = net.get(receiver_name)
    receiver_intf = receiver.defaultIntf().name
    tcpdump_proc = receiver.popen(
        ["tcpdump", "-i", receiver_intf, "-tt", "-n",
         f"icmp and src host {sender_ip} and dst host {dest_ip}"],
        stdout=open(dump_log_path, "w"), stderr=subprocess.STDOUT,
    )
    time.sleep(0.3)
    ping_proc = sender.popen(
        ["ping", "-c", str(PROBE_COUNT), "-i", str(PROBE_INTERVAL_S), "-W", "1", dest_ip],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return tcpdump_proc, ping_proc


def run_one(mechanism, repetition):
    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = None
    agent_proc = None
    controller_proc = None
    fwd_dump = None
    rev_dump = None
    try:
        controller_arg = None
        if mechanism == "controller_driven":
            controller_proc = subprocess.Popen(
                ["sudo", "-u", OSKEN_UNIX_USER, "-H", "bash", "-c",
                 f"export PATH=$PATH:/home/{OSKEN_UNIX_USER}/.local/bin && "
                 f"cd {NETWORK_DIR} && "
                 "python3 osken_launcher.py osken_recovery_baseline_controller"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            controller_events = []
            if not read_events(controller_proc, time.time() + 10, controller_events, "controller_started"):
                raise RuntimeError("controller did not start")
            controller_arg = lambda name: RemoteController(name, ip="127.0.0.1", port=6653)

        net = Mininet(
            topo=DiamondTopo(),
            controller=controller_arg,
            switch=OVSSwitch, link=TCLink, autoSetMacs=True,
        )
        net.start()

        events = []
        if mechanism == "agent":
            agent_proc = subprocess.Popen(
                [sys.executable, str(AGENT)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
            )
            if not read_events(agent_proc, time.time() + 10, events, "agent_started"):
                raise RuntimeError("agent did not start")
        elif mechanism == "fast_failover":
            install_fast_failover_flows()
        elif mechanism == "controller_driven":
            deadline = time.time() + 10
            while time.time() < deadline and sum(1 for e in controller_events if e.get("event") == "switch_connected") < 4:
                read_events(controller_proc, time.time() + 0.5, controller_events)
            events = controller_events

        time.sleep(0.5)

        fwd_dump = RESULTS / f"unified_{mechanism}_rep{repetition}_fwd.log"
        rev_dump = RESULTS / f"unified_{mechanism}_rep{repetition}_rev.log"
        fwd_tcpdump, fwd_ping = measure_direction(net, "h1", "h2", "10.0.0.1", "10.0.0.2", fwd_dump)
        rev_tcpdump, rev_ping = measure_direction(net, "h2", "h1", "10.0.0.2", "10.0.0.1", rev_dump)

        time.sleep(PRE_FAULT_S)
        fault_ns = time.perf_counter_ns()
        fault_wall = time.time()
        net.configLinkStatus("s1", "s2", "down")

        # Drain agent/controller stdout concurrently so its own log keeps
        # flowing while both ping streams run to completion.
        deadline = time.time() + (PROBE_COUNT * PROBE_INTERVAL_S) + 3.0
        proc = agent_proc if mechanism == "agent" else (controller_proc if mechanism == "controller_driven" else None)
        while time.time() < deadline:
            if fwd_ping.poll() is not None and rev_ping.poll() is not None:
                break
            if proc is not None:
                read_events(proc, time.time() + 0.2, events)
            else:
                time.sleep(0.2)

        for p in (fwd_ping, rev_ping):
            if p.poll() is None:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()
        for p in (fwd_tcpdump, rev_tcpdump):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if proc is not None:
            read_events(proc, time.time() + 2, events)

        fwd_arrivals = parse_tcpdump_arrivals(fwd_dump.read_text())
        rev_arrivals = parse_tcpdump_arrivals(rev_dump.read_text())

        def direction_result(arrivals, min_sustained_run=3):
            """A single packet arriving right at the fault boundary is NOT
            treated as recovery: an empirical debug run found the fault
            (calling net.configLinkStatus) is not instantaneous -- a packet
            already in flight microseconds before the call can still land
            microseconds after `fault_wall` was recorded, producing a false
            ~5ms "recovery" for a direction that is, in fact, permanently
            down (confirmed directly: in that debug run, the "recovering"
            packet was the LAST one ever seen out of 700 sent, with total
            silence afterward -- the textbook signature of a boundary race,
            not genuine restoration). Recovery therefore requires a
            SUSTAINED run of `min_sustained_run` consecutive sequence
            numbers, not a single arrival -- filters exactly this false
            positive while leaving every genuine fast recovery (already
            followed by continuous arrivals in every mechanism measured)
            unaffected."""
            before = [ts for ts in arrivals.values() if ts < fault_wall]
            last_before = max(before) if before else None
            if not arrivals:
                return {"last_good_before_fault_wall": last_before, "first_good_after_fault_wall": None,
                        "recovered": False, "outage_duration_ms": None}
            seqs_sorted = sorted(arrivals.keys())
            runs = []
            run_start = prev = seqs_sorted[0]
            for s in seqs_sorted[1:]:
                if s == prev + 1:
                    prev = s
                else:
                    runs.append((run_start, prev))
                    run_start = prev = s
            runs.append((run_start, prev))
            first_after = None
            for rs, rend in runs:
                if (rend - rs + 1) >= min_sustained_run and arrivals[rs] >= fault_wall:
                    first_after = arrivals[rs]
                    break
            recovered = first_after is not None
            outage_ms = ((first_after - last_before) * 1000) if (recovered and last_before) else None
            return {
                "last_good_before_fault_wall": last_before,
                "first_good_after_fault_wall": first_after,
                "recovered": recovered,
                "outage_duration_ms": round(outage_ms, 3) if outage_ms is not None else None,
            }

        forward = direction_result(fwd_arrivals)
        reverse = direction_result(rev_arrivals)

        # Control-plane phases, read unchanged from the mechanism's own log.
        cp = {
            "cp_detection_ns": None, "cp_bfs_ns": None, "cp_stage_ns": None,
            "cp_commit_ns": None, "cp_total_control_plane_ns": None,
        }
        if mechanism == "agent":
            down_events = [e for e in events if e.get("event") == "link_down_detected"]
            repair_events = [e for e in events if e.get("event") in ("repair_installed", "repair_installed_stale_withdraw")]
            if down_events:
                cp["cp_detection_ns"] = down_events[0].get("ns")
            if repair_events:
                r = repair_events[0]
                if r.get("bfs_start_ns") and r.get("bfs_end_ns"):
                    cp["cp_bfs_ns"] = r["bfs_end_ns"] - r["bfs_start_ns"]
                if r.get("stage_start_ns") and r.get("stage_end_ns"):
                    cp["cp_stage_ns"] = r["stage_end_ns"] - r["stage_start_ns"]
                if r.get("commit_start_ns") and r.get("commit_end_ns"):
                    cp["cp_commit_ns"] = r["commit_end_ns"] - r["commit_start_ns"]
                if r.get("repair_start_ns") and r.get("repair_end_ns"):
                    cp["cp_total_control_plane_ns"] = r["repair_end_ns"] - r["repair_start_ns"]
        elif mechanism == "controller_driven":
            down_events = [e for e in events if e.get("event") == "link_down_detected"]
            repair_events = [e for e in events if e.get("event") == "repair_installed"]
            if down_events:
                cp["cp_detection_ns"] = down_events[0].get("ns")
            if repair_events:
                r = repair_events[0]
                cp["cp_total_control_plane_ns"] = r["repair_end_ns"] - r["repair_start_ns"]

        result = {
            "mechanism": mechanism,
            "repetition": repetition,
            "fault_ns": fault_ns,
            "forward_recovered": forward["recovered"],
            "forward_outage_ms": forward["outage_duration_ms"],
            "reverse_recovered": reverse["recovered"],
            "reverse_outage_ms": reverse["outage_duration_ms"],
            **cp,
        }
        return result, events, {"forward": forward, "reverse": reverse}
    finally:
        for p in (fwd_dump, rev_dump):
            if p and p.exists():
                p.unlink()
        if net:
            net.stop()
        if agent_proc and agent_proc.poll() is None:
            agent_proc.terminate()
        if controller_proc and controller_proc.poll() is None:
            subprocess.run(["sudo", "pkill", "-f", "osken_launcher"], check=False)
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    setLogLevel("warning")
    mechanism = sys.argv[1]
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    start_rep = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    rows = []
    all_events = []
    for repetition in range(start_rep, start_rep + reps):
        print(f"stage3-service-restoration-unified mechanism={mechanism} repetition={repetition}", flush=True)
        row, events, directions = run_one(mechanism, repetition)
        print(json.dumps(row), flush=True)
        rows.append(row)
        all_events.append({"mechanism": mechanism, "repetition": repetition, "events": events})

    RAW.parent.mkdir(parents=True, exist_ok=True)
    write_header = not RAW.exists()
    with RAW.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    print(f"appended {len(rows)} rows to {RAW}")

    with EVENTS_LOG.open("a") as handle:
        for rep in all_events:
            handle.write(json.dumps(rep) + "\n")
    print(f"appended event logs to {EVENTS_LOG}")


if __name__ == "__main__":
    main()
