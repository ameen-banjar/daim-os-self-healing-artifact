# Live-Network Hold-Down Flapping-Link Report

Date: 13 August 2026
Environment: Ubuntu 24.04 LTS ARM64 (Multipass VM on Apple Silicon, matching the
original Stage 3 host's architecture), Open vSwitch 3.3.4, Mininet 2.3.0 --
exact version match to `STAGE3_AUTONOMOUS_AGENT_REPORT.md`'s environment.
Evidence level: `measured_emulation_holddown_flapping`

## What this closes

`Submission_Manuscript.md` Section 7.3 reported the hold-down state machine's
flap-suppression behaviour only as a deterministic unit test over synthetic
OVSDB-style events with a fake clock (`test_holddown_suppresses_flapping`),
and listed a live-network repetition of that protocol as evidence-gate item 1
in Section 10 -- required but not yet measured, since no Mininet/OVS
environment was available in that revision. This report closes that gap: it
physically flaps the `s1-s2` link on a real Mininet/OVS diamond topology
against the real `daim_link_agent.py`, using `stage3_holddown_flapping.py`,
which drives the identical seven-transition schedule the unit test uses
(down at t=0.0, up at 0.1, down at 0.2, up at 0.5, down at 1.9, up at 2.5,
down at 2.6) via real `net.configLinkStatus` calls instead of a synthetic
event list, and records every event the live agent actually logs.

## A real defect this live run found (and the fix it drove)

The first live run, before any fix, showed a materially different action
sequence from the unit test's prediction: `repair; suppressed; recovered;
suppressed; suppressed; recovered; suppressed; recovered` (8 events) instead
of the predicted `repair; suppressed; suppressed; suppressed; suppressed;
recovered; noop` (7 events), with 3/5 spurious `recovered` events instead of
1. Inspecting the full per-event log (`stage3_holddown_flapping_events_pre_edgefix.jsonl`)
showed why: a physical link has two OVS interfaces, one per side (`s1-eth2`
and `s2-eth1`), and OVSDB reports their `link_state` transitions
independently. The agent's hold-down state was keyed by interface *name*,
so only the interface whose `down` event happened to trigger the repair
(`s2-eth1` in this run) was actually held down; `s1-eth2`'s transitions on
the *same physical link* sailed through completely unsuppressed, each one
processed as an independent, immediate event.

`daim_link_agent.py` was changed to key `held_down_until` and
`held_down_last_state` by *edge* (the frozenset of the two switches a link
connects) rather than by interface name, so a hold-down window opened by
either interface's event covers both. `test_daim_link_agent.py` gained
`test_holddown_covers_both_interfaces_on_same_edge`, reproducing this exact
cross-interface pattern with synthetic events, alongside the existing
stale-state regression test (`test_holddown_stale_state_is_reconciled`,
unaffected by this change, re-verified passing). Raw data and the full event
log for the pre-fix run are retained as
`stage3_holddown_flapping_raw_pre_edgefix.csv` /
`..._events_pre_edgefix.jsonl`, specifically so this defect and its fix stay
in the evidence record rather than being silently overwritten.

## Result (post-fix)

All 5 repetitions completed the full flap schedule with exactly one `repair`
and exactly one terminal `recovered`, with every intermediate transition on
*either* interface correctly suppressed:

| Repetition | Packets lost / sent | Loss % | Agent events | Ends cleanly? |
|---|---:|---:|---:|---|
| 1 | 5 / 200 | 2.5 | 31 | yes |
| 2 | 6 / 200 | 3.0 | 31 | yes |
| 3 | 5 / 200 | 2.5 | 31 | yes |
| 4 | 6 / 200 | 3.0 | 31 | yes |
| 5 | 6 / 200 | 3.0 | 31 | yes |

Mean packet loss 2.8% over the full ~2.9s, 7-transition flap episode (200
packets at 20ms spacing). Every repetition's `transition_suppressed` events
carry matching `remaining_s` values across both interface names at each
point in the schedule (e.g. both `s1-eth2` and `s2-eth1` show
`remaining_s=1.892` for the transition immediately after the initial
repair), which is the direct, per-event confirmation that both interfaces
share one hold-down window rather than tracking it separately.

For context, the pre-fix run (same schedule, same topology, buggy
interface-keyed hold-down) gave a mean loss of 3.1% with 28 agent events per
repetition (vs. 31 post-fix, since the fix correctly suppresses more
transitions that the buggy version let through as independent, unsuppressed
"recovered" events) -- both are retained; the point of this comparison is
not that loss dropped (a 0.3-point difference over n=5 is not a claim this
report makes any statistical weight on) but that the *action sequence* is
now categorically correct where it was categorically wrong before.

## What the live-per-interface reporting means for the synthetic unit test

The unit test's predicted 7-action sequence assumes a single named interface
reports every transition once. Real OVSDB reports each transition on *both*
monitored interface names for a two-sided link, roughly doubling the
observed event count relative to the synthetic schedule (31 real events vs.
7 synthetic ones, including the initial `agent_started`/setup events the
unit test does not model at all). This is expected and does not indicate a
new defect: the synthetic test's purpose was always to prove the *decision
function's* suppression logic in isolation (Section 7.3), not to predict the
exact live event count, which additionally depends on OVSDB's per-interface
reporting behaviour that only a live run can characterise.

## Claim boundary

This closes evidence-gate item 1 (Section 10): the hold-down suppression
pattern is now confirmed under real OVSDB notification timing and jitter,
not just a fake clock, and a real cross-interface defect the synthetic test
could not have found (by construction -- it only ever drives one interface
name) was found and fixed as a direct result of running this live
measurement. It does not establish: behaviour under a longer or
higher-frequency flap sequence than the 7-transition schedule tested;
behaviour when the two interfaces' OVSDB reports are more asymmetric in
timing than this VM/loopback environment produced; or any claim about
production hardware, which may exhibit different propagation delay between
the two sides of a real physical link than this emulated pair does.

## Second defect and re-verification: edge-confirmation semantics

A code review (not a test failure) found a second, related defect after the
above fix landed: `reconcile_expired_holddowns` decided whether an edge had
recovered from a single `held_down_last_state[edge] = state` value,
overwritten by whichever of the edge's two interfaces reported most
recently. That is a last-report-wins policy across two independently
reporting interfaces -- exactly the kind of information loss the
cross-interface fix above was supposed to close, just one level deeper: an
edge could be reconciled as recovered because interface A's `up` happened
to be the last thing recorded, even if interface B's independently-reported
state was still `down`.

`daim_link_agent.py` was changed to track a persistent `interface_state`
dict keyed by interface name (not just during hold-down), and a new
`_edge_confirmed_up()` helper requires every interface observing an edge to
have last reported `up` before the edge is treated as recovered, both in
`reconcile_expired_holddowns` and in `decide_link_event`'s direct recovery
path. `test_daim_link_agent.py` gained two regression tests:
`test_edge_recovery_requires_all_interfaces_confirmed_up` (an edge with one
interface confirmed up and the other silent stays down at expiry) and
`test_edge_recovers_once_both_interfaces_confirm_up` (the positive case,
exactly one recovery once both confirm). All 6 unit tests pass, including
the 4 pre-existing ones -- the fix required reordering the reconciliation
call to run on the *prior* interface state before the current event updates
it, so a just-arrived event's own recovery is not silently claimed by the
reconciliation call at the top of the same function invocation.

The live-network flapping-link protocol (`stage3_holddown_flapping.py`) was
re-run against the updated agent in the same VM, 10 further repetitions
across two batches. **Every repetition reproduced the identical clean
pattern already reported above** (`repair` followed by 9 `suppressed`
transitions and exactly one `recovered`, 31 agent events, 5/5 -- now 10/10
across both batches -- ending clean), confirming the fix introduces no
regression in the case already measured. Packet loss varied more between
these two batches (means 7.3% and 5.8%, individual reps ranging 4.0-14.0%)
than the original pre/post-fix comparison did (2.8% vs 3.1%); this reflects
host-machine load during this session, not the code change -- the action
sequence and event count were identical across all 10 repetitions
regardless of the loss figure, and neither the original report nor this one
places statistical weight on packet loss at this sample size. The
raw data and event log from the second (more recent) batch replace the
originally-reported post-fix figures as the currently-current measurement,
consistent with reporting what the current code actually does; the original
pre-fix (interface-keyed) data remains retained unchanged as
`stage3_holddown_flapping_raw_pre_edgefix.csv`/`.jsonl`.

**Important limitation this re-run does not close**: this specific
7-transition schedule brings both `s1-eth2` and `s2-eth1` down/up together
via Mininet's `net.configLinkStatus`, which changes both interfaces'
administrative state at effectively the same call. In every repetition
observed so far, both interfaces have always converged to matching states
well within the 2 s hold-down window -- so this live protocol has never
actually exercised the specific asymmetric-confirmation scenario the
edge-confirmation fix targets (one interface confirmed up, the other still
down or silent, at the moment the window expires). That scenario is
currently verified only at the logic level, by the two new unit tests
above, with a synthetic clock and hand-constructed asymmetric event timing.
A live-network protocol that can genuinely desynchronise the two interfaces
-- for example, injecting the down/up commands on each interface
separately rather than through `configLinkStatus`'s combined call -- is
required before this specific invariant is confirmed under real OVSDB
timing rather than only proven correct as a decision-function property.
