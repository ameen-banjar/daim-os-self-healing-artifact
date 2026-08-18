#!/usr/bin/env python3
"""Statistics for the replicated hold-down window-length sensitivity sweep
(stage3_holddown_sensitivity.py). For each of the 6 (window, schedule)
combinations: descriptive stats (median/IQR/bootstrap 95% CI) for
suppressed-transition count and repair-action time, the precision-based N
derivation (n_final = ceil((1.96*pilot_sd/(0.20*pilot_mean))^2), same
formula used throughout this evidence set), and a regression check on
spurious recoveries (should be exactly 0 in every repetition -- the class
of defect the edge-keying/last-report-wins fixes, rounds 2-3 of this
session, closed)."""
import csv
import json
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results/network/stage3_holddown_sensitivity_raw.csv"
OUT = ROOT / "results/network/stage3_holddown_sensitivity_statistics.json"

RNG = np.random.default_rng(20260818)


def bootstrap_ci(values, n_resamples=10000, stat=np.median):
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return (float(values[0]), float(values[0])) if len(values) == 1 else (None, None)
    resamples = RNG.choice(values, size=(n_resamples, len(values)), replace=True)
    stats = stat(resamples, axis=1)
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def iqr(values):
    if len(values) < 2:
        return [float(values[0]), float(values[0])] if values else [None, None]
    q1, q3 = np.percentile(values, [25, 75])
    return [float(q1), float(q3)]


def n_required(sd, mean, pilot_n, precision=0.20, z=1.96):
    if mean == 0 or sd == 0:
        return pilot_n
    n = math.ceil((z * sd / (precision * mean)) ** 2)
    return max(n, pilot_n)


def describe(values):
    values = [float(v) for v in values]
    mean = st.mean(values)
    sd = st.stdev(values) if len(values) > 1 else 0.0
    lo, hi = bootstrap_ci(values)
    return {
        "n": len(values),
        "mean": mean,
        "sd": sd,
        "cv_pct": (sd / mean * 100.0) if mean else 0.0,
        "median": float(st.median(values)),
        "iqr": iqr(values),
        "ci95": [lo, hi],
        "n_required_20pct_precision": n_required(sd, mean, len(values)),
    }


def main():
    rows = list(csv.DictReader(RAW.open()))
    groups = defaultdict(list)
    for r in rows:
        key = (float(r["window_seconds"]), r["schedule"])
        groups[key].append(r)

    combinations = {}
    total_spurious = 0
    total_reps = 0
    for key in sorted(groups):
        window, schedule = key
        rs = groups[key]
        total_reps += len(rs)
        supp = [int(r["observed_suppressed_count"]) for r in rs]
        repus = [float(r["repair_action_us"]) for r in rs if r["repair_action_us"] not in (None, "", "None")]
        spurious = [int(r["spurious_recovered_count"]) for r in rs]
        total_spurious += sum(spurious)
        matches = sum(1 for r in rs if r["matches_prediction"] == "True")

        combinations[f"window={window}_schedule={schedule}"] = {
            "window_seconds": window,
            "schedule": schedule,
            "n": len(rs),
            "suppressed_count": describe(supp),
            "repair_action_us": describe(repus) if repus else None,
            "spurious_recovered_total": sum(spurious),
            "matches_prediction_rate": f"{matches}/{len(rs)}",
        }

    result = {
        "combinations": combinations,
        "total_repetitions": total_reps,
        "total_spurious_recoveries": total_spurious,
        "precision_target": "95% CI half-width <= 20% of the mean (same methodology as "
                             "paper3_service_restoration_statistics.py / paper3_final_statistics.py)",
    }
    OUT.write_text(json.dumps(result, indent=2))
    print(f"wrote {OUT}")

    print(f"\n{'window':>6} {'schedule':>10} {'n':>3} {'n_req(supp)':>11} {'n_req(repair_us)':>16}")
    for key, c in combinations.items():
        n_req_supp = c["suppressed_count"]["n_required_20pct_precision"]
        n_req_rep = c["repair_action_us"]["n_required_20pct_precision"] if c["repair_action_us"] else "n/a"
        print(f"{c['window_seconds']:>6} {c['schedule']:>10} {c['n']:>3} {n_req_supp:>11} {n_req_rep!s:>16}")
    print(f"\nspurious recoveries across all {total_reps} repetitions: {total_spurious} "
          f"(regression check for the edge-keying/last-report-wins defect classes)")


if __name__ == "__main__":
    main()
