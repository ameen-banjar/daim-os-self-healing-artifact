#!/usr/bin/env python3
"""Pure-logic unit tests for daim_link_agent, independent of Mininet/OVS/OVSDB.

Nineteen things are checked without a live network:
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
    (the I/O half of a "repair" decision) only uses the `repair_installed`
    event name and only advances the caller's `current_path` if every
    flow-mod call across withdraw and install actually succeeded; a
    partial failure is reported as `repair_incomplete` with `current_path`
    set to `None` (forwarding state no longer reliably known), not the
    stale prior path, and a degraded `None` current_path does not crash a
    subsequent repair attempt.
16. `test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names` --
    `bfs_path()`/`path_to_flows()` resolve source/destination hosts from the
    declared `SOURCE`/`DEST`/`HOST_ATTACHMENT` globals rather than the
    literal strings `"h1"`/`"h2"` an earlier revision hardcoded -- both
    topologies measured in this evidence set happen to use those names, so
    this never surfaced live, but a deployment declaring different host
    names via `load_topology_config()` would have hit a `KeyError` in
    `path_to_flows()`.
17. `test_delete_match_scopes_by_cookie_not_bare_in_port` -- `withdraw_path()`
    deletes flows by an `AGENT_COOKIE` mask plus `in_port`, not a bare
    `in_port` match, which non-strict `del-flows` would otherwise delete
    for ANY flow sharing that port regardless of owner -- confirmed
    empirically against a live OVS bridge with a real unrelated flow.
18. `test_execute_startup_install_reports_partial_failure` -- the same
    failure-honesty fix `execute_repair()` applies to the ongoing repair
    path, applied to agent startup: `execute_startup_install()` only uses
    the `agent_started` event name if the initial flow installation fully
    succeeded; a partial failure is reported as `startup_install_incomplete`
    with `current_path=None`, not a silent `agent_started`.
19. `test_maybe_retry_repair_retries_until_success` -- a degraded
    `current_path=None` is not a permanent dead end: `maybe_retry_repair()`,
    called from `main()`'s periodic tick (not the OVSDB event branch),
    retries a failed repair on later ticks until the underlying
    flow-installation failure clears, with no new OVSDB event required to
    trigger it -- closing a real liveness gap, since `decide_link_event()`
    alone would never re-trigger a repair for an edge already in
    `down_edges`."""
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
    withdraw_path,
    _delete_match,
    _monitored_ovsdb_targets,
    _ovsdb_target_for_interface,
    _owning_switch,
    _parse_monitor_line,
    SOURCE,
    DEST,
)

EXPECTED_PRIMARY = {
    ("s1", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:2"),
    ("s1", "cookie=0x5e1fea9e,priority=100,in_port=2,actions=output:1"),
    ("s2", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:2"),
    ("s2", "cookie=0x5e1fea9e,priority=100,in_port=2,actions=output:1"),
    ("s4", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:3"),
    ("s4", "cookie=0x5e1fea9e,priority=100,in_port=3,actions=output:1"),
}

EXPECTED_ALTERNATE = {
    ("s1", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:3"),
    ("s1", "cookie=0x5e1fea9e,priority=100,in_port=3,actions=output:1"),
    ("s3", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:2"),
    ("s3", "cookie=0x5e1fea9e,priority=100,in_port=2,actions=output:1"),
    ("s4", "cookie=0x5e1fea9e,priority=100,in_port=2,actions=output:3"),
    ("s4", "cookie=0x5e1fea9e,priority=100,in_port=3,actions=output:2"),
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
            interface_state, current_path, now, hold_down_seconds=hold_down_seconds,
        )
        actions.append(decision["action"])
        if decision["action"] == "repair":
            current_path = decision["new_path"]
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

    # t=0.0: down -> repair, hold-down window opens until t=2.0.
    d1 = decide_link_event(
        INTERFACE, "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    current_path = d1["new_path"]
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

    # s2-eth1 reports down first (as it did in the live run) -> repair.
    d1 = decide_link_event(
        "s2-eth1", "down", down_edges, held_down_until,
        interface_state, current_path, 0.0,
    )
    assert d1["action"] == "repair", d1
    current_path = d1["new_path"]

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


def test_execute_repair_reports_partial_failure_honestly():
    """execute_repair() must not use the `repair_installed` event name, and
    must not advance `current_path` to the attempted new path, unless every
    flow-mod call across both withdraw and install actually succeeded. An
    earlier revision (inline in main()'s loop) called
    install_path()/withdraw_path() for their side effects only, discarding
    apply_flow()'s per-call success/failure, and unconditionally logged
    `repair_installed` and advanced `current_path` regardless -- so a
    partial installation failure (a remote OVS instance unreachable, a
    timeout) was silently reported as a clean repair.

    On failure, `current_path` must become `None`, not the prior value
    unchanged -- an earlier revision of this fix returned the prior
    (pre-repair) path on failure, but `withdraw_path(current_path)` already
    ran before `install_path()`, so a withdraw-succeeded/install-failed
    outcome means the switches hold neither the old path nor the new one:
    claiming the old path is still current is its own false claim, one a
    later repair's `withdraw_path(current_path)` would act on incorrectly."""
    saved_apply_flow = daim_link_agent.apply_flow
    old_path = ["s1", "s2", "s4"]
    new_path = ["s1", "s3", "s4"]
    decision = {"action": "repair", "new_path": new_path}
    try:
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert result_path == new_path
        assert event_name == "repair_installed"
        assert fields["path"] == new_path

        def failing_apply_flow(action, bridge, arg):
            return not (action == "add" and bridge == "s3")

        daim_link_agent.apply_flow = failing_apply_flow
        result_path, event_name, fields = execute_repair(decision, old_path)
        assert result_path is None, (
            "current_path must become None, not the stale prior path, when an "
            "install call failed -- withdraw_path(current_path) already ran, "
            "so the prior path is no longer known to be installed either"
        )
        assert event_name == "repair_incomplete", (
            "a partially-failed installation must be reported under a distinct "
            "event name, not silently logged as repair_installed"
        )
        assert fields["install_ok"] is False
        assert fields["attempted_path"] == new_path
        assert fields["prior_path"] == old_path

        # A degraded current_path=None must not crash a subsequent repair
        # attempt: withdraw_path(None) has nothing known to withdraw.
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        assert daim_link_agent.withdraw_path(None) is True
        result_path, event_name, fields = execute_repair(decision, None)
        assert result_path == new_path
        assert event_name == "repair_installed"
    finally:
        daim_link_agent.apply_flow = saved_apply_flow

    print("daim_link_agent execute_repair failure-honesty regression test: PASS "
          "-- a partial installation failure is reported as repair_incomplete "
          "with current_path set to None (not stale), and a degraded None "
          "current_path does not crash a subsequent repair attempt.")


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

        flows = set(path_to_flows(path))
        assert flows == {
            ("x1", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:2"),
            ("x1", "cookie=0x5e1fea9e,priority=100,in_port=2,actions=output:1"),
            ("x2", "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:2"),
            ("x2", "cookie=0x5e1fea9e,priority=100,in_port=2,actions=output:1"),
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
    """_delete_match() must scope deletion by AGENT_COOKIE plus in_port, not
    a bare in_port match. An earlier revision derived the delete match by
    stripping the add-form flow string down to whatever was left
    (flow.split(",actions=")[0].split(",",1)[1]), which dropped both the
    cookie AND the priority field, leaving only "in_port=N" -- confirmed
    empirically against a live OVS bridge that this deletes ANY flow
    sharing that in_port regardless of owner, priority, or match fields,
    including a real unrelated flow another process installed. Confirmed
    empirically that a cookie-mask delete (cookie=<AGENT_COOKIE>/-1,in_port=N)
    leaves that same unrelated flow untouched. This test checks the string
    construction and the resulting apply_flow() call; the live OVS
    behaviour itself was verified directly against a running bridge, not
    re-verified here (this repo has no OVS instance to test against in
    plain unit tests)."""
    flow = "cookie=0x5e1fea9e,priority=100,in_port=1,actions=output:2"
    match = _delete_match(flow)
    assert match == "cookie=0x5e1fea9e/-1,in_port=1", match
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
            assert match_arg.startswith("cookie=0x5e1fea9e/-1,in_port="), match_arg
            assert "priority" not in match_arg
    finally:
        daim_link_agent.subprocess.run = saved_run

    print("daim_link_agent cookie-scoped-deletion regression test: PASS -- "
          "withdraw_path() deletes by AGENT_COOKIE + in_port, never a bare "
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
    down_edges = set()
    initial_path = ["s1", "s3", "s4"]
    try:
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

    print("daim_link_agent startup-install failure-honesty regression test: PASS -- "
          "a partially-failed startup installation is reported as "
          "startup_install_incomplete with current_path=None, not agent_started.")


def test_maybe_retry_repair_retries_until_success():
    """A degraded current_path=None (from a partial-failure repair, Section
    4.6) must not be a permanent dead end. Found by review: decide_link_event()
    only starts a repair on a "down" event for an edge NOT already in
    down_edges -- but a failed repair's edge is already in down_edges by the
    time execute_repair() runs, so a duplicate transition on the same edge,
    or no further transition at all (the physical link stays down and
    nothing about it changes again), would never re-trigger a repair
    attempt through the event-driven path alone. maybe_retry_repair(),
    called from main()'s periodic poll_interval tick (not the OVSDB event
    branch), is what gives a transient flow-installation failure -- a
    remote OVS instance briefly unreachable, a timeout -- a way to
    eventually succeed once conditions improve, with no new OVSDB event
    ever required. Simulates exactly the scenario found by review: down ->
    repair -> forced install failure -> no new link-state transition ->
    retry on a later tick -> still failing -> retry again -> succeeds."""
    saved_apply_flow = daim_link_agent.apply_flow
    down_edges = {frozenset({"s1", "s2"})}
    try:
        # Simulate the original partial-failure repair that leaves
        # current_path=None (Section 4.6's fix).
        daim_link_agent.apply_flow = lambda action, bridge, arg: not (action == "add" and bridge == "s3")
        decision = {"action": "repair", "new_path": ["s1", "s3", "s4"]}
        current_path, event_name, fields = execute_repair(decision, ["s1", "s2", "s4"])
        assert current_path is None
        assert event_name == "repair_incomplete"

        # No new link-state transition arrives -- decide_link_event() would
        # never re-trigger a repair for this edge (already in down_edges).
        # A later periodic tick (standing in for main()'s poll_interval
        # wake-up) must retry anyway, since the underlying failure is
        # still present.
        current_path, event_name, fields = maybe_retry_repair(current_path, down_edges)
        assert current_path is None, (
            "a retry against the still-failing adapter must stay degraded, "
            "not silently give up or falsely claim success"
        )
        assert event_name == "repair_incomplete"

        # Once the underlying failure clears (e.g. a remote instance
        # becomes reachable again), the very next tick must succeed -- with
        # no new OVSDB event ever having arrived for this edge.
        daim_link_agent.apply_flow = lambda action, bridge, arg: True
        current_path, event_name, fields = maybe_retry_repair(current_path, down_edges)
        assert current_path == ["s1", "s3", "s4"], (
            "a degraded current_path must not be a permanent dead end once "
            "the underlying flow-installation problem clears"
        )
        assert event_name == "repair_installed"

        # Once healthy, further ticks must be no-ops (nothing to retry).
        current_path, event_name, fields = maybe_retry_repair(current_path, down_edges)
        assert event_name is None
        assert current_path == ["s1", "s3", "s4"]

        # If BFS genuinely finds no path at all (not a flow-install
        # problem), retrying must not loop pointlessly -- report a
        # distinct event instead of repeatedly calling execute_repair.
        unreachable_down_edges = {
            frozenset({"s1", "s2"}), frozenset({"s1", "s3"}),
        }
        current_path, event_name, fields = maybe_retry_repair(None, unreachable_down_edges)
        assert current_path is None
        assert event_name == "repair_retry_no_path"
    finally:
        daim_link_agent.apply_flow = saved_apply_flow

    print("daim_link_agent repair-retry liveness regression test: PASS -- "
          "a degraded current_path=None is retried on later ticks until the "
          "underlying flow-installation failure clears, with no new OVSDB "
          "event required to trigger the retry.")


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
    test_execute_repair_reports_partial_failure_honestly()
    test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names()
    test_delete_match_scopes_by_cookie_not_bare_in_port()
    test_execute_startup_install_reports_partial_failure()
    test_maybe_retry_repair_retries_until_success()


if __name__ == "__main__":
    main()
