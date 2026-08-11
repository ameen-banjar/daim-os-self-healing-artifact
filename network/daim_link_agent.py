#!/usr/bin/env python3
"""Event-driven DAIM link-recovery agent.

Detects link failure by subscribing to real OVSDB `Interface` `link_state`
change notifications via `ovsdb-client monitor` (a blocking read on a
push-based OVSDB monitor connection -- the agent is asleep until the OVSDB
server sends it a row update, it does not poll on a timer). On a link-down
event it removes the failed edge from a declared topology graph, computes an
alternate switch-level path with breadth-first search, and installs/withdraws
real OVS flows for that path through the existing `daim_ovs_flow` CLI (the
DAIM OVS adapter). This replaces the scripted `install_alternate()` call in
stage3_link_recovery.py: the agent is a standalone long-running process that
reacts to whatever link goes down, rather than a test harness calling a
pre-written repair function for a specific known failure.

Simplification documented for the evidence record: the topology graph below
is declared, not discovered via LLDP or the DAIM_LINK_TABLE. Extending this
to live topology discovery is a separate increment.
"""
import json
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

_monitor_proc = None


def _terminate_monitor_and_exit(*_args):
    """SIGTERM/SIGINT handler: the ovsdb-client subscription is a child
    process that outlives a plain process exit (it is not killed by the
    parent dying), so without this it leaks as an orphan still attached to
    ovsdb-server -- confirmed during testing by dozens of accumulated
    `ovsdb-client monitor` processes across repeated experiment runs."""
    if _monitor_proc is not None and _monitor_proc.poll() is None:
        _monitor_proc.terminate()
    sys.exit(0)


signal.signal(signal.SIGTERM, _terminate_monitor_and_exit)
signal.signal(signal.SIGINT, _terminate_monitor_and_exit)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "implementation/build/daim_ovs_flow"

# switch -> {neighbor: (my_port, their_port)}
TOPOLOGY = {
    "s1": {"h1": (1, None), "s2": (2, 1), "s3": (3, 1)},
    "s2": {"s1": (1, 2), "s4": (2, 1)},
    "s3": {"s1": (1, 3), "s4": (2, 2)},
    "s4": {"s2": (1, 2), "s3": (2, 3), "h2": (3, None)},
}
HOST_ATTACHMENT = {"h1": "s1", "h2": "s4"}
SOURCE, DEST = "h1", "h2"

# Hold-down window (seconds) started on a monitored interface after a repair
# it triggered completes. Further link_state transitions on that interface
# are suppressed until the window elapses, so a flapping link causes one
# repair, not one repair per flap. Overridable per-call for testing.
HOLD_DOWN_SECONDS = 2.0

# OVS interface name -> the topology edge it belongs to.
MONITORED_INTERFACES = {
    "s1-eth2": ("s1", "s2"),
    "s2-eth1": ("s1", "s2"),
}


def log(event, **fields):
    record = {"ts": time.time(), "event": event}
    record.update(fields)
    print(json.dumps(record), flush=True)


def bfs_path(source, dest, down_edges):
    """Returns a list of switch names from the switch attached to `source`
    to the switch attached to `dest`, or None if no such path avoids
    down_edges. `source`/`dest` are host names (keys of HOST_ATTACHMENT)."""
    start, goal = HOST_ATTACHMENT[source], HOST_ATTACHMENT[dest]
    q = deque([[start]])
    seen = {start}
    while q:
        path = q.popleft()
        node = path[-1]
        if node == goal:
            return path
        for neighbor in TOPOLOGY.get(node, {}):
            if neighbor in ("h1", "h2"):
                continue
            edge = frozenset({node, neighbor})
            if edge in down_edges or neighbor in seen:
                continue
            seen.add(neighbor)
            q.append(path + [neighbor])
    return None


def path_to_flows(path):
    flows = []
    hops = ["h1"] + path + ["h2"]
    for i in range(1, len(hops) - 1):
        switch, prev_node, next_node = hops[i], hops[i - 1], hops[i + 1]
        in_port = TOPOLOGY[switch][prev_node][0]
        out_port = TOPOLOGY[switch][next_node][0]
        flows.append((switch, f"priority=100,in_port={in_port},actions=output:{out_port}"))
        flows.append((switch, f"priority=100,in_port={out_port},actions=output:{in_port}"))
    return flows


def apply_flow(action, bridge, arg):
    log("flow_start", action=action, bridge=bridge, arg=arg)
    try:
        r = subprocess.run([str(CLI), action, bridge, arg], text=True, capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        log("flow_timeout", action=action, bridge=bridge, arg=arg)
        return False
    if r.returncode != 0:
        log("flow_error", action=action, bridge=bridge, arg=arg, stderr=r.stderr.strip())
        return False
    return True


def install_path(path):
    for bridge, flow in path_to_flows(path):
        apply_flow("add", bridge, flow)


def withdraw_path(path):
    for bridge, flow in path_to_flows(path):
        # del-flows match syntax does not accept "priority=..."; strip it,
        # matching stage3_link_recovery.py's install_alternate() convention.
        match = flow.split(",actions=")[0].split(",", 1)[1]
        apply_flow("delete", bridge, match)


def is_held_down(name, held_down_until, now):
    return name in held_down_until and now < held_down_until[name]


def decide_link_event(name, state, down_edges, down_interfaces, held_down_until,
                       current_path, now, hold_down_seconds=HOLD_DOWN_SECONDS):
    """Pure decision function for one OVSDB link-state event on a monitored
    interface: IDLE/ACTIVE/HELD-DOWN state machine plus the existing BFS
    repair decision. Mutates down_edges/down_interfaces/held_down_until in
    place (the agent's state); does no I/O and calls no subprocess, so this
    is exercised directly by a synthetic event sequence and a fake clock in
    test_daim_link_agent.py without OVSDB, OVS, or Mininet.
    Returns a dict describing what the caller (main's I/O loop) should do.
    """
    switch, neighbor = MONITORED_INTERFACES[name]
    edge = frozenset({switch, neighbor})

    if is_held_down(name, held_down_until, now):
        return {"action": "suppressed", "interface": name, "state": state,
                "remaining_s": held_down_until[name] - now}

    if state == "down" and name not in down_interfaces:
        down_interfaces.add(name)
        down_edges.add(edge)
        new_path = bfs_path(SOURCE, DEST, down_edges)
        if not new_path:
            return {"action": "repair_failed", "interface": name,
                     "reason": "no alternate path avoiding down edges"}
        if new_path == current_path:
            return {"action": "noop", "interface": name}
        held_down_until[name] = now + hold_down_seconds
        return {"action": "repair", "interface": name, "edge": [switch, neighbor],
                "old_path": current_path, "new_path": new_path}

    if state == "up" and name in down_interfaces:
        down_interfaces.discard(name)
        down_edges.discard(edge)
        return {"action": "recovered", "interface": name}

    return {"action": "ignored", "interface": name, "state": state}


def monitor_link_rows():
    """Yields (name, link_state) for every real 'new' row OVSDB pushes on
    Interface.link_state, in the order ovsdb-client delivers them."""
    global _monitor_proc
    proc = subprocess.Popen(
        ["ovsdb-client", "monitor", "Interface", "name,link_state", "--format=json"],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    _monitor_proc = proc
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        headings = payload.get("headings", [])
        if "action" not in headings or "name" not in headings or "link_state" not in headings:
            continue
        action_i = headings.index("action")
        name_i = headings.index("name")
        state_i = headings.index("link_state")
        for row in payload.get("data", []):
            if row[action_i] != "new":
                continue
            name = row[name_i]
            state = row[state_i]
            if name:
                yield name, state


def main():
    current_path = bfs_path(SOURCE, DEST, down_edges=set())
    if not current_path:
        log("fatal", reason="no initial path")
        sys.exit(1)
    install_path(current_path)
    log("agent_started", initial_path=current_path)

    down_edges = set()
    down_interfaces = set()
    held_down_until = {}

    for name, state in monitor_link_rows():
        if name not in MONITORED_INTERFACES:
            continue
        detected_ns = time.perf_counter_ns()
        decision = decide_link_event(
            name, state, down_edges, down_interfaces, held_down_until,
            current_path, time.monotonic(),
        )
        action = decision["action"]

        if action == "suppressed":
            log("transition_suppressed", interface=name, state=state,
                remaining_s=decision["remaining_s"])
        elif action == "repair_failed":
            log("link_down_detected", interface=name, ns=detected_ns)
            log("repair_failed", reason=decision["reason"])
        elif action == "repair":
            log("link_down_detected", interface=name, edge=decision["edge"], ns=detected_ns)
            repair_start_ns = time.perf_counter_ns()
            withdraw_path(current_path)
            install_path(decision["new_path"])
            current_path = decision["new_path"]
            repair_end_ns = time.perf_counter_ns()
            log("repair_installed", path=current_path,
                repair_start_ns=repair_start_ns, repair_end_ns=repair_end_ns,
                held_down_seconds=HOLD_DOWN_SECONDS)
        elif action == "recovered":
            log("link_up_detected", interface=name)
        # "noop"/"ignored": no state change, nothing to log beyond the event
        # itself being a repeat notification OVSDB is allowed to send.


if __name__ == "__main__":
    main()
