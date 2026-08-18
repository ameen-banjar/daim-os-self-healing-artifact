#!/usr/bin/env python3
"""Live-network hold-down window-length sensitivity sweep against workload
burstiness. Extends stage3_holddown_flapping.py's protocol (same
DiamondTopo, same net.configLinkStatus("s1","s2",state) mechanism) across
multiple (window_length, flap_schedule) combinations. Originally a single
live repetition per combination (an exploratory pilot); this version adds
`--reps`/`--start-rep` so each combination can be replicated to a
precision-based N derived from that pilot's own variability, using the
same `n_final = ceil((1.96*sd/(0.20*mean))^2)` methodology as the rest of
this evidence set's replicated experiments (see
paper3_service_restoration_statistics.py / STAGE3_FINAL_STATISTICS
material). Rows now carry a `repetition` index and are appended, not
overwritten, so a pilot batch and its extension batches accumulate in the
same CSV.

For each combination, the exact logic-level PREDICTION is computed by
calling decide_link_event() directly (the same trusted, unit-tested pure
function, not a hand-derived guess) against the same schedule and window,
then compared against what the live run actually observed -- extending the
established "logic-level prediction vs. live measurement" comparison
stage3_holddown_flapping.py already uses for the single baseline case.
"""
import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo

ROOT = Path(__file__).resolve().parents[1]
NETWORK_DIR = ROOT / "network"
sys.path.insert(0, str(NETWORK_DIR))
import daim_link_agent  # noqa: E402  (needs sys.path set first)

AGENT = NETWORK_DIR / "daim_link_agent.py"
RAW = ROOT / "results/network/stage3_holddown_sensitivity_raw.csv"
EVENTS_LOG = ROOT / "results/network/stage3_holddown_sensitivity_events.jsonl"

# The original seven-transition schedule already measured at the 2.0s
# baseline (Section 7.3), reused here at other window lengths.
BASELINE_SCHEDULE = [
    (0.0, "down"), (0.1, "up"), (0.2, "down"), (0.5, "up"),
    (1.9, "down"), (2.5, "up"), (2.6, "down"),
]
# A denser, higher-frequency burst: fifteen transitions at a fixed 0.15s
# cadence -- stresses suppression under sustained, rapid flapping rather
# than the original's uneven gaps (up to 1.4s between some transitions).
BURST_SCHEDULE = [
    (round(i * 0.15, 2), "down" if i % 2 == 0 else "up") for i in range(15)
]

WINDOW_LENGTHS = [0.5, 2.0, 4.0]
SCHEDULES = {"baseline_7": BASELINE_SCHEDULE, "burst_15": BURST_SCHEDULE}

ACTION_FOR_EVENT = {
    "repair_installed": "repair",
    "repair_installed_stale_withdraw": "repair",
    "transition_suppressed": "suppressed",
    "link_up_detected": "recovered",
    "repair_failed": "repair_failed",
}


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


LOGGED_ACTIONS = {"repair", "suppressed", "recovered", "repair_failed"}


def predict(schedule, window_seconds):
    """The logic-level prediction for `schedule` at `window_seconds`,
    computed by driving the real decide_link_event() with a synthetic clock
    -- the same trusted function the live agent uses, not a hand-derived
    guess. Mirrors main()'s own post-commit hold-down semantics: a window
    opens only once a "repair" decision is later confirmed committed, which
    at the logic level (no I/O ever fails here) is immediately.

    Critically, `net.configLinkStatus("s1", "s2", state)` changes BOTH of
    the edge's interfaces at once (confirmed empirically -- both s1-eth2's
    and s2-eth1's OVSDB rows update from a single call), so each logical
    schedule transition generates TWO separate decide_link_event() calls in
    the real agent, not one; an earlier version of this function modelled
    only one interface per transition and under-predicted the suppressed
    count by roughly half as a result. `"ignored"`/`"noop"` decisions are
    not logged by main() (Section 4.7), so they are filtered out here to
    match what a live run's log actually contains -- only the four action
    names main() does log."""
    down_edges = set()
    held_down_until, interface_state = {}, {}
    current_path = ["s1", "s2", "s4"]
    edge = frozenset({"s1", "s2"})
    actions = []
    for now, state in schedule:
        for iface in ("s2-eth1", "s1-eth2"):
            decision = daim_link_agent.decide_link_event(
                iface, state, down_edges, held_down_until,
                interface_state, current_path, now,
            )
            if decision["action"] in LOGGED_ACTIONS:
                actions.append(decision["action"])
            if decision["action"] == "repair":
                current_path = decision["new_path"]
                held_down_until[edge] = now + window_seconds
    return actions


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


def run_one(window_seconds, schedule_name, schedule):
    subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(topo=DiamondTopo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    agent = None
    try:
        net.start()
        agent = subprocess.Popen(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(NETWORK_DIR)!r}); "
             f"import daim_link_agent; "
             f"daim_link_agent.HOLD_DOWN_SECONDS = {window_seconds}; "
             f"daim_link_agent.main()"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        events = []
        if not read_agent_events(agent, time.time() + 10, events, stop_event_name="agent_started"):
            raise RuntimeError("agent did not report agent_started within 10s")

        schedule_start = time.perf_counter()
        idx = 0
        span = schedule[-1][0] + max(window_seconds, 1.0) + 1.5
        deadline = time.time() + span
        while time.time() < deadline:
            if idx < len(schedule):
                t_offset, state = schedule[idx]
                if time.perf_counter() - schedule_start >= t_offset:
                    net.configLinkStatus("s1", "s2", state)
                    idx += 1
            ready, _, _ = select.select([agent.stdout], [], [], 0.05)
            if agent.stdout in ready:
                line = agent.stdout.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            if idx >= len(schedule) and time.perf_counter() - schedule_start > span - 0.5:
                break
        read_agent_events(agent, time.time() + 1, events)

        observed_actions = [
            ACTION_FOR_EVENT[e["event"]] for e in events
            if e.get("event") in ACTION_FOR_EVENT
        ]
        predicted_actions = predict(schedule, window_seconds)

        observed_recovered = observed_actions.count("recovered")
        predicted_recovered = predicted_actions.count("recovered")
        # A "spurious recovery" is an observed recovered-edge event beyond
        # what the trusted logic-level prediction says should occur --
        # exactly the class of defect stage3_holddown_flapping.py's
        # cross-interface bug (round 2, v0.3.0) produced (3 spurious
        # recoveries per run instead of 1). Tracked per-repetition here so
        # a regression would show up as a nonzero rate across reps rather
        # than requiring a fresh manual audit each time.
        spurious_recovered = max(0, observed_recovered - predicted_recovered)

        repair_events = [e for e in events if e.get("event") in
                         ("repair_installed", "repair_installed_stale_withdraw")]
        first_repair = repair_events[0] if repair_events else None
        repair_action_us = (
            (first_repair["repair_end_ns"] - first_repair["repair_start_ns"]) / 1000.0
            if first_repair and "repair_start_ns" in first_repair and "repair_end_ns" in first_repair
            else None
        )
        bfs_us = (
            (first_repair["bfs_end_ns"] - first_repair["bfs_start_ns"]) / 1000.0
            if first_repair and "bfs_start_ns" in first_repair and "bfs_end_ns" in first_repair
            else None
        )

        return {
            "window_seconds": window_seconds,
            "schedule": schedule_name,
            "schedule_transitions": len(schedule),
            "processed_transitions": idx,
            "observed_action_sequence": ";".join(observed_actions),
            "predicted_action_sequence": ";".join(predicted_actions),
            "matches_prediction": observed_actions == predicted_actions,
            "observed_suppressed_count": observed_actions.count("suppressed"),
            "predicted_suppressed_count": predicted_actions.count("suppressed"),
            "observed_repair_count": observed_actions.count("repair") + observed_actions.count("repair_failed"),
            "predicted_repair_count": predicted_actions.count("repair") + predicted_actions.count("repair_failed"),
            "observed_recovered_count": observed_recovered,
            "predicted_recovered_count": predicted_recovered,
            "spurious_recovered_count": spurious_recovered,
            "repair_action_us": repair_action_us,
            "bfs_us": bfs_us,
        }, events
    finally:
        if agent and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
        net.stop()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


FIELDNAMES = [
    "repetition", "window_seconds", "schedule", "schedule_transitions",
    "processed_transitions", "observed_action_sequence", "predicted_action_sequence",
    "matches_prediction", "observed_suppressed_count", "predicted_suppressed_count",
    "observed_repair_count", "predicted_repair_count", "observed_recovered_count",
    "predicted_recovered_count", "spurious_recovered_count", "repair_action_us", "bfs_us",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=float, default=None,
                         help="restrict to one window length (default: all of WINDOW_LENGTHS)")
    parser.add_argument("--schedule", choices=list(SCHEDULES), default=None,
                         help="restrict to one schedule (default: all of SCHEDULES)")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--start-rep", type=int, default=1)
    args = parser.parse_args()

    windows = [args.window] if args.window is not None else WINDOW_LENGTHS
    schedules = {args.schedule: SCHEDULES[args.schedule]} if args.schedule else SCHEDULES

    setLogLevel("warning")
    rows = []
    all_events = []
    for window_seconds in windows:
        for schedule_name, schedule in schedules.items():
            for rep in range(args.start_rep, args.start_rep + args.reps):
                print(f"stage3-holddown-sensitivity rep={rep} window={window_seconds} schedule={schedule_name}", flush=True)
                row, events = run_one(window_seconds, schedule_name, schedule)
                row = {"repetition": rep, **row}
                print(json.dumps(row), flush=True)
                rows.append(row)
                all_events.append({"repetition": rep, "window_seconds": window_seconds,
                                    "schedule": schedule_name, "events": events})

    RAW.parent.mkdir(parents=True, exist_ok=True)
    import csv
    write_header = not RAW.exists()
    with RAW.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    print(f"appended {len(rows)} rows to {RAW}")

    with EVENTS_LOG.open("a") as handle:
        for rep in all_events:
            handle.write(json.dumps(rep) + "\n")
    print(f"appended event logs to {EVENTS_LOG}")

    matches = sum(1 for r in rows if r["matches_prediction"])
    print(f"combinations matching logic-level prediction: {matches}/{len(rows)}")
    for r in rows:
        print(f"  window={r['window_seconds']}s schedule={r['schedule']}: "
              f"observed={r['observed_action_sequence']} matches={r['matches_prediction']}")


if __name__ == "__main__":
    main()
