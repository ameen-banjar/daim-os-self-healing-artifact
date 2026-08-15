#!/usr/bin/env python3
"""Layer 2 step 3 (Section 10: multiple topologies + scale + overhead).

Every prior live experiment in this evidence set (single-host, multi-OVS,
hold-down flapping, asymmetric-interface) used one of two structurally
IDENTICAL four-switch diamond graphs. This harness runs the real,
unmodified daim_link_agent.py (via topology_gen.py's generators, not a
hand-built diamond) against THREE structurally different topologies:

- linear_N: an N-switch chain with NO redundant path for any edge --
  deliberately the topology self-healing cannot succeed on, testing that a
  genuinely unrecoverable failure is reported honestly (repair_failed, no
  false-positive success, no crash) rather than only ever being tested
  against topologies where a repair happens to be possible.
- ring_N: an N-switch cycle, single-fault-tolerant everywhere, exercised at
  two sizes (8 and 20) to see repair-action time trend against a growing
  hop count on the SAME topology shape.
- fattree_k4: a 20-switch (>10, closing the scale requirement) three-tier
  fat-tree with genuine multi-path redundancy at both the aggregation and
  core layers -- the failed edge is chosen so the discovered reroute must
  use a DIFFERENT core switch, not just a trivial same-pod detour.

For each live repetition, also samples the agent PROCESS's own CPU time
and resident memory directly from /proc (Linux-only, matching every other
live experiment's VM environment) immediately after startup and again
immediately after the repair (or repair_failed) decision, and derives a
flow-mod message-count overhead metric analytically from the real
`path_to_flows()` contract (2 OpenFlow flow-mod calls per traversed hop,
confirmed by direct code reading, not re-implemented here) applied to the
paths daim_link_agent.py itself reports in its own log.
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

NETWORK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(NETWORK_DIR))
import daim_link_agent as dla  # noqa: E402
import topology_gen as tg  # noqa: E402

ROOT = NETWORK_DIR.parent
AGENT = NETWORK_DIR / "daim_link_agent.py"
CONFIG_DIR = NETWORK_DIR
RAW = ROOT / "results/network/stage3_topology_scale_raw.csv"
EVENTS_LOG = ROOT / "results/network/stage3_topology_scale_events.jsonl"

REPETITIONS = {"linear_10": 1, "ring_8": 3, "ring_20": 3, "fattree_k4": 3}


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


def build_spec(name):
    if name == "linear_10":
        return tg.linear_topology(10)
    if name == "ring_8":
        return tg.ring_topology(8)
    if name == "ring_20":
        return tg.ring_topology(20)
    if name == "fattree_k4":
        return tg.fat_tree_topology(4)
    raise ValueError(name)


def primary_edge_to_fail(spec):
    """Loads the spec's config into a scratch pair of daim_link_agent
    globals (not the live agent -- this is a local, in-process BFS call
    only, used to pick which edge to fail) and returns the second hop of
    the discovered primary path when long enough to have one (forcing a
    deeper, more interesting reroute than the trivial host-adjacent edge),
    otherwise the first hop."""
    cfg = spec["config"]
    topology = {sw: {n: tuple(p) for n, p in nbrs.items()} for sw, nbrs in cfg["topology"].items()}
    # bfs_path reads module globals directly; do a scoped monkeypatch/restore.
    saved = (dla.TOPOLOGY, dla.HOST_ATTACHMENT, dla.SOURCE, dla.DEST)
    try:
        dla.TOPOLOGY = topology
        dla.HOST_ATTACHMENT = cfg["host_attachment"]
        dla.SOURCE, dla.DEST = cfg["source"], cfg["dest"]
        path = dla.bfs_path(cfg["source"], cfg["dest"], set())
    finally:
        dla.TOPOLOGY, dla.HOST_ATTACHMENT, dla.SOURCE, dla.DEST = saved
    assert path, f"{spec['name']}: no primary path found -- generator bug"
    if len(path) >= 4:
        return path, (path[1], path[2])
    return path, (path[0], path[1])


def sample_proc(pid):
    """Reads /proc/<pid>/status (VmRSS) and /proc/<pid>/stat (utime+stime
    in clock ticks, converted to seconds via the kernel's own USER_HZ,
    almost always 100 on Linux -- read from os.sysconf rather than
    hardcoded) for the agent process, returning None for either field if
    the process has already exited or /proc is transiently unreadable
    (never crashes the experiment over a missing sample)."""
    import os
    hz = os.sysconf("SC_CLK_TCK")
    rss_kb = None
    cpu_s = None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
    except (FileNotFoundError, ProcessLookupError):
        pass
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        utime, stime = int(fields[13]), int(fields[14])
        cpu_s = (utime + stime) / hz
    except (FileNotFoundError, ProcessLookupError, IndexError):
        pass
    return {"rss_kb": rss_kb, "cpu_s": cpu_s}


def run_one(topo_name, repetition):
    spec = build_spec(topo_name)
    primary_path, (fail_a, fail_b) = primary_edge_to_fail(spec)
    config_path = CONFIG_DIR / f"{spec['name']}_topology.json"
    tg.write_config(spec, CONFIG_DIR)

    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(
        topo=tg.build_mininet_topo(spec["edges"], spec["switches"], spec["hosts"]),
        controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True,
    )
    agent = None
    try:
        net.start()

        import os
        env = dict(os.environ)
        env["DAIM_TOPOLOGY_CONFIG"] = str(config_path)
        agent = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True, env=env,
        )

        events = []
        if not read_agent_events(agent, time.time() + 15, events, stop_event_name="agent_started"):
            raise RuntimeError("agent did not report agent_started within 15s")

        baseline = sample_proc(agent.pid)
        time.sleep(0.5)

        h1, h2 = net.get("h1", "h2")
        ping = h1.popen(
            ["ping", "-c", "60", "-i", "0.02", "-W", "1", "10.0.0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.3)

        failure_ns = time.perf_counter_ns()
        net.configLinkStatus(fail_a, fail_b, "down")

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
        ping_result = parse_ping("".join(ping_chunks))

        read_agent_events(agent, time.time() + 5, events)
        after_repair = sample_proc(agent.pid)

        down_events = [e for e in events if e.get("event") == "link_down_detected"]
        repair_events = [e for e in events if e.get("event") in ("repair_installed", "repair_installed_stale_withdraw")]
        failed_events = [e for e in events if e.get("event") == "repair_failed"]

        detection_ns = down_events[0]["ns"] if down_events and "ns" in down_events[0] else None
        repair_start_ns = repair_events[0]["repair_start_ns"] if repair_events else None
        repair_end_ns = repair_events[0]["repair_end_ns"] if repair_events else None
        new_path = repair_events[0]["path"] if repair_events else None

        old_hops = len(primary_path) - 1
        new_hops = (len(new_path) - 1) if new_path else 0
        flow_mod_overhead = 2 * old_hops + 2 * new_hops  # withdraw old + install new, 2 flows/hop each

        result = {
            "topology": topo_name,
            "switch_count": len(spec["switches"]),
            "switch_switch_link_count": sum(1 for a, b in spec["edges"] if a in spec["switches"] and b in spec["switches"]),
            "monitored_interface_count": len(spec["config"]["monitored_interfaces"]),
            "repetition": repetition,
            "primary_path_hops": old_hops,
            "failed_edge": f"{fail_a}-{fail_b}",
            "recoverable_by_design": topo_name != "linear_10",
            "repair_succeeded": bool(repair_events),
            "repair_failed": bool(failed_events),
            "new_path_hops": new_hops if new_path else None,
            "flow_mod_message_overhead": flow_mod_overhead,
            "failure_to_detection_us": (detection_ns - failure_ns) / 1000.0 if detection_ns else None,
            "repair_action_us": (repair_end_ns - repair_start_ns) / 1000.0 if repair_start_ns and repair_end_ns else None,
            "packet_loss_pct": ping_result["packet_loss_pct"],
            "packets_sent": ping_result["packets_sent"],
            "agent_baseline_rss_kb": baseline["rss_kb"],
            "agent_after_repair_rss_kb": after_repair["rss_kb"],
            "agent_rss_delta_kb": (
                after_repair["rss_kb"] - baseline["rss_kb"]
                if baseline["rss_kb"] is not None and after_repair["rss_kb"] is not None else None
            ),
            "agent_cpu_seconds_baseline": baseline["cpu_s"],
            "agent_cpu_seconds_after_repair": after_repair["cpu_s"],
            "agent_cpu_seconds_used": (
                after_repair["cpu_s"] - baseline["cpu_s"]
                if baseline["cpu_s"] is not None and after_repair["cpu_s"] is not None else None
            ),
            "agent_event_count": len(events),
        }
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
    for topo_name, reps in REPETITIONS.items():
        for repetition in range(1, reps + 1):
            print(f"stage3-topology-scale topology={topo_name} repetition={repetition}", flush=True)
            row, events = run_one(topo_name, repetition)
            print(json.dumps(row), flush=True)
            rows.append(row)
            all_events.append({"topology": topo_name, "repetition": repetition, "events": events})

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

    bad = [r for r in rows if r["recoverable_by_design"] and not r["repair_succeeded"]]
    bad += [r for r in rows if not r["recoverable_by_design"] and r["repair_succeeded"]]
    if bad:
        print(f"stage3_topology_scale_verification=FAIL ({len(bad)} rows with unexpected outcome)")
        sys.exit(1)
    print("stage3_topology_scale_verification=PASS")


if __name__ == "__main__":
    main()
