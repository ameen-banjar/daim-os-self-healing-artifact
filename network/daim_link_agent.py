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
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

_monitor_procs = []


def _terminate_monitor_and_exit(*_args):
    """SIGTERM/SIGINT handler: the ovsdb-client subscription is a child
    process that outlives a plain process exit (it is not killed by the
    parent dying), so without this it leaks as an orphan still attached to
    ovsdb-server -- confirmed during testing by dozens of accumulated
    `ovsdb-client monitor` processes across repeated experiment runs. A
    multi-OVS deployment (REMOTE_ENDPOINTS non-empty) can have more than one
    such subscription open at once -- one per distinct OVSDB endpoint -- so
    this terminates all of them, not just one."""
    for proc in _monitor_procs:
        if proc.poll() is None:
            proc.terminate()
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

# OVS interface name -> the topology edge it belongs to.
MONITORED_INTERFACES = {
    "s1-eth2": ("s1", "s2"),
    "s2-eth1": ("s1", "s2"),
}

# switch -> {"ovsdb": <ovsdb-client target>, "openflow": <ovs-ofctl target>}
# for a switch that lives on an OVS instance other than the agent's local
# one. A switch absent from this dict is local: monitored via the agent's
# own default ovsdb-client connection, and controlled via the local
# daim_ovs_flow CLI exactly as in every single-host deployment measured so
# far. Empty by default -- this is the single-shared-OVSDB deployment every
# other experiment in this evidence set uses; set DAIM_TOPOLOGY_CONFIG to a
# JSON file (see load_topology_config below) to point at a real multi-OVS
# testbed instead, without changing this file's behaviour when unset.
REMOTE_ENDPOINTS = {}


def load_topology_config(path):
    """Overrides TOPOLOGY/HOST_ATTACHMENT/SOURCE/DEST/MONITORED_INTERFACES/
    REMOTE_ENDPOINTS from a JSON file, for deployments (like the multi-OVS
    experiment in Section 7.7 of the manuscript) that need a different
    topology and a non-empty REMOTE_ENDPOINTS than the single-host diamond
    hardcoded above. Deliberately opt-in via the DAIM_TOPOLOGY_CONFIG
    environment variable rather than always reading a config file, so every
    existing test and single-host experiment keeps working unchanged with
    no config file present -- this is additive, not a replacement for the
    default configuration."""
    global TOPOLOGY, HOST_ATTACHMENT, SOURCE, DEST, MONITORED_INTERFACES, REMOTE_ENDPOINTS
    with open(path) as f:
        config = json.load(f)
    TOPOLOGY = {
        switch: {neighbor: tuple(ports) for neighbor, ports in neighbors.items()}
        for switch, neighbors in config["topology"].items()
    }
    HOST_ATTACHMENT = config["host_attachment"]
    SOURCE, DEST = config["source"], config["dest"]
    MONITORED_INTERFACES = {
        name: tuple(edge) for name, edge in config["monitored_interfaces"].items()
    }
    REMOTE_ENDPOINTS = config.get("remote_endpoints", {})


_config_path = os.environ.get("DAIM_TOPOLOGY_CONFIG")
if _config_path:
    load_topology_config(_config_path)
del _config_path

# Hold-down window (seconds) started on a monitored interface after a repair
# it triggered completes. Further link_state transitions on that interface
# are suppressed until the window elapses, so a flapping link causes one
# repair, not one repair per flap. Overridable per-call for testing.
HOLD_DOWN_SECONDS = 2.0


def _edge_interfaces():
    """edge -> the set of monitored interface names that observe it. A
    physical link has two independently-reporting OVS interfaces; an edge
    is only confirmed recovered once every interface that observes it has
    reported `up` (see _edge_confirmed_up below). Recomputed as a function,
    not a module-level constant, so it reflects MONITORED_INTERFACES after
    load_topology_config() has possibly overridden it."""
    result = {}
    for name, (a, b) in MONITORED_INTERFACES.items():
        result.setdefault(frozenset({a, b}), set()).add(name)
    return result


EDGE_INTERFACES = _edge_interfaces()


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
    """Installs/withdraws one flow. A `bridge` present in REMOTE_ENDPOINTS is
    routed directly through `ovs-ofctl` at that switch's registered remote
    OpenFlow target instead of through the local `daim_ovs_flow` CLI -- the
    per-hop routing logic a multi-OVS deployment needs (Section 8.3): the
    agent still runs as a single process with one decision loop, but its I/O
    now goes to whichever OVS instance actually owns the bridge in question,
    not always the local one. The remote path calls the identical
    `ovs-ofctl -O OpenFlow13 <add-flow|del-flows> <target> <flow>` command
    daim_ovs_flow.c wraps for the local case (Section 4.6), just with an
    explicit `tcp:HOST:PORT` target instead of a local bridge name -- both
    are valid `ovs-ofctl` connection targets, so this is not a different
    code path in spirit, only in which binary invokes it."""
    log("flow_start", action=action, bridge=bridge, arg=arg)
    remote = REMOTE_ENDPOINTS.get(bridge)
    if remote:
        ofctl_action = "add-flow" if action == "add" else "del-flows"
        argv = ["ovs-ofctl", "-O", "OpenFlow13", ofctl_action, remote["openflow"], arg]
    else:
        argv = [str(CLI), action, bridge, arg]
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=5)
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

    # Runtime counterpart of the startup snapshot's link_state validation
    # (down_edges_from_snapshot): an unrecognised value is rejected outright,
    # before touching any state, rather than falling through to the final
    # "ignored" return -- the same silent-unknown-treated-as-fine gap the
    # startup fix closed, reached via the ongoing event stream instead of
    # the initial snapshot. Found by code review, not a live failure.
    if state not in ("up", "down"):
        return {"action": "invalid_link_state", "interface": name, "state": state}

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


def _owning_switch(name):
    """The switch an OVS interface name belongs to, by naming convention
    (e.g. "s3-eth1" belongs to "s3") -- used to look up which OVSDB/OpenFlow
    endpoint a monitored interface's switch lives on."""
    return name.split("-eth")[0]


def _ovsdb_target_for_interface(name):
    remote = REMOTE_ENDPOINTS.get(_owning_switch(name))
    return remote["ovsdb"] if remote else None


def _monitored_ovsdb_targets():
    """The distinct set of ovsdb-client monitor targets needed to observe
    every interface in MONITORED_INTERFACES: None for the agent's own local
    OVSDB instance, plus one entry per distinct remote OVSDB endpoint
    referenced by REMOTE_ENDPOINTS. In the default (empty REMOTE_ENDPOINTS)
    configuration this is always exactly `[None]` -- a single local
    connection, the deployment every other experiment in this evidence set
    measures."""
    targets = {_ovsdb_target_for_interface(name) for name in MONITORED_INTERFACES}
    return sorted(targets, key=lambda t: (t is not None, t))


def _start_monitor(ovsdb_target=None):
    cmd = ["ovsdb-client", "monitor"]
    if ovsdb_target:
        cmd.append(ovsdb_target)
    cmd += ["Interface", "name,link_state", "--format=json"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    _monitor_procs.append(proc)
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


def monitor_link_rows(procs, poll_interval=None):
    """Yields (name, link_state) for every real 'new' row pushed by any of
    the given *already-started* monitor subprocesses (see _start_monitor/
    read_initial_snapshot, which must run first for each so the initial
    snapshot this generator does not handle is not lost), multiplexed via
    `select()` across however many there are. A multi-OVS deployment opens
    one connection per distinct OVSDB endpoint its MONITORED_INTERFACES
    span (Section 8.3's connection-multiplexing requirement) and passes all
    of them here as a list; the default single-endpoint configuration
    passes a list of exactly one, and this behaves identically to watching
    that one connection as before.

    If `poll_interval` is given, also yields `None` at least every
    `poll_interval` seconds when no OVSDB event has arrived on *any*
    connection. This is *not* failure-detection polling -- a real
    link-down/up event still arrives immediately via the push subscription
    it occurred on and is yielded the instant it is read. The `None` ticks
    exist only so main() can call reconcile_expired_holddowns() on a
    bounded schedule even when an interface's hold-down window expires with
    no further event ever seen for it (see reconcile_expired_holddowns'
    docstring for why that case needs a wake-up source at all)."""
    import select
    streams = [p.stdout for p in procs]
    while streams:
        ready, _, _ = select.select(streams, [], [], poll_interval)
        if not ready:
            yield None
            continue
        for stream in ready:
            line = stream.readline()
            if not line:
                streams.remove(stream)
                continue
            for name, state in _parse_monitor_line(line):
                yield name, state


def down_edges_from_snapshot(snapshot):
    """Derives the set of down edges implied by an initial (or any other)
    {interface_name: link_state} snapshot -- pulled out of main() as its own
    pure function so the startup-state-detection logic is exercised
    identically by main() and by test_startup_detects_already_down_edge().

    Requires every monitored interface's reported state to be exactly "up"
    or "down"; any other value (OVS documents Interface.link_state as
    optional, and it can in principle be empty for a non-applicable port)
    is treated as fatal rather than silently falling through to "not down",
    which would otherwise be indistinguishable from a genuinely healthy
    link at startup -- the same kind of implicit-default gap the startup
    snapshot fix itself was written to close."""
    down_edges = set()
    for name, state in snapshot.items():
        if name not in MONITORED_INTERFACES:
            continue
        if state not in ("up", "down"):
            raise RuntimeError(
                f"unexpected initial link_state {state!r} for monitored interface "
                f"{name!r}: expected exactly 'up' or 'down'"
            )
        if state == "down":
            switch, neighbor = MONITORED_INTERFACES[name]
            down_edges.add(frozenset({switch, neighbor}))
    return down_edges


def main():
    targets = _monitored_ovsdb_targets()
    procs = [_start_monitor(target) for target in targets]
    if len(targets) > 1:
        log("multi_ovs_connections_opened", targets=[t or "local" for t in targets])
    # Every exit from this block -- a normal sys.exit(1) on a fatal startup
    # condition, or an unexpected exception such as down_edges_from_snapshot's
    # RuntimeError on an unrecognised link_state value -- must still
    # terminate every monitor child; without this, a fatal startup path
    # leaked an orphaned `ovsdb-client monitor` exactly like the case the
    # SIGTERM/SIGINT handler above already guards against, just reached a
    # different way (an in-process exit rather than a signal). A multi-OVS
    # deployment can have more than one such child open at once.
    try:
        initial = {}
        for proc in procs:
            initial.update(read_initial_snapshot(proc))
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
    except BaseException:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        raise

    held_down_until = {}

    # poll_interval only drives reconcile_expired_holddowns() wake-ups
    # (see that function's docstring); it is well under HOLD_DOWN_SECONDS so
    # an expired window is reconciled promptly even with no further event.
    for event in monitor_link_rows(procs, poll_interval=HOLD_DOWN_SECONDS / 4):
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
        elif action == "invalid_link_state":
            log("invalid_link_state", interface=name, state=decision["state"])
        # "noop"/"ignored": no state change, nothing to log beyond the event
        # itself being a repeat notification OVSDB is allowed to send.


if __name__ == "__main__":
    main()
