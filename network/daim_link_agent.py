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

# edge -> the set of monitored interface names that observe it. A physical
# link has two independently-reporting OVS interfaces; an edge is only
# confirmed recovered once every interface that observes it has reported
# `up` (see _edge_confirmed_up below).
EDGE_INTERFACES = {}
for _name, (_a, _b) in MONITORED_INTERFACES.items():
    EDGE_INTERFACES.setdefault(frozenset({_a, _b}), set()).add(_name)
del _name, _a, _b


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


def is_held_down(edge, held_down_until, now):
    return edge in held_down_until and now < held_down_until[edge]


def _edge_confirmed_up(edge, interface_state):
    """An edge is only considered recovered once *every* monitored interface
    that observes it has independently reported `up`. A physical link has
    two independently-reporting OVS interfaces; collapsing them into a
    single last-report-wins value (an earlier revision of this function did
    exactly that) means whichever side's `up` happens to arrive last would
    recover the edge even if the other side is still reporting `down` --
    losing the specific side's identity is the bug, not just the ordering.
    An interface that has never reported anything defaults to `up`, since
    down_edges/interface_state both start empty on the assumption that
    nothing has failed yet; this only matters once an interface has actually
    reported `down` and is why single-interface synthetic tests, which never
    drive the second interface at all, still behave correctly here."""
    interfaces = EDGE_INTERFACES.get(edge, frozenset())
    return all(interface_state.get(name, "up") == "up" for name in interfaces)


def reconcile_expired_holddowns(held_down_until, interface_state, down_edges, now):
    """Fixes a stale-state bug: a transition suppressed during HELD-DOWN was
    previously dropped with no record, so an edge that went down, was
    repaired, then came back up *during* the hold-down window (a suppressed
    `up`) stayed marked down forever if no further OVSDB event ever arrived
    for it -- the agent had nothing to trigger re-evaluation on. This
    function must be called (a) opportunistically, whenever any event for a
    given interface is processed (decide_link_event does this), and (b) on a
    bounded timeout even when no event arrives at all, which is why
    monitor_link_rows() below accepts a `poll_interval` -- purely to drive
    this reconciliation call, not to poll for failures (detection stays
    100% push-based). Returns the list of edges that were reconciled as
    recovered (every interface observing that edge last reported "up")."""
    recovered = []
    for edge in list(held_down_until):
        if now < held_down_until[edge]:
            continue
        del held_down_until[edge]
        if edge in down_edges and _edge_confirmed_up(edge, interface_state):
            down_edges.discard(edge)
            recovered.append(edge)
    return recovered


def decide_link_event(name, state, down_edges, held_down_until,
                       interface_state, current_path, now,
                       hold_down_seconds=HOLD_DOWN_SECONDS):
    """Pure decision function for one OVSDB link-state event on a monitored
    interface: IDLE/ACTIVE/HELD-DOWN state machine plus the existing BFS
    repair decision. Mutates down_edges/held_down_until/interface_state
    in place (the agent's state); does no I/O and calls no subprocess, so
    this is exercised directly by a synthetic event sequence and a fake
    clock in test_daim_link_agent.py without OVSDB, OVS, or Mininet.

    Hold-down state is keyed by *edge* (the frozenset of the two switches a
    link connects), not by interface name. A physical link corresponds to
    two OVS interfaces -- one per side -- and OVSDB reports their
    link_state transitions independently and not always simultaneously; a
    live-network run of the flapping-link protocol (Section 6.6 of the
    manuscript) found that keying hold-down by interface name only
    suppressed the side whose `down` event happened to trigger the repair,
    leaving the other side's transitions on the *same physical link*
    completely unsuppressed. Keying by edge instead means either side
    reporting a transition is evaluated against the same hold-down window.
    A second, related defect found by code review (not by testing): an
    earlier revision recorded only a single last-observed-state value per
    *edge*, keyed by whichever interface happened to report most recently
    -- so one side's `up` could recover the edge while the other side's
    independently-reported state was still `down`. `_edge_confirmed_up()`
    closes this by tracking state per *interface* and requiring every
    interface that observes an edge to agree it is `up` before treating the
    edge as recovered, both here and in `reconcile_expired_holddowns`.
    `interface_state` is updated with *this* event only after the
    reconciliation call above has run, so a just-arrived event cannot cause
    reconcile_expired_holddowns to silently claim the recovery this
    function's own return value should report.
    Returns a dict describing what the caller (main's I/O loop) should do.
    """
    switch, neighbor = MONITORED_INTERFACES[name]
    edge = frozenset({switch, neighbor})

    reconcile_expired_holddowns(held_down_until, interface_state, down_edges, now)
    interface_state[name] = state

    if is_held_down(edge, held_down_until, now):
        return {"action": "suppressed", "interface": name, "state": state,
                "remaining_s": held_down_until[edge] - now}

    if state == "down" and edge not in down_edges:
        down_edges.add(edge)
        new_path = bfs_path(SOURCE, DEST, down_edges)
        if not new_path:
            return {"action": "repair_failed", "interface": name,
                     "reason": "no alternate path avoiding down edges"}
        if new_path == current_path:
            return {"action": "noop", "interface": name}
        held_down_until[edge] = now + hold_down_seconds
        return {"action": "repair", "interface": name, "edge": [switch, neighbor],
                "old_path": current_path, "new_path": new_path}

    if state == "up" and edge in down_edges and _edge_confirmed_up(edge, interface_state):
        down_edges.discard(edge)
        return {"action": "recovered", "interface": name}

    return {"action": "ignored", "interface": name, "state": state}


def _parse_monitor_line(line, actions=("new",)):
    """Parses one ovsdb-client monitor JSON line into a list of (name, state)
    pairs for Interface rows whose `action` is in `actions`. Pure function,
    split out so it is testable without a subprocess.

    OVSDB's monitor reply reports the table's *current* contents as its
    first line, with each of those rows' `action` field set to `"initial"`
    -- not `"new"`, which is reserved for a row added or changed by a
    *later* update (confirmed empirically against a real ovsdb-server: the
    initial dump is one JSON blob with `action":"initial"` on every row;
    each subsequent transition instead sends an `"old"` row, giving the
    previous value, immediately followed by a `"new"` row with the current
    one). The default `actions=("new",)` is for the ongoing event stream;
    `read_initial_snapshot()` below calls this with `actions=("initial",)`
    for the one-time startup read."""
    line = line.strip()
    if not line:
        return []
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return []
    headings = payload.get("headings", [])
    if "action" not in headings or "name" not in headings or "link_state" not in headings:
        return []
    action_i = headings.index("action")
    name_i = headings.index("name")
    state_i = headings.index("link_state")
    rows = []
    for row in payload.get("data", []):
        if row[action_i] not in actions:
            continue
        name = row[name_i]
        state = row[state_i]
        if name:
            rows.append((name, state))
    return rows


def _start_monitor():
    global _monitor_proc
    proc = subprocess.Popen(
        ["ovsdb-client", "monitor", "Interface", "name,link_state", "--format=json"],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    _monitor_proc = proc
    return proc


def read_initial_snapshot(proc, timeout=10.0):
    """Reads exactly the first line ovsdb-client monitor sends -- the table's
    current contents at subscription time, one row per interface, every row
    carrying `action=="initial"` -- and returns {interface_name: link_state}
    for every *monitored* interface reported in it.

    This closes a real startup/restart correctness gap: an earlier revision
    of this agent only ever matched `action=="new"` (see _parse_monitor_line),
    so a monitored interface that was already `down` when the agent started
    or restarted was silently invisible -- the agent would compute and
    install its initial path assuming every edge was up, potentially routing
    traffic through a link that was already failed, and nothing would ever
    correct that unless a further transition happened to arrive later for
    that specific interface. Found by code review, verified empirically
    against a real ovsdb-server before being treated as real (a fresh
    `ovsdb-client monitor` against a live OVS bridge does report
    `action":"initial"` on its first line, not `"new"`).

    Interfaces absent from the initial snapshot are left absent from the
    returned dict -- deliberately not defaulted to "up" here, unlike
    `_edge_confirmed_up`'s runtime default: `main()` treats any
    `MONITORED_INTERFACES` entry missing from this snapshot as a fatal
    misconfiguration rather than silently assuming it is fine."""
    import select
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("no initial OVSDB snapshot received within timeout")
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("OVSDB monitor closed before sending an initial snapshot")
    rows = _parse_monitor_line(line, actions=("initial",))
    return {name: state for name, state in rows if name in MONITORED_INTERFACES}


def monitor_link_rows(proc, poll_interval=None):
    """Yields (name, link_state) for every real 'new' row OVSDB pushes on
    Interface.link_state on an *already-started* monitor subprocess (see
    _start_monitor/read_initial_snapshot, which must run first so the
    initial snapshot this generator does not handle is not lost), in the
    order ovsdb-client delivers them.

    If `poll_interval` is given, also yields `None` at least every
    `poll_interval` seconds when no OVSDB event has arrived. This is *not*
    failure-detection polling -- a real link-down/up event still arrives
    immediately via the push subscription below and is yielded the instant
    it is read. The `None` ticks exist only so main() can call
    reconcile_expired_holddowns() on a bounded schedule even when an
    interface's hold-down window expires with no further event ever seen
    for it (see reconcile_expired_holddowns' docstring for why that case
    needs a wake-up source at all)."""
    if poll_interval is None:
        for line in proc.stdout:
            for name, state in _parse_monitor_line(line):
                yield name, state
        return

    import select
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], poll_interval)
        if not ready:
            yield None
            continue
        line = proc.stdout.readline()
        if not line:
            return
        for name, state in _parse_monitor_line(line):
            yield name, state


def down_edges_from_snapshot(snapshot):
    """Derives the set of down edges implied by an initial (or any other)
    {interface_name: link_state} snapshot -- pulled out of main() as its own
    pure function so the startup-state-detection logic is exercised
    identically by main() and by test_startup_detects_already_down_edge()."""
    down_edges = set()
    for name, state in snapshot.items():
        if state == "down" and name in MONITORED_INTERFACES:
            switch, neighbor = MONITORED_INTERFACES[name]
            down_edges.add(frozenset({switch, neighbor}))
    return down_edges


def main():
    proc = _start_monitor()
    initial = read_initial_snapshot(proc)
    missing = [name for name in MONITORED_INTERFACES if name not in initial]
    if missing:
        log("fatal", reason="missing initial OVSDB state for monitored interface(s)",
            interfaces=missing)
        sys.exit(1)

    interface_state = dict(initial)
    down_edges = down_edges_from_snapshot(initial)

    current_path = bfs_path(SOURCE, DEST, down_edges)
    if not current_path:
        log("fatal", reason="no initial path avoiding edges already down at startup",
            down_edges=[list(edge) for edge in down_edges])
        sys.exit(1)
    install_path(current_path)
    log("agent_started", initial_path=current_path,
        down_edges=[list(edge) for edge in down_edges])

    held_down_until = {}

    # poll_interval only drives reconcile_expired_holddowns() wake-ups
    # (see that function's docstring); it is well under HOLD_DOWN_SECONDS so
    # an expired window is reconciled promptly even with no further event.
    for event in monitor_link_rows(proc, poll_interval=HOLD_DOWN_SECONDS / 4):
        if event is None:
            recovered = reconcile_expired_holddowns(
                held_down_until, interface_state, down_edges, time.monotonic(),
            )
            for edge in recovered:
                log("link_up_detected", edge=list(edge), reconciled_after_holddown=True)
            continue

        name, state = event
        if name not in MONITORED_INTERFACES:
            continue
        detected_ns = time.perf_counter_ns()
        decision = decide_link_event(
            name, state, down_edges, held_down_until,
            interface_state, current_path, time.monotonic(),
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
