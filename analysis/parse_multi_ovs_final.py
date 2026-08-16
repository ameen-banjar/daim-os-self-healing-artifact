#!/usr/bin/env python3
"""Parses the 5 fresh final-round multi-OVS repetition logs (agent JSON +
ping stdout) into stage3_multi_ovs_raw.csv, matching Table 4's existing
columns exactly, so paper3_analysis.py's draw_multi_ovs_deployment() and
the statistics script can consume it unchanged."""
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/network"
OUT = RESULTS / "stage3_multi_ovs_raw.csv"


def parse_ping_gaps(text, interval_ms=20):
    seqs = sorted(int(m) for m in re.findall(r"icmp_seq=(\d+)", text))
    if not seqs:
        return 0, None, None
    full = set(range(seqs[0], seqs[-1] + 1))
    missing = sorted(full - set(seqs))
    if not missing:
        return 0, 0, interval_ms
    longest = 1
    cur = 1
    for i in range(1, len(missing)):
        if missing[i] == missing[i - 1] + 1:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 1
    lower = max(0, (longest - 1) * interval_ms)
    upper = (longest + 1) * interval_ms
    return longest, lower, upper


def main():
    rows = []
    for i in range(1, 6):
        agent_log = (RESULTS / f"stage3_multi_ovs_agent_rep{i}.log").read_text()
        ping_log = (RESULTS / f"stage3_multi_ovs_ping_rep{i}.log").read_text()
        events = []
        for line in agent_log.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # OVS/interpreter shutdown noise after SIGTERM, not agent JSON
        repair = next(e for e in events if e.get("event") == "repair_installed")
        repair_ms = (repair["repair_end_ns"] - repair["repair_start_ns"]) / 1e6
        missing, lower, upper = parse_ping_gaps(ping_log)
        rows.append({
            "repetition": i,
            "repaired_path": ",".join(repair["path"]),
            "repair_action_ms": round(repair_ms, 2),
            "consecutive_missing_pings": missing,
            "ping_outage_bound_lower_ms": lower,
            "ping_outage_bound_upper_ms": upper,
        })

    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT}")
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
