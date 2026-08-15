#!/usr/bin/env python3
"""Pure-logic unit tests for daim_link_agent, independent of Mininet/OVS/OVSDB.

Thirty-three things are checked without a live network:
1. `test_bfs_path_computation` -- the BFS-computed primary and alternate
   paths produce exactly the flow sets that were previously hand-written in
   stage3_link_recovery.py's install_primary()/install_alternate(), so the
   agent's graph search is not silently wrong before it is trusted to react
   to a real link failure.
2. `test_holddown_suppresses_flapping` -- feeds `decide_link_event` (the
   agent's pure per-event decision function) an identical flapping-link
   event sequence with a synthetic clock, once with the hold-down window
   enabled and once disabled, and asserts both install exactly one repair
   (this agent never reverts to the primary path on recovery, so repeated
   downs on the same already-excluded edge are no-ops either way) while the
   enabled run suppresses 4 of 6 subsequent transitions outright, cutting
   BFS/decision calls from 4 to 2. This is a logic-level proof over
   synthetic OVSDB-style events with a fake clock; the corresponding
   live-network measurement against a real Mininet/OVS link is in
   `stage3_holddown_flapping.py` (Section 6.6/7.3 of the manuscript).
3. `test_holddown_stale_state_is_reconciled` -- a stale-state regression
   test (Section 4.7).
4. `test_holddown_covers_both_interfaces_on_same_edge` -- a cross-interface
   regression test: hold-down suppresses transitions on either of an edge's
   two interfaces once either one has triggered a repair (Section 4.7).
5. `test_edge_recovery_requires_all_interfaces_confirmed_up` -- a
   last-report-wins regression test found by code review, not by testing:
   an edge must not be treated as recovered until *every* interface that
   observes it has independently reported `up`, not just whichever
   interface's `up` happened to be reported most recently (Section 4.7).
6. `test_edge_recovers_once_both_interfaces_confirm_up` -- the positive
   case of the same fix: once every interface has confirmed `up`, the edge
   is reconciled exactly once, not once per interface.
7. `test_parse_monitor_line_initial_vs_new` -- a startup/restart regression
   test found by code review and confirmed against a real ovsdb-server:
   OVSDB's monitor reply reports a table's current contents with
   `action=="initial"`, not `"new"`, so matching only `"new"` (an earlier
   revision's default) silently discarded the whole startup snapshot
   (Section 4.4).
8. `test_read_initial_snapshot_reflects_already_down_interface` -- the same
   fix one level up, using a real OS pipe to exercise the actual
   select()/readline() code path main() uses.
9. `test_startup_detects_already_down_edge` -- the other half of the same
   fix: an edge already down at startup must be excluded from the initial
   path computation, not silently assumed up.
10. `test_startup_rejects_unexpected_link_state` -- a robustness check found
    by review: an unrecognised `link_state` value must be rejected at
    startup, not silently treated as "up".
11. `test_decide_link_event_rejects_unexpected_link_state` -- the same
    check's runtime counterpart: an unrecognised `link_state` value in the
    ongoing event stream must be rejected outright, not fall through to
    "ignored".
12. `test_default_config_uses_single_local_ovsdb_target` -- an empty
    `REMOTE_ENDPOINTS` (the default) resolves to exactly one local monitor
    target, so the multi-OVS extension is inert unless opted into.
13. `test_multi_ovs_target_routing` -- a non-empty `REMOTE_ENDPOINTS` routes
    each interface to its own switch's registered remote endpoint, with
    connections deduplicated per distinct target.
14. `test_apply_flow_routes_remote_bridge_through_adapter` -- `apply_flow()`
    calls the DAIM-OS `daim_ovs_flow` adapter binary for both local and
    remote bridges, passing the remote OpenFlow target string in the same
    argument position a local bridge name would occupy, rather than
    bypassing the adapter for the remote case (Section 4.4/4.6).
15. `test_execute_repair_reports_partial_failure_honestly` -- `execute_repair()`
    (the I/O half of a "repair" decision) implements the two-phase
    prepare/commit protocol of Section 5.2: the new path is staged
    (installed) first, and the old path is only withdrawn once every
    new-path install call succeeds. A staging failure rolls back whatever
    of the new path got staged and retains the OLD path unchanged (not
    `None`), reported as `repair_incomplete`; a commit-step failure (old
    path withdrawal fails after successful staging) still advances
    `current_path` to the new path -- forwarding is correct -- but under
    the distinct `repair_installed_stale_withdraw` event; and a `None`
    prior path (no old path to preserve) does not crash a subsequent
    repair attempt.
16. `test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names` --
    `bfs_path()`/`path_to_flows()` resolve source/destination hosts from the
    declared `SOURCE`/`DEST`/`HOST_ATTACHMENT` globals rather than the
    literal strings `"h1"`/`"h2"` an earlier revision hardcoded -- both
    topologies measured in this evidence set happen to use those names, so
    this never surfaced live, but a deployment declaring different host
    names via `load_topology_config()` would have hit a `KeyError` in
    `path_to_flows()`.
17. `test_delete_match_scopes_by_cookie_not_bare_in_port` -- `withdraw_path()`
    deletes flows by a cookie mask plus `in_port`, not a bare
    `in_port` match, which non-strict `del-flows` would otherwise delete
    for ANY flow sharing that port regardless of owner -- confirmed
    empirically against a live OVS bridge with a real unrelated flow.
18. `test_execute_startup_install_reports_partial_failure` -- the same
    failure-honesty fix `execute_repair()` applies to the ongoing repair
    path, applied to agent startup: `execute_startup_install()` only uses
    the `agent_started` event name if the initial flow installation fully
    succeeded; a partial failure is reported as `startup_install_incomplete`
    with `current_path=None`, not a silent `agent_started`.
19. `test_maybe_retry_repair_retries_until_success` -- a `current_path`
    left retained-but-faulty by a staged-then-rolled-back repair (Section
    5.2) is not a permanent dead end: `maybe_retry_repair()`, called from
    `main()`'s periodic tick (not the OVSDB event branch), retries a
    failed repair on later ticks -- using `_path_uses_down_edge()` to
    detect that the retained path still traverses a down edge, since
    `current_path is None` is no longer the only "retry needed" signal --
    until the underlying flow-installation failure clears, with no new
    OVSDB event required to trigger it -- closing a real liveness gap,
    since `decide_link_event()` alone would never re-trigger a repair for
    an edge already in `down_edges`.
20. `test_agent_cookie_is_deterministic_and_pair_scoped` -- `_agent_cookie()`
    returns the same value across repeated calls for the same `(SOURCE,
    DEST)` pair (deterministic across a restart, so a restarted agent still
    recognises flows a prior instance of itself installed) and a different
    value for a different pair (so two agents protecting different pairs on
    a shared switch do not delete each other's flows) -- not a single fixed
    constant an earlier revision used, correct only for a single-agent
    deployment.
21. `test_resync_from_reconnect_updates_state_without_touching_holddown` --
    `resync_from_reconnect()` (Section 4.4's runtime counterpart to the
    startup-state-sync fix) updates `interface_state`/`down_edges` from a
    fresh post-reconnect snapshot, skips and reports an unrecognised
    `link_state` rather than applying or raising on it, and leaves
    `held_down_until` untouched -- reconnect is about regaining
    observability, not about repair timing.
22. `test_monitor_link_rows_reconnects_on_stream_death` -- when an
    `ovsdb-client monitor` child's stream closes (a crash, a dropped OVSDB
    connection), `monitor_link_rows()` attempts one immediate reconnect
    (respawn plus a fresh initial snapshot) instead of silently dropping
    that target from the poll set forever, resuming event delivery on
    success and yielding a distinct failure event, not crashing or
    hanging, when the reconnect attempt itself fails -- closing the
    monitor-subprocess reconnect gap (Section 10) using real OS pipes to
    exercise the actual `select()`/`readline()` code path, with
    `_start_monitor()` monkeypatched rather than spawning a real
    subprocess.
23. `test_conflicting_flow_cookie_parses_dump_flows_output` --
    `_conflicting_flow_cookie()` (Section 5.1's forwarding-consistency
    read-before-write query) returns the cookie of an existing flow at the
    exact `priority=100` match this agent is about to install over, `None`
    when that match is free or occupied only at a different priority
    (`ovs-ofctl dump-flows` rejects a literal `priority=` filter keyword
    outright, confirmed empirically, so the query filters on `in_port=`
    alone and the priority check happens against the returned lines), and
    raises `_ForwardingCheckError` -- not silently `None` -- when the query
    itself could not be completed.
24. `test_install_path_rejects_forwarding_conflict` -- `install_path()`
    refuses to call `apply_flow("add", ...)` for a flow whose exact match
    is already occupied by a different process's flow (a foreign cookie),
    logging `forwarding_conflict_rejected` and failing the whole call,
    while flows with no conflict on other switches still install;
    re-installing over this agent's own prior flow (same cookie) is not
    treated as a conflict; and a pre-check that could not be completed at
    all blocks the add fail-safe, the same as a real conflict.
25. `test_boundary_hop_flow_match_collides_across_alternate_paths` --
    documents the root cause behind items 26-27 below: the SOURCE-facing
    switch's and DEST-facing switch's flow match toward the host is
    identical across every alternate path through that switch (the
    host-attachment port never changes), so `install_path()` "staging"
    those two specific flow entries is an immediate, live OVS replace, not
    a non-disruptive side-by-side install the way an interior hop's
    staging is -- confirmed by comparing `_delete_match()` output across
    two alternate paths, not by a fresh live OVS call (Section 4.6's
    existing live verification already confirms `add-flow` at an
    identical match replaces the action in place).
26. `test_withdraw_stale_path_skips_boundary_hop_collisions` --
    `_withdraw_stale_path()`, `execute_repair()`'s commit step, must never
    delete a match `new_path`'s own staged flows also occupy -- found via a
    real live fault injection against the multi-OVS testbed: a naive
    `withdraw_path(old_path)` at commit time deleted the just-staged,
    live, wanted entry at both boundary switches (item 25), leaving an
    otherwise `repair_installed`-reported repair with a real blackhole,
    confirmed via `ovs-ofctl dump-flows` showing the colliding match
    completely empty afterward.
27. `test_rollback_staged_flows_restores_boundary_hop_collisions` --
    `_rollback_staged_flows()`, `execute_repair()`'s rollback step on a
    staging failure, must RESTORE `old_path`'s original action at a
    boundary-hop match (an add-flow call), not merely delete it -- the
    same live blackhole as item 26, on the rollback side instead of the
    commit side. A purely-additive staged flow with no `old_path`
    counterpart is still deleted, as ordinary cleanup.
28. `test_rollback_staged_flows_ignores_never_staged_matches` --
    `_rollback_staged_flows()` must act only on the flows a failed
    `install_path()` call actually confirmed staged (its `staged` return
    value), never on the full intended new path: a flow the
    forwarding-consistency pre-check rejected before ever calling
    `apply_flow()` was never touched by this attempt, and rolling it back
    anyway at a boundary-hop collision match would RE-INSTALL `old_path`'s
    action there -- silently overwriting the very foreign flow that check
    had just correctly refused to overwrite, defeating that check through
    its own supposed safety net.
29. `test_install_path_stages_boundary_collisions_last` --
    `install_path(new_path, old_path=...)` must stage every non-colliding,
    purely-additive flow FIRST, deferring the (at most two) boundary-hop
    colliding flows -- which immediately repoint a live switch's
    forwarding action away from `old_path` -- to the very end, so
    `old_path` stays fully live and correct for as long as possible during
    staging rather than being repointed onto a still-incomplete new path.
30. `test_apply_flow_resolves_ambiguous_timeout_via_readback` --
    `apply_flow()` must not treat a subprocess timeout as a confirmed
    failure the way a non-zero exit is: a timeout only means the client
    gave up waiting, not proof the switch never applied the operation. On
    a timeout, it reads back the switch's actual state directly and
    returns `True`/`False` only once that read-back confirms the real
    outcome, raising `_ForwardingCheckError` -- not guessing -- if even
    the read-back itself cannot determine the answer.
31. `test_apply_flow_or_fail_safe_treats_unresolved_ambiguity_as_failure` --
    `_apply_flow_or_fail_safe()`, what every caller of `apply_flow()` in
    this file actually uses, converts an unresolved ambiguous outcome into
    a plain `False` rather than letting `_ForwardingCheckError` propagate
    to callers that only handle a boolean result.
32. `test_decide_link_event_includes_bfs_timing` -- a `"repair"` or
    `"repair_failed"` decision must include `bfs_start_ns`/`bfs_end_ns`
    bracketing the real `bfs_path()` call (Section 10's
    service-restoration decomposition), using `time.perf_counter_ns()`,
    distinct from the synthetic `now` this function's own state machine
    uses for hold-down timing.
33. `test_execute_repair_includes_decomposed_timing` -- `execute_repair()`'s
    returned `event_fields` must include `stage_start_ns`/`stage_end_ns`
    bracketing `install_path()` in every outcome, `commit_start_ns`/
    `commit_end_ns` bracketing `_withdraw_stale_path()` only when a commit
    actually ran, and the decision's own `bfs_start_ns`/`bfs_end_ns`
    copied through unchanged when present (and simply omitted, not
    crashing, when absent) -- while `repair_start_ns`/`repair_end_ns` keep
    their pre-existing meaning, so no previously-reported timing number's
    definition changes."""
import json
import os

import daim_link_agent
from daim_link_agent import (
    MONITORED_INTERFACES,
    bfs_path,
    decide_link_event,
    down_edges_from_snapshot,
    execute_repair,
    execute_startup_install,
    maybe_retry_repair,
    path_to_flows,
    read_initial_snapshot,
    reconcile_expired_holddowns,
    resync_from_reconnect,
    withdraw_path,
    _agent_cookie,
    _delete_match,
    _monitored_ovsdb_targets,
    _ovsdb_target_for_interface,
    _owning_switch,
    _parse_monitor_line,
    SOURCE,
    DEST,
)

# The cookie is now derived from (SOURCE, DEST) rather than a fixed constant
# (Section 4.6) -- computed once here, at the default h1/h2 pair, rather than
# hardcoded, so these expected sets track _agent_cookie()'s actual behaviour
# instead of a value that would silently go stale if the derivation changes.
_C = f"{_agent_cookie():x}"

EXPECTED_PRIMARY = {
    ("s1", f"cookie=0x{_C},priority=100,in_port=1,actions=output:2"),
    ("s1", f"cookie=0x{_C},priority=100,in_port=2,actions=output:1"),
    ("s2", f"cookie=0x{_C},priority=100,in_port=1,actions=output:2"),
    ("s2", f"cookie=0x{_C},priority=100,in_port=2,actions=output:1"),
    ("s4", f"cookie=0x{_C},priority=100,in_port=1,actions=output:3"),
    ("s4", f"cookie=0x{_C},priority=100,in_port=3,actions=output:1"),
}

EXPECTED_ALTERNATE = {
    ("s1", f"cookie=0x{_C},priority=100,in_port=1,actions=output:3"),
    ("s1", f"cookie=0x{_C},priority=100,in_port=3,actions=output:1"),
    ("s3", f"cookie=0x{_C},priority=100,in_port=1,actions=output:2"),
    ("s3", f"cookie=0x{_C},priority=100,in_port=2,actions=output:1"),
    ("s4", f"cookie=0x{_C},priority=100,in_port=2,actions=output:3"),
    ("s4", f"cookie=0x{_C},priority=100,in_port=3,actions=output:2"),
}


def test_bfs_path_computation():
    primary = bfs_path(SOURCE, DEST, down_edges=set())
    assert primary == ["s1", "s2", "s4"], primary
    assert set(path_to_flows(primary)) == EXPECTED_PRIMARY

    alternate = bfs_path(SOURCE, DEST, down_edges={frozenset({"s1", "s2"})})
    assert alternate == ["s1", "s3", "s4"], alternate
    assert set(path_to_flows(alternate)) == EXPECTED_ALTERNATE

    unreachable = bfs_path(
        SOURCE, DEST,
        down_edges={frozenset({"s1", "s2"}), frozenset({"s1", "s3"})},
    )
    assert unreachable is None, unreachable

    print("daim_link_agent path-computation unit test: PASS")


# (timestamp, link_state) on the monitored s1-eth2/s2-eth1 interface: a flap
# that goes down/up five times inside a 2-second window, then one more down
# after the window would have elapsed.
FLAP_EVENTS = [
    (0.0, "down"),
    (0.1, "up"),
    (0.2, "down"),
    (0.5, "up"),
    (1.9, "down"),
    (2.5, "up"),
    (2.6, "down"),
]
INTERFACE = "s1-eth2"


EDGE = frozenset({"s1", "s2"})


def run_flap_sequence(hold_down_seconds, interface=INTERFACE):
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]
    actions = []
    for now, state in FLAP_EVENTS:
        decision = decide_link_event(
            interface, state, down_edges, held_down_until,
            interface_state, current_path, now,
        )
        actions.append(decision["action"])
        if decision["action"] == "repair":
            # decide_link_event() no longer starts the hold-down window
            # itself (Section 5.2/4.7's timing-precision fix) -- main()
            # does, only once execute_repair() confirms the new path was
            # actually committed. This simulates that commit inline, since
            # this test drives the pure decision logic without I/O.
            current_path = decision["new_path"]
            held_down_until[frozenset(decision["edge"])] = now + hold_down_seconds
    return actions


def test_holddown_suppresses_flapping():
    assert INTERFACE in MONITORED_INTERFACES

    with_holddown = run_flap_sequence(hold_down_seconds=2.0)
    without_holddown = run_flap_sequence(hold_down_seconds=0.0)

    print(f"hold-down enabled  (window=2.0s): {with_holddown}")
    print(f"hold-down disabled (window=0.0s): {without_holddown}")

    # Both sequences end with exactly one actual flow repair: the agent
    # never reverts to the primary path on recovery (Section IV-B/design),
    # so a repeated down on the *same* already-excluded edge has BFS return
    # the alternate path it already installed, which is a no-op rather than
    # a second repair, with or without hold-down. Constructing a scenario
    # that reinstalls flows repeatedly would require adding revert-to-primary
    # behaviour the agent does not have; that is not this test's claim.
    assert with_holddown.count("repair") == 1, with_holddown
    assert without_holddown.count("repair") == 1, without_holddown

    # What hold-down actually changes: without it, every one of the 7
    # transitions is fully processed (state mutated, BFS re-run, a decision
    # logged) -- 4 of those 7 calls re-run BFS. With hold-down, 4 of the 6
    # transitions after the first are suppressed before any state mutation,
    # BFS call, or log line beyond "transition_suppressed" -- only 2 BFS
    # calls happen in total for the identical input.
    assert without_holddown == [
        "repair", "recovered", "noop", "recovered", "noop", "recovered", "noop",
    ], without_holddown
    assert with_holddown == [
        "repair", "suppressed", "suppressed", "suppressed",
        "suppressed", "recovered", "noop",
    ], with_holddown

    suppressed_with = with_holddown.count("suppressed")
    bfs_calls_without = without_holddown.count("repair") + without_holddown.count("noop")
    bfs_calls_with = with_holddown.count("repair") + with_holddown.count("noop")
    assert suppressed_with == 4
    assert bfs_calls_without == 4
    assert bfs_calls_with == 2

    print(
        "daim_link_agent hold-down flapping-link unit test: PASS -- for the "
        "identical 7-transition flap, hold-down suppresses 4 of 6 subsequent "
        f"transitions outright ({bfs_calls_with} BFS/decision calls total "
        f"vs. {bfs_calls_without} without hold-down); flow-table repairs "
        "are 1 either way because this agent does not revert to the "
        "primary path on recovery."
    )


def test_holddown_stale_state_is_reconciled():
    """Regression test for a real correctness bug flagged in editorial
    review: a down->up sequence where the `up` lands *inside* the hold-down
    window and no further event ever arrives for that interface. Before the
    fix, the suppressed `up` was simply dropped -- the edge stayed marked
    down forever, because nothing ever triggered re-evaluation. This test
    drives exactly that silent case (no third event) and confirms
    reconcile_expired_holddowns(), called once the window has passed,
    corrects the state without needing a new OVSDB event."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]

    # t=0.0: down -> repair; the caller (main(), simulated here) starts the
    # hold-down window only once the repair commits, open until t=2.0.
    d1 = decide_link_event(
        INTERFACE, "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    current_path = d1["new_path"]
    held_down_until[EDGE] = 0.0 + daim_link_agent.HOLD_DOWN_SECONDS
    assert down_edges == {EDGE}, down_edges

    # t=0.1: up, suppressed -- this is the transition that used to be lost.
    d2 = decide_link_event(
        INTERFACE, "up", down_edges, held_down_until,
        interface_state, current_path, 0.1,
    )
    assert d2["action"] == "suppressed", d2
    # Bug behaviour (pre-fix): down_edges still == {EDGE} forever from here,
    # since no further event was going to arrive to correct it.
    assert down_edges == {EDGE}, "still down during the window, as expected"

    # No further OVSDB event ever arrives for this interface. In the live
    # agent, main()'s poll_interval tick is what calls this after t=2.0;
    # here we call it directly to prove the reconciliation logic itself.
    recovered = reconcile_expired_holddowns(
        held_down_until, interface_state, down_edges, 2.1,
    )
    assert recovered == [EDGE], recovered
    assert down_edges == set(), (
        "stale-state bug: edge still marked down after its hold-down "
        f"window expired despite the suppressed transition being 'up': {down_edges}"
    )
    assert EDGE not in held_down_until
    assert interface_state[INTERFACE] == "up"

    print("daim_link_agent hold-down stale-state regression test: PASS -- "
          "a down->up flap fully absorbed inside the hold-down window, with "
          "no further event ever arriving, is still correctly reconciled "
          "back to 'up' once the window expires.")


def test_holddown_covers_both_interfaces_on_same_edge():
    """Regression test for a real defect found via a live-network run of the
    flapping-link protocol in a Mininet/OVS VM (Section 6.6/7.3 of the
    manuscript): OVSDB reports link_state transitions for the two
    interfaces on a physical link (s1-eth2 and s2-eth1, both mapping to the
    same edge {s1,s2}) independently, and the live run showed the *other*
    interface's transitions sailing through completely unsuppressed while
    the interface that happened to trigger the repair was correctly held
    down -- because the original implementation keyed hold-down state by
    interface name, not by edge. This test reproduces that exact pattern
    with synthetic events on both interface names and confirms the
    edge-keyed fix suppresses transitions on *either* interface once one of
    them has triggered a repair."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]

    # s2-eth1 reports down first (as it did in the live run) -> repair; the
    # caller (main(), simulated here) starts the hold-down window once the
    # repair commits.
    d1 = decide_link_event(
        "s2-eth1", "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    current_path = d1["new_path"]
    held_down_until[EDGE] = 0.0 + daim_link_agent.HOLD_DOWN_SECONDS

    # s1-eth2 -- the *other* interface for the same physical link -- then
    # also reports down. Pre-fix, this interface had no hold-down entry of
    # its own and would have been processed normally (a "noop", since BFS
    # returns the same already-installed alternate path); post-fix, it must
    # be suppressed because the *edge* is held down, regardless of which
    # interface name is reporting.
    d2 = decide_link_event(
        "s1-eth2", "down", down_edges, held_down_until,
        interface_state, current_path, 0.05,
    )
    assert d2["action"] == "suppressed", (
        f"cross-interface hold-down gap: s1-eth2's transition was not "
        f"suppressed even though s2-eth1 (the same physical link) is held "
        f"down: {d2}"
    )

    # s1-eth2 reporting up shortly after must also be suppressed, not
    # treated as an independent "recovered" event the way the live run
    # showed pre-fix.
    d3 = decide_link_event(
        "s1-eth2", "up", down_edges, held_down_until,
        interface_state, current_path, 0.1,
    )
    assert d3["action"] == "suppressed", (
        f"cross-interface hold-down gap: s1-eth2's recovery fired "
        f"independently of s2-eth1's hold-down window: {d3}"
    )

    print("daim_link_agent cross-interface hold-down regression test: PASS "
          "-- transitions on s1-eth2 are suppressed by a hold-down window "
          "s2-eth1's repair opened, because both are keyed by the same "
          "edge, matching the fix for the gap the live-network run found.")


def test_edge_recovery_requires_all_interfaces_confirmed_up():
    """Regression test for a defect found by code review, not by testing:
    an earlier revision recorded a single last-observed-state value per
    *edge*, overwritten by whichever interface reported most recently. That
    meant one interface's `up` could recover an edge even while the other
    interface's independently-reported state was still `down` -- a real gap
    given the two interfaces genuinely report independently (the same fact
    that motivated the cross-interface hold-down fix above). This drives
    exactly that pattern -- s2-eth1 down, s1-eth2 down, s1-eth2 up, then
    silence from s2-eth1 -- and confirms the edge is NOT reconciled as
    recovered once the window expires, because s2-eth1's last known state is
    still `down`."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]

    d1 = decide_link_event(
        "s2-eth1", "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    current_path = d1["new_path"]
    held_down_until[EDGE] = 0.0 + daim_link_agent.HOLD_DOWN_SECONDS

    d2 = decide_link_event(
        "s1-eth2", "down", down_edges, held_down_until,
        interface_state, current_path, 0.05,
    )
    assert d2["action"] == "suppressed", d2

    d3 = decide_link_event(
        "s1-eth2", "up", down_edges, held_down_until,
        interface_state, current_path, 0.1,
    )
    assert d3["action"] == "suppressed", d3

    # No further event ever arrives for s2-eth1 -- it is still, as far as
    # the agent knows, reporting "down".
    recovered = reconcile_expired_holddowns(
        held_down_until, interface_state, down_edges, 2.1,
    )
    assert recovered == [], (
        f"last-report-wins bug: edge was reconciled as recovered ({recovered}) "
        f"even though s2-eth1 never confirmed 'up'"
    )
    assert down_edges == {EDGE}, (
        f"edge should still be down -- only s1-eth2 confirmed up, not "
        f"s2-eth1: {down_edges}"
    )

    print("daim_link_agent edge-confirmation regression test (partial "
          "recovery): PASS -- an edge with one interface confirmed up and "
          "the other never reporting stays down at hold-down expiry.")


def test_edge_recovers_once_both_interfaces_confirm_up():
    """Positive case of the same fix: once every interface observing an
    edge has independently reported `up`, the edge is reconciled as
    recovered exactly once at expiry, not once per interface and not
    dropped because of which interface reported last."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]

    d1 = decide_link_event(
        "s2-eth1", "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    current_path = d1["new_path"]
    held_down_until[EDGE] = 0.0 + daim_link_agent.HOLD_DOWN_SECONDS

    decide_link_event("s1-eth2", "down", down_edges, held_down_until,
                       interface_state, current_path, 0.05)
    decide_link_event("s1-eth2", "up", down_edges, held_down_until,
                       interface_state, current_path, 0.1)
    d4 = decide_link_event("s2-eth1", "up", down_edges, held_down_until,
                            interface_state, current_path, 0.15)
    assert d4["action"] == "suppressed", d4

    recovered = reconcile_expired_holddowns(
        held_down_until, interface_state, down_edges, 2.1,
    )
    assert recovered == [EDGE], recovered
    assert down_edges == set(), down_edges

    print("daim_link_agent edge-confirmation regression test (full "
          "recovery): PASS -- once both s1-eth2 and s2-eth1 confirm 'up', "
          "the edge is reconciled as recovered exactly once.")


def test_parse_monitor_line_initial_vs_new():
    """Regression test for a real startup/restart correctness bug found by
    code review and confirmed against a live ovsdb-server (not assumed from
    documentation alone): OVSDB's monitor reply reports the table's current
    contents as its first line, with every row's `action` field set to
    `"initial"`, not `"new"` -- `"new"` is what a *later* transition uses.
    `_parse_monitor_line`'s old, undocumented default of matching only
    `"new"` silently discarded the entire initial snapshot. This confirms
    the `actions` parameter correctly distinguishes the two: the default
    (`actions=("new",)`) still ignores an initial-snapshot line exactly as
    before (real transitions are unaffected by this fix), while
    `actions=("initial",)` -- what `read_initial_snapshot` uses -- correctly
    extracts it."""
    initial_line = json.dumps({
        "headings": ["row", "action", "name", "link_state"],
        "data": [
            ["r1", "initial", "s1-eth2", "down"],
            ["r2", "initial", "s2-eth1", "up"],
        ],
    })
    update_line = json.dumps({
        "headings": ["row", "action", "name", "link_state"],
        "data": [
            ["r1", "old", None, "down"],
            ["", "new", "s1-eth2", "up"],
        ],
    })
    assert _parse_monitor_line(initial_line) == [], (
        "default actions=('new',) must still ignore action=='initial' rows, "
        "matching real transition behaviour"
    )
    assert set(_parse_monitor_line(initial_line, actions=("initial",))) == {
        ("s1-eth2", "down"), ("s2-eth1", "up"),
    }, "actions=('initial',) must extract every initial-snapshot row"
    assert _parse_monitor_line(update_line) == [("s1-eth2", "up")], (
        "a real transition's 'new' row must still be extracted correctly"
    )

    print("daim_link_agent _parse_monitor_line initial-vs-new regression "
          "test: PASS -- action=='initial' rows (the real startup snapshot "
          "format) and action=='new' rows (real transitions) are correctly "
          "distinguished.")


def test_read_initial_snapshot_reflects_already_down_interface():
    """Regression test for the startup bug itself, one level up from the
    parser: read_initial_snapshot() must return a monitored interface's
    already-`down` state from the very first line ovsdb-client monitor
    sends, using a real OS pipe (not a mock) so this exercises the same
    select()/readline() code path main() uses against a real subprocess."""
    read_fd, write_fd = os.pipe()
    reader, writer = os.fdopen(read_fd, "r"), os.fdopen(write_fd, "w")
    try:
        class FakeProc:
            pass
        proc = FakeProc()
        proc.stdout = reader

        writer.write(json.dumps({
            "headings": ["row", "action", "name", "link_state"],
            "data": [
                ["r1", "initial", "s1-eth2", "down"],
                ["r2", "initial", "s2-eth1", "down"],
                ["r3", "initial", "s3-eth1", "up"],
            ],
        }) + "\n")
        writer.flush()

        snapshot = read_initial_snapshot(proc, timeout=2.0)
        assert snapshot == {"s1-eth2": "down", "s2-eth1": "down"}, (
            f"expected only monitored interfaces, correctly reflecting the "
            f"already-down state a pre-fix agent would have silently missed: "
            f"{snapshot}"
        )
    finally:
        writer.close()
        reader.close()

    print("daim_link_agent read_initial_snapshot regression test: PASS -- "
          "an interface already down at startup is correctly captured from "
          "the initial OVSDB snapshot, filtered to monitored interfaces "
          "only (s3-eth1 excluded).")


def test_startup_detects_already_down_edge():
    """The other half of the startup fix: down_edges_from_snapshot() (the
    exact function main() calls) must turn an already-down monitored
    interface into a down edge that BFS then routes around for the initial
    path -- not the empty-set assumption a pre-fix agent made regardless of
    real starting conditions."""
    snapshot = {"s1-eth2": "down", "s2-eth1": "down"}
    down_edges = down_edges_from_snapshot(snapshot)
    assert down_edges == {EDGE}, down_edges

    initial_path = bfs_path(SOURCE, DEST, down_edges)
    assert initial_path == ["s1", "s3", "s4"], (
        f"agent must install the alternate path from startup when the "
        f"primary-path edge is already down, not the primary path a "
        f"pre-fix agent would have installed regardless: {initial_path}"
    )

    print("daim_link_agent startup-state regression test: PASS -- an edge "
          "already down at startup is correctly excluded from the initial "
          "path computation, instead of being silently assumed up.")


def test_startup_rejects_unexpected_link_state():
    """Robustness regression test: down_edges_from_snapshot() must not
    silently treat an unrecognised link_state value (OVS documents
    Interface.link_state as optional, so an empty string is possible for a
    non-applicable port) as equivalent to "up". Found by review, not by any
    live failure -- this scenario has not been observed against real OVS in
    this environment, but the fix is cheap and the alternative (silently
    assuming a link is fine) is exactly the class of bug the startup fix
    itself exists to close."""
    for bad_state in ("", "unknown", "up "):
        try:
            down_edges_from_snapshot({"s1-eth2": bad_state, "s2-eth1": "up"})
        except RuntimeError:
            continue
        raise AssertionError(
            f"down_edges_from_snapshot silently accepted link_state={bad_state!r} "
            f"instead of raising"
        )

    print("daim_link_agent unexpected-link_state regression test: PASS -- "
          "down_edges_from_snapshot rejects any link_state that isn't "
          "exactly 'up' or 'down' instead of silently treating it as up.")


def test_decide_link_event_rejects_unexpected_link_state():
    """Runtime counterpart of the startup-snapshot validation above:
    decide_link_event() must reject an unrecognised link_state value
    outright, before touching down_edges/interface_state/held_down_until,
    rather than falling through to the generic "ignored" action the way an
    earlier revision did. Found by review: only the startup snapshot was
    validated, not the ongoing event stream, which is the same
    silently-assume-fine gap in a different code path."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]

    d = decide_link_event(
        INTERFACE, "unknown", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d["action"] == "invalid_link_state", d
    assert down_edges == set(), "must not mutate down_edges on an invalid state"
    assert interface_state == {}, "must not record an invalid state as observed"

    print("daim_link_agent runtime invalid-link_state regression test: "
          "PASS -- decide_link_event rejects an unrecognised state instead "
          "of silently treating it as 'ignored'.")


def test_default_config_uses_single_local_ovsdb_target():
    """The default configuration (empty REMOTE_ENDPOINTS, the single-host
    diamond every other experiment in this evidence set measures) must
    resolve to exactly one local monitor target -- confirms the multi-OVS
    connection-multiplexing machinery is fully inert unless a deployment
    opts into it, so every existing experiment's behaviour is unaffected by
    this capability existing."""
    assert daim_link_agent.REMOTE_ENDPOINTS == {}
    assert _monitored_ovsdb_targets() == [None]
    for name in MONITORED_INTERFACES:
        assert _ovsdb_target_for_interface(name) is None, name

    print("daim_link_agent default-config multi-OVS inertness regression "
          "test: PASS -- an empty REMOTE_ENDPOINTS resolves to exactly one "
          "local monitor target, matching every single-host experiment.")


def test_multi_ovs_target_routing():
    """With a non-empty REMOTE_ENDPOINTS (as the live multi-OVS deployment
    in Section 7.7 uses), _monitored_ovsdb_targets() must return one entry
    per distinct OVSDB endpoint actually referenced by MONITORED_INTERFACES,
    and _ovsdb_target_for_interface() must route each interface to its own
    switch's endpoint -- the per-hop routing logic Section 8.3 requires,
    exercised here without needing a live multi-VM testbed."""
    saved_endpoints = dict(daim_link_agent.REMOTE_ENDPOINTS)
    saved_monitored = dict(daim_link_agent.MONITORED_INTERFACES)
    try:
        daim_link_agent.REMOTE_ENDPOINTS = {
            "s3": {"ovsdb": "tcp:192.0.2.2:6640", "openflow": "tcp:192.0.2.2:6634"},
            "s4": {"ovsdb": "tcp:192.0.2.2:6640", "openflow": "tcp:192.0.2.2:6635"},
        }
        daim_link_agent.MONITORED_INTERFACES = {
            "s3-eth1": ("s3", "s4"),
            "s4-eth1": ("s3", "s4"),
        }
        assert _owning_switch("s3-eth1") == "s3"
        assert _ovsdb_target_for_interface("s3-eth1") == "tcp:192.0.2.2:6640"
        assert _ovsdb_target_for_interface("s4-eth1") == "tcp:192.0.2.2:6640"
        # Both interfaces share the same remote OVSDB endpoint here, so only
        # one connection is needed to observe both -- not one per interface.
        assert _monitored_ovsdb_targets() == ["tcp:192.0.2.2:6640"]

        daim_link_agent.MONITORED_INTERFACES = {
            "s1-eth2": ("s1", "s2"),
            "s2-eth1": ("s1", "s2"),
            "s3-eth1": ("s3", "s4"),
            "s4-eth1": ("s3", "s4"),
        }
        assert _monitored_ovsdb_targets() == [None, "tcp:192.0.2.2:6640"], (
            "a deployment spanning both a local edge and a remote edge must "
            "open exactly two connections: one local, one remote"
        )
    finally:
        daim_link_agent.REMOTE_ENDPOINTS = saved_endpoints
        daim_link_agent.MONITORED_INTERFACES = saved_monitored

    print("daim_link_agent multi-OVS target-routing regression test: PASS "
          "-- interfaces route to their own switch's registered remote "
          "endpoint, and connections are deduplicated per distinct target.")


def test_apply_flow_routes_remote_bridge_through_adapter():
    """apply_flow() must call the DAIM-OS `daim_ovs_flow` adapter binary for
    BOTH local and remote bridges -- an earlier revision bypassed the
    adapter for the remote case and called `ovs-ofctl` directly, which
    worked (confirmed live: a real flow was added to and deleted from VM2's
    `s5` from VM1 through the unmodified adapter binary before this fix, so
    the adapter already forwards its target argument opaquely), but broke
    the manuscript's own claim that repair paths go through the existing
    DAIM-OS OVS adapter for a multi-OVS deployment's remote hops. Captures
    the actual subprocess.run() argv instead of letting anything run, so
    this needs no adapter binary or live OVS instance to check."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class Result:
            returncode = 0
            stderr = ""
        return Result()

    saved_endpoints = dict(daim_link_agent.REMOTE_ENDPOINTS)
    saved_run = daim_link_agent.subprocess.run
    try:
        daim_link_agent.subprocess.run = fake_run
        daim_link_agent.REMOTE_ENDPOINTS = {
            "s3": {"ovsdb": "tcp:192.0.2.2:6640", "openflow": "tcp:192.0.2.2:6634"},
        }
        assert daim_link_agent.apply_flow("add", "s1", "priority=100,in_port=1,actions=output:2")
        assert calls[-1] == [str(daim_link_agent.CLI), "add", "s1", "priority=100,in_port=1,actions=output:2"], (
            "a local bridge must still go through the adapter with the bridge name unchanged"
        )
        assert daim_link_agent.apply_flow("add", "s3", "priority=100,in_port=1,actions=output:2")
        assert calls[-1] == [str(daim_link_agent.CLI), "add", "tcp:192.0.2.2:6634", "priority=100,in_port=1,actions=output:2"], (
            "a remote bridge must go through the SAME adapter binary, with the remote "
            "OpenFlow target substituted for the bridge name -- not a direct ovs-ofctl call"
        )
    finally:
        daim_link_agent.subprocess.run = saved_run
        daim_link_agent.REMOTE_ENDPOINTS = saved_endpoints

    print("daim_link_agent apply_flow adapter-unification regression test: PASS "
          "-- local and remote bridges both go through the daim_ovs_flow adapter binary, "
          "just with a different target argument.")


def test_apply_flow_resolves_ambiguous_timeout_via_readback():
    """apply_flow() must not treat a subprocess timeout as a confirmed
    failure the way a non-zero exit is -- a timeout only means the client
    gave up waiting, not proof the switch never received or applied the
    operation. Found by review: an earlier revision returned False on any
    `TimeoutExpired`, so `install_path()`'s `staged` list (Section 5.2)
    could wrongly exclude a flow that the switch actually did apply just
    slowly, and a subsequent rollback would then never touch it, believing
    it was "never staged" when it may actually still be sitting there
    with the new path's action. Fixed: on a timeout, apply_flow() reads
    back the switch's actual state directly (a real `ovs-ofctl dump-flows`
    query, outside the daim_ovs_flow adapter, a read not an install) and
    resolves the ambiguity precisely: True only if the intended add/delete
    outcome is confirmed to have actually happened, False if confirmed not
    to have, and `_ForwardingCheckError` propagates -- not a silent guess
    either way -- if even the read-back itself cannot determine the
    answer. Distinguishes the two subprocess.run() calls (the
    daim_ovs_flow adapter binary vs. the direct ovs-ofctl read-back) by
    argv[0]."""
    saved_run = daim_link_agent.subprocess.run
    flow = f"cookie=0x{_C},priority=100,in_port=1,actions=output:2"
    delete_match = f"cookie=0x{_C}/-1,in_port=1"

    def present_result():
        class Result:
            returncode = 0
            stdout = (
                "OFPST_FLOW reply (OF1.3) (xid=0x2):\n"
                f" cookie=0x{_C}, duration=1s, table=0, n_packets=0, "
                "n_bytes=0, priority=100,in_port=1 actions=output:2\n"
            )
            stderr = ""
        return Result()

    def absent_result():
        class Result:
            returncode = 0
            stdout = "OFPST_FLOW reply (OF1.3) (xid=0x2):\n"
            stderr = ""
        return Result()

    def make_timeout_then(readback_result):
        def run(argv, **kwargs):
            if argv[0] == str(daim_link_agent.CLI):
                raise daim_link_agent.subprocess.TimeoutExpired(cmd=argv, timeout=5)
            return readback_result()
        return run

    try:
        # ADD times out, read-back CONFIRMS the flow is actually present:
        # the add DID take effect, just slowly -- must return True.
        daim_link_agent.subprocess.run = make_timeout_then(present_result)
        assert daim_link_agent.apply_flow("add", "s1", flow) is True

        # ADD times out, read-back CONFIRMS the flow is absent: the add
        # did NOT take effect -- must return False.
        daim_link_agent.subprocess.run = make_timeout_then(absent_result)
        assert daim_link_agent.apply_flow("add", "s1", flow) is False

        # ADD times out, and the read-back itself ALSO fails: genuinely
        # cannot tell -- must raise, not silently guess True or False.
        def always_timeout(argv, **kwargs):
            raise daim_link_agent.subprocess.TimeoutExpired(cmd=argv, timeout=5)
        daim_link_agent.subprocess.run = always_timeout
        try:
            daim_link_agent.apply_flow("add", "s1", flow)
            assert False, "expected _ForwardingCheckError"
        except daim_link_agent._ForwardingCheckError:
            pass

        # DELETE times out, read-back CONFIRMS the flow is gone: the
        # delete DID take effect -- must return True.
        daim_link_agent.subprocess.run = make_timeout_then(absent_result)
        assert daim_link_agent.apply_flow("delete", "s1", delete_match) is True

        # DELETE times out, read-back shows the flow is STILL present: the
        # delete did NOT take effect -- must return False.
        daim_link_agent.subprocess.run = make_timeout_then(present_result)
        assert daim_link_agent.apply_flow("delete", "s1", delete_match) is False
    finally:
        daim_link_agent.subprocess.run = saved_run

    print("daim_link_agent ambiguous-timeout read-back regression test: PASS "
          "-- a timed-out add/delete call is resolved via a real read-back "
          "query instead of being treated as a confirmed failure, returning "
          "True/False only when the switch's actual state confirms the "
          "answer, and raising rather than guessing when even the "
          "read-back cannot tell.")


def test_apply_flow_or_fail_safe_treats_unresolved_ambiguity_as_failure():
    """_apply_flow_or_fail_safe() -- what install_path()/withdraw_path()/
    _withdraw_stale_path()/_rollback_staged_flows() actually call instead
    of apply_flow() directly -- must convert an unresolved ambiguous
    outcome (_ForwardingCheckError, raised when even the read-back could
    not determine the switch's true state) into a plain `False`, not let
    the exception propagate to callers that only know how to handle a
    boolean success/failure result."""
    saved_run = daim_link_agent.subprocess.run
    try:
        def always_timeout(argv, **kwargs):
            raise daim_link_agent.subprocess.TimeoutExpired(cmd=argv, timeout=5)
        daim_link_agent.subprocess.run = always_timeout
        result = daim_link_agent._apply_flow_or_fail_safe(
            "add", "s1", f"cookie=0x{_C},priority=100,in_port=1,actions=output:2"
        )
        assert result is False
    finally:
        daim_link_agent.subprocess.run = saved_run

    print("daim_link_agent _apply_flow_or_fail_safe regression test: PASS -- "
          "an unresolved ambiguous outcome is treated as a plain failure, "
          "not an uncaught exception.")


def test_execute_repair_reports_partial_failure_honestly():
    """execute_repair() implements the two-phase prepare/commit protocol
    Section 5.2 specifies: the new path is staged (installed) FIRST, and
    the old path is only withdrawn -- committed -- once every new-path
    install call has actually succeeded. An earlier revision withdrew the
    old path unconditionally BEFORE attempting the new install, so a
    partial installation failure left the switches holding neither path,
    and `current_path` could only honestly become `None`.

    On a staging failure, `current_path` must be the OLD path, retained
    unchanged, not `None` -- staging never touches the old path, so unlike
    the old withdraw-first order, a failed install is confirmed NOT to
    have disturbed it, and whatever new-path flows DID get staged before
    the failure must be rolled back (withdrawn) rather than left as
    partial garbage on the switches. If the ROLLBACK itself then fails to
    fully complete, `current_path` must NOT be reported as the old path --
    an earlier revision of this fix discarded the rollback helper's return
    value and unconditionally claimed the old path was confirmed intact
    regardless, the same class of "logical state vs actual switch state"
    gap this whole section exists to close, found one level deeper on a
    second look. If staging succeeds but the commit step (withdrawing the
    old path) then fails, forwarding is still correct -- traffic follows
    the fully-staged new path -- so `current_path` advances to the new
    path, but under the distinct `repair_installed_stale_withdraw` event,
    not a clean `repair_installed`, since the old path's flows are left
    behind uncleaned."""
    saved_apply_flow = daim_link_agent.apply_flow
    saved_check = daim_link_agent._conflicting_flow_cookie
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    decision = {"action": "repair", "new_path": new_path}
    try:
        # This test is about apply_flow()'s own success/failure, not the
        # forwarding-consistency pre-check (Section 5.1, covered separately
        # by test_install_path_rejects_forwarding_conflict) -- stub it out
        # so install_path() never shells out to a real ovs-ofctl.
        daim_link_agent._conflicting_flow_cookie = lambda bridge, flow: None

        # Clean success: every add and delete call succeeds.
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert result_path == new_path
        assert event_name == "repair_installed"
        assert fields["path"] == new_path

        # Staging (install) fails partway through: s3's add calls fail.
        # Two-phase order means install_path(new_path) runs BEFORE the old
        # path is touched at all, so the old path must come back exactly
        # as it was -- including at the boundary-hop matches s1 and s4
        # share with new_path, which _rollback_staged_flows() must RESTORE
        # (re-install old_path's own action), not merely delete (see
        # test_rollback_staged_flows_restores_boundary_hop_collisions below
        # for that mechanism in isolation; a real live blackhole at exactly
        # these two matches was found and fixed this round).
        install_calls = []

        def failing_install(action, bridge, arg):
            install_calls.append((action, bridge, arg))
            return not (action == "add" and bridge == "s3")

        daim_link_agent.apply_flow = failing_install
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert result_path == old_path, (
            "a staging failure must retain the OLD path, since staging never "
            f"touches it -- got {result_path!r}"
        )
        assert event_name == "repair_incomplete", (
            "a partially-failed installation must be reported under a distinct "
            "event name, not silently logged as repair_installed"
        )
        assert fields["install_ok"] is False
        assert fields["attempted_path"] == new_path
        assert fields["prior_path"] == old_path
        # s2 is exclusive to old_path -- new_path never touches it -- so a
        # rollback call naming bridge "s2" would only happen if a separate,
        # unwanted withdraw_path(old_path)-style call had been issued.
        assert all(bridge != "s2" for action, bridge, arg in install_calls), (
            f"rollback must never touch old_path's own exclusive flows: {install_calls}"
        )

        # Staging succeeds, but the commit step (withdrawing the old path)
        # then fails: forwarding is correct (new path fully installed), so
        # current_path must still advance, but under a distinct event name
        # that flags the old path's flows as stale, uncleaned leftovers.
        def failing_withdraw(action, bridge, arg):
            return action != "delete"

        daim_link_agent.apply_flow = failing_withdraw
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert result_path == new_path, (
            "a commit-step failure must still advance current_path to the "
            "new path -- it IS fully staged and is what traffic follows now"
        )
        assert event_name == "repair_installed_stale_withdraw"
        assert fields["path"] == new_path
        assert fields["stale_path"] == old_path

        # Staging fails (s3) AND the resulting rollback itself fails too:
        # the restore-add for the boundary-hop collision at s1 (undoing
        # what staging did, re-installing old_path's own action there)
        # times out. An earlier revision discarded the rollback helper's
        # own return value and unconditionally reported old_path as
        # confirmed intact regardless -- exactly the same class of false
        # "logical state vs actual switch state" gap this whole section
        # exists to close, just one level deeper (in the rollback path
        # itself). current_path must become None, under a distinct
        # repair_rollback_incomplete event, not silently claim old_path is
        # fine.
        def failing_install_and_rollback(action, bridge, arg):
            if action == "add" and bridge == "s3":
                return False  # staging fails at s3
            if action == "add" and bridge == "s1" and "output:2" in arg:
                # This is old_path's own s1-forward action (in_port=1,
                # actions=output:2) -- the rollback restore-add for the
                # boundary-hop collision, not the staging add (which
                # would carry new_path's output:3 action instead).
                return False
            return True

        daim_link_agent.apply_flow = failing_install_and_rollback
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert result_path is None, (
            "a rollback that itself did not fully succeed must not claim "
            f"old_path is confirmed intact -- got {result_path!r}"
        )
        assert event_name == "repair_rollback_incomplete", (
            "a failed rollback must be reported under a distinct event name, "
            "not silently folded into repair_incomplete"
        )
        assert fields["install_ok"] is False
        assert fields["rollback_ok"] is False
        assert fields["attempted_path"] == new_path
        assert fields["prior_path"] == old_path

        # execute_repair(decision, None) -- e.g. a retry following a failed
        # startup install with no old path at all -- must not crash:
        # _rollback_staged_flows(staged, None) has no prior path to
        # restore anything to.
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        assert daim_link_agent._rollback_staged_flows([], None) is True
        result_path, event_name, fields = execute_repair(decision, None)
        assert result_path == new_path
        assert event_name == "repair_installed"
    finally:
        daim_link_agent.apply_flow = saved_apply_flow
        daim_link_agent._conflicting_flow_cookie = saved_check

    print("daim_link_agent execute_repair two-phase rollback regression test: "
          "PASS -- a staging failure rolls back the partially-staged new path "
          "and retains the untouched old path; a rollback that itself fails "
          "reports current_path=None under a distinct event rather than "
          "claiming the old path is confirmed intact; a commit-step failure "
          "still advances current_path but under a distinct stale-withdraw "
          "event; and a None prior path does not crash a subsequent repair "
          "attempt.")


def test_decide_link_event_includes_bfs_timing():
    """decide_link_event()'s "repair" and "repair_failed" decisions must
    include bfs_start_ns/bfs_end_ns bracketing the bfs_path() call
    (Section 10's service-restoration decomposition), with
    bfs_end_ns >= bfs_start_ns, using the real time.perf_counter_ns()
    clock -- distinct from the synthetic `now` this function's own state
    machine uses -- so a caller can reconstruct BFS cost as a phase
    separate from repair-action time (Section 6.3: repair_start_ns marks
    the start of staging, AFTER this BFS call has already run, so BFS
    cost was never actually inside that window)."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]

    d1 = decide_link_event(
        INTERFACE, "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    assert "bfs_start_ns" in d1 and "bfs_end_ns" in d1
    assert d1["bfs_end_ns"] >= d1["bfs_start_ns"]

    # repair_failed (no alternate path) must also carry BFS timing. The
    # OTHER path (s1-s3-s4) is already down; triggering "down" on the
    # monitored s1-eth2 interface (edge s1-s2, not already in down_edges)
    # leaves no path avoiding both, so BFS itself fails.
    down_edges2 = {frozenset({"s1", "s3"})}
    held_down_until2, interface_state2 = {}, {}
    d2 = decide_link_event(
        "s1-eth2", "down", down_edges2, held_down_until2,
        interface_state2, ["s1", "s2", "s4"], 0.0,
    )
    assert d2["action"] == "repair_failed", d2
    assert "bfs_start_ns" in d2 and "bfs_end_ns" in d2
    assert d2["bfs_end_ns"] >= d2["bfs_start_ns"]

    print("daim_link_agent decide_link_event BFS-timing regression test: "
          "PASS -- both a successful repair decision and a repair_failed "
          "decision include bfs_start_ns/bfs_end_ns bracketing the real "
          "BFS call.")


def test_execute_repair_includes_decomposed_timing():
    """execute_repair()'s returned event_fields must include the full
    service-restoration decomposition (Section 10): stage_start_ns/
    stage_end_ns bracketing install_path() in every outcome,
    commit_start_ns/commit_end_ns bracketing _withdraw_stale_path() when
    a commit actually runs, and the decision's own bfs_start_ns/bfs_end_ns
    (if present) copied through unchanged -- so a single logged event
    carries detection-to-restoration as distinct phases (BFS, staging,
    commit) rather than one opaque repair_start_ns-to-repair_end_ns span.
    repair_start_ns/repair_end_ns keep their pre-existing meaning
    (identical to stage_start_ns, and the overall end respectively) so no
    previously-reported number's definition changes."""
    saved_apply_flow = daim_link_agent.apply_flow
    saved_check = daim_link_agent._conflicting_flow_cookie
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    decision = {
        "action": "repair", "new_path": new_path,
        "bfs_start_ns": 1000, "bfs_end_ns": 1500,
    }
    try:
        daim_link_agent._conflicting_flow_cookie = lambda bridge, flow: None

        # Clean success: staging AND commit both ran, both bracketed, and
        # the decision's BFS timing is copied through unchanged.
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert event_name == "repair_installed"
        assert fields["bfs_start_ns"] == 1000 and fields["bfs_end_ns"] == 1500
        assert fields["repair_start_ns"] == fields["stage_start_ns"]
        assert fields["stage_end_ns"] >= fields["stage_start_ns"]
        assert fields["commit_start_ns"] >= fields["stage_end_ns"]
        assert fields["commit_end_ns"] >= fields["commit_start_ns"]
        assert fields["repair_end_ns"] == fields["commit_end_ns"]

        # A decision with no BFS timing at all (e.g. a hand-built decision
        # in a test, or a future caller that doesn't compute one) must not
        # crash and must simply omit the bfs_*_ns keys.
        no_bfs_decision = {"action": "repair", "new_path": new_path}
        _, event_name, fields = execute_repair(no_bfs_decision, old_path)
        assert event_name == "repair_installed"
        assert "bfs_start_ns" not in fields and "bfs_end_ns" not in fields

        # Staging failure: stage_start_ns/stage_end_ns still present, no
        # commit_*_ns (commit never ran).
        def failing_install(action, bridge, arg):
            return not (action == "add" and bridge == "s3")
        daim_link_agent.apply_flow = failing_install
        _, event_name, fields = execute_repair(decision, old_path)
        assert event_name == "repair_incomplete"
        assert fields["bfs_start_ns"] == 1000 and fields["bfs_end_ns"] == 1500
        assert fields["stage_end_ns"] >= fields["stage_start_ns"]
        assert "commit_start_ns" not in fields
    finally:
        daim_link_agent.apply_flow = saved_apply_flow
        daim_link_agent._conflicting_flow_cookie = saved_check

    print("daim_link_agent execute_repair decomposed-timing regression "
          "test: PASS -- stage_*_ns and commit_*_ns bracket their own "
          "phases correctly, the decision's own bfs_*_ns is copied through "
          "unchanged when present and simply omitted when not, and "
          "repair_start_ns/repair_end_ns keep their pre-existing meaning.")


def test_withdraw_stale_path_skips_boundary_hop_collisions():
    """_withdraw_stale_path(old_path, new_path) -- execute_repair()'s commit
    step -- must never issue a delete for a match new_path's own flows also
    occupy. Found via a real live fault injection against the multi-OVS
    testbed: a naive `withdraw_path(old_path)` at commit time deletes by
    (cookie, in_port) match alone, and at the switch attached to SOURCE and
    the one attached to DEST, one flow's match is IDENTICAL between
    old_path and new_path (the host-attachment port never changes across
    alternate routes) -- `install_path(new_path)` already updated that
    entry's action in place during staging, so deleting it "to clean up the
    old path" removed the just-installed, live, wanted entry instead,
    leaving that hop with no matching flow at all in one direction. This
    was confirmed live: after an otherwise-successful repair (logged as
    `repair_installed`), `ovs-ofctl dump-flows` on both boundary switches
    showed the colliding match completely empty -- a blackhole a
    bookkeeping-only test could not have caught."""
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    calls = []
    saved_apply_flow = daim_link_agent.apply_flow
    try:
        daim_link_agent.apply_flow = lambda action, bridge, arg: (
            calls.append((action, bridge, arg)) or True
        )
        ok = daim_link_agent._withdraw_stale_path(old_path, new_path)
        assert ok is True

        colliding = {
            (bridge, _delete_match(flow))
            for bridge, flow in path_to_flows(old_path)
        } & {
            (bridge, _delete_match(flow))
            for bridge, flow in path_to_flows(new_path)
        }
        assert colliding, "expected old_path and new_path to share a boundary-hop match"

        issued = {(bridge, arg) for action, bridge, arg in calls}
        assert not (issued & colliding), (
            f"a delete must never be issued for a match new_path also occupies: "
            f"{issued & colliding}"
        )
        # s2 is exclusive to old_path -- it must still be withdrawn normally.
        assert any(bridge == "s2" for action, bridge, arg in calls), (
            "old_path's own exclusive flows (s2) must still be withdrawn"
        )
        assert all(action == "delete" for action, bridge, arg in calls), (
            "the commit step only ever deletes -- it never needs to add anything"
        )
    finally:
        daim_link_agent.apply_flow = saved_apply_flow

    print("daim_link_agent _withdraw_stale_path regression test: PASS -- "
          "the commit step withdraws old_path's own exclusive flows but "
          "never deletes a match new_path's staged flows also occupy.")


def test_rollback_staged_flows_restores_boundary_hop_collisions():
    """_rollback_staged_flows(staged, old_path) -- execute_repair()'s
    rollback step on a staging failure -- must RESTORE old_path's original
    action at a match the two paths share, not merely delete it. A flow
    whose match does not collide with old_path is purely additive (staging
    created it from nothing), so deleting it is correct cleanup; but at the
    boundary-hop matches (see test_withdraw_stale_path_skips_boundary_hop_collisions
    above), staging already overwrote old_path's own live entry in place --
    a plain delete there removes the entry outright, leaving old_path
    missing a working flow at exactly that hop rather than genuinely "left
    in place" as Section 5.2 requires. This was confirmed live alongside
    the commit-side fix: the naive delete-everything-in-new_path rollback
    left the same two boundary matches completely empty. Exercised here
    with every one of new_path's flows as `staged` (as if the whole new
    path got confirmed installed before some later, unmodelled failure);
    the narrower case where only SOME of new_path's flows were ever
    actually staged is covered separately by
    test_rollback_staged_flows_ignores_never_staged_matches below."""
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    staged = path_to_flows(new_path)
    calls = []
    saved_apply_flow = daim_link_agent.apply_flow
    try:
        daim_link_agent.apply_flow = lambda action, bridge, arg: (
            calls.append((action, bridge, arg)) or True
        )
        ok = daim_link_agent._rollback_staged_flows(staged, old_path)
        assert ok is True

        old_by_match = {
            (bridge, _delete_match(flow)): flow
            for bridge, flow in path_to_flows(old_path)
        }
        new_by_match = {
            (bridge, _delete_match(flow)): flow
            for bridge, flow in path_to_flows(new_path)
        }
        colliding_keys = set(old_by_match) & set(new_by_match)
        assert colliding_keys, "expected old_path and new_path to share a boundary-hop match"

        adds = {(bridge, arg) for action, bridge, arg in calls if action == "add"}
        deletes = {(bridge, arg) for action, bridge, arg in calls if action == "delete"}

        for bridge, match in colliding_keys:
            restored_flow = old_by_match[(bridge, match)]
            assert (bridge, restored_flow) in adds, (
                f"a colliding match must be RESTORED to old_path's own action "
                f"via an add-flow call, not merely deleted: expected add "
                f"({bridge!r}, {restored_flow!r})"
            )
            assert (bridge, match) not in deletes, (
                f"a colliding match must never be deleted outright: {(bridge, match)}"
            )

        non_colliding_keys = set(new_by_match) - colliding_keys
        assert non_colliding_keys, "expected at least one purely-additive new_path flow"
        for bridge, match in non_colliding_keys:
            assert (bridge, match) in deletes, (
                f"a purely-additive staged flow (no old_path counterpart) must "
                f"be deleted during rollback, not left behind: {(bridge, match)}"
            )
    finally:
        daim_link_agent.apply_flow = saved_apply_flow

    print("daim_link_agent _rollback_staged_flows regression test: PASS -- "
          "a boundary-hop match shared with old_path is restored to "
          "old_path's own action via add-flow, while a purely-additive "
          "staged flow is deleted as ordinary rollback cleanup.")


def test_rollback_staged_flows_ignores_never_staged_matches():
    """_rollback_staged_flows(staged, old_path) must act ONLY on the flows
    a failed install_path() call actually confirmed staged -- passed in as
    `staged` -- never on the full set path_to_flows(new_path) would
    produce. Found by review: install_path() can reject a flow before ever
    calling apply_flow() (Section 5.1's forwarding-consistency pre-check),
    so that flow was never touched by this attempt at all. If rollback
    recomputed from the intended new path instead of the actual `staged`
    list, a rejected flow at a boundary-hop collision match (see
    test_withdraw_stale_path_skips_boundary_hop_collisions) would still
    look like it "collides with old_path", and rollback would RE-INSTALL
    old_path's action there via add-flow -- silently overwriting the very
    foreign flow the forwarding-consistency check just correctly refused
    to overwrite in the first place. This test simulates exactly that:
    new_path's SOURCE-facing (boundary) flow is excluded from `staged` (as
    if the forwarding-consistency check had rejected it), and confirms
    _rollback_staged_flows() issues no call at all for that match -- not a
    delete, and critically not an add-flow "restore" that would clobber
    whatever is actually sitting there now."""
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    all_new_flows = path_to_flows(new_path)

    old_by_match = {
        (bridge, _delete_match(flow)) for bridge, flow in path_to_flows(old_path)
    }
    new_by_match = {
        (bridge, _delete_match(flow)): (bridge, flow) for bridge, flow in all_new_flows
    }
    colliding_keys = set(old_by_match) & set(new_by_match)
    assert colliding_keys, "expected old_path and new_path to share a boundary-hop match"
    rejected_key = next(iter(colliding_keys))
    rejected_bridge, rejected_flow = new_by_match[rejected_key]

    # staged = everything install_path() would have produced EXCEPT the
    # boundary-hop flow the forwarding-consistency check rejected.
    staged = [
        (bridge, flow) for bridge, flow in all_new_flows
        if (bridge, flow) != (rejected_bridge, rejected_flow)
    ]
    assert len(staged) == len(all_new_flows) - 1

    calls = []
    saved_apply_flow = daim_link_agent.apply_flow
    try:
        daim_link_agent.apply_flow = lambda action, bridge, arg: (
            calls.append((action, bridge, arg)) or True
        )
        ok = daim_link_agent._rollback_staged_flows(staged, old_path)
        assert ok is True

        touched = {(bridge, arg) for action, bridge, arg in calls}
        rejected_bridge_out, rejected_match = rejected_key
        assert (rejected_bridge_out, rejected_match) not in touched, (
            "rollback must never touch a match this attempt never actually "
            f"staged, even if it collides with old_path: {touched}"
        )
        # Every other (genuinely staged) flow must still be rolled back
        # normally -- this isn't a case of rollback doing nothing at all.
        assert touched, "rollback must still act on the flows that WERE staged"
    finally:
        daim_link_agent.apply_flow = saved_apply_flow

    print("daim_link_agent _rollback_staged_flows staged-subset regression "
          "test: PASS -- a boundary-hop match this attempt never actually "
          "staged (e.g. rejected by the forwarding-consistency pre-check) "
          "is never touched by rollback, even though it collides with "
          "old_path -- preventing rollback from overwriting a foreign flow "
          "the conflict check correctly protected.")


def test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names():
    """bfs_path() and path_to_flows() must resolve source/destination hosts
    from the declared SOURCE/DEST/HOST_ATTACHMENT globals, not the literal
    strings "h1"/"h2" an earlier revision hardcoded -- both measured
    topologies in this evidence set happen to name their hosts `h1`/`h2`, so
    this defect never surfaced live, but load_topology_config() explicitly
    allows a deployment to declare differently-named hosts (Section 4.4),
    and the hardcoded version would have raised KeyError in path_to_flows()
    the first time one did (`TOPOLOGY[switch]["h1"]` on a switch whose
    topology dict has no "h1" key at all)."""
    saved = (
        dict(daim_link_agent.TOPOLOGY), dict(daim_link_agent.HOST_ATTACHMENT),
        daim_link_agent.SOURCE, daim_link_agent.DEST,
    )
    try:
        daim_link_agent.TOPOLOGY = {
            "x1": {"alpha": (1, None), "x2": (2, 1)},
            "x2": {"x1": (1, 2), "beta": (2, None)},
        }
        daim_link_agent.HOST_ATTACHMENT = {"alpha": "x1", "beta": "x2"}
        daim_link_agent.SOURCE, daim_link_agent.DEST = "alpha", "beta"

        path = bfs_path("alpha", "beta", down_edges=set())
        assert path == ["x1", "x2"]
        assert not any(node in daim_link_agent.HOST_ATTACHMENT for node in path), (
            "a host node must never appear in a BFS-computed switch path"
        )

        alpha_beta_cookie = f"{_agent_cookie():x}"
        assert alpha_beta_cookie != _C, (
            "a different protected pair (alpha/beta vs. h1/h2) must derive a "
            "different cookie -- confirms the cookie is genuinely pair-scoped, "
            "not still a fixed constant"
        )
        flows = set(path_to_flows(path))
        assert flows == {
            ("x1", f"cookie=0x{alpha_beta_cookie},priority=100,in_port=1,actions=output:2"),
            ("x1", f"cookie=0x{alpha_beta_cookie},priority=100,in_port=2,actions=output:1"),
            ("x2", f"cookie=0x{alpha_beta_cookie},priority=100,in_port=1,actions=output:2"),
            ("x2", f"cookie=0x{alpha_beta_cookie},priority=100,in_port=2,actions=output:1"),
        }, (
            "path_to_flows() must resolve the declared SOURCE/DEST ('alpha'/'beta') "
            "to compute in_port/out_port, not crash or silently miscompute by "
            "looking for hardcoded 'h1'/'h2' entries that do not exist for this "
            "topology's switches"
        )
    finally:
        daim_link_agent.TOPOLOGY, daim_link_agent.HOST_ATTACHMENT, \
            daim_link_agent.SOURCE, daim_link_agent.DEST = saved

    print("daim_link_agent SOURCE/DEST genericity regression test: PASS -- "
          "bfs_path()/path_to_flows() work correctly for a topology whose "
          "hosts are not named 'h1'/'h2', instead of assuming those literal names.")


def test_delete_match_scopes_by_cookie_not_bare_in_port():
    """_delete_match() must scope deletion by the flow's own cookie plus
    in_port, not a bare in_port match. An earlier revision derived the
    delete match by stripping the add-form flow string down to whatever was
    left (flow.split(",actions=")[0].split(",",1)[1]), which dropped both
    the cookie AND the priority field, leaving only "in_port=N" -- confirmed
    empirically against a live OVS bridge that this deletes ANY flow
    sharing that in_port regardless of owner, priority, or match fields,
    including a real unrelated flow another process installed. Confirmed
    empirically that a cookie-mask delete (cookie=<cookie>/-1,in_port=N)
    leaves that same unrelated flow untouched. This test uses an arbitrary
    test cookie (0xdeadbeef) to check _delete_match()'s parsing logic in
    isolation from _agent_cookie()'s specific derivation, which
    test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names
    covers separately; the live OVS behaviour itself was verified directly
    against a running bridge, not re-verified here (this repo has no OVS
    instance to test against in plain unit tests)."""
    flow = "cookie=0xdeadbeef,priority=100,in_port=1,actions=output:2"
    match = _delete_match(flow)
    assert match == "cookie=0xdeadbeef/-1,in_port=1", match
    assert "priority" not in match, (
        "non-strict del-flows rejects a literal priority= field outright "
        "(confirmed empirically: 'ovs-ofctl: unknown keyword priority') "
        "-- it must never appear in the delete match"
    )

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class Result:
            returncode = 0
            stderr = ""
        return Result()

    saved_run = daim_link_agent.subprocess.run
    try:
        daim_link_agent.subprocess.run = fake_run
        withdraw_path(["s1", "s2", "s4"])
        delete_calls = [c for c in calls if c[1] == "delete"]
        assert delete_calls, "withdraw_path must issue delete calls"
        for call in delete_calls:
            match_arg = call[3]
            assert match_arg.startswith(f"cookie=0x{_C}/-1,in_port="), match_arg
            assert "priority" not in match_arg
    finally:
        daim_link_agent.subprocess.run = saved_run

    print("daim_link_agent cookie-scoped-deletion regression test: PASS -- "
          "withdraw_path() deletes by a cookie mask + in_port, never a bare "
          "in_port match or a rejected priority= field.")


def test_execute_startup_install_reports_partial_failure():
    """execute_startup_install() must not use the `agent_started` event
    name, and must not report a non-None current_path, unless the initial
    flow installation fully succeeded. An earlier revision called
    install_path(current_path) at startup for its side effect only,
    discarding whether it actually succeeded, so main() always logged
    agent_started with the intended path regardless -- the same class of
    defect execute_repair() fixes for the ongoing repair path, found here
    on the startup path by a second look at the same review. On failure,
    current_path must be None (not the attempted path) so
    maybe_retry_repair() picks up the unfinished installation on a later
    tick, exactly as it does for a failed ongoing repair."""
    saved_apply_flow = daim_link_agent.apply_flow
    saved_check = daim_link_agent._conflicting_flow_cookie
    down_edges = set()
    initial_path = ["s1", "s3", "s4"]
    try:
        daim_link_agent._conflicting_flow_cookie = lambda bridge, flow: None
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        current_path, event_name, fields = execute_startup_install(initial_path, down_edges)
        assert current_path == initial_path
        assert event_name == "agent_started"
        assert fields["initial_path"] == initial_path

        daim_link_agent.apply_flow = lambda action, bridge, arg: not (action == "add" and bridge == "s3")
        current_path, event_name, fields = execute_startup_install(initial_path, down_edges)
        assert current_path is None, (
            "current_path must be None, not the attempted initial path, when "
            "the startup installation only partially succeeded"
        )
        assert event_name == "startup_install_incomplete", (
            "a partially-failed startup installation must be reported under a "
            "distinct event name, not silently logged as agent_started"
        )
        assert fields["attempted_path"] == initial_path
    finally:
        daim_link_agent.apply_flow = saved_apply_flow
        daim_link_agent._conflicting_flow_cookie = saved_check

    print("daim_link_agent startup-install failure-honesty regression test: PASS -- "
          "a partially-failed startup installation is reported as "
          "startup_install_incomplete with current_path=None, not agent_started.")


def test_maybe_retry_repair_retries_until_success():
    """A retained-but-still-faulty current_path (from a staged-then-rolled-
    back repair, Section 5.2) must not be a permanent dead end. Found by
    review: decide_link_event() only starts a repair on a "down" event for
    an edge NOT already in down_edges -- but a failed repair's edge is
    already in down_edges by the time execute_repair() runs, so a
    duplicate transition on the same edge, or no further transition at all
    (the physical link stays down and nothing about it changes again),
    would never re-trigger a repair attempt through the event-driven path
    alone. maybe_retry_repair(), called from main()'s periodic
    poll_interval tick (not the OVSDB event branch), is what gives a
    transient flow-installation failure -- a remote OVS instance briefly
    unreachable, a timeout -- a way to eventually succeed once conditions
    improve, with no new OVSDB event ever required. Simulates exactly the
    scenario found by review: down -> repair -> forced install failure
    (old path retained, since it was never touched) -> no new link-state
    transition -> retry on a later tick -> still failing (same old path
    retained again) -> retry again -> succeeds."""
    saved_apply_flow = daim_link_agent.apply_flow
    saved_check = daim_link_agent._conflicting_flow_cookie
    old_path = ["s1", "s2", "s4"]
    down_edges = {frozenset({"s1", "s2"})}
    try:
        daim_link_agent._conflicting_flow_cookie = lambda bridge, flow: None
        # Simulate a repair whose staging fails partway through: the OLD
        # path is retained (Section 5.2's two-phase protocol never touches
        # it), but that old path itself traverses the very edge that just
        # went down, which is exactly when a retry is still needed.
        daim_link_agent.apply_flow = lambda action, bridge, arg: not (action == "add" and bridge == "s3")
        decision = {"action": "repair", "new_path": ["s1", "s3", "s4"]}
        current_path, event_name, fields = execute_repair(decision, old_path)
        assert current_path == old_path, (
            "a staging failure must retain the untouched old path, not degrade "
            f"to None -- got {current_path!r}"
        )
        assert event_name == "repair_incomplete"

        # No new link-state transition arrives -- decide_link_event() would
        # never re-trigger a repair for this edge (already in down_edges).
        # A later periodic tick (standing in for main()'s poll_interval
        # wake-up) must retry anyway, since current_path still traverses a
        # down edge.
        current_path, event_name, fields = maybe_retry_repair(current_path, down_edges)
        assert current_path == old_path, (
            "a retry against the still-failing adapter must retain the same "
            "old path, not silently give up or falsely claim success"
        )
        assert event_name == "repair_incomplete"

        # Once the underlying failure clears (e.g. a remote instance
        # becomes reachable again), the very next tick must succeed -- with
        # no new OVSDB event ever having arrived for this edge.
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        current_path, event_name, fields = maybe_retry_repair(current_path, down_edges)
        assert current_path == ["s1", "s3", "s4"], (
            "a retained-but-faulty current_path must not be a permanent dead "
            "end once the underlying flow-installation problem clears"
        )
        assert event_name == "repair_installed"

        # Once healthy, further ticks must be no-ops (nothing to retry):
        # current_path no longer traverses any down edge.
        current_path, event_name, fields = maybe_retry_repair(current_path, down_edges)
        assert event_name is None
        assert current_path == ["s1", "s3", "s4"]

        # If BFS genuinely finds no path at all (not a flow-install
        # problem), retrying must not loop pointlessly -- report a
        # distinct event instead of repeatedly calling execute_repair.
        # current_path=None here stands in for a failed startup install,
        # which has no old path to retain in the first place.
        unreachable_down_edges = {
            frozenset({"s1", "s2"}), frozenset({"s1", "s3"}),
        }
        current_path, event_name, fields = maybe_retry_repair(None, unreachable_down_edges)
        assert current_path is None
        assert event_name == "repair_retry_no_path"
    finally:
        daim_link_agent.apply_flow = saved_apply_flow
        daim_link_agent._conflicting_flow_cookie = saved_check

    print("daim_link_agent repair-retry liveness regression test: PASS -- "
          "a retained-but-faulty current_path is retried on later ticks "
          "until the underlying flow-installation failure clears, with no "
          "new OVSDB event required to trigger the retry.")


def test_agent_cookie_is_deterministic_and_pair_scoped():
    """_agent_cookie() must return the identical value across repeated calls
    for the same (SOURCE, DEST) pair -- deterministic across a restart is a
    deliberate design property (Section 4.6): a restarted agent protecting
    the same pair must still recognise, and be able to withdraw, flows a
    prior instance of itself installed before the restart, which a
    randomised-at-startup cookie would break. It must also differ between
    two different pairs, so two agent processes protecting different pairs
    on a switch they share do not collide and delete each other's flows --
    the exact bug a single fixed AGENT_COOKIE constant (an earlier revision)
    would reintroduce."""
    saved = daim_link_agent.SOURCE, daim_link_agent.DEST
    try:
        daim_link_agent.SOURCE, daim_link_agent.DEST = "h1", "h2"
        first = _agent_cookie()
        second = _agent_cookie()
        assert first == second, "the same (SOURCE, DEST) pair must always yield the same cookie"

        daim_link_agent.SOURCE, daim_link_agent.DEST = "h3", "h4"
        different_pair = _agent_cookie()
        assert different_pair != first, (
            "a different protected pair must yield a different cookie, or two "
            "agents sharing a switch but protecting different pairs would "
            "delete each other's flows"
        )
    finally:
        daim_link_agent.SOURCE, daim_link_agent.DEST = saved

    print("daim_link_agent cookie determinism/pair-scoping regression test: PASS -- "
          "_agent_cookie() is stable across calls for the same pair and "
          "distinct across different pairs, not a single fixed constant.")


def test_resync_from_reconnect_updates_state_without_touching_holddown():
    """resync_from_reconnect() must update interface_state/down_edges from a
    fresh post-reconnect snapshot exactly like the startup-state fix does
    for a fresh process (Section 4.4), but must not touch held_down_until --
    reconnect is about regaining observability after a monitor child died,
    not about repair timing, so an in-progress hold-down window continues
    on its existing schedule unaffected. Also confirms an unrecognised
    link_state is skipped (not applied, not raised -- a live server
    misbehaving after reconnect should not crash an already-running agent)
    and reported back to the caller."""
    down_edges = set()
    interface_state = {}
    # "s3-eth1" is not in the default MONITORED_INTERFACES, so it must be
    # ignored entirely (not reported as invalid, not applied) -- only a
    # bogus state on a genuinely *monitored* interface ("s2-eth1" here)
    # counts as the unrecognised-link_state case this test also checks.
    snapshot = {"s1-eth2": "down", "s2-eth1": "bogus", "s3-eth1": "up"}
    invalid = resync_from_reconnect(snapshot, down_edges, interface_state)
    assert invalid == [("s2-eth1", "bogus")], invalid
    assert interface_state.get("s1-eth2") == "down"
    assert "s2-eth1" not in interface_state, (
        "an unrecognised link_state must not be applied to interface_state"
    )
    assert "s3-eth1" not in interface_state, (
        "an interface absent from MONITORED_INTERFACES must be ignored entirely"
    )
    assert frozenset({"s1", "s2"}) in down_edges, (
        "s1-eth2 reporting down must add its edge to down_edges"
    )

    # Both interfaces of the s1-s2 edge now confirm up -> edge recovers.
    down_edges2 = {frozenset({"s1", "s2"})}
    interface_state2 = {"s1-eth2": "down", "s2-eth1": "down"}
    resync_from_reconnect({"s1-eth2": "up", "s2-eth1": "up"}, down_edges2, interface_state2)
    assert frozenset({"s1", "s2"}) not in down_edges2, (
        "an edge must be discarded from down_edges once every interface "
        "observing it confirms up, exactly as _edge_confirmed_up requires "
        "elsewhere in this file"
    )

    print("daim_link_agent resync_from_reconnect regression test: PASS -- "
          "state is reconciled from a fresh reconnect snapshot, an "
          "unrecognised link_state is skipped and reported rather than "
          "applied or raised, and hold-down timers are left untouched.")


def test_monitor_link_rows_reconnects_on_stream_death():
    """monitor_link_rows() must not permanently drop a target when its
    monitor child's stream closes (EOF -- the ovsdb-client monitor child
    died: a crash, a dropped OVSDB connection). An earlier revision just
    removed the dead stream from the poll set and kept going, silently
    blind to that target for the rest of the agent's run -- the same class
    of gap the startup-state fix closed for a fresh process, left open at
    runtime. This drives the actual select()/readline() reconnect path
    against real OS pipes (not mocks), with _start_monitor() monkeypatched
    to return a second real pipe standing in for the respawned child,
    covering both a successful reconnect and a failed one."""
    r1, w1 = os.pipe()
    reader1, writer1 = os.fdopen(r1, "r"), os.fdopen(w1, "w")
    r2, w2 = os.pipe()
    reader2, writer2 = os.fdopen(r2, "r"), os.fdopen(w2, "w")

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout

    saved_start_monitor = daim_link_agent._start_monitor
    try:
        # A normal event on the original connection must still work.
        writer1.write(json.dumps({
            "headings": ["row", "action", "name", "link_state"],
            "data": [["r1", "new", "s1-eth2", "down"]],
        }) + "\n")
        writer1.flush()

        # Pre-load the reconnect target's initial snapshot, so
        # read_initial_snapshot() (called synchronously inside the
        # reconnect path) does not have to wait on the default timeout.
        writer2.write(json.dumps({
            "headings": ["row", "action", "name", "link_state"],
            "data": [["r1", "initial", "s1-eth2", "up"]],
        }) + "\n")
        writer2.flush()

        daim_link_agent._start_monitor = lambda target=None: FakeProc(reader2)

        gen = daim_link_agent.monitor_link_rows({None: FakeProc(reader1)}, poll_interval=2.0)
        assert next(gen) == ("s1-eth2", "down"), "a normal event must still be yielded first"

        writer1.close()  # simulate the original monitor child dying (EOF)
        event = next(gen)
        assert event[0] == daim_link_agent.RECONNECT_EVENT
        assert event[1] is None  # the local target
        assert event[2] == {"s1-eth2": "up"}, event[2]

        # The reconnected stream must now be the one actually polled.
        writer2.write(json.dumps({
            "headings": ["row", "action", "name", "link_state"],
            "data": [["r2", "new", "s1-eth2", "down"]],
        }) + "\n")
        writer2.flush()
        assert next(gen) == ("s1-eth2", "down"), (
            "events must keep flowing from the newly-reconnected stream"
        )
        writer2.close()
    finally:
        daim_link_agent._start_monitor = saved_start_monitor
        for f in (reader1, reader2):
            try:
                f.close()
            except OSError:
                pass

    # Failure path: the reconnect attempt itself fails (e.g. respawn error
    # or no initial snapshot within timeout) -- must yield a
    # (RECONNECT_EVENT, target, None) rather than crashing the generator,
    # and must not re-add that target to the poll set.
    r3, w3 = os.pipe()
    reader3, writer3 = os.fdopen(r3, "r"), os.fdopen(w3, "w")
    try:
        def failing_start_monitor(target=None):
            raise RuntimeError("simulated respawn failure")
        daim_link_agent._start_monitor = failing_start_monitor

        gen = daim_link_agent.monitor_link_rows({None: FakeProc(reader3)}, poll_interval=2.0)
        writer3.close()
        event = next(gen)
        assert event == (daim_link_agent.RECONNECT_EVENT, None, None), event
        # Nothing left to poll -> the generator must end cleanly, not hang.
        remaining = list(gen)
        assert remaining == []
    finally:
        daim_link_agent._start_monitor = saved_start_monitor
        try:
            reader3.close()
        except OSError:
            pass

    print("daim_link_agent monitor-reconnect regression test: PASS -- "
          "a dead monitor stream triggers one reconnect attempt, resuming "
          "event delivery on success and yielding a distinct failure event "
          "(not crashing or hanging) when the reconnect itself fails.")


def test_conflicting_flow_cookie_parses_dump_flows_output():
    """_conflicting_flow_cookie() must parse `ovs-ofctl dump-flows` output
    correctly: return the cookie of an existing flow at the exact
    priority=100 match this agent is about to install over, None when the
    match is free (or occupied only at a different priority -- confirmed
    live that a non-strict dump-flows filter on in_port alone returns
    every priority sharing that port, so the priority=100 check against
    the returned lines is what narrows to the actual match, since
    `priority=` itself is rejected as a dump-flows filter keyword,
    confirmed empirically: 'ovs-ofctl: unknown keyword priority'), and
    raise _ForwardingCheckError -- not silently return None -- when the
    query itself could not be completed, so install_path() fails safe
    instead of treating an unreadable switch as conflict-free."""
    calls = []

    def make_fake_run(stdout, returncode=0):
        def fake_run(argv, **kwargs):
            calls.append(argv)
            class Result:
                pass
            r = Result()
            r.returncode = returncode
            r.stdout = stdout
            r.stderr = "" if returncode == 0 else "boom"
            return r
        return fake_run

    saved_run = daim_link_agent.subprocess.run
    flow = f"cookie=0x{_C},priority=100,in_port=1,actions=output:2"
    try:
        # Free match: no flow at all on this in_port.
        daim_link_agent.subprocess.run = make_fake_run(
            "OFPST_FLOW reply (OF1.3) (xid=0x2):\n"
        )
        assert daim_link_agent._conflicting_flow_cookie("s1", flow) is None
        assert calls[-1][:4] == ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows"]
        assert calls[-1][-1] == "in_port=1"

        # Occupied match: a different process's flow at priority=100.
        daim_link_agent.subprocess.run = make_fake_run(
            "OFPST_FLOW reply (OF1.3) (xid=0x6):\n"
            " cookie=0xdeadbeef, duration=0.02s, table=0, n_packets=0, "
            "n_bytes=0, priority=100,in_port=1 actions=output:9\n"
        )
        assert daim_link_agent._conflicting_flow_cookie("s1", flow) == "deadbeef"

        # Same in_port, but only at a different priority -- not this
        # agent's own match, so not a conflict for install_path()'s purposes.
        daim_link_agent.subprocess.run = make_fake_run(
            "OFPST_FLOW reply (OF1.3) (xid=0x6):\n"
            " cookie=0xdeadbeef, duration=0.02s, table=0, n_packets=0, "
            "n_bytes=0, priority=50,in_port=1 actions=output:9\n"
        )
        assert daim_link_agent._conflicting_flow_cookie("s1", flow) is None

        # Query failure (non-zero exit) must fail safe, not return None.
        daim_link_agent.subprocess.run = make_fake_run("", returncode=1)
        try:
            daim_link_agent._conflicting_flow_cookie("s1", flow)
            assert False, "expected _ForwardingCheckError"
        except daim_link_agent._ForwardingCheckError:
            pass

        # Query timeout must also fail safe.
        def timeout_run(argv, **kwargs):
            raise daim_link_agent.subprocess.TimeoutExpired(cmd=argv, timeout=5)
        daim_link_agent.subprocess.run = timeout_run
        try:
            daim_link_agent._conflicting_flow_cookie("s1", flow)
            assert False, "expected _ForwardingCheckError"
        except daim_link_agent._ForwardingCheckError:
            pass
    finally:
        daim_link_agent.subprocess.run = saved_run

    print("daim_link_agent forwarding-consistency dump-flows-parsing "
          "regression test: PASS -- an occupied priority=100 match returns "
          "its cookie, a free or different-priority match returns None, and "
          "a failed or timed-out query raises _ForwardingCheckError instead "
          "of returning None.")


def test_install_path_rejects_forwarding_conflict():
    """install_path() must not call apply_flow("add", ...) for a flow whose
    exact match is already occupied by a different process's flow (a
    foreign cookie), and must report the whole call as failed -- Section
    5.1's forwarding-consistency check, the higher-priority of the two
    checks that section flagged as unimplemented. Re-installing over this
    agent's OWN prior flow (same cookie) must still proceed normally: that
    is a legitimate re-install (e.g. a retried repair), not a conflict. A
    pre-check that could not be completed at all must also block the add,
    fail-safe, exactly like a real conflict."""
    add_calls = []
    logged = []

    def fake_apply_flow(action, bridge, arg):
        if action == "add":
            add_calls.append((bridge, arg))
        return True

    def fake_log(event, **fields):
        if event == "forwarding_conflict_rejected":
            logged.append(fields)

    saved_apply_flow = daim_link_agent.apply_flow
    saved_log = daim_link_agent.log
    saved_check = daim_link_agent._conflicting_flow_cookie
    path = ["s1", "s3", "s4"]
    my_cookie = _C
    try:
        daim_link_agent.apply_flow = fake_apply_flow
        daim_link_agent.log = fake_log

        # A foreign cookie on switch s3 only -- that switch's two flows
        # must be rejected; s1's and s4's flows (no conflict) must still
        # install, but the overall call must still be reported as failed.
        def conflict_on_s3(bridge, flow):
            return "deadbeef" if bridge == "s3" else None
        daim_link_agent._conflicting_flow_cookie = conflict_on_s3
        ok, staged = daim_link_agent.install_path(path)
        assert ok is False, (
            "a foreign-cookie conflict on any switch must fail the whole "
            "install_path() call"
        )
        assert all(bridge != "s3" for bridge, _ in add_calls), (
            "a flow whose match is occupied by a foreign cookie must never "
            "reach apply_flow()"
        )
        assert any(bridge == "s1" for bridge, _ in add_calls) and \
            any(bridge == "s4" for bridge, _ in add_calls), (
            "switches with no conflict must still be installed even though "
            "a different switch in the same path was rejected"
        )
        assert logged and logged[0]["existing_cookie"] == "deadbeef"
        assert all(bridge != "s3" for bridge, _ in staged), (
            "a rejected flow must never appear in the staged list -- a "
            "rollback acting on `staged` must not believe it was installed"
        )
        assert {bridge for bridge, _ in staged} == {"s1", "s4"}, (
            "staged must contain exactly the flows that were actually "
            f"confirmed installed: {staged}"
        )

        # Same cookie as this agent's own -- not a conflict, a legitimate
        # re-install proceeds normally.
        add_calls.clear()
        logged.clear()
        daim_link_agent._conflicting_flow_cookie = lambda bridge, flow: my_cookie
        ok, staged = daim_link_agent.install_path(path)
        assert ok is True
        assert len(add_calls) == 6
        assert len(staged) == 6
        assert logged == []

        # A pre-check that could not be completed at all must also block
        # the add, fail-safe -- not be treated as "no conflict found".
        add_calls.clear()
        def always_fails(bridge, flow):
            raise daim_link_agent._ForwardingCheckError()
        daim_link_agent._conflicting_flow_cookie = always_fails
        ok, staged = daim_link_agent.install_path(path)
        assert ok is False
        assert add_calls == [], (
            "an incomplete pre-check must block the add, not silently proceed"
        )
        assert staged == []
    finally:
        daim_link_agent.apply_flow = saved_apply_flow
        daim_link_agent.log = saved_log
        daim_link_agent._conflicting_flow_cookie = saved_check

    print("daim_link_agent forwarding-consistency install_path() regression "
          "test: PASS -- a foreign-cookie conflict on one switch blocks only "
          "that switch's flows and fails the whole call, re-installing this "
          "agent's own cookie proceeds normally, and an incomplete pre-check "
          "fails safe instead of proceeding.")


def test_install_path_stages_boundary_collisions_last():
    """install_path(new_path, old_path=...) must install every flow whose
    match does NOT collide with old_path FIRST, and defer the (at most
    two) boundary-hop colliding flows -- the ones that immediately repoint
    a live switch's forwarding action away from old_path -- to the very
    end. Found by review: an earlier revision installed new_path's flows
    in path_to_flows()'s natural SOURCE-to-DEST order, so the SOURCE-facing
    boundary flow could repoint live traffic onto the new path BEFORE the
    rest of the new path was even staged, let alone confirmed -- a
    transient window where forwarding could follow a still-incomplete new
    path, even though a final failure would still correctly roll back to
    old_path. Staging non-colliding flows first instead means old_path
    stays fully live and correct for as long as possible: those flows have
    zero effect on live traffic (nothing upstream forwards through them
    yet), so a mid-staging failure among them never touches old_path's
    live forwarding at all, and even the LAST two (colliding) calls are
    each independently safe -- one governs the forward direction, the
    other the reverse, and each direction's own non-colliding hops are
    already staged by the time its boundary flow runs."""
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    old_matches = {
        (bridge, _delete_match(flow)) for bridge, flow in path_to_flows(old_path)
    }
    new_flows = path_to_flows(new_path)
    colliding_flows = [
        (bridge, flow) for bridge, flow in new_flows
        if (bridge, _delete_match(flow)) in old_matches
    ]
    assert len(colliding_flows) == 2, (
        "expected exactly one boundary-hop collision each at the "
        f"SOURCE-facing and DEST-facing switches: {colliding_flows}"
    )

    call_order = []
    saved_apply_flow = daim_link_agent.apply_flow
    saved_check = daim_link_agent._conflicting_flow_cookie
    try:
        daim_link_agent._conflicting_flow_cookie = lambda bridge, flow: None
        daim_link_agent.apply_flow = lambda action, bridge, arg: (
            call_order.append((bridge, arg)) or True
        )
        ok, staged = daim_link_agent.install_path(new_path, old_path=old_path)
        assert ok is True
        assert len(call_order) == 6

        colliding_set = set(colliding_flows)
        last_two = set(call_order[-2:])
        assert last_two == colliding_set, (
            "the two boundary-hop colliding flows must be staged LAST, "
            f"not in path_to_flows()'s natural order: got {call_order}"
        )
        first_four = set(call_order[:4])
        assert first_four == (set(new_flows) - colliding_set), (
            "every non-colliding flow must be staged before either "
            f"colliding flow: got {call_order}"
        )
    finally:
        daim_link_agent.apply_flow = saved_apply_flow
        daim_link_agent._conflicting_flow_cookie = saved_check

    print("daim_link_agent install_path() staging-order regression test: "
          "PASS -- boundary-hop colliding flows are staged last, after "
          "every purely-additive flow has already succeeded, so old_path "
          "stays fully live and correct for as long as possible during "
          "staging.")


def test_boundary_hop_flow_match_collides_across_alternate_paths():
    """Documents the root cause `_withdraw_stale_path()`/
    `_rollback_staged_flows()` (above) exist to handle: at the switch
    directly attached to SOURCE and the switch directly attached to DEST,
    one of the two flow entries has a match -- (cookie, priority=100,
    in_port) -- that is IDENTICAL across every alternate path through that
    switch, because the host-attachment port never changes regardless of
    which downstream route is chosen. `path_to_flows()`'s SOURCE-facing
    flow at the first switch, and its DEST-facing flow at the last switch,
    therefore collide between the old path and any new alternate path
    sharing that switch -- confirmed here by comparing `_delete_match()`
    output, not by a live OVS call (Section 4.6's existing live
    verification already confirms `add-flow` at an identical match
    replaces the existing entry's action in place, rather than coexisting
    as a second entry). This means "staging" those TWO specific flow
    entries is not actually a non-disruptive, side-by-side install the way
    staging an interior hop's flows is: the moment `install_path()` reaches
    them, the switch's live forwarding action for that hop changes
    immediately, before the rest of the new path is verified or the repair
    has committed. A naive commit/rollback that deletes by match alone does
    not undo that -- it deletes the entry outright rather than restoring or
    leaving it -- confirmed as a real live blackhole against the multi-OVS
    testbed before `_withdraw_stale_path()`/`_rollback_staged_flows()`
    replaced the naive plain-`withdraw_path()` calls an earlier revision of
    `execute_repair()` used; those two functions' own dedicated tests
    (`test_withdraw_stale_path_skips_boundary_hop_collisions`,
    `test_rollback_staged_flows_restores_boundary_hop_collisions`) confirm
    the fix. This test only documents that the collision itself exists and
    is exactly one flow per boundary switch, not two."""
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    old_deletes = {bridge: set() for bridge in ("s1", "s2", "s4")}
    for bridge, flow in path_to_flows(old_path):
        old_deletes[bridge].add(_delete_match(flow))
    new_deletes = {bridge: set() for bridge in ("s1", "s3", "s4")}
    for bridge, flow in path_to_flows(new_path):
        new_deletes[bridge].add(_delete_match(flow))

    shared_s1 = old_deletes["s1"] & new_deletes["s1"]
    shared_s4 = old_deletes["s4"] & new_deletes["s4"]
    assert len(shared_s1) == 1, (
        f"expected exactly one colliding match at the SOURCE-facing switch: {shared_s1}"
    )
    assert len(shared_s4) == 1, (
        f"expected exactly one colliding match at the DEST-facing switch: {shared_s4}"
    )
    # The OTHER flow at each of those switches (the one facing the
    # downstream neighbour, which DOES differ between the two paths) must
    # NOT collide -- only one of the two flows at each boundary switch is
    # affected, not both.
    assert len(old_deletes["s1"] - new_deletes["s1"]) == 1
    assert len(old_deletes["s4"] - new_deletes["s4"]) == 1
    # s2 (interior to the old path only) is not touched by the new path's
    # install at all -- new_path never has a flow on s2 -- confirming
    # interior hops genuinely stage additively, with none of the
    # boundary-hop collision risk (match strings alone don't encode which
    # bridge they're on, so this must be checked per-bridge, not by
    # comparing bare match strings across different switches).
    assert "s2" not in new_deletes

    print("daim_link_agent boundary-hop match-collision documentation test: "
          "PASS -- the SOURCE-facing and DEST-facing switches each have "
          "exactly one flow entry whose match is shared with every "
          "alternate path, confirming two-phase staging's non-disruptive "
          "guarantee holds for interior hops but not for those two boundary "
          "entries (Section 8.3).")


def main():
    test_bfs_path_computation()
    test_holddown_suppresses_flapping()
    test_holddown_stale_state_is_reconciled()
    test_holddown_covers_both_interfaces_on_same_edge()
    test_edge_recovery_requires_all_interfaces_confirmed_up()
    test_edge_recovers_once_both_interfaces_confirm_up()
    test_parse_monitor_line_initial_vs_new()
    test_read_initial_snapshot_reflects_already_down_interface()
    test_startup_detects_already_down_edge()
    test_startup_rejects_unexpected_link_state()
    test_decide_link_event_rejects_unexpected_link_state()
    test_default_config_uses_single_local_ovsdb_target()
    test_multi_ovs_target_routing()
    test_apply_flow_routes_remote_bridge_through_adapter()
    test_apply_flow_resolves_ambiguous_timeout_via_readback()
    test_apply_flow_or_fail_safe_treats_unresolved_ambiguity_as_failure()
    test_execute_repair_reports_partial_failure_honestly()
    test_decide_link_event_includes_bfs_timing()
    test_execute_repair_includes_decomposed_timing()
    test_withdraw_stale_path_skips_boundary_hop_collisions()
    test_rollback_staged_flows_restores_boundary_hop_collisions()
    test_rollback_staged_flows_ignores_never_staged_matches()
    test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names()
    test_delete_match_scopes_by_cookie_not_bare_in_port()
    test_execute_startup_install_reports_partial_failure()
    test_maybe_retry_repair_retries_until_success()
    test_agent_cookie_is_deterministic_and_pair_scoped()
    test_resync_from_reconnect_updates_state_without_touching_holddown()
    test_monitor_link_rows_reconnects_on_stream_death()
    test_conflicting_flow_cookie_parses_dump_flows_output()
    test_install_path_rejects_forwarding_conflict()
    test_install_path_stages_boundary_collisions_last()
    test_boundary_hop_flow_match_collides_across_alternate_paths()


if __name__ == "__main__":
    main()
