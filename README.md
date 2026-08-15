# DAIM-OS Self-Healing Artifact

Reproducibility artifact for the paper:

**"Event-Driven Link-Failure Recovery for a DAIM-OS Client Using OVSDB
Notifications and Reactive Path Recomputation"** — Ameen Banjar (2026).
Paper 3 in the DAIM six-paper programme. (Retitled from an earlier, broader
"Self-Healing DAIM-OS" framing after external review noted the agent does
not read or write DAIM-OS core tables/signals directly — see the
manuscript's Section 4.1 for the actual DAIM-OS integration boundary.)

**Status: this artifact accompanies a prototype-stage, not-yet-submitted
manuscript.** Per the author's plan, submission of Paper 3 is held until
Papers 1 and 2 of this programme reach an editorial decision; this repository
is published now so the code, data, and figures already produced are citable
and reviewable in the meantime. See `Submission_Manuscript.md` in the paper
directory of the main repository for the full, explicit evidence-gate plan
(Section 10) of what is and is not yet measured.

This repository contains only the code, raw data, logs, and figures cited by
that paper. It is deliberately scoped to that single paper: the author's
related work on the base DAIM-OS execution contract and its distributed
deployment are separate, independent contributions with their own artifacts,
linked below.

## Relationship to Papers 1 and 2 and the DAIM-OS specification

This artifact **extends, without modifying**, the OVS adapter (`daim_ovs_flow`)
reconstructed by Paper 1 of this programme:

- Paper 1 artifact: https://github.com/ameen-banjar/daim-os-packetin-artifact
  (DOI: https://doi.org/10.5281/zenodo.21855229)
- Paper 1 manuscript: "Reconstructing the DAIM-OS Table-and-Signal Control
  Path" — under review, *Computer Networks*, manuscript COMNET-S-26-07153.
- Paper 2 artifact: https://github.com/ameen-banjar/daim-os-distributed-execution
  (DOI: https://doi.org/10.5281/zenodo.21878194)
- Paper 2 manuscript: "Distributing the DAIM-OS Table-and-Signal Contract" —
  in preparation for *Computer Networks*, submission held pending Paper 1's
  decision.
- DAIM-OS specification: https://github.com/ameen-banjar/DAIM-OS
  (DOI: https://doi.org/10.5281/zenodo.21426560)

`implementation/` is a snapshot of the same shared build tree used by Paper 2's
artifact, included here so this repository builds standalone. Paper 3's own
code only calls `implementation/build/daim_ovs_flow` (built from
`implementation/tools/daim_ovs_flow.c`); the distributed-state/peer-protocol
modules also present in that tree belong to Paper 2, not this paper, and are
included only because they live in the same build tree, not because Paper 3
uses them.

## What is new here

- `network/daim_link_agent.py` — the event-driven self-healing agent: an
  OVSDB watcher (`ovsdb-client monitor`), a BFS path-recomputation engine, and
  an IDLE/ACTIVE/HELD-DOWN hold-down state machine, all decided by one pure
  function (`decide_link_event`) with no I/O, called from an outer loop that
  performs the actual OVSDB/adapter I/O. Hold-down state is keyed by physical
  edge (the frozenset of the two switches a link connects), not by interface
  name, since v0.3.0 (see below).
- `network/test_daim_link_agent.py` — thirty-three pure-logic unit tests, runnable
  with plain `python3` and no Mininet/OVS/OVSDB: (1) BFS path computation
  against hand-verified expected flow sets; (2) the hold-down state machine
  driven through a synthetic seven-transition flapping-link sequence, with
  and without the hold-down window, confirming it suppresses 4 of 6
  subsequent transitions before any BFS call; (3) a stale-state regression
  test; (4) a cross-interface regression test reproducing the v0.3.0 defect
  described below; (5)-(6) two edge-confirmation regression tests
  reproducing the v0.4.0 defect described below; (7)-(9) three
  startup-synchronization regression tests reproducing the v0.5.0 defect
  described below; (10) a startup-validation robustness test confirming an
  unrecognised `link_state` value is rejected rather than silently treated
  as "up" (v0.6.0); (11) the same check's runtime counterpart, confirming
  `decide_link_event()` rejects an unrecognised `link_state` in the
  ongoing event stream instead of falling through to "ignored" (v0.7.0);
  (12) confirming an empty `REMOTE_ENDPOINTS` resolves to exactly one local
  monitor target, matching every single-host experiment; (13) confirming a
  non-empty `REMOTE_ENDPOINTS` routes each interface to its own switch's
  endpoint with connections deduplicated per distinct target (v0.8.0);
  (14) confirming `apply_flow()` routes both local and remote bridges
  through the same `daim_ovs_flow` adapter binary; (15) confirming a
  partially-failed repair is reported as `repair_incomplete` with
  `current_path` set to `None` (forwarding state no longer reliably known,
  not a stale prior value) and that a degraded `None` current_path does not
  crash a subsequent repair attempt (v0.9.0/v0.10.0); (16) confirming
  `bfs_path()`/`path_to_flows()` resolve the declared `SOURCE`/`DEST`
  rather than the hardcoded literal names `"h1"`/`"h2"` (v0.10.0); (17)
  confirming `withdraw_path()` deletes by an `AGENT_COOKIE` mask plus
  `in_port`, not a bare `in_port` match that non-strict `del-flows` would
  otherwise match far too broadly; (18) confirming a partially-failed
  startup installation is reported as `startup_install_incomplete` with
  `current_path=None`, not a silent `agent_started`; (19) confirming a
  degraded `current_path=None` is retried on later ticks until the
  underlying failure clears, with no new OVSDB event required (v0.11.0,
  see below); (20) confirming `_agent_cookie()` is deterministic across
  calls for the same `(SOURCE, DEST)` pair and distinct for a different
  pair, replacing the single fixed `AGENT_COOKIE` constant items 1-19
  above still describe; (21) confirming `resync_from_reconnect()`
  reconciles state from a fresh post-reconnect snapshot without touching
  hold-down timers; (22) confirming `monitor_link_rows()` attempts one
  reconnect on a dead monitor stream and yields a distinct failure event
  rather than crashing or hanging (v0.12.0, see below); (23)-(24) two
  tests for the forwarding-consistency pre-check
  (`_conflicting_flow_cookie()`, `install_path()`'s rejection of a
  foreign-cookie conflict); (25) documenting the boundary-hop flow-match
  collision between alternate paths that items 26-27 handle; (26)-(27)
  two tests for the two-phase rollback protocol's commit
  (`_withdraw_stale_path()`) and rollback (`_rollback_staged_path()`)
  steps, confirming a boundary-hop collision is skipped (commit) or
  restored via `add-flow` (rollback) rather than deleted outright
  (v0.12.0); (28) confirming rollback never acts on a match this attempt
  never actually staged (e.g. rejected by the forwarding-consistency
  check), even if it collides with `old_path`, so rollback cannot
  overwrite a foreign flow that check just protected; (29) confirming
  `install_path()` stages boundary-hop colliding flows LAST, after every
  purely-additive flow has already succeeded, so `old_path` stays fully
  live and correct for as long as possible during staging (v0.13.0);
  (30) confirming `apply_flow()` resolves a timed-out add/delete by
  reading the switch's actual state back directly, rather than treating
  the timeout as a confirmed failure; (31) confirming
  `_apply_flow_or_fail_safe()` converts an unresolved ambiguous outcome
  into a plain failure rather than an uncaught exception (v0.14.0); (32)
  confirming `decide_link_event()`'s `"repair"`/`"repair_failed"`
  decisions include `bfs_start_ns`/`bfs_end_ns` bracketing the real BFS
  call; (33) confirming `execute_repair()`'s returned fields include
  `stage_*_ns`/`commit_*_ns` bracketing their own phases in every
  outcome, with the decision's own BFS timing copied through when present
  (v0.15.0, see below).
- `network/test_stage3_startup_already_down.py` — a regression test for a
  bug in the startup live-network harness itself (v0.6.0, see below), kept
  separate from `test_daim_link_agent.py` since it tests the harness, not
  the agent.
- `network/stage3_autonomous_agent.py`, `stage3_link_recovery.py`,
  `stage3_controller_switch_faults.py`, `stage3_holddown_flapping.py`,
  `stage3_startup_already_down.py` — the Mininet/OVS network-level test
  harnesses that produced the raw data in `results/network/` (require a
  Linux host with Open vSwitch and Mininet installed; run in a Multipass
  Ubuntu 24.04 ARM64 VM for this revision, matching the original Stage 3
  environment's exact OVS/Mininet versions).
- `network/vm1_topo.py`, `network/vm2_topo.py` — the two-VM Mininet topology
  halves used for the multi-OVS deployment measurement (v0.8.0, see below):
  VM1 runs `h1`-`s1`, VM2 runs `s3`-`s4`-`h2` (primary) and `s3`-`s5`-`s4`
  (alternate).
- `network/stage3_multi_ovs_setup.sh` — one-time GRE tunnel and remote
  OVSDB/OpenFlow listener setup between the two VMs.
- `network/stage3_multi_ovs_deployment.sh` — orchestrates n=5 repetitions of
  the remote-edge-failure protocol across both VMs.
- `network/multi_ovs_topology.json` — the topology/host-attachment/
  monitored-interfaces/`REMOTE_ENDPOINTS` declaration for this deployment,
  loaded via `daim_link_agent.py`'s `DAIM_TOPOLOGY_CONFIG` environment
  variable.
- `results/network/` — raw CSVs, event logs, and reports from the
  network-level measurements (`stage3_autonomous_agent_raw.csv`: 5
  repetitions of autonomous link-failure detection and repair;
  `stage3_controller_switch_faults_raw.csv`: scripted, non-autonomous
  switch/controller-failure measurements; `stage3_holddown_flapping_raw.csv`
  and `..._events.jsonl`: repetitions of the live flapping-link protocol,
  post-fix, with the pre-fix run retained alongside as
  `..._pre_edgefix.csv`/`.jsonl` so the defect stays in the evidence record;
  `stage3_startup_already_down_result.json` and `..._pre_fix.json`: the
  before/after startup-synchronization scenario; `stage3_multi_ovs_raw.csv`,
  `stage3_multi_ovs_agent_rep{1..5}.log`, `stage3_multi_ovs_ping_rep{1..5}.log`:
  5 repetitions of the multi-OVS remote-edge-failure protocol;
  `STAGE3_HOLDDOWN_FLAPPING_REPORT.md`, `STAGE3_STARTUP_ALREADY_DOWN_REPORT.md`,
  and `STAGE3_MULTI_OVS_REPORT.md` document what each measurement found and
  fixed).
- `results/paper3/` — the eight figures used in the manuscript, generated by
  `analysis/paper3_analysis.py` from the real raw data and the real output of
  the hold-down unit test (nothing in these figures is hand-drawn or
  invented; re-run the script to regenerate them from source).

## Service-restoration decomposition instrumentation (v0.15.0)

The first step of this evidence set's move from correctness-debugging (Layer 1, v0.3.0-v0.14.0) to
evaluation breadth (Layer 2): with the reviewer's own explicit reasoning that this instrumentation
should land BEFORE any further live experiments, so the eventual final timing dataset is collected
once against fully-instrumented code rather than repeated after every addition.

Every repair-related log line now carries a full phase breakdown instead of one opaque start/end
span. Whichever function computes the BFS path -- `decide_link_event()`, `maybe_retry_repair()`, or
`main()`'s post-reconnect resync branch -- now brackets its own `bfs_path()` call with
`bfs_start_ns`/`bfs_end_ns` and passes the pair through in the repair decision it constructs.
`execute_repair()` copies that pair into its own returned event fields unchanged when present
(simply omitted, not crashing, when a decision has none), alongside two new phase pairs of its own:
`stage_start_ns`/`stage_end_ns` bracketing `install_path()`, and `commit_start_ns`/`commit_end_ns`
bracketing `_withdraw_stale_path()` (present only in outcomes where a commit actually runs). The
pre-existing `repair_start_ns`/`repair_end_ns` fields keep their exact prior meaning -- identical to
`stage_start_ns`, and the overall end of the whole operation, respectively -- so no previously
reported timing number's definition changes; this only makes phases that were always implicitly
present separately observable.

Live-verified against the multi-OVS testbed: a real fault injection produced a `repair_installed` log
line with an internally consistent BFS/stage/commit breakdown --

```
{"event": "repair_installed", "path": ["s3", "s5", "s4"],
 "repair_start_ns": ..., "repair_end_ns": ...,
 "stage_start_ns": ..., "stage_end_ns": ...,
 "commit_start_ns": ..., "commit_end_ns": ...,
 "bfs_start_ns": ..., "bfs_end_ns": ..., "held_down_seconds": 2.0}
```

-- with BFS on the order of 10 microseconds for the tested topology, well under the <0.3 ms bound
already cited for a 200-node graph, confirming rather than revising that earlier estimate.

This closes the instrumentation half of the service-restoration-decomposition evidence-gate item, not
the dataset half: replacing the single repair-action figure/table with the decomposed timeline this
now makes possible, and correlating it against the first successful post-failure packet observed
independently by the probe traffic, remain open -- the latter needs the experiment harness scripts
themselves updated to capture per-packet probe timestamps and a shared clock reference against the
agent's own `perf_counter_ns()` timeline, not yet done.

New unit tests: `decide_link_event()`'s BFS timing on both a successful repair decision and a
`repair_failed` decision; `execute_repair()`'s phase-bracketing across every outcome (clean success,
staging failure, commit failure), including that a decision with no BFS timing at all does not crash
and simply omits those keys. 33/33 tests pass (2 new).

## Ambiguous Flow-Mod outcomes resolved by read-back (v0.14.0)

A seventh external review pass -- reading v0.13.0's own `install_path()`/`apply_flow()` contract
again, not new functionality -- found one more real gap: `apply_flow()` treated a `subprocess`
timeout as a confirmed failure, exactly like a non-zero exit. That equivalence is wrong: a non-zero
exit means the adapter/`ovs-ofctl` itself reported the operation failed, before or without applying
anything; a timeout means the client gave up waiting, which is not proof the switch never received
or applied the operation. Left unresolved, this understated what `install_path()`'s `staged` list
(v0.13.0) actually knows -- a flow whose add call timed out but which the switch had, in fact,
already applied would be wrongly excluded from `staged`, and a later rollback would then never touch
it, believing it was never staged, while it might actually still be sitting on the switch carrying
the new path's action.

Fixed: on a timeout, `apply_flow()` now reads the switch's actual state back directly
(`_add_confirmed_by_readback()`/`_delete_confirmed_by_readback()`, a real `ovs-ofctl dump-flows`
query, outside the `daim_ovs_flow` adapter -- a read, not an install, the same reasoning Section
5.1's pre-install conflict check already uses). It returns `True` only if the read-back confirms the
intended add/delete outcome was actually reached, `False` only if the read-back confirms it was not,
and raises `_ForwardingCheckError` -- not a silent guess either way -- if even the read-back itself
cannot determine the answer. Every caller (`install_path()`, `withdraw_path()`,
`_withdraw_stale_path()`, `_rollback_staged_flows()`) now goes through a new
`_apply_flow_or_fail_safe()` wrapper, which converts that unresolved case into a plain failure rather
than propagating an exception each caller would otherwise need to handle individually. Also
refactored: `_conflicting_flow_cookie()`'s `dump-flows` query logic is now shared with the new
read-back functions via `_dump_flows_for_match()`, removing duplicated timeout/error handling.

This closes the gap for the one ambiguous outcome this deployment can actually produce and detect (a
`subprocess` timeout against a real target); it is not claimed as safety under every conceivable
ambiguous network condition. Verified with unit tests mocking a real `subprocess.TimeoutExpired`
followed by a controlled read-back response for both add and delete, in every combination (confirmed
present, confirmed absent, read-back itself fails); live-verified only as a regression check that the
ordinary, non-timeout repair path is unaffected by this restructuring, since a real 5-second
`subprocess` timeout was never reached in any repetition measured for this evidence set -- the
timeout-handling branch itself is exercised by the unit tests, not by a live timeout ever actually
occurring.

**A note on this round's manuscript changes, not just the code:** the paper's own reported
repair-action timings (single-host and multi-OVS) were measured against earlier implementations, not
against this version's stage-then-commit, forwarding-consistency, cookie-widening, and
ambiguous-outcome logic, all of which add work inside the same measured code path. The manuscript now
states this explicitly (Section 6.1, Section 8.3) rather than implying the existing numbers
characterize the current implementation's performance; a final timing re-measurement is deferred
until the broader evaluation work (topologies, baselines, statistical replication) is otherwise
complete, so it is collected once against final code rather than repeated after every fix.

New unit tests: `apply_flow()`'s ambiguous-timeout resolution (add/delete, each in all three
outcomes); `_apply_flow_or_fail_safe()`'s failure-safe conversion. 31/31 tests pass (2 new).

## Rollback-protocol correctness, closed by live testing, not by trusting the unit tests (v0.13.0)

A sixth external review pass, this time directed specifically at the two-phase rollback protocol
v0.12.0 had just shipped -- reading the code again after it was already tested and live-verified,
not accepting that as the end of the story. Found and closed three further gaps, each one level
deeper than the last, the same pattern that closed v0.9.0/v0.10.0's execute_repair() gaps two rounds
in a row:

- **Rollback's own result was discarded.** `execute_repair()` called `_rollback_staged_path()` for
  its side effect only and unconditionally reported the caller's prior `current_path` as confirmed
  intact, regardless of whether the rollback calls themselves actually succeeded. Fixed: a distinct
  `repair_rollback_incomplete` event and `current_path=None` whenever rollback itself does not fully
  complete, picked up by `maybe_retry_repair()` on the next tick exactly like any other unknown
  state.
- **Rollback acted on the intended new path, not on what staging actually touched.**
  `install_path()` returned only a success/failure boolean; on a staging failure, rollback
  recomputed the full intended path from scratch and rolled back every flow it would ever produce --
  including one the forwarding-consistency check (v0.12.0) had rejected before ever calling
  `apply_flow()` for it. If that rejected flow sat at a boundary-hop collision match, a
  `staged`-blind rollback would still "restore" `old_path`'s action there via `add-flow`, silently
  overwriting the very foreign flow the forwarding-consistency check had just correctly refused to
  overwrite -- defeating that check through its own supposed safety net. Fixed: `install_path()` now
  returns `(ok, staged)`, the actual list of flows this call confirmed installed, and
  `_rollback_staged_flows()` (renamed from `_rollback_staged_path()`) acts only on that list.
- **Staging itself repointed live traffic before the new path was ready.** An earlier
  `install_path()` staged flows in `path_to_flows()`'s natural SOURCE-to-DEST order, so the
  SOURCE-facing boundary flow (which immediately repoints that switch's live forwarding action)
  could be staged before the rest of the new path was even attempted. Fixed: `install_path(new_path,
  old_path=current_path)` now stages every non-colliding, purely-additive flow FIRST -- these have
  zero effect on live traffic until the boundary switches are repointed -- and defers the (at most
  two) boundary-hop colliding flows to the very end, so `old_path` stays fully live and correct for
  as long as possible during staging.

All three fixes were re-verified against the same live fault injection (a real link killed on the
multi-OVS testbed) that found v0.12.0's original boundary-hop blackhole, confirming both the
ordinary repair path and the new edge cases behave correctly. Also this round: `_agent_cookie()`
widened from a truncated 32-bit value to the full 64-bit OpenFlow cookie-field width (confirmed
empirically that `ovs-ofctl` round-trips a full 64-bit cookie correctly), reducing the collision
probability between two different protected pairs; and the forwarding-consistency check's
documentation corrected to state precisely what it guarantees (a conflict present at check time is
detected and rejected) rather than implying an atomic, race-free guarantee the
`dump-flows`-then-`add-flow` sequence does not actually provide against a concurrent writer -- this
paper's declared deployment model is one process per protected pair, not concurrent multi-client
coordination on a shared match.

New unit tests: rollback ignores a match this attempt never actually staged (confirming the
foreign-flow-protection corner case above); `install_path()` stages boundary-hop collisions last.
29/29 tests pass (2 new).

## Pair-scoped cookies, reconnect, forwarding consistency, and two-phase rollback (v0.12.0)

A fifth external review pass, closing four of Section 10's remaining evidence-gate items in one
round:

- **`AGENT_COOKIE` was a single fixed constant, correct only for a single-agent deployment.**
  Replaced with `_agent_cookie()`, deriving a deterministic cookie from the declared `(SOURCE,
  DEST)` pair (a truncated SHA-256 digest, read live like every other topology-derived global in
  this file). Deterministic across restarts, so a restarted agent still recognises and can withdraw
  flows a prior instance of itself installed; distinct per pair, so two agents protecting different
  pairs on a shared switch no longer delete each other's flows. `_delete_match()` now parses the
  cookie out of the flow string actually installed, rather than recomputing it, so it deletes
  exactly what was installed even if `SOURCE`/`DEST` were reconfigured in between. New unit test:
  `_agent_cookie()` is stable per pair and distinct across pairs.
- **The `repair-action time` metric definition (manuscript Section 6.3) was wrong.** It said the
  timer started "from the start of BFS recomputation"; the timer actually starts inside
  `execute_repair()`, after `decide_link_event()`'s own BFS call has already run. A wording
  correction only -- no numbers changed, and the omission is immaterial at the measured scale (BFS
  costs under 0.3 ms on a 200-node graph, Section 2.5).
- **Monitor-subprocess reconnect/resynchronization.** `monitor_link_rows()` now attempts one
  immediate reconnect (respawn plus a fresh initial snapshot, reusing the same machinery the
  startup path already uses) when an `ovsdb-client monitor` child's stream dies, reconciling state
  via `resync_from_reconnect()` without touching hold-down timers, and triggering a real repair
  through `execute_repair()` if the resync reveals the installed path no longer avoids every down
  edge. Verified live: the real remote monitor connection was killed on the multi-OVS testbed, the
  agent logged `monitor_reconnected` for the correct target within one poll tick, and a subsequent
  real fault injection on the same edge was still correctly detected and repaired. Reconnect is
  attempted once per disconnection, not retried indefinitely -- a disclosed limitation, not a
  designed backoff policy.
- **Forwarding-consistency check (manuscript Section 5.1).** `install_path()` now queries each
  target switch directly (`ovs-ofctl dump-flows`, a read-only call outside the `daim_ovs_flow`
  adapter, since reading existing state for a safety pre-check is not an installation operation)
  before every add, and refuses -- reporting `forwarding_conflict_rejected` and failing the whole
  call -- an add whose exact match is already occupied by a flow bearing a different process's
  cookie. Verified live: a foreign-cookie flow was installed directly at the exact match a
  two-switch repair path's own install would use; `install_path()` correctly rejected only that
  flow, installed the path's other non-conflicting flows, and `dump-flows` afterward confirmed the
  foreign flow was left untouched.
- **Two-phase rollback protocol (manuscript Section 5.2), and a real bug found only by testing it
  live.** `execute_repair()` now stages (installs) the new path FIRST, and only withdraws the old
  path once every new-path install call has succeeded; a staging failure rolls back whatever got
  staged and retains the old path, untouched, rather than degrading to `current_path=None`.
  Implementing this the naive way -- deleting by OpenFlow match at commit and rollback -- turned out
  to be unsafe: the switch attached to `SOURCE` and the switch attached to `DEST` each have one flow
  whose match is identical across every alternate path (the host-attachment port never changes), so
  `add-flow` at that match during staging silently overwrites the old path's live entry in place,
  and a naive match-based delete during commit or rollback removes that entry outright instead of
  leaving or restoring it. Confirmed live: a real link failure and repair, logged as a clean
  `repair_installed`, left both boundary switches with no matching flow entry at all in one
  direction -- a real blackhole `ovs-ofctl dump-flows` exposed, that a purely bookkeeping-level test
  could not have caught. `_withdraw_stale_path()` (commit: skip a match the new path already
  occupies) and `_rollback_staged_path()` (rollback: restore the old path's action via `add-flow` at
  a colliding match, delete only the purely-additive rest) replace the naive versions, re-verified
  against the same live fault injection that found the bug. Tied to this same change: the hold-down
  window's start moved out of `decide_link_event()` (which used to start it at decision time, before
  any of the withdrawal/install I/O ran) into `main()`, gated on `execute_repair()` reporting an
  actual commit -- re-verified live by observing the physical link's other interface, reporting the
  identical fault moments later, correctly suppressed.

New unit tests: pair-scoped-cookie determinism; `resync_from_reconnect()` state reconciliation;
`monitor_link_rows()` reconnect-on-stream-death (using real OS pipes); `_conflicting_flow_cookie()`
dump-flows parsing; `install_path()`'s conflict rejection; the boundary-hop match-collision
documentation; and `_withdraw_stale_path()`/`_rollback_staged_path()` in isolation -- eight new
tests, bringing the total to twenty-seven in `test_daim_link_agent.py`.

## A real defect found by live-network testing (v0.3.0)

The first live run of the flapping-link protocol (`stage3_holddown_flapping.py`)
against a real Mininet/OVS link produced a materially different action
sequence than the logic-level unit test predicted, with 3 spurious
`recovered` events instead of 1. The cause: a physical link has two OVS
interfaces (`s1-eth2` and `s2-eth1` for the monitored `s1`-`s2` link), and
OVSDB reports each side's transitions independently; the pre-v0.3.0
hold-down state was keyed by interface *name*, so a window opened by one
side's repair did not cover the other side's transitions on the same
physical link. The fix keys hold-down state by *edge* instead. This is a
genuine example of a defect that a synthetic, single-interface unit test
cannot find by construction, and that only surfaced once the same protocol
was run against a real network. `results/network/STAGE3_HOLDDOWN_FLAPPING_REPORT.md`
has the full before/after data and event-log evidence.

## A second, deeper defect found by code review (v0.4.0)

Keying hold-down by edge (v0.3.0, above) closed the suppression gap, but the
fix still recorded a single last-observed-state value *per edge*, overwritten
by whichever of its two interfaces reported most recently -- a last-report-wins
policy across two interfaces that report independently, the same fact that
motivated the v0.3.0 fix in the first place. One interface's `up` could
recover an edge while the other interface's independently-reported state was
still `down`. This was found by reviewing the code, not by any test failing.
`daim_link_agent.py` now tracks a persistent `interface_state` dict keyed by
interface name and requires *every* interface observing an edge to confirm
`up` before the edge is treated as recovered
(`_edge_confirmed_up()`), both at hold-down expiry and in the direct recovery
path. Two new regression tests reproduce the failing and passing cases.
The live flapping-link protocol was re-run against the fix (10 further
repetitions) and reproduced the identical clean pattern with no regression --
but that re-run does not actually confirm the new invariant, since
`net.configLinkStatus` changes both interfaces together and has never
produced the asymmetric timing this fix targets; that remains verified only
at the logic level pending a live protocol that can desynchronise the two
interfaces (see `results/network/STAGE3_HOLDDOWN_FLAPPING_REPORT.md`).

## A third defect found by code review, confirmed against a live ovsdb-server (v0.5.0)

`_parse_monitor_line()` only ever matched OVSDB rows whose `action` field was
`"new"`. That is correct for real transitions, but not for the very first
line `ovsdb-client monitor` sends: OVSDB reports a subscribed table's
*current contents* as its initial reply, with every one of those rows
carrying `action=="initial"`, not `"new"` -- confirmed empirically against a
real `ovsdb-server` in the VM before being treated as a genuine defect, not
assumed from documentation. The consequence: if a monitored interface was
already `down` when the agent started or restarted, the agent had no way to
find out. It always computed and installed its initial path assuming
`down_edges = set()`, and if the already-down interface never produced a
further transition -- true by definition, since its state was not changing
-- nothing would ever correct that.

`stage3_startup_already_down.py` reproduces this live: the `s1`-`s2` link is
brought down *before* the agent process even starts. The pre-fix agent
(`v0.4.0`, unmodified) installed its primary path straight through the dead
link and stayed that way -- a 20-packet ping ran to completion at **100%
packet loss**, with no recovery possible by design. The fixed agent reads
OVSDB's real initial snapshot via the new `read_initial_snapshot()`, derives
`down_edges` from it before computing any path (`down_edges_from_snapshot()`),
and installed the correct alternate path directly: **0% packet loss** from
the first probe packet. Both raw JSON results are retained side by side in
`results/network/`; the full writeup is
`results/network/STAGE3_STARTUP_ALREADY_DOWN_REPORT.md`.

## Runtime link_state validation and a figure mislabelling fix (v0.7.0)

Two more findings from re-reading the v0.6.0 code and figures together:

- **Runtime `link_state` validation.** v0.6.0 validated the OVSDB *startup*
  snapshot but not the ongoing event stream: `decide_link_event()` took
  whatever `state` a live event reported and, if it wasn't `"up"` or
  `"down"`, silently fell through to a generic `"ignored"` action -- the
  same silently-assume-fine gap the startup fix closed, reached through a
  different code path. `decide_link_event()` now returns a distinct
  `invalid_link_state` action for this case, before touching any state,
  with its own regression test.
- **`paper3_holddown_live_comparison.png`'s "BFS/recompute calls" row was
  wrong.** It was computed as `len(sequence) - suppressed_count`, i.e.
  every non-suppressed action -- but `"recovered"` never calls
  `bfs_path()` (only `decide_link_event()`'s `state=="down"` branch does),
  so counting it as a BFS call was wrong. The real count, using only
  `repair`/`noop`/`repair_failed`, is 1 in both the pre-fix and post-fix
  live runs for this specific flap schedule (it only ever pushes one edge
  through the down-and-not-already-down branch once) -- not the 4-to-2 the
  figure previously showed. Figure 5's synthetic, multi-transition
  schedule is what actually demonstrates the BFS-call reduction; this live
  figure now says so explicitly instead of implying a reduction the live
  data never measured.

## Repair-retry liveness, startup honesty, and cookie-scoped deletion (v0.11.0)

A third external review pass over v0.10.0 -- re-reading the code again, not just re-reading its own
description -- found three further real issues.

- **`current_path=None` (v0.10.0's fix) was not a guarantee of retry.** `decide_link_event()` only
  starts a repair on a `state=="down"` event for an edge *not already* in `down_edges` -- but a
  failed repair's edge is already in `down_edges` by the time `execute_repair()` runs. A duplicate
  transition on the same edge, or no further transition at all (the physical link stays down and
  nothing about it changes again), would never re-trigger a repair through the event-driven path
  alone: a transient flow-installation failure (a remote instance briefly unreachable, a timeout)
  could leave `current_path=None` a *permanent* dead end, not a temporary one. Fixed: new
  `maybe_retry_repair()`, called from `main()`'s periodic `poll_interval` tick (not the OVSDB event
  branch), recomputes BFS and retries whenever `current_path` is `None`, with no new OVSDB event
  required. No bounded retry count or backoff -- an explicit, disclosed limitation. New test:
  `test_maybe_retry_repair_retries_until_success`.
- **The startup path had the identical false-success gap `execute_repair()` was fixed for, on a
  different code path.** `main()`'s startup sequence called `install_path(current_path)` for its
  side effect only, always logging `agent_started` regardless of whether it actually succeeded. New
  `execute_startup_install()` mirrors `execute_repair()`'s contract exactly, feeding a `None`
  `current_path` into the same `maybe_retry_repair()` machinery on failure. New test:
  `test_execute_startup_install_reports_partial_failure`.
- **Flow deletion matched far more broadly than intended.** `withdraw_path()` derived its delete
  match by stripping the `add`-form string down to a bare `in_port=N`, with no priority, no cookie,
  nothing else. Confirmed empirically against a live OVS bridge before writing any fix: a real
  unrelated flow sharing only the same `in_port` (different priority, an extra match field, a
  different action) was silently deleted by that bare match, since non-strict `ovs-ofctl del-flows`
  deletes every flow whose fields are a superset of the given match. `--strict` deletion would work,
  but is not reachable through the existing `daim_ovs_flow add|delete BRIDGE MATCH` CLI, and
  extending it would mean modifying the DAIM-OS OVS adapter's C source -- contradicting this
  artifact's "extends, without modifying" boundary, for a change affecting every caller of the same
  shared, vendored adapter copy. Fixed with OpenFlow cookies instead, entirely within
  `daim_link_agent.py`: `path_to_flows()` now tags every flow with a fixed `AGENT_COOKIE`
  (`0x5e1fea9e`), and `withdraw_path()` deletes by a cookie mask plus `in_port` -- confirmed
  empirically that this deletes only the agent's own flow and leaves the unrelated flow untouched,
  no C-code change of any kind. New test: `test_delete_match_scopes_by_cookie_not_bare_in_port`.

Because the cookie-scoping fix changes the actual flow-mod strings sent on every install/withdraw
call (unlike the retry and startup-honesty fixes, which only change behaviour on a failure path the
clean n=5 run never exercised), the live multi-OVS n=5 measurement was re-run again in full: still
5/5 clean (no `repair_incomplete` or `startup_install_incomplete` ever produced), mean repair-action
time 203.54 ms (up from 191.48 ms pre-cookie-scoping -- previous-generation data retained as
`..._pre_cookie_scoping.*`). The retry and startup-honesty fixes remain verified at the unit-test
level only; neither measured live run (before or after cookie-scoping) ever actually failed a
repair, so their new code paths were not exercised live.

16/16 → 19/19 unit tests pass (three new tests added).

## Safe degraded state and endpoint genericity, found by a second external review pass (v0.10.0)

A second review pass over v0.9.0 -- reading the code, raw report, and figure together, not just the
diff -- found two more real issues, neither requiring new live-network data (both fixes touch code
paths the measured n=5 multi-OVS run never exercised: it never failed a repair, and both measured
topologies name their hosts `h1`/`h2`), plus a real math error in the previous report's own
cross-check.

- **`execute_repair()`'s failure path was itself unsound.** v0.9.0 left `current_path` at its
  pre-repair value on a partial failure. But `withdraw_path(current_path)` runs *before*
  `install_path()`; if withdrawal succeeded and installation then failed, the switches hold neither
  the old path nor the new one, so reporting the old path as still current is its own false claim --
  a later repair's `withdraw_path(current_path)` would issue delete calls against flows that no
  longer reflect reality. Fixed: `current_path` becomes `None` ("forwarding state not reliably
  known") on any partial failure. `withdraw_path(None)` now treats that as nothing to withdraw
  rather than crashing (`path_to_flows(None)` would otherwise raise `TypeError`), and
  `decide_link_event`'s `new_path == current_path` no-op check naturally never matches `None`, so
  the next fault event always attempts a fresh repair instead of trusting stale bookkeeping. This is
  an interim safety fix, not the two-phase rollback protocol Section 5.2 of the manuscript
  specifies -- it stops the agent from acting on a false belief about its own state, it does not
  prevent or undo the mixed-flow-state risk itself.
- **`bfs_path()`/`path_to_flows()` hardcoded the literal host names `"h1"`/`"h2"`** instead of
  reading the declared `SOURCE`/`DEST`/`HOST_ATTACHMENT` globals `load_topology_config()` already
  lets a deployment override. Both topologies measured in this evidence set happen to name their
  hosts `h1`/`h2`, so this never surfaced live -- but a deployment declaring different host names
  would have hit a `KeyError` in `path_to_flows()` (`TOPOLOGY[switch]["h1"]` on a switch whose
  topology dict has no such key). Fixed to resolve both functions against the live globals; this is
  a prerequisite for the multiple-topologies-and-scale evidence-gate item, not a gap in anything
  currently measured.
- **A real math error in the v0.9.0 report's own cross-check.** `STAGE3_MULTI_OVS_REPORT.md` claimed
  the agent-reported repair-action time fell inside the independently-derived ping-outage bound "in
  every repetition" -- false for repetition 5 (184.69 ms repair-action time vs. a 140-180 ms bound,
  exceeding the upper limit by 4.69 ms). Not a data error -- the raw numbers were always correct, and
  are unchanged -- a wording error in a summary claim about them, found by checking the table against
  the prose rather than trusting the prose. Fixed: the report and manuscript now state 4-of-5, with
  an explicit, honest reason the two measurements need not coincide exactly (`repair_end_ns` marks
  control-plane flow-mod completion, not necessarily the same instant the data plane resumes
  forwarding in both directions) rather than treating the mismatch as a discrepancy to explain away
  or silently correcting the claim to fit.

Two new unit tests (`test_execute_repair_reports_partial_failure_honestly` updated,
`test_bfs_and_flows_use_declared_source_dest_not_hardcoded_host_names` new): 16/16 agent tests pass
(18/18 including the two harness-parser tests in the separate file).

## Adapter unification and failure-honesty fix, found by external review (v0.9.0)

External review of the v0.8.0 report and manuscript found two real issues in the multi-OVS
extension, neither a test failure -- both found by reading the actual code and the actual claims
side by side.

- **Architecture inconsistency.** The manuscript's Abstract and Section 4.1 claim repair paths are
  installed "through the existing DAIM-OS OVS adapter", but `apply_flow()`'s remote-bridge path
  bypassed that adapter (`daim_ovs_flow`) and called `ovs-ofctl` directly -- functionally correct,
  but inconsistent with the paper's own claim for exactly the new experiment meant to extend it.
  Before assuming a C-code change was needed, this was checked empirically: `daim_ovs_flow`/
  `ovs_switch_adapter.c` (Paper 1) already forward their target argument as an opaque string into
  `ovs-ofctl -O OpenFlow13 <add-flow|del-flows> <target> <flow>`, and `ovs-ofctl` itself accepts
  either a local bridge name or a remote `tcp:HOST:PORT` target at that position. `daim_ovs_flow add
  tcp:<VM2>:6636 "priority=100,in_port=1,actions=output:2"`, run against the live testbed, installed
  a real flow on the remote switch with zero C-code changes -- confirmed with `ovs-ofctl dump-flows`
  immediately after, and the matching `delete` call removed it. The fix was entirely in
  `apply_flow()`, which now always calls `daim_ovs_flow` for both local and remote bridges. New
  test: `test_apply_flow_routes_remote_bridge_through_adapter`.
- **Partial-failure misreporting.** `install_path()`/`withdraw_path()` discarded `apply_flow()`'s
  per-call success/failure entirely, so `main()` could log `repair_installed` and advance
  `current_path` even if a flow-mod call had failed -- a partial installation failure was silently
  reported as a clean repair. New `execute_repair()` only uses the `repair_installed` event name and
  advances `current_path` if every flow-mod call across withdraw and install succeeded; otherwise it
  reports a distinct `repair_incomplete` event with `current_path` left unchanged. This does not
  implement the two-phase staged/commit/rollback protocol Section 5.2 specifies -- flows that did
  succeed before a failure are still not undone -- it only stops the agent from claiming success it
  did not achieve. New test: `test_execute_repair_reports_partial_failure_honestly`.

The n=5 multi-OVS measurement was re-run in full against the fixed code (not reused): all five
repetitions again succeeded cleanly (no `repair_incomplete` ever produced), with mean repair-action
time rising modestly from 185.75 ms (pre-fix, direct `ovs-ofctl`) to 191.48 ms (post-fix, through the
adapter) -- about 5.7 ms attributable to the extra process-spawn hop `daim_ovs_flow` itself
introduces (it `posix_spawn`s `ovs-ofctl`), reported as the honest cost of closing the
architecture-consistency finding, not hidden. Pre-fix raw data is retained alongside the new data as
`..._pre_adapter_unification.*`. Same review also corrected an outage-duration bound in the
ping-gap analysis (`STAGE3_MULTI_OVS_REPORT.md`): for `N` consecutive lost probes at a fixed
interval `Δ`, the true outage duration lies strictly between `(N-1)Δ` and `(N+1)Δ`, not the tighter
`[N Δ, (N+1)Δ)` an earlier version stated; and fixed imprecise wording that called the ping probe
"an independent process on a different host" when it in fact runs in `h1`'s Mininet namespace on the
same VM as the agent (a different OS process, real inter-VM traffic, just not a different host).
13/13 → now 15/15 unit tests pass (two new tests added).

## Multi-OVS connection multiplexing and per-hop flow routing, measured live (v0.8.0)

Every measurement before this release ran the agent and every declared switch
against one `ovsdb-server` inside a single Mininet host. `REMOTE_ENDPOINTS`
(empty by default, so every other experiment is unaffected) now maps a switch
name to the OVSDB and OpenFlow TCP targets of the OVS instance that owns it;
`load_topology_config()` loads this, and an overriding topology declaration,
from a JSON file named by the `DAIM_TOPOLOGY_CONFIG` environment variable.
`main()` opens one `ovsdb-client monitor` child per distinct OVSDB endpoint
the declared `MONITORED_INTERFACES` actually span (`_monitored_ovsdb_targets()`),
multiplexed via `select.select()` in `monitor_link_rows()` instead of reading
a single subprocess's stdout. `apply_flow()` routes each install/withdraw
call through the local `daim_ovs_flow` CLI when the target bridge has no
entry in `REMOTE_ENDPOINTS`, and otherwise issues the equivalent `ovs-ofctl
-O OpenFlow13 add-flow`/`del-flows` command directly against that switch's
registered remote OpenFlow target.

This was measured against a real two-VM testbed (`network/vm1_topo.py`,
`network/vm2_topo.py`, `network/stage3_multi_ovs_setup.sh`), joined by a real
GRE tunnel, not a shared bridge or namespace: VM1 runs the agent and `s1`;
VM2 runs `s3`, `s4`, `s5` independently, each with its own OVSDB and
OpenFlow endpoints. `network/stage3_multi_ovs_deployment.sh` repeated a
remote-edge failure -- both interfaces of the `s3`-`s4` edge brought down
entirely on VM2, an edge the agent shares no local OVSDB connection with --
five times. All five reproduced the identical sequence: detection over the
remote OVSDB connection, a BFS-computed repair (`s1,s3,s5,s4`) with flows
withdrawn/installed across the local and remote instances, correct
suppression of the redundant same-edge report, and a clean SIGTERM shutdown
of both monitor subprocesses -- no special-casing needed anywhere for the
multi-OVS case. Mean repair-action time was 185.75 ms (range 180.2-191.8 ms),
independently corroborated by the exact packet-loss gap (8-9 missing
`icmp_seq` values at the ping's fixed 20 ms interval) observed in the
concurrent probe traffic captured on a different host -- real, packet-level
evidence that the reported timing reflects an actual data-plane
interruption. Full data and claim boundary:
`results/network/STAGE3_MULTI_OVS_REPORT.md`.

## Robustness hardening and a harness bug fix (v0.6.0)

Two further review passes over the v0.5.0 fix, before moving on to larger
experiments:

- **`link_state` validation.** `down_edges_from_snapshot()` originally
  treated anything other than `"down"` as implicitly `"up"`. OVS documents
  `Interface.link_state` as optional, so an empty string or unexpected
  value is possible in principle even if not observed in this environment.
  It now requires exactly `"up"` or `"down"`, raising otherwise, with its
  own regression test. `main()` was also wrapped so a fatal startup path
  (missing interface, no viable path, or a rejected `link_state`) still
  terminates the monitor subprocess rather than leaking it.
- **A bug in the startup-scenario test harness itself.**
  `stage3_startup_already_down.py`'s original `ping_had_loss` field used
  `"0% packet loss" not in ping_output`, a substring check -- and `"100%
  packet loss"` contains `"0% packet loss"` as a substring (the trailing
  `"0%"` of `"100%"`), so the 100%-loss pre-fix run was silently recorded
  as `ping_had_loss: false` in the stored JSON. The script's overall
  `correct` verdict was unaffected (it never used that field, and already
  failed the pre-fix run for the right reasons), and neither was Figure 7,
  which parses the loss percentage independently -- but the field itself
  was wrong until fixed. Replaced with a proper `parse_ping_loss_pct()`
  function, with its own regression test reproducing the exact string that
  triggered the bug (`test_stage3_startup_already_down.py`), and `correct`
  now also explicitly requires `ping_loss_pct == 0.0` for the fixed-agent
  run. The startup-already-down scenario was also re-run three times
  against the fixed agent as a robustness check (all three: `CORRECT`, 0%
  loss).

## Reproducing the logic-level results (no network testbed needed)

```
cd network
python3 test_daim_link_agent.py
```

This runs in well under a second and prints the same PASS output referenced
in the manuscript's Section 7.3, including the real action sequence for both
the hold-down-enabled and hold-down-disabled runs, plus the stale-state and
cross-interface regression tests.

## Reproducing the figures

```
cd analysis
python3 paper3_analysis.py
```

Regenerates all eight PNGs in `results/paper3/` from `results/network/`'s raw
CSVs/JSON and from a live run of the hold-down unit test's decision function.

## Reproducing the network-level results

Requires Ubuntu (the original measurements used 24.04 ARM64), Open vSwitch
3.3.4, Mininet 2.3.0, and the compiled `daim_ovs_flow` adapter (`cd
implementation && make`). Run `network/stage3_autonomous_agent.py` for the
autonomous link-failure condition, `network/stage3_controller_switch_faults.py`
for the scripted switch/controller conditions,
`network/stage3_holddown_flapping.py` for the live flapping-link hold-down
measurement, and `network/stage3_startup_already_down.py` for the
startup-synchronization scenario. `results/network/` holds the measured
output from a Multipass Ubuntu 24.04 ARM64 VM matching the original Stage 3
environment.

The multi-OVS deployment measurement additionally requires a *second*
independent VM on the same subnet: run `network/vm1_topo.py` on one VM and
`network/vm2_topo.py` on the other, run `network/stage3_multi_ovs_setup.sh`
once to establish the GRE tunnel and remote OVSDB/OpenFlow listeners
(adjust the VM IPs at the top of the script if your subnet differs from
`192.168.252.0/24`), then run `network/stage3_multi_ovs_deployment.sh`
(set `H1PID` to the VM1 `mininet:h1` shell PID, discoverable with `ps aux |
grep mininet:h1`) for the n=5 remote-edge-failure protocol.

## License

Apache License 2.0 (see `LICENSE`), matching Papers 1 and 2 of this
programme.

## Citation

See `CITATION.cff`.
