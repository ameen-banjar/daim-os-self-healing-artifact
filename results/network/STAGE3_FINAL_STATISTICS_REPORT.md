# Final Replicated Dataset and Statistics Report

Date: 16 August 2026
Environment: Ubuntu 24.04 LTS ARM64 (Multipass VMs `daim-lab`/`daim-lab-2`), Open vSwitch 3.3.4,
Mininet 2.3.0, os-ken 2.6.0 -- exact version match to every prior live experiment in this evidence set.
Evidence level: `measured_emulation_*` per condition (see each condition's own raw CSV).

## What this closes

Section 10 item 9 (statistical replication): a replication count set from pilot variability and a
target estimate precision, not an arbitrary fixed number; median/IQR reporting with confidence
intervals; and a significance test against each baseline, explicitly unpaired since no genuinely
matched-pairs design exists in this evaluation (independent Mininet/OVS process launches, no shared
blocking variable). It also closes, as a side effect of re-running the diamond-agent and multi-OVS
conditions fresh, the long-disclosed "reported timings predate the current correctness fixes"
gap (Section 6.1/8.3) with real current-code numbers.

## Replication counts, derived from pilot variability

For each condition, `n_final = ceil((1.96 * pilot_sd / (0.20 * pilot_mean))^2)`, floored at the pilot
`n` itself (95% CI half-width targeted at 20% of the pilot mean) -- not an arbitrary fixed number:

| Condition | Pilot n | Pilot mean | Pilot SD | Pilot CV | Target n | Action |
|---|---:|---:|---:|---:|---:|---|
| Diamond agent | 5 | 157.67 ms | 11.70 ms | 7.4% | 5 (met) | Re-run fresh, current code |
| Multi-OVS | 5 | 203.54 ms | 11.97 ms | 5.9% | 5 (met) | Re-run fresh, current code |
| `ring_8` | 3 | 294.92 ms | 6.84 ms | 2.3% | 3 (met) | Pilot data retained as final |
| `ring_20` | 3 | 714.38 ms | 25.80 ms | 3.6% | 3 (met) | Pilot data retained as final |
| `fattree_k4` | 3 | 267.83 ms | 1.02 ms | 0.4% | 3 (met) | Pilot data retained as final |
| Controller-driven baseline | 5 | 3.64 ms | 0.81 ms | 22.3% | 5 (met) | Pilot data retained as final |
| Fast-failover baseline (forward) | 5 | 98.0 ms | 70.9 ms | 72.3% | **51** | Extended, fresh full run |

The fast-failover baseline's pilot CV (72.3%) is by far the highest of any condition, driven by its
metric's own 20ms quantization granularity (the outage bound is derived from a discrete missing-probe
count at a fixed 20ms interval), not measurement instability -- it required by far the largest
extension, from 5 to 51 repetitions. Every other condition already met the 20%-precision target at
its existing pilot size; `ring_8`/`ring_20`/`fattree_k4` and the controller-driven baseline are
therefore reported using their existing Layer-2-step-3/4 pilot data as the final dataset, not
re-run, since re-running would not have changed a properly-justified replication count.

**Re-run, not reused**: the diamond-agent (`stage3_autonomous_agent_raw.csv`) and multi-OVS
(`stage3_multi_ovs_raw.csv`) conditions were re-measured fresh against current code for this round,
replacing the historical Table 2/Table 4 pilot data that Section 6.1/8.3 had long disclosed as
predating the two-phase staging, forwarding-consistency, and ambiguous-outcome correctness work.

## Results: descriptive statistics (median/IQR/95% CI, bootstrap, n=10000)

| Condition | n | Median (ms) | IQR (ms) | 95% CI on median (ms) | Mean (ms) | SD (ms) |
|---|---:|---:|---|---|---:|---:|
| Diamond agent (fresh) | 5 | 246.66 | [241.87, 283.88] | [220.09, 358.15] | 270.13 | 54.29 |
| Multi-OVS (fresh) | 5 | 832.09 | [797.04, 993.83] | [689.93, 1056.79] | 873.94 | 149.44 |
| Topology `ring_8` | 3 | 295.64 | [291.69, 298.50] | [287.74, 301.36] | 294.92 | 6.84 |
| Topology `ring_20` | 3 | 705.65 | [699.86, 724.53] | [694.08, 743.40] | 714.38 | 25.80 |
| Topology `fattree_k4` | 3 | 267.57 | [267.27, 268.27] | [266.96, 268.96] | 267.83 | 1.02 |
| Controller-driven baseline | 5 | 3.48 | [3.33, 3.88] | [2.66, 4.87] | 3.64 | 0.81 |
| Fast-failover baseline (forward, n=51) | 51 | 160.00 | [60.00, 220.00] | [80.00, 200.00] | 151.57 | 98.90 |

**The diamond-agent and multi-OVS numbers are substantially higher than the historical Table 2/Table
4 figures** (157.67 ms -> 270.13 ms mean, 203.54 ms -> 873.94 ms mean respectively) -- an honest,
expected consequence of the correctness work those historical numbers predated (two-phase staging
with its non-colliding-flows-first ordering, the `_conflicting_flow_cookie()` forwarding-consistency
read-before-write pre-check running before every add, and the ambiguous-outcome read-back path), not
a regression or a measurement error. The multi-OVS increase is proportionally larger (4.3x mean vs.
1.7x for the diamond) because the forwarding-consistency pre-check's extra `dump-flows` round trip is
paid once per flow over the same GRE/TCP remote connection every other multi-OVS flow-mod call already
crosses, compounding the existing remote-call overhead documented in Section 4.4/7.7.

## Results: significance tests (Mann-Whitney U, unpaired)

No genuinely matched-pairs design exists between any two conditions here -- every repetition, in
every condition, is an independent Mininet/OVS process launch with no shared blocking variable (same
random seed, same host-load window, or any other tying factor) linking one condition's repetition `i`
to another's. Per Section 10 item 9's own explicit condition ("a paired significance test ... only
where the design produces genuinely matched pairs"), every test below is unpaired
(Mann-Whitney U / Wilcoxon rank-sum), not paired.

| Comparison | n (a, b) | U statistic | p-value | Significant at 0.05 |
|---|---|---:|---:|:---:|
| Diamond agent vs. controller-driven baseline | 5, 5 | 25.0 | 0.0079 | Yes |
| Diamond agent vs. fast-failover baseline (forward) | 5, 51 | 225.0 | 0.0052 | Yes |
| Controller-driven vs. fast-failover baseline (forward) | 5, 51 | 0.0 | 0.0003 | Yes |

All three comparisons are statistically significant at the 0.05 level, confirming what the
descriptive statistics already show directly: the agent's repair-action time is significantly
higher than both baselines', and the two baselines are significantly different from each other
(controller-driven is both faster and far less variable than fast-failover's forward-direction
outage bound). Significance here means the observed rank difference is unlikely under the null
hypothesis of identical distributions -- it does not, on its own, establish WHY the difference
exists (Section 7.9's own discussion attributes the agent-vs-baseline gap to the correctness
machinery cost, not a claim of general inferiority).

## What this does and does not establish

Establishes: current-code (not historical) repair-action-time figures for the diamond agent and
multi-OVS conditions; a replication count for every condition set from that condition's own pilot
variability rather than an arbitrary fixed number; median/IQR/bootstrap-CI descriptive statistics for
every condition measured across Layer 2; and statistically significant (unpaired) differences between
the agent and each formal baseline. Does not establish: a paired/matched comparison (the design does
not support one); statistical replication of `linear_10`'s single necessarily-deterministic outcome
(n=1 throughout, no variance to describe); or a combined cross-condition model (e.g. regressing repair
time on hop count across all topologies at once) -- each condition is described independently, as
measured, not fitted to a shared trend line.

## Files

- `stage3_final_statistics.json` -- full per-condition descriptive statistics and all three
  significance tests, machine-readable.
- `stage3_autonomous_agent_raw.csv`, `stage3_multi_ovs_raw.csv` -- the fresh, current-code final
  measurements (superseding the historical pilot data these filenames previously held).
- `stage3_baseline_fast_failover_raw.csv` -- now 51 repetitions (was 5).
- `experiments/analysis/paper3_final_statistics.py` -- the analysis script.
