#!/usr/bin/env python3
"""Pure-Python BFS scaling microbenchmark, replacing Section 2.5's own
explicitly-flagged gap: "the 200-node bound above is a design target, not a
measured result." Times the REAL, unmodified `bfs_path()` from
daim_link_agent.py (the identical function every live experiment in this
evidence set actually calls) against synthetic topology graphs of
increasing size, up to and beyond the 200-node design target -- no Mininet
or OVS needed, since bfs_path() is pure in-process graph search over the
TOPOLOGY/HOST_ATTACHMENT globals, with zero I/O.

Graph generator: a roughly-square grid graph (rows x cols switches, each
connected to its up/down/left/right neighbours), chosen over a random graph
because it is deterministic, reproducible, and gives a stable, realistic
average node degree (~4, matching typical campus/data-centre switch
fan-out) without relying on a random seed's specific structure. The
source/dest hosts are attached at opposite corners of the grid, which
maximises the shortest-path length BFS has to discover for a given grid
size -- the worst case for this generator, not a favourable one.
"""
import json
import statistics
import sys
import time
from pathlib import Path

NETWORK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(NETWORK_DIR))
import daim_link_agent  # noqa: E402

ROOT = NETWORK_DIR.parent
RAW = ROOT / "results/network/bfs_scaling_benchmark_raw.csv"
SUMMARY = ROOT / "results/network/bfs_scaling_benchmark_summary.json"

# Node counts to benchmark, spanning well below and well above the 200-node
# design target Section 2.5 cites.
GRID_SIZES = [(3, 3), (5, 5), (7, 7), (10, 10), (14, 14), (20, 20), (32, 32), (45, 45)]
REPETITIONS = 2000


def build_grid_topology(rows, cols):
    """A rows x cols grid graph: switch (r,c) connected to its up/down/
    left/right neighbours (where they exist). Host h1 attached to the
    (0,0) corner switch, h2 to the (rows-1, cols-1) corner -- the longest
    possible shortest path for this generator, i.e. the BFS worst case for
    a given node count, not a favourable case."""
    def name(r, c):
        return f"s{r}_{c}"

    topology = {name(r, c): {} for r in range(rows) for c in range(cols)}
    port_counters = {name(r, c): 0 for r in range(rows) for c in range(cols)}

    def connect(a, b):
        port_counters[a] += 1
        port_counters[b] += 1
        topology[a][b] = (port_counters[a], port_counters[b])
        topology[b][a] = (port_counters[b], port_counters[a])

    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                connect(name(r, c), name(r, c + 1))
            if r + 1 < rows:
                connect(name(r, c), name(r + 1, c))

    src, dst = name(0, 0), name(rows - 1, cols - 1)
    port_counters[src] += 1
    topology[src]["h1"] = (port_counters[src], None)
    port_counters[dst] += 1
    topology[dst]["h2"] = (port_counters[dst], None)

    host_attachment = {"h1": src, "h2": dst}
    node_count = rows * cols
    edge_count = sum(len(n) for n in topology.values()) // 2 - 2  # exclude the two host links
    return topology, host_attachment, node_count, edge_count


def benchmark_one(rows, cols):
    topology, host_attachment, node_count, edge_count = build_grid_topology(rows, cols)
    daim_link_agent.TOPOLOGY = topology
    daim_link_agent.HOST_ATTACHMENT = host_attachment
    daim_link_agent.SOURCE, daim_link_agent.DEST = "h1", "h2"

    down_edges = set()
    durations_ns = []
    path = None
    for _ in range(REPETITIONS):
        start = time.perf_counter_ns()
        path = daim_link_agent.bfs_path("h1", "h2", down_edges)
        end = time.perf_counter_ns()
        durations_ns.append(end - start)

    assert path is not None, f"grid {rows}x{cols} produced no path -- generator bug"
    return {
        "rows": rows,
        "cols": cols,
        "node_count": node_count,
        "edge_count": edge_count,
        "path_length_hops": len(path) - 1,
        "repetitions": REPETITIONS,
        "mean_ns": statistics.mean(durations_ns),
        "median_ns": statistics.median(durations_ns),
        "min_ns": min(durations_ns),
        "max_ns": max(durations_ns),
        "stdev_ns": statistics.stdev(durations_ns),
        "mean_ms": statistics.mean(durations_ns) / 1e6,
        "max_ms": max(durations_ns) / 1e6,
    }


def main():
    rows_out = []
    for rows, cols in GRID_SIZES:
        row = benchmark_one(rows, cols)
        print(
            f"grid {rows}x{cols} ({row['node_count']} nodes, {row['edge_count']} edges, "
            f"path {row['path_length_hops']} hops): mean={row['mean_ms']:.4f}ms "
            f"max={row['max_ms']:.4f}ms (n={REPETITIONS})"
        )
        rows_out.append(row)

    RAW.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with RAW.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows_out[0]))
        writer.writeheader()
        writer.writerows(rows_out)
    print(f"wrote {len(rows_out)} rows to {RAW}")

    bound_200 = min(rows_out, key=lambda r: abs(r["node_count"] - 200))
    largest = rows_out[-1]
    summary = {
        "design_target_claim": "BFS runtime on a 200-node graph is under 0.3 ms (Section 2.5)",
        "closest_measured_to_200_nodes": bound_200,
        "largest_measured": largest,
        "design_target_holds": (bound_200["max_ms"] < 0.3) if bound_200 else None,
        "generator": "rows x cols grid graph, host-to-host worst-case corner-to-corner path",
        "note": (
            "bfs_path() is the real, unmodified function from daim_link_agent.py -- "
            "not a reimplementation -- monkeypatching only its module-level "
            "TOPOLOGY/HOST_ATTACHMENT/SOURCE/DEST globals per grid size."
        ),
    }
    with SUMMARY.open("w") as handle:
        json.dump(summary, handle, indent=2)
    print(f"wrote summary to {SUMMARY}")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
