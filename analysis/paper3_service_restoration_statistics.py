#!/usr/bin/env python3
"""Statistics for the UNIFIED, construct-valid data-plane service-restoration
metric (stage3_service_restoration_unified.py), replacing the earlier
comparison that pitted the agent's own control-plane repair_action_us
against the fast-failover baseline's data-plane ping-outage bound as if they
measured the same endpoint. Here, all three mechanisms are compared on the
IDENTICAL data-plane observation (real tcpdump-captured packet arrivals,
last-good-before-fault to first-good-after-fault, per direction), with each
mechanism's own control-plane phase decomposition reported separately, not
conflated into the comparison.

Replication count per mechanism is the pilot-variability-derived value
(Section 6.11's own established methodology, reapplied here): for each
mechanism, the larger of its two directions' required n (since one live
repetition yields both directions' data simultaneously, a single N must
satisfy both).
"""
import csv
import json
import statistics
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/network"
RAW = RESULTS / "stage3_service_restoration_unified_raw.csv"
OUT_JSON = RESULTS / "stage3_service_restoration_statistics.json"

RNG = np.random.default_rng(20260817)
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


def describe(name, data):
    data = [float(x) for x in data]
    n = len(data)
    median = statistics.median(data)
    q1, q3 = (np.percentile(data, 25), np.percentile(data, 75)) if n > 1 else (median, median)
    ci_lo, ci_hi = bootstrap_median_ci(data)
    mean = statistics.mean(data)
    sd = statistics.stdev(data) if n > 1 else 0.0
    result = {
        "condition": name, "n": n,
        "mean_ms": round(mean, 3), "sd_ms": round(sd, 3),
        "median_ms": round(median, 3), "q1_ms": round(float(q1), 3), "q3_ms": round(float(q3), 3),
        "iqr_ms": round(float(q3 - q1), 3),
        "median_ci95_lower_ms": round(ci_lo, 3) if ci_lo is not None else None,
        "median_ci95_upper_ms": round(ci_hi, 3) if ci_hi is not None else None,
    }
    print(f"{name}: n={n} median={median:.2f}ms IQR=[{q1:.2f},{q3:.2f}] "
          f"95% CI=[{ci_lo:.2f},{ci_hi:.2f}] mean={mean:.2f} sd={sd:.2f}")
    return result


def mann_whitney(name_a, data_a, name_b, data_b):
    stat, p = stats.mannwhitneyu(data_a, data_b, alternative="two-sided")
    print(f"Mann-Whitney U ({name_a} n={len(data_a)} vs {name_b} n={len(data_b)}): "
          f"U={stat:.1f} p={p:.4g}")
    return {"a": name_a, "b": name_b, "n_a": len(data_a), "n_b": len(data_b),
            "u_statistic": float(stat), "p_value": float(p),
            "significant_at_0.05": bool(p < 0.05)}


def main():
    rows = list(csv.DictReader(open(RAW)))
    # Two fast_failover repetitions (of 41) show BOTH directions completely
    # empty (no packets captured at all, not even a pre-fault baseline) --
    # a diagnostic signature distinct from every other fast_failover row
    # (which always has valid forward data, since a genuine capture never
    # fails to observe the pre-fault baseline). Confirmed via a targeted
    # 15-repetition follow-up (all 15 clean) that this is a rare
    # harness/capture-startup flake, not a property of the mechanism, and
    # excluded from the statistics accordingly rather than silently kept or
    # silently dropped without comment.
    harness_failures = [r for r in rows if r["mechanism"] == "fast_failover"
                         and r["forward_recovered"] == "False" and not r["forward_outage_ms"]]
    if harness_failures:
        print(f"Excluding {len(harness_failures)} fast_failover repetition(s) with a total capture "
              f"failure (both directions empty) as a harness/capture-startup flake, not a mechanism "
              f"finding: repetitions {[r['repetition'] for r in harness_failures]}")
    rows = [r for r in rows if r not in harness_failures]
    by_mech = {}
    for r in rows:
        by_mech.setdefault(r["mechanism"], []).append(r)

    print("=== Recovery rates ===")
    recovery = {}
    for mech, rs in by_mech.items():
        fwd_rate = sum(1 for r in rs if r["forward_recovered"] == "True") / len(rs)
        rev_rate = sum(1 for r in rs if r["reverse_recovered"] == "True") / len(rs)
        recovery[mech] = {"n": len(rs), "forward_recovery_rate": fwd_rate, "reverse_recovery_rate": rev_rate}
        print(f"{mech}: n={len(rs)} forward_recovered={fwd_rate*100:.0f}% reverse_recovered={rev_rate*100:.0f}%")

    print("\n=== Data-plane restoration time (ms), forward direction ===")
    forward_data = {mech: [float(r["forward_outage_ms"]) for r in rs if r["forward_outage_ms"]] for mech, rs in by_mech.items()}
    forward_desc = {mech: describe(f"{mech}_forward", data) for mech, data in forward_data.items()}

    print("\n=== Data-plane restoration time (ms), reverse direction (recovered cases only) ===")
    reverse_data = {mech: [float(r["reverse_outage_ms"]) for r in rs if r["reverse_outage_ms"]] for mech, rs in by_mech.items()}
    reverse_desc = {mech: describe(f"{mech}_reverse", data) for mech, data in reverse_data.items() if data}

    print("\n=== Control-plane phase decomposition (ms), mean where available ===")
    cp_desc = {}
    for mech, rs in by_mech.items():
        bfs_vals = [float(r["cp_bfs_ns"]) / 1e6 for r in rs if r.get("cp_bfs_ns")]
        stage_vals = [float(r["cp_stage_ns"]) / 1e6 for r in rs if r.get("cp_stage_ns")]
        commit_vals = [float(r["cp_commit_ns"]) / 1e6 for r in rs if r.get("cp_commit_ns")]
        total_vals = [float(r["cp_total_control_plane_ns"]) / 1e6 for r in rs if r.get("cp_total_control_plane_ns")]
        cp_desc[mech] = {
            "bfs_mean_ms": round(statistics.mean(bfs_vals), 4) if bfs_vals else None,
            "stage_mean_ms": round(statistics.mean(stage_vals), 3) if stage_vals else None,
            "commit_mean_ms": round(statistics.mean(commit_vals), 3) if commit_vals else None,
            "total_control_plane_mean_ms": round(statistics.mean(total_vals), 3) if total_vals else None,
        }
        print(f"{mech}: {cp_desc[mech]}")

    print("\n=== Unpaired significance tests, forward-direction data-plane restoration (apples-to-apples) ===")
    tests = []
    mechs = list(forward_data.keys())
    for i in range(len(mechs)):
        for j in range(i + 1, len(mechs)):
            tests.append(mann_whitney(mechs[i], forward_data[mechs[i]], mechs[j], forward_data[mechs[j]]))

    print("\n=== Unpaired significance tests, reverse-direction data-plane restoration (agent vs controller only; fast_failover never recovers) ===")
    if "agent" in reverse_data and "controller_driven" in reverse_data:
        tests.append(mann_whitney("agent", reverse_data["agent"], "controller_driven", reverse_data["controller_driven"]))

    output = {
        "recovery_rates": recovery,
        "forward_restoration": forward_desc,
        "reverse_restoration": reverse_desc,
        "control_plane_decomposition": cp_desc,
        "significance_tests": tests,
    }
    with OUT_JSON.open("w") as f:
        json.dump(output, f, indent=2)
    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
