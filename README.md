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
- `network/test_daim_link_agent.py` — thirteen pure-logic unit tests, runnable
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
  endpoint with connections deduplicated per distinct target (v0.8.0, see
  below).
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
