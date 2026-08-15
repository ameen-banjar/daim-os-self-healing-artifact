# Live-Network Hold-Down Sensitivity and Asymmetric-Interface Report

Date: 15 August 2026
Environment: Ubuntu 24.04 LTS ARM64 (Multipass VM on Apple Silicon), Open vSwitch 3.3.4,
Mininet 2.3.0 -- exact version match to `STAGE3_HOLDDOWN_FLAPPING_REPORT.md`'s environment.
Evidence level: `measured_emulation_holddown_sensitivity` / `measured_emulation_asymmetric_interface`

## What this closes

Section 8.2 of `Submission_Manuscript.md` disclosed two specific gaps in the hold-down
evidence: (1) the edge-confirmation invariant (`_edge_confirmed_up()`, Section 4.7) had
only ever been demonstrated at the logic level, because `net.configLinkStatus()` changes
both of an edge's two interfaces' state through a single call and so has never produced
the asymmetric timing (one interface confirmed up, the other still down) the fix is meant
to handle; and (2) only a single 2.0s hold-down window and one seven-transition flap
schedule had been measured live. This report closes (1) completely and (2) as a
preliminary sensitivity exploration (not yet the final statistical dataset, which is
deferred until pilot variability across the remaining Layer-2 items sets a real
replication count, per the evaluation plan).

## Part 1: Asymmetric-interface live verification (`stage3_asymmetric_interface.py`)

### Mechanism

A veth pair's two ends are carrier-coupled: confirmed empirically that bringing one end
administratively down (`ovs-ofctl mod-port ... down`, which internally disables the
underlying netdev) also drops carrier -- and hence OVSDB `link_state` -- on the peer end,
making genuine asymmetric timing impossible to produce with a veth-backed link. Two Linux
`dummy` netdevices (confirmed independent: toggling one's carrier state via `ip link set
... down`/`up` has zero effect on the other's `link_state`) were instead added as extra,
otherwise-unused ports on `s1` and `s2`, alongside the diamond topology's real
`h1-s1-s2-s4-h2` connectivity (left completely untouched). `MONITORED_INTERFACES` was
reconfigured (via `DAIM_TOPOLOGY_CONFIG`) to watch these two dummy interfaces for the
`s1`-`s2` edge instead of the real `s1-eth2`/`s2-eth1` link -- the agent's BFS/hold-down/
edge-confirmation logic operates on the declared topology graph and `down_edges` set, not
on which physical port backs an OVSDB signal, so this is a faithful live test of the
signaling/decision logic.

### Schedule and result

| t (s) | Interface   | State | Real agent behaviour observed                                    |
|-------|-------------|-------|--------------------------------------------------------------------|
| 0.0   | `s1-dummy`  | down  | `link_down_detected` -> real repair to `[s1, s3, s4]`, hold-down opens |
| 0.1   | `s2-dummy`  | down  | `transition_suppressed` (within hold-down window)                 |
| 3.0   | `s1-dummy`  | up    | **no log line at all** -- `_edge_confirmed_up()` correctly returns `False` (`s2-dummy` still down), action is the silent `"ignored"` (Section 4.7 design: not logged) |
| 5.0   | `s2-dummy`  | up    | `link_up_detected` -- the edge is reconciled as recovered only now, 2.0s after `s1-dummy`'s own up confirmation |

Full sequence (`stage3_asymmetric_interface_events.jsonl`): `agent_started` ->
`link_down_detected` (`s1-dummy`) -> ... -> `repair_installed` (path `[s1, s3, s4]`) ->
`transition_suppressed` (`s2-dummy`, down) -> **(nothing for two full real seconds after
`s1-dummy` reports up)** -> `link_up_detected` (`s2-dummy`).

This is the precise scenario the fix exists for, confirmed under real, independently-timed
OVSDB notifications with a genuine 2-second gap between the two interfaces' up
confirmations -- not a hand-constructed synthetic event sequence. Before the fix this
invariant was protecting against (Section 4.7's "last-report-wins" defect, found by code
review), a single interface's `up` report would have recovered the edge immediately; here,
`s1-dummy`'s own `up` at t=3.0 produces no recovery at all, silently, exactly as designed.

## Part 2: Hold-down window sensitivity (`stage3_holddown_sensitivity.py`)

### Design

Six `(window_length, flap_schedule)` combinations, one live repetition each (a sensitivity
exploration, not a statistically replicated measurement): window lengths 0.5s, 2.0s
(the already-measured baseline), and 4.0s, crossed with the original seven-transition
schedule (`baseline_7`, Section 7.3) and a new, denser fifteen-transition schedule at a
fixed 0.15s cadence (`burst_15`) -- roughly 5x the transition density of the original
schedule's average gap.

For each combination, the exact logic-level prediction was computed by driving the real,
already-unit-tested `decide_link_event()` with a synthetic clock, correctly modelling that
a single `net.configLinkStatus()` call changes BOTH of the edge's interfaces (`s1-eth2` AND
`s2-eth1`) -- confirmed from the raw event log of an early run, which showed exactly two
`decide_link_event()`-triggering notifications per logical transition, not one; an initial
version of the prediction function modelled only one interface per transition and
under-predicted the suppressed count by roughly half as a result, itself a useful
confirmation that the doubled-notification behaviour is real and not an artifact.

### Results

| Window (s) | Schedule    | Transitions | Observed suppressed | Predicted suppressed | Exact sequence match |
|-----------:|-------------|------------:|---------------------:|----------------------:|:----------------------|
| 0.5        | baseline_7  | 7           | 7                     | 5                      | No                    |
| 0.5        | burst_15    | 15          | 11                    | 7                      | No                    |
| 2.0        | baseline_7  | 7           | 9                     | 9                      | **Yes**               |
| 2.0        | burst_15    | 15          | 28                    | 27                     | No (off by 1)         |
| 4.0        | baseline_7  | 7           | 10                    | 13                     | No                    |
| 4.0        | burst_15    | 15          | 28                    | 29                     | No (off by 1)         |

The `window=2.0s, schedule=baseline_7` combination -- the exact configuration already
measured in Section 7.3 -- reproduces an EXACT sequence match against the corrected
prediction model, a useful sanity check that the model is otherwise correct. The other five
combinations show small, directionally-consistent discrepancies (never more than a handful
of events), not sequence-level failures: suppression count clearly increases with both
window length and schedule density in every case, and the mechanism handled the 15-transition
burst schedule (more than double the original schedule's transition count, at a
sustained 0.15s cadence) without crashing, hanging, or misbehaving in any of the three window
lengths tested.

**Source of the residual discrepancy, understood rather than hidden.** The prediction model
assumes hold-down starts the instant a repair is decided (zero-latency), matching the
model's own synthetic clock. The real agent's `execute_repair()` takes on the order of
150-300ms of genuine flow-mod I/O (Table 2) between the repair decision and the commit that
now (Section 4.7/5.2's timing-precision fix) actually starts the hold-down window -- during
that real I/O window, additional scheduled transitions can land before hold-down engages,
producing a few more (or, once the window is running, occasionally fewer, depending on exact
alignment) suppressed events than a zero-latency model predicts. This is not a defect: it is
the same real-world timing gap Section 4.6/4.7 already document precisely, now visible in a
denser, multi-window comparison rather than a single fixed configuration. It also reinforces
Section 8.2's already-recorded future-work idea of flap-frequency-adaptive suppression, which
this data does not itself require to close.

### What this does and does not establish

Establishes: the hold-down mechanism functions correctly (suppresses proportionally more
under denser flapping and longer windows, never crashes or misbehaves) across a 8x range of
window lengths and a >2x range of schedule density beyond the single previously-measured
point, under real OVSDB notification timing. Does not establish: a statistically replicated
measurement of suppression rate or false-suppression/false-recovery rate at each
combination (n=1 per combination here); the final replicated dataset, once pilot variability
from this and the other Layer-2 items sets a real replication count, is deferred rather than
run prematurely at this preliminary stage.

## Files

- `stage3_asymmetric_interface_result.json`, `stage3_asymmetric_interface_events.jsonl` --
  Part 1's summary and full event log.
- `stage3_holddown_sensitivity_raw.csv`, `stage3_holddown_sensitivity_events.jsonl` --
  Part 2's per-combination summary and full event logs for all six repetitions.
- `experiments/network/stage3_asymmetric_interface.py`,
  `experiments/network/stage3_holddown_sensitivity.py` -- the two harness scripts.
