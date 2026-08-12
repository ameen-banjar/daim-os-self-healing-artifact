#!/usr/bin/env python3
"""Pure-logic unit tests for daim_link_agent, independent of Mininet/OVS/OVSDB.

Six things are checked without a live network:
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
   is reconciled exactly once, not once per interface."""
from daim_link_agent import (
    MONITORED_INTERFACES,
    bfs_path,
    decide_link_event,
    path_to_flows,
    reconcile_expired_holddowns,
    SOURCE,
    DEST,
)

EXPECTED_PRIMARY = {
    ("s1", "priority=100,in_port=1,actions=output:2"),
    ("s1", "priority=100,in_port=2,actions=output:1"),
    ("s2", "priority=100,in_port=1,actions=output:2"),
    ("s2", "priority=100,in_port=2,actions=output:1"),
    ("s4", "priority=100,in_port=1,actions=output:3"),
    ("s4", "priority=100,in_port=3,actions=output:1"),
}

EXPECTED_ALTERNATE = {
    ("s1", "priority=100,in_port=1,actions=output:3"),
    ("s1", "priority=100,in_port=3,actions=output:1"),
    ("s3", "priority=100,in_port=1,actions=output:2"),
    ("s3", "priority=100,in_port=2,actions=output:1"),
    ("s4", "priority=100,in_port=2,actions=output:3"),
    ("s4", "priority=100,in_port=3,actions=output:2"),
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


def main():
    test_bfs_path_computation()
    test_holddown_suppresses_flapping()
    test_holddown_stale_state_is_reconciled()
    test_holddown_covers_both_interfaces_on_same_edge()
    test_edge_recovery_requires_all_interfaces_confirmed_up()
    test_edge_recovers_once_both_interfaces_confirm_up()


if __name__ == "__main__":
    main()
