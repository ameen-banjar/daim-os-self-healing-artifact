#!/usr/bin/env python3
"""Layer 2 step 5/6: the final statistically-described dataset (Section 10
items 9, and the concluding half of item 6/7's own "preliminary exploration"
disclosures). Per-condition replication counts were set from PILOT
variability and a target estimate precision (Section 6.5's own stated
methodology), not an arbitrary fixed number -- computed once, separately,
from each condition's own existing pilot coefficient of variation, targeting
a 95%-CI half-width of 20% of the pilot mean:

    n_final = ceil((1.96 * pilot_sd / (0.20 * pilot_mean)) ** 2), floored at
    the pilot n itself (never shrink a condition that already met the target).

This yielded n=5 for the diamond agent, multi-OVS, and controller-driven
baseline (already met at pilot n=5); n=3 for the three new topologies
(already met at pilot n=3, itself confirmed low-variance); and n=51 for the
fast-failover baseline (its pilot coefficient of variation was 72%, driven
by the metric's own 20ms quantization granularity, not measurement
instability).

The diamond-agent and multi-OVS conditions were RE-RUN fresh against
current code for this final round (not reusing the historical Table 2/
Table 4 pilot numbers), closing the long-disclosed "reported timings predate
the two-phase staging/forwarding-consistency/ambiguous-outcome correctness
work" gap (Section 6.1/8.3) with real current numbers -- which are
substantially higher than the historical ones, an honest, expected
consequence of that same correctness work, not a regression.

No genuinely matched-pairs design exists between conditions here: every
repetition across every condition is an independent Mininet/OVS process
launch with no shared blocking variable (same random seed, same host-load
window, etc.) tying an agent repetition to a specific baseline repetition.
Per Section 10 item 9's own explicit condition ("a paired significance test
... only where the design produces genuinely matched pairs"), comparisons
here use an UNPAIRED test (Mann-Whitney U / Wilcoxon rank-sum), not a paired
one.
"""
import csv
import json
import statistics
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/network"
OUT_JSON = RESULTS / "stage3_final_statistics.json"

RNG = np.random.default_rng(20260816)
BOOTSTRAP_N = 10000


def bootstrap_median_ci(data, n_boot=BOOTSTRAP_N, alpha=0.05):
    data = np.array(data, dtype=float)
    if len(data) < 2:
        return None, None
    boots = RNG.choice(data, size=(n_boot, len(data)), replace=True)
    medians = np.median(boots, axis=1)
    lo = np.percentile(medians, 100 * alpha / 2)
    hi = np.percentile(medians, 100 * (1 - alpha / 2))
    return float(lo), float(hi)


def describe(name, data, unit="ms"):
    data = [float(x) for x in data]
    n = len(data)
    median = statistics.median(data)
    q1, q3 = (np.percentile(data, 25), np.percentile(data, 75)) if n > 1 else (median, median)
    ci_lo, ci_hi = bootstrap_median_ci(data)
    mean = statistics.mean(data)
    sd = statistics.stdev(data) if n > 1 else 0.0
    result = {
        "condition": name, "n": n, "unit": unit,
        "mean": round(mean, 3), "sd": round(sd, 3),
        "median": round(median, 3), "q1": round(float(q1), 3), "q3": round(float(q3), 3),
        "iqr": round(float(q3 - q1), 3),
        "median_ci95_lower": round(ci_lo, 3) if ci_lo is not None else None,
        "median_ci95_upper": round(ci_hi, 3) if ci_hi is not None else None,
        "raw": data,
    }
    print(f"{name}: n={n} median={median:.2f}{unit} IQR=[{q1:.2f},{q3:.2f}] "
          f"95% CI on median=[{ci_lo:.2f},{ci_hi:.2f}] mean={mean:.2f} sd={sd:.2f}")
    return result


def mann_whitney(name_a, data_a, name_b, data_b):
    stat, p = stats.mannwhitneyu(data_a, data_b, alternative="two-sided")
    print(f"Mann-Whitney U ({name_a} n={len(data_a)} vs {name_b} n={len(data_b)}): "
          f"U={stat:.1f} p={p:.4g}")
    return {"a": name_a, "b": name_b, "n_a": len(data_a), "n_b": len(data_b),
            "u_statistic": float(stat), "p_value": float(p),
            "significant_at_0.05": bool(p < 0.05)}


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main():
    conditions = {}

    agent_rows = load_csv(RESULTS / "stage3_autonomous_agent_raw.csv")
    conditions["agent_diamond"] = [float(r["repair_action_us"]) / 1000 for r in agent_rows]

    multi_ovs_rows = load_csv(RESULTS / "stage3_multi_ovs_raw.csv")
    conditions["multi_ovs"] = [float(r["repair_action_ms"]) for r in multi_ovs_rows]

    topo_rows = load_csv(RESULTS / "stage3_topology_scale_raw.csv")
    for topo in ("ring_8", "ring_20", "fattree_k4"):
        vals = [float(r["repair_action_us"]) / 1000 for r in topo_rows if r["topology"] == topo]
        conditions[f"topology_{topo}"] = vals

    ctrl_rows = load_csv(RESULTS / "stage3_baseline_controller_driven_raw.csv")
    conditions["baseline_controller_driven"] = [float(r["repair_action_us"]) / 1000 for r in ctrl_rows]

    ff_rows = load_csv(RESULTS / "stage3_baseline_fast_failover_raw.csv")
    conditions["baseline_fast_failover_forward"] = [
        (float(r["forward_outage_bound_lower_ms"]) + float(r["forward_outage_bound_upper_ms"])) / 2
        for r in ff_rows
    ]

    print("=== Per-condition descriptive statistics (repair/recovery time, ms) ===")
    descriptions = {name: describe(name, data) for name, data in conditions.items()}

    print("\n=== Unpaired significance tests (Mann-Whitney U, no matched-pairs design) ===")
    tests = [
        mann_whitney("agent_diamond", conditions["agent_diamond"],
                     "baseline_controller_driven", conditions["baseline_controller_driven"]),
        mann_whitney("agent_diamond", conditions["agent_diamond"],
                     "baseline_fast_failover_forward", conditions["baseline_fast_failover_forward"]),
        mann_whitney("baseline_controller_driven", conditions["baseline_controller_driven"],
                     "baseline_fast_failover_forward", conditions["baseline_fast_failover_forward"]),
    ]

    output = {"conditions": descriptions, "significance_tests": tests}
    with OUT_JSON.open("w") as f:
        json.dump(output, f, indent=2)
    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
