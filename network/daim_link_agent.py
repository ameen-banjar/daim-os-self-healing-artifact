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
import hashlib
import json
import os
import re
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
    down_edges. `source`/`dest` are host names (keys of HOST_ATTACHMENT).
    Neighbours that are themselves hosts (checked against HOST_ATTACHMENT,
    not the literal names "h1"/"h2" an earlier revision hardcoded here) are
    skipped -- BFS only walks switch-to-switch edges, since TOPOLOGY's
    per-switch adjacency dict includes the switch's own attached host as a
    neighbour alongside its switch neighbours. Deployments declaring
    differently-named hosts via load_topology_config() (none of the
    topologies measured in this paper do -- both use `h1`/`h2`) now resolve
    correctly instead of silently walking through a host as if it were a
    transit switch."""
    start, goal = HOST_ATTACHMENT[source], HOST_ATTACHMENT[dest]
    q = deque([[start]])
    seen = {start}
    while q:
        path = q.popleft()
        node = path[-1]
        if node == goal:
            return path
        for neighbor in TOPOLOGY.get(node, {}):
            if neighbor in HOST_ATTACHMENT:
                continue
            edge = frozenset({node, neighbor})
            if edge in down_edges or neighbor in seen:
                continue
            seen.add(neighbor)
            q.append(path + [neighbor])
    return None


def _agent_cookie():
    """The OpenFlow cookie this agent tags its own flows with, so a
    withdrawal can be scoped to exactly this agent's flows (see
    _delete_match() below) rather than matching broadly on in_port alone,
    which would also delete an unrelated flow some other DAIM-OS process
    installed on the same switch/port. Deterministic, derived from the
    declared SOURCE/DEST pair (read live, like REMOTE_ENDPOINTS elsewhere in
    this file, so a deployment overriding them via load_topology_config()
    gets its own cookie) via a truncated SHA-256 digest, rather than a
    single fixed constant an earlier revision used -- that constant was
    correct for the one-agent-per-process deployments measured in this
    paper, but would collide if two agent processes ever protected
    different source-destination pairs on a switch they share: both would
    use the identical cookie, and each agent's withdrawal calls would
    delete the other's flows too, exactly the bug the fixed constant was
    introduced to close. Section 4.1's declared deployment model is one
    process per protected pair, so keying the cookie to that pair is the
    natural scope, not the process or the topology. Deterministic across
    restarts (not randomised at startup) is deliberate: a restarted agent
    protecting the same pair must still recognise, and be able to
    withdraw, flows a prior instance of itself installed before the
    restart. Uses the full 64-bit width of OpenFlow's cookie field (16 hex
    digits of the SHA-256 digest, not 8/32 bits as an earlier revision
    took) -- confirmed empirically that `ovs-ofctl add-flow`/`dump-flows`/
    a cookie-masked `del-flows` all accept and correctly round-trip a full
    64-bit cookie value unchanged. A 32-bit truncation was still
    deterministic and pair-scoped, just with a needlessly higher collision
    probability between two different pairs than the field width allows
    for no benefit -- widening it is a strict improvement, not a new
    guarantee: this is deterministic scoping between COOPERATING agents in
    this paper's declared one-process-per-pair deployment model, not a
    cryptographic uniqueness or security guarantee against an adversarial
    or colluding process choosing the same pair on purpose."""
    digest = hashlib.sha256(f"{SOURCE}->{DEST}".encode()).hexdigest()
    return int(digest[:16], 16)


def path_to_flows(path):
    """Translates a switch path into per-switch flow rules, walking from the
    declared SOURCE host to the declared DEST host -- reads these as module
    globals at call time (like REMOTE_ENDPOINTS elsewhere in this file) so a
    deployment that overrides them via load_topology_config() is honoured;
    an earlier revision hardcoded the literal host names "h1"/"h2" here
    instead. Every flow carries this agent's cookie (_agent_cookie()) so
    withdraw_path() can scope its delete calls to this agent's own flows
    only (see _delete_match())."""
    flows = []
    cookie = _agent_cookie()
    hops = [SOURCE] + path + [DEST]
    for i in range(1, len(hops) - 1):
        switch, prev_node, next_node = hops[i], hops[i - 1], hops[i + 1]
        in_port = TOPOLOGY[switch][prev_node][0]
        out_port = TOPOLOGY[switch][next_node][0]
        flows.append((switch, f"cookie=0x{cookie:x},priority=100,in_port={in_port},actions=output:{out_port}"))
        flows.append((switch, f"cookie=0x{cookie:x},priority=100,in_port={out_port},actions=output:{in_port}"))
    return flows


def _openflow_target(bridge):
    """The `ovs-ofctl` target for `bridge`: its registered remote OpenFlow
    endpoint (a `tcp:HOST:PORT` string) if `bridge` is in REMOTE_ENDPOINTS,
    otherwise the bridge name unchanged for a local bridge. Shared by
    apply_flow() (through the daim_ovs_flow adapter) and
    _conflicting_flow_cookie() (a direct, read-only ovs-ofctl call) so the
    two never resolve a bridge to different targets."""
    remote = REMOTE_ENDPOINTS.get(bridge)
    return remote["openflow"] if remote else bridge


class _ForwardingCheckError(Exception):
    """Raised when a direct, read-only `ovs-ofctl dump-flows` query --
    Section 5.1's pre-install conflict check (`_conflicting_flow_cookie()`)
    or the ambiguous-outcome read-back below (`_add_confirmed_by_readback()`/
    `_delete_confirmed_by_readback()`) -- itself could not be completed
    (timeout or non-zero exit), as opposed to completing and returning a
    definite answer. Kept distinct from a definite "no conflict"/"not
    present" result so every caller can fail safe: an unreadable switch
    state is treated as unresolved, never as a green light."""


def _dump_flows_for_match(bridge, in_port):
    """Runs a real `ovs-ofctl dump-flows <target> in_port=N` query on
    `bridge`, filtered to `in_port` -- shared by every read-only switch-state
    check in this file (Section 5.1's pre-install conflict check, and the
    ambiguous-outcome read-back below), so they all resolve `bridge` to a
    target and handle a failed query identically. `priority=` cannot be
    used as a `dump-flows` filter keyword either (rejected outright,
    `unknown keyword priority`, confirmed empirically -- the same
    restriction non-strict `del-flows` has, see `_delete_match()` above),
    so callers filter on `in_port=` alone and check `priority=100` (or
    whatever else they need) against the returned lines themselves.
    Returns the raw output lines. Raises `_ForwardingCheckError` if the
    query itself could not be completed."""
    target = _openflow_target(bridge)
    argv = ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", target, f"in_port={in_port}"]
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise _ForwardingCheckError()
    if r.returncode != 0:
        raise _ForwardingCheckError()
    return r.stdout.splitlines()


def _conflicting_flow_cookie(bridge, flow):
    """Section 5.1's forwarding-consistency check: a read-before-write query
    -- direct `ovs-ofctl dump-flows` (`_dump_flows_for_match()`), not the
    DAIM-OS OVS adapter, since `daim_ovs_flow` only exposes `add`/`delete`
    (see daim_ovs_flow.c) and this is a read, not an installation, so it
    does not touch the adapter's "flows are installed through the adapter"
    claim (Section 4.1), which is about how flows are installed, not how
    existing switch state is read for a safety pre-check -- for any flow
    already occupying the exact `priority=100,in_port=N` match `flow` is
    about to use on `bridge`. Returns that flow's cookie (as a lowercase
    hex string with no `0x` prefix) if one is installed there, or None if
    the match is free. Without this check, install_path() would call
    `apply_flow("add", ...)` unconditionally, and OVS's `add-flow` at an
    already-occupied exact priority+match is a silent in-place replace, not
    a coexist or a rejection -- confirmed empirically -- so a second
    process's flow at that match would be overwritten with no record of
    what was lost. Note: this check is detect-and-reject, not a race-free
    guarantee -- the read (this query) and the write (the caller's
    subsequent `apply_flow("add", ...)`) are two separate calls, not one
    atomic operation, so it detects and rejects a conflict present at
    check time, not every conflict a concurrent writer could ever produce
    (Section 3's claim boundary explicitly excludes concurrent multi-client
    coordination on a shared match)."""
    in_port = flow.split("in_port=")[1].split(",")[0]
    try:
        lines = _dump_flows_for_match(bridge, in_port)
    except _ForwardingCheckError:
        log("forwarding_check_timeout_or_error", bridge=bridge, flow=flow)
        raise
    for line in lines:
        if "priority=100," not in line:
            continue
        match = re.search(r"cookie=0x([0-9a-fA-F]+)", line)
        if match:
            return match.group(1)
    return None


def _add_confirmed_by_readback(bridge, flow):
    """Resolves an AMBIGUOUS `apply_flow("add", ...)` outcome -- a
    subprocess timeout, where the client gave up waiting but the
    underlying OVS operation's actual effect on the switch is unknown, not
    a confirmed failure the way a non-zero exit is (that means the
    adapter/`ovs-ofctl` itself reported failure before or without applying
    anything). A direct read-back (`_dump_flows_for_match()`, a read, not
    an install, for the same reason `_conflicting_flow_cookie()` above
    is): True if `flow` -- this agent's own cookie, at its
    `priority=100,in_port=N` match, with exactly its intended action -- is
    confirmed present on `bridge` right now (the add DID take effect,
    however slowly); False if that exact match is confirmed absent (the
    add did NOT take effect). Raises `_ForwardingCheckError` if the
    read-back query itself could not be completed, so the caller can
    still distinguish "confirmed" from "cannot tell" rather than guessing
    either way."""
    cookie = flow.split("cookie=0x")[1].split(",")[0]
    in_port = flow.split("in_port=")[1].split(",")[0]
    actions = flow.split("actions=")[1]
    lines = _dump_flows_for_match(bridge, in_port)
    for line in lines:
        if f"cookie=0x{cookie}," not in line:
            continue
        if "priority=100," not in line:
            continue
        if line.rstrip().endswith(f"actions={actions}"):
            return True
    return False


def _delete_confirmed_by_readback(bridge, delete_match):
    """Resolves an AMBIGUOUS `apply_flow("delete", ...)` outcome (a
    subprocess timeout, the same ambiguity `_add_confirmed_by_readback()`
    above resolves for adds): True if no flow at `delete_match`'s cookie
    and `in_port` remains on `bridge` (the delete DID take effect); False
    if a flow at that cookie+`in_port` is still present (it did not).
    Raises `_ForwardingCheckError` if the read-back query itself could not
    be completed."""
    cookie = delete_match.split("cookie=0x")[1].split("/")[0]
    in_port = delete_match.split("in_port=")[1]
    lines = _dump_flows_for_match(bridge, in_port)
    return not any(f"cookie=0x{cookie}," in line for line in lines)


def apply_flow(action, bridge, arg):
    """Installs/withdraws one flow through the DAIM-OS OVS adapter
    (`daim_ovs_flow`), for both local and remote bridges. A `bridge` present
    in REMOTE_ENDPOINTS passes that switch's registered remote OpenFlow
    target (a `tcp:HOST:PORT` string) as the adapter's target argument
    instead of a local bridge name; a bridge absent from REMOTE_ENDPOINTS
    passes the bridge name unchanged, exactly as every single-host
    experiment in this evidence set already does. This is not two different
    code paths: `daim_ovs_flow`/`ovs_switch_adapter.c` treat their target
    argument as an opaque string, forwarded unchanged into `ovs-ofctl -O
    OpenFlow13 <add-flow|del-flows> <target> <flow>` -- `ovs-ofctl` itself
    accepts either a local bridge name or a remote `tcp:HOST:PORT` target at
    that position, so the adapter already supported this with no C-code
    change, confirmed empirically (`daim_ovs_flow add tcp:<VM2>:6636 ...`
    installed a real flow on VM2's `s5` from VM1, and the matching `delete`
    removed it) before this function was changed to use it. An earlier
    revision of this function bypassed the adapter for the remote case and
    called `ovs-ofctl` directly, which technically worked but broke this
    paper's own claim that repair paths are installed "through the existing
    DAIM-OS OVS adapter" for the remote hops of a multi-OVS deployment; this
    version keeps that claim true for both hop types, and additionally gets
    the adapter's `valid_token()` length/newline validation on the remote
    target argument, which the direct-`ovs-ofctl` version did not have.

    A subprocess timeout is resolved, not simply treated as failure: an
    earlier revision returned False on any `TimeoutExpired`, but a timeout
    only means the client gave up waiting -- it is not proof the switch
    never received or applied the operation, unlike a confirmed non-zero
    exit, which means the adapter/`ovs-ofctl` itself reported failure
    before or without applying anything. On a timeout, this function reads
    back the switch's actual state (`_add_confirmed_by_readback()`/
    `_delete_confirmed_by_readback()`) to resolve the ambiguity: if the
    read-back confirms the intended state was actually reached, this
    returns True (however slowly); if it confirms the intended state was
    NOT reached, this returns False, the same as an ordinary failure. If
    the read-back itself cannot determine the answer, `_ForwardingCheckError`
    propagates out of this function rather than this function guessing
    True or False -- every caller in this file
    (`install_path()`/`withdraw_path()`/`_withdraw_stale_path()`/
    `_rollback_staged_flows()`) goes through `_apply_flow_or_fail_safe()`
    below, which treats that the same way they already treat a failed
    forwarding-consistency pre-check: fail safe, not proceed as if nothing
    happened."""
    log("flow_start", action=action, bridge=bridge, arg=arg)
    target = _openflow_target(bridge)
    argv = [str(CLI), action, target, arg]
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        log("flow_timeout", action=action, bridge=bridge, arg=arg)
        if action == "add":
            confirmed = _add_confirmed_by_readback(bridge, arg)
        else:
            confirmed = _delete_confirmed_by_readback(bridge, arg)
        log("flow_timeout_resolved_by_readback", action=action, bridge=bridge, arg=arg, confirmed=confirmed)
        return confirmed
    if r.returncode != 0:
        log("flow_error", action=action, bridge=bridge, arg=arg, stderr=r.stderr.strip())
        return False
    return True


def _apply_flow_or_fail_safe(action, bridge, arg):
    """apply_flow(), but an ambiguous outcome that even the read-back
    could not resolve (`_ForwardingCheckError`) is treated as failure
    rather than propagated as an exception. Every caller of `apply_flow()`
    in this file already tracks success/failure as a plain boolean and
    reacts to any kind of failure the same way (mark the whole call as
    failed, do not add the flow to `staged`/count it as cleaned up), so
    this collapses "confirmed failed" and "still cannot tell" into the
    single failure case those callers already handle, rather than making
    every one of them separately catch a new exception type."""
    try:
        return apply_flow(action, bridge, arg)
    except _ForwardingCheckError:
        return False


def install_path(path, old_path=None):
    """Returns (ok, staged): `ok` is True only if every flow-add call for
    `path` succeeded, and none of them would have overwritten a
    conflicting flow entry a different process already installed at the
    same priority/match (Section 5.1's forwarding-consistency check).
    `staged` is the list of (bridge, flow) pairs THIS call actually
    confirmed installed via a successful `apply_flow("add", ...)` call --
    not simply every flow `path_to_flows(path)` would produce. A flow
    rejected by the forwarding-consistency pre-check (a live conflict, or
    the pre-check itself failing) never reaches `apply_flow()` at all, so
    it is excluded from `staged`; a caller rolling back a failed attempt
    (`_rollback_staged_flows()`, below) needs this distinction, since
    "restore whatever this attempt might have touched" and "restore
    everything `path` would ever produce" are not the same operation --
    acting on a match this attempt never actually reached would overwrite
    whatever is legitimately there now, which could be exactly the foreign
    flow the forwarding-consistency check just correctly refused to
    overwrite in the first place.

    `old_path`, when given (`execute_repair()`'s ongoing-repair case, not
    `execute_startup_install()`'s initial-install case where there is no
    prior path), identifies which of `path`'s own flows share a match with
    a currently-active flow -- the boundary-hop collision documented in
    `test_boundary_hop_flow_match_collides_across_alternate_paths`, where
    `old_path` and `path` both use the exact same `(cookie, priority,
    in_port)` at the switch attached to SOURCE and the one attached to
    DEST, since the host-attachment port never changes across alternate
    routes. Those specific flows are staged LAST, after every
    non-colliding flow has succeeded: a colliding flow's `add-flow`
    immediately repoints that switch's LIVE forwarding action away from
    `old_path` (Section 4.6's confirmed replace-in-place semantics), so
    installing every other, purely-additive flow first -- which has zero
    effect on live traffic, since nothing upstream of them is forwarding
    through them yet -- means a mid-staging failure on any of THOSE flows
    never touches `old_path`'s live forwarding at all. Even a failure on
    one of the (at most two) final colliding flows leaves the other,
    already-successful one active with no inconsistency: the SOURCE-facing
    collision only affects the forward direction, the DEST-facing
    collision only the reverse, and each direction's own non-colliding
    hops were already fully staged beforehand -- so a direction whose
    boundary flow succeeded is immediately fully functional via the new
    path, while a direction whose boundary flow did not yet run (or
    failed) is untouched and still fully functional via the old path. This
    is what makes the protocol genuinely make-before-break, not merely
    eventually-consistent-via-rollback."""
    my_cookie = f"{_agent_cookie():x}"
    flows = path_to_flows(path)
    if old_path is not None:
        old_matches = {
            (bridge, _delete_match(flow)) for bridge, flow in path_to_flows(old_path)
        }
        non_colliding = [(b, f) for b, f in flows if (b, _delete_match(f)) not in old_matches]
        colliding = [(b, f) for b, f in flows if (b, _delete_match(f)) in old_matches]
        flows = non_colliding + colliding
    ok = True
    staged = []
    for bridge, flow in flows:
        try:
            existing_cookie = _conflicting_flow_cookie(bridge, flow)
        except _ForwardingCheckError:
            ok = False
            continue
        if existing_cookie is not None and existing_cookie != my_cookie:
            log("forwarding_conflict_rejected", bridge=bridge, flow=flow, existing_cookie=existing_cookie)
            ok = False
            continue
        if _apply_flow_or_fail_safe("add", bridge, flow):
            staged.append((bridge, flow))
        else:
            ok = False
    return ok, staged


def _delete_match(flow):
    """Builds the delete match for one previously-installed flow: a cookie
    mask scoped to this agent's own flows, plus the flow's `in_port`. The
    cookie value is parsed out of `flow` itself, the same string
    `path_to_flows()` embedded it into, rather than recomputed by calling
    `_agent_cookie()` a second time -- deleting exactly what was actually
    installed, not whatever the live `SOURCE`/`DEST` globals would produce
    now if they happened to change between install and withdraw. Deliberately
    NOT derived by stripping the `add`-form string down to whatever is left
    over -- an earlier revision did exactly that
    (`flow.split(",actions=")[0].split(",", 1)[1]`), which dropped BOTH the
    cookie and the priority field, leaving only a bare `in_port=N` match.
    That match is dangerously broad: OVS's `ovs-ofctl del-flows` without
    `--strict` deletes every flow whose fields are a superset of the given
    match, regardless of priority or any other field -- confirmed
    empirically against a live OVS bridge, a real unrelated flow sharing
    the same `in_port` (a different priority, an extra `dl_type` match, a
    different action) was silently deleted by a bare `in_port=N` delete
    call. `priority=` cannot fix this either: non-strict `del-flows`
    rejects it outright (`ovs-ofctl: unknown keyword priority`, confirmed
    empirically -- this is the actual reason an earlier revision stripped
    it, not merely a style choice). A cookie mask, by contrast, IS accepted
    by non-strict `del-flows` and correctly scopes the match: confirmed
    empirically that `cookie=<cookie>/-1,in_port=N` deletes only this
    agent's own flow and leaves an unrelated same-`in_port` flow with a
    different (or absent) cookie completely untouched. No change to the
    DAIM-OS OVS adapter (`daim_ovs_flow`) was needed for this -- cookie
    scoping works through the same unmodified `add`/`delete` CLI."""
    cookie = flow.split("cookie=0x")[1].split(",")[0]
    in_port = flow.split("in_port=")[1].split(",")[0]
    return f"cookie=0x{cookie}/-1,in_port={in_port}"


def withdraw_path(path):
    """Returns True only if every flow-delete call for `path` succeeded.
    `path=None` -- the agent's forwarding-state bookkeeping is unknown
    following a prior partial-failure repair, see `execute_repair()` -- is
    treated as nothing to withdraw and returns True vacuously: there is no
    known path to issue delete calls against, and guessing would risk
    deleting flows a different, unrelated repair installed."""
    if path is None:
        return True
    ok = True
    for bridge, flow in path_to_flows(path):
        if not _apply_flow_or_fail_safe("delete", bridge, _delete_match(flow)):
            ok = False
    return ok


def _withdraw_stale_path(old_path, new_path):
    """The commit half of execute_repair()'s two-phase protocol (Section
    5.2): withdraws `old_path`'s flows EXCEPT any whose (bridge, cookie,
    in_port) match is also occupied by `new_path`. Confirmed live against
    the multi-OVS testbed that a plain `withdraw_path(old_path)` here is
    unsafe: at the switch directly attached to SOURCE and the one directly
    attached to DEST, one flow's match is identical across every alternate
    path through that switch (the host-attachment port never changes) --
    see test_boundary_hop_flow_match_collides_across_alternate_paths.
    `install_path(new_path)` already updated that shared-match entry IN
    PLACE to the new path's action during staging (OVS `add-flow` at an
    identical match replaces the action, it does not create a second,
    coexisting entry); a plain `withdraw_path(old_path)` would then issue
    a delete for that same match as part of "cleaning up the old path" and
    remove the entry outright -- deleting the flow this repair JUST
    installed, not anything stale. Confirmed with a real fault injection
    against a real two-VM OVS testbed: the naive implementation left both
    boundary switches completely without a matching flow entry in one
    direction after an otherwise-`repair_installed`-reported repair, a
    live blackhole a purely bookkeeping-level test could not have caught."""
    new_matches = {
        (bridge, _delete_match(flow)) for bridge, flow in path_to_flows(new_path)
    }
    ok = True
    for bridge, flow in path_to_flows(old_path):
        match = _delete_match(flow)
        if (bridge, match) in new_matches:
            continue
        if not _apply_flow_or_fail_safe("delete", bridge, match):
            ok = False
    return ok


def _rollback_staged_flows(staged, old_path):
    """The rollback half of execute_repair()'s two-phase protocol (Section
    5.2), used when `install_path()` fails partway through: undoes exactly
    the flows that specific call's own `staged` return value says it
    actually confirmed installed -- not every flow the intended new path
    would ever produce. An earlier revision rolled back by recomputing
    `path_to_flows(new_path)` from scratch, which is wrong whenever a flow
    was rejected before ever reaching `apply_flow()` (Section 5.1's
    forwarding-consistency pre-check): that flow was never staged, so
    acting on it during rollback is acting on state this attempt never
    touched. Concretely, if a foreign-cookie flow already occupies a
    boundary-hop match (Section 5.1) and the pre-check correctly refuses
    to stage over it, a `staged`-blind rollback would still see that match
    "colliding with `old_path`" and RE-INSTALL `old_path`'s own action
    there via `add-flow` -- silently overwriting the very foreign flow the
    forwarding-consistency check just correctly protected. Iterating only
    `staged` excludes that match entirely, so rollback never touches it.

    For a staged flow whose match does not collide with `old_path` (or
    `old_path` is `None` -- no prior path to restore anything to, e.g.
    following a failed `execute_startup_install()`), deleting it is
    correct: it was purely additive, so there is nothing to restore. For a
    staged flow whose match DOES collide with `old_path` (see
    `_withdraw_stale_path()` above), staging already overwrote that
    entry's action in place; a plain delete there would remove the entry
    outright, leaving `old_path` down a working flow at exactly that hop
    rather than genuinely "left in place" as Section 5.2 requires.
    Instead, `old_path`'s own original flow is RE-INSTALLED at that match
    -- an `add-flow` call, replacing the action back to what it was before
    staging touched it, exactly mirroring how staging itself got there.
    Confirmed live (alongside `_withdraw_stale_path()`) that the naive
    delete-everything-attempted version of this rollback leaves the
    colliding entry missing (not restored), the same live blackhole its
    commit-side counterpart has. Returns True only if every rollback call
    (restore or delete) succeeded; the caller (`execute_repair()`) must
    not claim `old_path` is confirmed intact if this returns False."""
    old_by_match = {}
    if old_path is not None:
        old_by_match = {
            (bridge, _delete_match(flow)): (bridge, flow)
            for bridge, flow in path_to_flows(old_path)
        }
    ok = True
    for bridge, flow in staged:
        match = _delete_match(flow)
        collision = old_by_match.get((bridge, match))
        if collision is not None:
            restore_bridge, restore_flow = collision
            if not _apply_flow_or_fail_safe("add", restore_bridge, restore_flow):
                ok = False
        elif not _apply_flow_or_fail_safe("delete", bridge, match):
            ok = False
    return ok


def execute_repair(decision, current_path):
    """Executes the I/O for a "repair" decision using the two-phase
    prepare/commit protocol Section 5.2 specifies: the new path's flows are
    staged (installed) FIRST, in an order that defers the boundary-hop
    collision matches to last (`install_path(new_path, old_path=current_path)`,
    see its docstring), and the old path's flows are only withdrawn --
    "commit" -- once every new-path install call has actually succeeded.
    An earlier revision withdrew the old path unconditionally BEFORE
    attempting the new install, so a partial installation failure left the
    switches holding neither the old path nor the new one, and the only
    honest thing that revision could report was `current_path=None`
    ("forwarding state is not reliably known").

    On a staging failure, rollback (`_rollback_staged_flows()`) acts only
    on the flows `install_path()` itself confirmed were actually staged,
    not on the full intended new path -- a flow the forwarding-consistency
    check rejected before ever calling `apply_flow()` was never touched by
    this attempt, and rolling it back anyway could overwrite a foreign
    flow that check just correctly protected (see `install_path()` and
    `_rollback_staged_flows()` docstrings). Rollback's own result is not
    discarded either: an earlier revision of this function called the
    rollback helper for its side effect only and unconditionally reported
    `current_path` as the untouched, confirmed-intact old path -- but if
    rollback itself fails partway through (a delete or restore call times
    out), that claim is exactly the kind of false "logical state says X
    but the switches don't" gap this whole section exists to close.
    `current_path` becomes `None` and a distinct `repair_rollback_incomplete`
    event is reported whenever rollback itself did not fully succeed;
    `maybe_retry_repair()` then picks up the now-genuinely-unknown state on
    the next tick, exactly as it already does for any other `None`
    `current_path`. Only when rollback itself is fully confirmed does
    `current_path` stay at the old path, since only then is it genuinely
    known to be the switches' actual forwarding state again.

    If staging succeeds but the commit step (withdrawing the old path)
    then fails partway through, forwarding is still correct -- the new
    path is fully installed and is what traffic actually follows now -- so
    `current_path` still advances to the new path, but this is reported
    under the distinct `repair_installed_stale_withdraw` event rather than
    a clean `repair_installed`, since the old path's flows are left behind
    as uncleaned, stale leftovers rather than a forwarding problem.

    Returns (new_current_path, event_name, event_fields) for the caller to
    log."""
    new_path = decision["new_path"]
    repair_start_ns = time.perf_counter_ns()
    install_ok, staged = install_path(new_path, old_path=current_path)
    if not install_ok:
        rollback_ok = _rollback_staged_flows(staged, current_path)
        repair_end_ns = time.perf_counter_ns()
        if not rollback_ok:
            return None, "repair_rollback_incomplete", {
                "attempted_path": new_path, "prior_path": current_path,
                "install_ok": False, "rollback_ok": False,
                "repair_start_ns": repair_start_ns, "repair_end_ns": repair_end_ns,
            }
        return current_path, "repair_incomplete", {
            "attempted_path": new_path, "prior_path": current_path,
            "install_ok": False,
            "repair_start_ns": repair_start_ns, "repair_end_ns": repair_end_ns,
        }
    withdraw_ok = True if current_path is None else _withdraw_stale_path(current_path, new_path)
    repair_end_ns = time.perf_counter_ns()
    if withdraw_ok:
        return new_path, "repair_installed", {
            "path": new_path,
            "repair_start_ns": repair_start_ns, "repair_end_ns": repair_end_ns,
            "held_down_seconds": HOLD_DOWN_SECONDS,
        }
    return new_path, "repair_installed_stale_withdraw", {
        "path": new_path, "stale_path": current_path,
        "repair_start_ns": repair_start_ns, "repair_end_ns": repair_end_ns,
        "held_down_seconds": HOLD_DOWN_SECONDS,
    }


def execute_startup_install(current_path, down_edges):
    """Installs the initial path computed at agent startup, and reports the
    outcome without claiming success it did not achieve -- an earlier
    revision called `install_path(current_path)` for its side effect only,
    discarding whether the initial flow installation actually succeeded, so
    `main()` always logged `agent_started` with the intended path even if
    the underlying flow-mod calls partially failed. This is the same class
    of defect `execute_repair()` fixes for the ongoing repair path, found on
    the startup path by a second look at the same review.

    On failure, whatever flows DID get staged before the failure are rolled
    back (`_rollback_staged_flows(staged, None)` -- only the flows this
    specific attempt actually confirmed installed, see `install_path()`'s
    docstring, not every flow the intended path would ever produce) --
    there is no old path to preserve at startup the way `execute_repair()`'s
    two-phase protocol (Section 5.2) preserves one for an ongoing repair
    (`old_path=None`, so every staged flow is purely additive cleanup, no
    restore-on-collision logic applies), but the same
    don't-leave-partial-flows-behind principle applies, so this degenerate
    "no old path" case is cleaned up the same way rather than left as a mix
    of installed and missing flows. The returned current_path is `None`,
    exactly like `execute_repair()`'s failure case -- not a distinct
    "broken at startup" state -- so the agent does not silently start in a
    state it believes is healthy, and `maybe_retry_repair()`'s periodic
    retry (below) picks up the unfinished installation on the next tick,
    with no special-casing needed for "this failure happened during
    startup" versus "this failure happened during an ongoing repair".
    Returns (new_current_path, event_name, event_fields) for the caller to
    log."""
    install_ok, staged = install_path(current_path)
    if install_ok:
        return current_path, "agent_started", {
            "initial_path": current_path,
            "down_edges": [list(edge) for edge in down_edges],
        }
    _rollback_staged_flows(staged, None)
    return None, "startup_install_incomplete", {
        "attempted_path": current_path,
        "down_edges": [list(edge) for edge in down_edges],
    }


def _path_uses_down_edge(path, down_edges):
    """Whether `path` (a list of switch names, hop by hop) traverses any
    edge in `down_edges`. Used by maybe_retry_repair() below: since
    execute_repair()'s two-phase protocol (Section 5.2) now retains the
    OLD path on a failed repair instead of degrading to
    `current_path=None`, "is a retry still needed" can no longer be
    answered by checking `current_path is None` alone -- a retained old
    path may itself traverse the very edge that just went down and
    triggered the failed repair in the first place, which is exactly when
    a retry is still required."""
    return any(
        frozenset({path[i], path[i + 1]}) in down_edges
        for i in range(len(path) - 1)
    )


def maybe_retry_repair(current_path, down_edges):
    """If `current_path` is `None` (forwarding state genuinely unknown,
    e.g. following a failed `execute_startup_install()`, which has no old
    path to fall back to) or `current_path` is known but traverses an edge
    in `down_edges` (a repair attempt staged-then-rolled-back under
    Section 5.2's two-phase protocol, correctly retaining the old path --
    but that old path is exactly the one the just-failed repair was
    trying to replace, so it still does not avoid the fault) -- attempts a
    fresh repair, recomputing the BFS path against the current
    `down_edges` rather than reusing whichever attempt failed. Returns
    (new_current_path, event_name, event_fields) exactly like
    `execute_repair()`, or `(current_path, None, None)` if no retry is
    needed (`current_path` is known and avoids every down edge).

    This closes a real liveness gap found by review: `decide_link_event()`
    only starts a repair on a `state=="down"` event for an edge `not in
    down_edges` -- but a failed repair's edge is already in `down_edges` by
    the time `execute_repair()` runs (added when the "down" transition was
    first processed), so a *duplicate* transition on the same edge, or no
    further transition at all (the physical link stays down and nothing
    about it changes again), would never re-trigger a repair attempt
    through the event-driven path alone. A retained-but-still-faulty
    `current_path` becoming a permanent dead end -- rather than "not yet
    fixed" -- would mean a transient flow-installation failure (a remote
    OVS instance briefly unreachable, a timeout) could leave the agent
    silently stuck indefinitely, with no further action, even after the
    underlying problem clears. Called from `main()`'s periodic
    `poll_interval` tick (the same wake-up source
    `reconcile_expired_holddowns()` already uses), not from the OVSDB event
    branch, since this must keep trying with no event required to trigger
    it. If BFS finds no path at all avoiding `down_edges` (the edge is
    genuinely unreachable another way, not a flow-installation problem),
    this reports `repair_retry_no_path` rather than retrying pointlessly
    every tick. There is no bounded retry count or give-up threshold --
    retries continue at the existing tick interval for as long as
    `current_path` stays unknown or faulty, an explicit, disclosed
    limitation (a permanently unreachable remote OVS instance would retry
    forever) rather than a designed backoff/circuit-breaker policy."""
    if current_path is not None and not _path_uses_down_edge(current_path, down_edges):
        return current_path, None, None
    retry_path = bfs_path(SOURCE, DEST, down_edges)
    if retry_path is None:
        return current_path, "repair_retry_no_path", {
            "down_edges": [list(edge) for edge in down_edges],
        }
    if retry_path == current_path:
        # Cannot actually happen: BFS never returns a path that traverses
        # down_edges, so it can never equal a current_path this function
        # has just determined DOES traverse one. Kept as a defensive
        # no-op guard, matching decide_link_event()'s own no-op check,
        # rather than assumed unreachable.
        return current_path, None, None
    return execute_repair({"action": "repair", "new_path": retry_path}, current_path)


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


def resync_from_reconnect(snapshot, down_edges, interface_state):
    """Reconciles state after one OVSDB target's monitor connection
    reconnects (see `monitor_link_rows()`) and a fresh initial snapshot is
    available -- the runtime counterpart of the startup-state-sync fix
    (Section 4.4): the `ovsdb-client monitor` child for one endpoint can die
    while the agent's own process keeps running (a crash, a dropped OVSDB
    connection), and any link-state transition that happened while
    disconnected is invisible until this fresh snapshot is read. Mutates
    `interface_state`/`down_edges` in place, like
    `reconcile_expired_holddowns()` and `decide_link_event()` do, so all
    three share the same state-mutation contract.

    Only interfaces present in `snapshot`, that are also in
    `MONITORED_INTERFACES`, with a value of exactly `"up"` or `"down"`, are
    applied; anything else is skipped, not treated as implicitly up --
    matching `down_edges_from_snapshot()`'s startup validation, but does
    NOT raise: an unrecognised `link_state` from a live server after
    reconnect is a reason to skip that one interface's update, not to
    crash an already-running agent (unlike the startup case, where the
    process has not begun serving anything yet). Does not touch hold-down
    timers -- reconnect is about regaining observability, not about repair
    timing; any hold-down already in progress continues on its existing
    schedule unaffected.

    Returns the list of `(name, state)` pairs skipped for an unrecognised
    `link_state`, for the caller to log."""
    invalid = []
    for name, state in snapshot.items():
        if name not in MONITORED_INTERFACES:
            continue
        if state not in ("up", "down"):
            invalid.append((name, state))
            continue
        interface_state[name] = state
    for name in snapshot:
        if name not in MONITORED_INTERFACES:
            continue
        switch, neighbor = MONITORED_INTERFACES[name]
        edge = frozenset({switch, neighbor})
        if _edge_confirmed_up(edge, interface_state):
            down_edges.discard(edge)
        elif interface_state.get(name) == "down":
            down_edges.add(edge)
    return invalid


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
                       interface_state, current_path, now):
    """Pure decision function for one OVSDB link-state event on a monitored
    interface: IDLE/ACTIVE/HELD-DOWN state machine plus the existing BFS
    repair decision. Mutates down_edges/interface_state in place (the
    agent's state); does no I/O and calls no subprocess, so this is
    exercised directly by a synthetic event sequence and a fake clock in
    test_daim_link_agent.py without OVSDB, OVS, or Mininet. It reads
    `held_down_until` (to check suppression) but no longer writes to it: a
    "repair" decision does not itself start a hold-down window (see the
    "Timing precision" note below) -- `held_down_until` is mutated only by
    `reconcile_expired_holddowns()` (expiry) and by the caller (a new
    window, once a repair actually commits).

    Timing precision (Section 5.2/Section 4.7): an earlier revision set
    `held_down_until[edge]` inside this function, at the moment the
    "repair" decision was made -- before the caller performed any of the
    withdrawal/install I/O that decision triggers. Since that I/O takes on
    the order of 150-180 ms (Table 2), the hold-down window's clock started
    running for roughly that long before the repair it was meant to cover
    had actually completed, and if the flow-install call failed partway
    through, the window would already be running regardless of whether
    anything was actually installed. The window is now started by the
    caller instead, only once `execute_repair()` reports the new path was
    actually committed (`repair_installed` or `repair_installed_stale_withdraw`,
    Section 5.2) -- tying "hold-down started" to "repair complete" rather
    than to "repair decided", and giving a failed-and-rolled-back repair
    (which retains the old path untouched, Section 5.2) no window at all,
    since nothing about the forwarding state actually changed for that
    attempt.

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


RECONNECT_EVENT = "__monitor_reconnect__"


def monitor_link_rows(procs_by_target, poll_interval=None):
    """Yields (name, link_state) for every real 'new' row pushed by any of
    the given *already-started* monitor subprocesses (see _start_monitor/
    read_initial_snapshot, which must run first for each so the initial
    snapshot this generator does not handle is not lost), multiplexed via
    `select()` across however many there are. `procs_by_target` maps each
    OVSDB target (`None` for the local connection, matching every other
    target-keyed structure in this file) to its already-started Popen. A
    multi-OVS deployment opens one connection per distinct OVSDB endpoint
    its MONITORED_INTERFACES span (Section 8.3's connection-multiplexing
    requirement); the default single-endpoint configuration passes a dict
    of exactly one entry (`{None: proc}`), and this behaves identically to
    watching that one connection as before.

    If `poll_interval` is given, also yields `None` at least every
    `poll_interval` seconds when no OVSDB event has arrived on *any*
    connection. This is *not* failure-detection polling -- a real
    link-down/up event still arrives immediately via the push subscription
    it occurred on and is yielded the instant it is read. The `None` ticks
    exist only so main() can call reconcile_expired_holddowns() on a
    bounded schedule even when an interface's hold-down window expires with
    no further event ever seen for it (see reconcile_expired_holddowns'
    docstring for why that case needs a wake-up source at all).

    Reconnect: when a stream closes (EOF -- the `ovsdb-client monitor`
    child died: a crash, a dropped OVSDB connection, not a normal
    condition), an earlier revision just silently dropped that connection
    from the poll set and kept going, permanently blind to that target from
    then on -- a real reliability gap: the same problem the startup-state
    fix (Section 4.4) closed for a fresh agent process, left open for the
    rest of a long-running one. This now attempts exactly one immediate
    reconnect for that target: spawns a fresh monitor child
    (`_start_monitor(target)`) and reads its initial snapshot
    (`read_initial_snapshot()`). On success, yields `(RECONNECT_EVENT,
    target, snapshot)` so the caller can reconcile state
    (`resync_from_reconnect()`) and swaps the new child's stream into the
    active set so it keeps yielding future events normally. On failure (the
    respawn itself fails, or no initial snapshot arrives within
    `read_initial_snapshot`'s timeout), yields `(RECONNECT_EVENT, target,
    None)` and that target's connection is not retried again by this
    function -- an explicit, disclosed limitation: reconnect is attempted
    once per disconnection, not retried indefinitely the way
    `maybe_retry_repair()` retries a failed repair."""
    import select
    stream_targets = {p.stdout: t for t, p in procs_by_target.items()}
    streams = list(stream_targets)
    while streams:
        ready, _, _ = select.select(streams, [], [], poll_interval)
        if not ready:
            yield None
            continue
        for stream in ready:
            line = stream.readline()
            if not line:
                target = stream_targets.pop(stream)
                streams.remove(stream)
                try:
                    new_proc = _start_monitor(target)
                    snapshot = read_initial_snapshot(new_proc)
                except Exception:
                    yield (RECONNECT_EVENT, target, None)
                    continue
                stream_targets[new_proc.stdout] = target
                streams.append(new_proc.stdout)
                yield (RECONNECT_EVENT, target, snapshot)
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
    procs_by_target = {target: _start_monitor(target) for target in targets}
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
        for proc in procs_by_target.values():
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
        current_path, event_name, event_fields = execute_startup_install(current_path, down_edges)
        log(event_name, **event_fields)
    except BaseException:
        for proc in procs_by_target.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        raise

    held_down_until = {}

    # poll_interval drives reconcile_expired_holddowns() and
    # maybe_retry_repair() wake-ups (see each function's docstring); it is
    # well under HOLD_DOWN_SECONDS so
    # an expired window is reconciled promptly even with no further event.
    for event in monitor_link_rows(procs_by_target, poll_interval=HOLD_DOWN_SECONDS / 4):
        if event is None:
            recovered = reconcile_expired_holddowns(
                held_down_until, interface_state, down_edges, time.monotonic(),
            )
            for edge in recovered:
                log("link_up_detected", edge=list(edge), reconciled_after_holddown=True)
            current_path, event_name, event_fields = maybe_retry_repair(current_path, down_edges)
            if event_name is not None:
                log(event_name, **event_fields)
            continue

        if event[0] == RECONNECT_EVENT:
            _, target, snapshot = event
            if snapshot is None:
                log("monitor_reconnect_failed", target=target or "local")
                continue
            invalid = resync_from_reconnect(snapshot, down_edges, interface_state)
            for bad_name, bad_state in invalid:
                log("invalid_link_state", interface=bad_name, state=bad_state)
            log("monitor_reconnected", target=target or "local", interfaces=sorted(snapshot))
            new_path = bfs_path(SOURCE, DEST, down_edges)
            if new_path != current_path:
                if new_path is None:
                    log("repair_failed",
                        reason="no alternate path avoiding down edges (post-reconnect resync)")
                else:
                    current_path, event_name, event_fields = execute_repair(
                        {"action": "repair", "new_path": new_path}, current_path,
                    )
                    log(event_name, **event_fields)
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
            current_path, event_name, event_fields = execute_repair(decision, current_path)
            # Start the hold-down window only now, once the repair is
            # confirmed to have actually committed the new path (Section
            # 5.2/4.7's timing-precision fix) -- not at decision time,
            # before the withdrawal/install I/O above ran. A repair that
            # failed and rolled back (current_path still the old path,
            # decision["new_path"] not reached) gets no window at all,
            # since forwarding state did not actually change.
            if current_path == decision["new_path"]:
                held_down_until[frozenset(decision["edge"])] = time.monotonic() + HOLD_DOWN_SECONDS
            log(event_name, **event_fields)
        elif action == "recovered":
            log("link_up_detected", interface=name)
        elif action == "invalid_link_state":
            log("invalid_link_state", interface=name, state=decision["state"])
        # "noop"/"ignored": no state change, nothing to log beyond the event
        # itself being a repeat notification OVSDB is allowed to send.


if __name__ == "__main__":
    main()
