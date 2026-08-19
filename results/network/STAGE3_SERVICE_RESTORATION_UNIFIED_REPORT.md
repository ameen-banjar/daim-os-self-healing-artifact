# Unified Service-Restoration Metric Report

Date: 17-18 August 2026; agent replication extended to n=20, 18 August 2026 (see "Agent replication
extended" below)
Environment: Ubuntu 24.04 LTS ARM64 (Multipass VM `daim-lab`), Open vSwitch 3.3.4, Mininet 2.3.0,
os-ken 2.6.0 -- exact version match to every prior live experiment in this evidence set.
Evidence level: `measured_emulation_service_restoration_unified`

## What this closes, and why it was necessary

The formal-baseline comparison in Section 7.9 (Table 6) originally compared the agent's own
`repair_action_us` (a **control-plane** interval -- Section 6.3 defines it as the time inside
`execute_repair()`, ending at Flow-Mod confirmation, explicitly NOT a traffic-confirmed restoration
time) against the fast-failover baseline's ping-derived outage **bound** (a **data-plane**
observation). A reviewer correctly identified this as a construct-validity problem: the two numbers
measure different endpoints, so a Mann-Whitney test between them, however correctly computed
arithmetically, does not support a claim about which mechanism restores traffic faster. This report
replaces that comparison with one unified, construct-valid data-plane metric measured identically for
all three mechanisms, while keeping each mechanism's own control-plane decomposition as a separate,
clearly-labelled set of columns.

## Method

`stage3_service_restoration_unified.py` runs each mechanism (autonomous agent, fast-failover group,
controller-driven recovery) on the identical diamond topology and `s1-s2` fault. Two independent,
continuous ICMP probes run for the whole repetition -- `h1->h2` and `h2->h1` -- at a 5 ms interval (4x
finer than the 20 ms interval used elsewhere in this evidence set, verified empirically beforehand to
sustain 100% delivery under normal conditions). Each direction's arrivals are captured independently
via `tcpdump` on the RECEIVING host, filtered to genuine echo-REQUESTS from the expected sender only
(not replies, and not the receiving host's own concurrent outbound probe traffic for the other
direction -- both were found and fixed as real capture-conflation bugs during development, described
below). For each direction: `outage_duration_ms = first_sustained_good_after_fault -
last_good_before_fault`, using a REAL observed arrival, not a formula-derived range.

**Sustained-recovery requirement, and a real bug it fixes.** An early version declared "recovered" on
the first packet arriving after the fault. This produced a false ~5 ms "recovery" for
fast-failover's reverse direction in roughly 5% of repetitions -- directly contradicting the
already-established, carefully-validated 0/51 finding from Section 7.9's own earlier measurement.
Root-caused by retaining raw capture files for one debug repetition: the "recovering" packet was the
LAST one ever seen out of 700 sent, with total silence afterward -- the textbook signature of a
packet already in flight microseconds before `net.configLinkStatus()` was called, not genuine
restoration (the interface-down transition is not instantaneous). Fixed by requiring a SUSTAINED run
of at least 3 consecutive received sequence numbers to count as recovery, confirmed via the same
debug methodology to correctly reclassify the false positive as `recovered=False` while leaving every
genuine recovery (already followed by continuous arrivals in every mechanism measured) unaffected.

**Replication counts** were re-derived from this metric's own pilot data (3 repetitions per
mechanism), using the same methodology as Section 6.11: `n_final =
ceil((1.96*pilot_sd/(0.20*pilot_mean))^2)`, taking the larger of each mechanism's two directions since
one repetition yields both simultaneously. Agent (very low pilot variance, both directions) already
met the target at n=3; controller-driven required n=54 (driven by a high-CV forward direction);
fast-failover required n=23 (extended to n=41 for robustness, matching this evidence set's earlier
fast-failover sample size).

**Two fast-failover repetitions (of 41) excluded** as a harness/capture-startup flake, not a
mechanism finding: both directions showed completely empty captures (no packets at all, not even a
pre-fault baseline) -- a diagnostic signature distinct from every other fast-failover repetition
(which always captured valid forward data). A targeted 15-repetition follow-up ran clean in all 15,
supporting this attribution. 39 valid fast-failover repetitions are reported.

**Agent replication extended to n=20 (18 August 2026).** A subsequent review noted that while the
agent's pilot n=3 satisfied the mean/CV-based formula above, this left the headline
agent-vs-baseline comparison's own distribution represented by only three observations -- a deeper
concern than that formula addresses, given the reported statistics are the median, its bootstrap CI,
and the unpaired Mann-Whitney test rather than the mean. 17 further agent repetitions were
pragmatically run (`stage3_service_restoration_unified.py agent 17 4`; no numeric target was fixed
in advance for this specific extension), then the result re-examined: at the resulting n=20, the
bootstrap 95% CI on the median is approximately 2.5% of the median -- tight and stable compared to
the wider, less certain estimate n=3 supported -- so collection stopped there. All 17 additional
repetitions recovered both directions cleanly (20/20
forward, 20/20 reverse). The controller-driven and fast-failover datasets were not re-run, since
their own precision was already established and this review did not raise a concern specific to
them. All numbers below reflect the n=20 agent dataset.

## Results

**Recovery rates.**

| Mechanism | n | Forward recovered | Reverse recovered |
|---|---:|---|---|
| Autonomous agent | 20 | 100% (20/20), Wilson 95% CI [83.9%, 100%] | 100% (20/20), Wilson 95% CI [83.9%, 100%] |
| Controller-driven | 54 | 100% (54/54), Wilson 95% CI [93.4%, 100%] | 98% (53/54), Wilson 95% CI [90.2%, 99.7%] |
| Fast-failover group | 39 | 100% (39/39), Wilson 95% CI [91.0%, 100%] | **0% (0/39), Wilson 95% CI [0.0%, 9.0%]** |

Wilson score intervals are reported, not bare percentages, given the small failure counts involved
(most proportions here are 0/n or n/n). Controller-driven's one reverse non-recovery (of 54) is
reported honestly as a real, rare observation -- the control-plane log confirms the controller
detected the fault and acted (forward direction recovered normally in that same repetition), so this
is not attributable to the same harness-flake signature the two excluded fast-failover repetitions
showed; it is not further explained here, kept as a disclosed, low-frequency finding rather than
dropped.

**Data-plane restoration time (ms), forward direction (the unified, construct-valid comparison).**

| Mechanism | n | Median | IQR | 95% CI | Mean | SD |
|---|---:|---:|---|---|---:|---:|
| Autonomous agent | 20 | 285.95 | [279.45, 295.18] | [280.15, 294.43] | 290.19 | 27.86 |
| Controller-driven | 54 | 20.10 | [15.56, 20.58] | [15.87, 20.20] | 21.26 | 9.56 |
| Fast-failover group | 39 | 15.89 | [15.59, 20.70] | [15.69, 20.54] | 19.60 | 6.87 |

**Data-plane restoration time (ms), reverse direction (recovered cases only).**

| Mechanism | n | Median | IQR | 95% CI | Mean | SD |
|---|---:|---:|---|---|---:|---:|
| Autonomous agent | 20 | 339.56 | [328.41, 352.70] | [328.81, 350.50] | 346.11 | 32.45 |
| Controller-driven | 53 | 20.10 | [15.49, 21.01] | [15.93, 20.48] | 20.09 | 6.07 |
| Fast-failover group | -- | -- | -- | -- | never recovers | -- |

**Unpaired significance tests (Mann-Whitney U -- no genuinely matched-pairs design exists), with
Holm-Bonferroni-adjusted p-values correcting for testing this four-comparison family together.**

| Comparison | Direction | n (a, b) | U | p-value | Holm-adjusted p | Significant |
|---|---|---|---:|---:|---:|:---:|
| Agent vs. controller-driven | Forward | 20, 54 | 1080.0 | 5.1e-11 | 2.1e-10 | Yes |
| Agent vs. fast-failover | Forward | 20, 39 | 780.0 | 4.5e-10 | 8.9e-10 | Yes |
| Controller-driven vs. fast-failover | Forward | 54, 39 | 1061.5 | **0.950** | **0.950** | **No** |
| Agent vs. controller-driven | Reverse | 20, 53 | 1060.0 | 5.8e-11 | 2.1e-10 | Yes |

The Holm correction does not change which comparisons are significant at alpha=0.05.

**The agent is significantly slower than both baselines on the unified, construct-valid metric** --
consistent with the earlier (n=3) result's headline conclusion, now on a materially more precise
estimate (agent n=20, bootstrap CI half-width ~2.5% of the median). **Controller-driven and
fast-failover show no statistically detectable difference in forward-direction restoration speed**
(p=0.950) -- a genuinely new, clean finding this unified metric reveals that the earlier comparison
could not, since it never compared these two baselines to each other on the same construct. This is
reported as absence of evidence for a difference, not evidence of equivalence: no equivalence margin
was pre-specified, so this study does not and cannot claim the two mechanisms perform identically.

**Control-plane phase decomposition (ms), kept separate from the data-plane comparison above.**

| Mechanism | Detection | BFS | Stage | Commit | Total control-plane |
|---|---:|---:|---:|---:|---:|
| Autonomous agent (n=20) | ~5 (Section 7.1) | 0.016 | 329.93 | 135.34 | 465.27 |
| Controller-driven | ~15 (Table 8, Section 7.10) | N/A (hardcoded) | N/A | N/A | 2.53 |
| Fast-failover group | N/A (no software control plane) | N/A | N/A | N/A | N/A |

The agent's own control-plane total (465.27 ms mean, n=20) is HIGHER than its own measured
data-plane restoration time (290.19 ms forward / 346.11 ms reverse mean) -- consistent with the
two-phase protocol's own design: forwarding is genuinely restored once the relevant flows are
staged, which can complete before `execute_repair()`'s own bookkeeping (including old-path
withdrawal, counted in `commit`) finishes. This is not a contradiction, and is the same class of
observation Section 7.7's own multi-OVS discussion already made about `repair_end_ns` not
necessarily coinciding with the exact data-plane resumption instant -- now directly confirmed by an
independent, real packet-level measurement rather than inferred.

## What this does and does not establish

Establishes: a construct-valid, apples-to-apples comparison of real data-plane restoration time
across all three mechanisms on the identical topology and fault, with the agent's own distribution
now backed by n=20 rather than a 3-observation pilot; that the agent is significantly slower than
both baselines (read, as before, as the cost of its own correctness machinery); that this sample
provides no evidence the two baselines differ from each other in forward-direction speed (not
evidence they are equivalent); and that fast-failover's reverse-direction non-recovery is a hard,
structural 0% (39/39 attempts, Wilson 95% CI 0.0-9.0%), not a rare event. Does not establish: a
resolution of why the agent's own control-plane total exceeds its data-plane restoration time (noted,
not further decomposed here); or behaviour on any topology other than the diamond.

## Files

- `stage3_service_restoration_unified_raw.csv`, `..._events.jsonl` -- per-repetition raw data and
  event logs for all three mechanisms.
- `stage3_service_restoration_statistics.json` -- full descriptive statistics and significance tests.
- `experiments/network/stage3_service_restoration_unified.py` -- the harness script.
- `experiments/analysis/paper3_service_restoration_statistics.py` -- the analysis script.
