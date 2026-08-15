#!/usr/bin/env python3
"""Generic topology generators for Layer 2 step 3 (Section 10: multiple
topologies + scale + overhead), producing BOTH a Mininet `Topo` and the
matching DAIM_TOPOLOGY_CONFIG JSON for `daim_link_agent.py`'s
`load_topology_config()` -- for topologies structurally different from the
one four-switch diamond (and its four-switch multi-OVS variant) every prior
live experiment in this evidence set has used.

Port numbering must exactly match what Mininet itself assigns, since the
JSON config's `topology` dict hardcodes port numbers independently of
Mininet (see daim_link_agent.py's own hardcoded DiamondTopo-matching
TOPOLOGY constant for the existing precedent). Mininet assigns each node's
port numbers in the order `addLink()` calls touch that node, starting at 1
-- confirmed against the existing diamond topology's hardcoded ports, which
match its `DiamondTopo.build()` method's addLink call order exactly. Every
generator below builds its edge list in a fixed order and replicates that
same per-node incrementing-counter rule when computing the JSON `topology`
dict, then a single shared `build_mininet_topo()` constructs the actual
Mininet `Topo` by calling `addLink()` in that identical order -- so the two
representations of port numbers are guaranteed to agree, not just assumed
to.
"""
import json
from pathlib import Path


def _assign_ports(edges, host_attachment):
    """edges: ordered list of (a, b) node-name pairs (switches or hosts),
    in the exact order they will be passed to addLink(). Returns the
    daim_link_agent-style `topology` dict: switch -> {neighbor: [my_port,
    their_port]}, with host neighbours getting `their_port=None` (matching
    the existing diamond/multi-OVS convention, since a host's own single
    NIC isn't a monitored/routed switch port)."""
    hosts = set(host_attachment)
    port_counters = {}
    topology = {}

    def next_port(node):
        port_counters[node] = port_counters.get(node, 0) + 1
        return port_counters[node]

    for a, b in edges:
        pa = next_port(a)
        pb = None if b in hosts else next_port(b)
        if a not in hosts:
            topology.setdefault(a, {})[b] = (pa, pb)
        if b not in hosts:
            topology.setdefault(b, {})[a] = (pb, pa)
    return topology


def build_mininet_topo(edges, switches, hosts):
    """Builds a Mininet Topo subclass instance whose addLink() calls occur
    in EXACTLY `edges`' order, so Mininet's own port assignment matches
    `_assign_ports(edges, ...)` above."""
    from mininet.topo import Topo

    class GeneratedTopo(Topo):
        def build(self):
            for sw in switches:
                self.addSwitch(sw, protocols="OpenFlow13", failMode="secure")
            for h, ip in hosts.items():
                self.addHost(h, ip=ip)
            for a, b in edges:
                self.addLink(a, b)

    return GeneratedTopo()


def _config(topology, host_attachment, source, dest, monitored_interfaces):
    return {
        "topology": {sw: {n: list(p) for n, p in nbrs.items()} for sw, nbrs in topology.items()},
        "host_attachment": host_attachment,
        "source": source,
        "dest": dest,
        "monitored_interfaces": {name: list(edge) for name, edge in monitored_interfaces.items()},
        "remote_endpoints": {},
    }


def _monitor_all_switch_edges(edges, hosts):
    """Every switch-switch edge (not host-facing) gets both of its
    interfaces monitored -- `<switch>-eth<port>` is OVS's own naming
    convention for a Mininet-created interface, confirmed against every
    prior live experiment's interface names (e.g. `s1-eth2`)."""
    port_counters = {}

    def next_port(node):
        port_counters[node] = port_counters.get(node, 0) + 1
        return port_counters[node]

    monitored = {}
    for a, b in edges:
        pa = next_port(a)
        pb = next_port(b)
        if a in hosts or b in hosts:
            continue
        monitored[f"{a}-eth{pa}"] = (a, b)
        monitored[f"{b}-eth{pb}"] = (a, b)
    return monitored


def linear_topology(n):
    """n switches in a chain s1-s2-...-sn, h1 on s1, h2 on sn. No
    redundant path exists for ANY link -- deliberately the worst case for
    self-healing, since removing any single edge partitions the network.
    Exercises correct, safe repair_failed reporting (no false-positive
    success, no crash) at scale, rather than successful rerouting, which
    this topology structurally cannot support."""
    switches = [f"s{i}" for i in range(1, n + 1)]
    hosts = {"h1": "10.0.0.1/24", "h2": "10.0.0.2/24"}
    edges = [("h1", "s1")]
    for i in range(1, n):
        edges.append((f"s{i}", f"s{i+1}"))
    edges.append((f"s{n}", "h2"))
    host_attachment = {"h1": "s1", "h2": f"s{n}"}
    topology = _assign_ports(edges, host_attachment)
    monitored = _monitor_all_switch_edges(edges, set(host_attachment))
    return {
        "name": f"linear_{n}",
        "switches": switches,
        "hosts": hosts,
        "edges": edges,
        "config": _config(topology, host_attachment, "h1", "h2", monitored),
    }


def ring_topology(n):
    """n switches s1..sn in a cycle, h1 on s1, h2 on the switch roughly
    opposite s1 (index n//2 + 1), maximising the shortest-path length in
    both directions around the ring. A single link failure anywhere always
    leaves exactly one surviving path (the other way around the ring), so
    every fault on this topology is recoverable -- unlike the linear
    topology above."""
    assert n >= 4, "ring needs at least 4 switches for a meaningful opposite-side host split"
    switches = [f"s{i}" for i in range(1, n + 1)]
    hosts = {"h1": "10.0.0.1/24", "h2": "10.0.0.2/24"}
    edges = [("h1", "s1")]
    for i in range(1, n):
        edges.append((f"s{i}", f"s{i+1}"))
    edges.append((f"s{n}", "s1"))
    dest_switch = f"s{n // 2 + 1}"
    edges.append((dest_switch, "h2"))
    host_attachment = {"h1": "s1", "h2": dest_switch}
    topology = _assign_ports(edges, host_attachment)
    monitored = _monitor_all_switch_edges(edges, set(host_attachment))
    return {
        "name": f"ring_{n}",
        "switches": switches,
        "hosts": hosts,
        "edges": edges,
        "config": _config(topology, host_attachment, "h1", "h2", monitored),
    }


def fat_tree_topology(k):
    """A standard k-ary fat-tree: k pods, each with k/2 edge switches and
    k/2 aggregation switches (fully connected to each other within the
    pod), plus (k/2)^2 core switches organised as a (k/2)x(k/2) grid, where
    aggregation switch j in every pod connects to all core switches in
    core-group j. For k=4: 8 edge + 8 agg + 4 core = 20 switches, matching
    the >10-switch scale requirement with genuine multi-path redundancy at
    two layers (agg and core), not just one. h1 attaches to pod 0's first
    edge switch, h2 to the LAST pod's first edge switch -- the maximum
    possible pod separation, forcing every repair to cross both the
    aggregation and core layers."""
    assert k % 2 == 0 and k >= 2, "fat-tree k must be even and >= 2"
    half = k // 2
    switches = []
    edges = []
    hosts = {"h1": "10.0.0.1/24", "h2": "10.0.0.2/24"}

    def edge_sw(pod, i):
        return f"e{pod}_{i}"

    def agg_sw(pod, j):
        return f"a{pod}_{j}"

    def core_sw(gi, gj):
        return f"c{gi}_{gj}"

    for pod in range(k):
        for i in range(half):
            switches.append(edge_sw(pod, i))
        for j in range(half):
            switches.append(agg_sw(pod, j))
    for gi in range(half):
        for gj in range(half):
            switches.append(core_sw(gi, gj))

    for pod in range(k):
        for i in range(half):
            for j in range(half):
                edges.append((edge_sw(pod, i), agg_sw(pod, j)))
    for pod in range(k):
        for j in range(half):
            for gj in range(half):
                edges.append((agg_sw(pod, j), core_sw(j, gj)))

    edges.insert(0, ("h1", edge_sw(0, 0)))
    edges.append((edge_sw(k - 1, 0), "h2"))
    host_attachment = {"h1": edge_sw(0, 0), "h2": edge_sw(k - 1, 0)}

    topology = _assign_ports(edges, host_attachment)
    monitored = _monitor_all_switch_edges(edges, set(host_attachment))
    return {
        "name": f"fattree_k{k}",
        "switches": switches,
        "hosts": hosts,
        "edges": edges,
        "config": _config(topology, host_attachment, "h1", "h2", monitored),
    }


def write_config(spec, out_dir):
    out_path = Path(out_dir) / f"{spec['name']}_topology.json"
    out_path.write_text(json.dumps(spec["config"], indent=2))
    return out_path


if __name__ == "__main__":
    import sys
    for spec in (linear_topology(6), ring_topology(8), fat_tree_topology(4)):
        n_sw = len(spec["switches"])
        n_edges = sum(1 for a, b in spec["edges"] if a not in spec["hosts"] and b not in spec["hosts"])
        print(f"{spec['name']}: {n_sw} switches, {n_edges} switch-switch links, "
              f"{len(spec['config']['monitored_interfaces'])} monitored interfaces")
