# Multi-OVS Deployment Report

Date: 14 August 2026
Environment: two independent Multipass Ubuntu 24.04 LTS ARM64 VMs (`daim-lab`,
`daim-lab-2`) on the same host-only subnet, connected by a real GRE tunnel
(not a shared bridge or namespace) -- Open vSwitch 3.3.4 (`daim-lab`) /
3.3.9 (`daim-lab-2`), Mininet 2.3.0 on each. Evidence level:
`measured_emulation_multi_ovs_remote_edge`

## What this closes

Evidence-gate item 3 from Section 10 of the manuscript: *"Multi-OVS-instance
deployment. Test the agent against switches spread across independent OVSDB
endpoints, including a remote-edge failure the agent does not share a
connection with, before relying on Figure 1's architecture at more than one
shared-OVSDB host."*

Every measurement in this evidence set before this report ran the agent and
every declared switch inside one Mininet host, sharing one `ovsdb-server`
(Section 4.2's "Deployment boundary" paragraph). That is a real limitation,
not a simplification for exposition: nothing about the design guaranteed the
agent would even *connect* to a second, independent OVS instance, let alone
detect and repair a failure on it, since the original code opened exactly
one `ovsdb-client monitor` subprocess and issued every flow-installation
call through the local `daim_ovs_flow` CLI unconditionally.

## The extension

- `REMOTE_ENDPOINTS` (empty by default) -- maps a switch name to the OVSDB
  and OpenFlow TCP targets of the OVS instance that owns it. Empty by
  default, so every other experiment and both existing unit tests are
  unaffected unless a deployment opts in.
- `load_topology_config(path)` -- reads `REMOTE_ENDPOINTS`, plus an
  overriding topology/host-attachment/monitored-interfaces declaration, from
  a JSON file named by the `DAIM_TOPOLOGY_CONFIG` environment variable
  (`multi_ovs_topology.json` for this experiment).
- `_monitored_ovsdb_targets()` / `_ovsdb_target_for_interface()` /
  `_owning_switch()` -- compute the distinct set of OVSDB endpoints the
  declared `MONITORED_INTERFACES` actually span, by parsing the owning
  switch from the interface-name convention (`"s3-eth1"` belongs to `"s3"`).
- `_start_monitor(ovsdb_target=None)` / `monitor_link_rows(procs, ...)` --
  `main()` now starts one `ovsdb-client monitor` child per distinct target
  instead of always one, and the event-reading generator was changed from
  reading a single subprocess's stdout to multiplexing an arbitrary list of
  them with `select.select()`.
- `apply_flow()` -- routes each install/withdraw call through the existing
  local `daim_ovs_flow` CLI when the target bridge has no entry in
  `REMOTE_ENDPOINTS`, and otherwise issues the equivalent `ovs-ofctl -O
  OpenFlow13 add-flow`/`del-flows` command directly against that switch's
  registered remote OpenFlow target -- the identical command
  `daim_ovs_flow` itself wraps for the local case, just addressed at a
  `tcp:HOST:PORT` target instead of a local bridge name.

Two new unit tests (`test_default_config_uses_single_local_ovsdb_target`,
`test_multi_ovs_target_routing`) confirm, respectively, that an empty
`REMOTE_ENDPOINTS` resolves to exactly the one local connection every other
experiment in this evidence set uses (so the extension is provably inert
unless opted into), and that a non-empty one routes each interface to its
own switch's endpoint with connections deduplicated per distinct target. All
13 unit tests pass (the 11 from prior fixes, unmodified, plus these 2).

## Testbed

Two switches on VM1 (`h1`, `s1`) and three on VM2 (`s3`, `s4`, `s5`, `h2`),
declared as a single logical topology split across two independent OVS
instances:

```
h1 -- s1 -- [GRE tunnel] -- s3 -- s4 -- h2      (primary path)
                             \    /
                              s5                 (alternate path)
```

- `vm1_topo.py` (on `daim-lab`, 192.168.252.2): `h1 -- s1`.
- `vm2_topo.py` (on `daim-lab-2`, 192.168.252.3): `s3 -- s4 -- h2` (primary,
  monitored edge under test) and `s3 -- s5 -- s4` (alternate).
- `stage3_multi_ovs_setup.sh`: adds a real GRE port on each side
  (`ovs-vsctl add-port ... type=gre options:remote_ip=...`) so the two OVS
  instances form one L2 topology across the host-only subnet; exposes VM2's
  `ovsdb-server` over `ptcp:6640:0.0.0.0` so the agent, running only on
  VM1, can open a remote OVSDB monitor connection to it; and gives each of
  VM2's three switches its own passive OpenFlow listener
  (`ovs-vsctl set-controller sN ptcp:663N:0.0.0.0`) so `apply_flow()` can
  issue `ovs-ofctl` calls directly at them (there is no real SDN controller
  in this experiment -- this listener is functionally equivalent to a local
  bridge-name target).
- `multi_ovs_topology.json`: the declared topology, host attachment,
  `monitored_interfaces` (`s3-eth1`, `s4-eth1` -- both ends of the `s3-s4`
  edge, entirely on VM2), and `remote_endpoints` (`s3`, `s4`, `s5` all
  mapped to VM2's OVSDB at `tcp:192.168.252.3:6640` and their individual
  OpenFlow listeners). Port numbers were verified against a live
  `ovs-ofctl show` on each switch and matched exactly (Mininet assigns
  ports deterministically by link-addition order).

The agent itself runs only on VM1, started as `DAIM_TOPOLOGY_CONFIG=.../
multi_ovs_topology.json python3 daim_link_agent.py`. Its declared topology
places `s1` locally reachable but `s3`, `s4`, and `s5` all on VM2 -- so the
monitored `s3-s4` edge is one the agent has **no local OVSDB connection to
at all**: both detection (the OVSDB notification) and repair (the flow
install/withdraw calls on `s3`, `s4`, and `s5`) must cross the GRE
tunnel/TCP connections to VM2, not just the failure itself.

## Method

`stage3_multi_ovs_deployment.sh` repeats the following five times: reset
link state and flow tables on both VMs and kill any stray agent/monitor
process; start the agent fresh on VM1, logging every event to a per-rep
JSON-lines file; start a 200-packet, 20 ms-interval ping from `h1` to `h2`;
bring both `s3-eth1` and `s4-eth1` down on VM2 (the complete `s3-s4` edge,
injected entirely on the remote host); wait 5 s; stop the agent with
SIGTERM; transfer both the agent log and the ping output back for analysis.

This differs from Section 6.6's flapping-link protocol and Section 6.7's
startup-synchronization protocol in what it is testing: those confirm the
hold-down state machine and initial-snapshot handling respectively, on the
single-host diamond; this protocol holds those mechanisms fixed (the same
code path is exercised, unmodified) and instead varies where the switches
and the fault physically live.

## Live-network result

All five repetitions produced the identical qualitative outcome. Per-agent-log
events, in order: `agent_started` (`initial_path: ["s1","s3","s4"]`, local
flow on `s1`, remote flows on `s3`/`s4`) &rarr; `link_down_detected`
(`interface: "s4-eth1"`, `edge: ["s3","s4"]`, arriving over the remote OVSDB
connection) &rarr; `repair_installed` (`path: ["s1","s3","s5","s4"]`, flows
withdrawn/installed across the local `s1` and the remote `s3`/`s4`/`s5`)
&rarr; `transition_suppressed` (`interface: "s3-eth1"`, the redundant report
of the same edge, correctly absorbed by the existing edge-keyed hold-down
window) &rarr; clean SIGTERM shutdown terminating both monitor subprocesses.
No special-casing was needed anywhere in this sequence for the multi-OVS
case: edge-keying, edge-confirmation, and the startup-snapshot fix all
continued to behave exactly as they do in the single-host measurements.

**Table.** Per-repetition repair-action timing
(`repair_start_ns`&rarr;`repair_end_ns`, the same instrumentation used for
Section 7.1's single-host figures) and independent packet-level
corroboration from the concurrent ping stream.

| Rep | Repaired path | Repair-action time (ms) | Ping: missing packets in outage gap | Ping-implied outage (ms) |
|---:|---|---:|---:|---:|
| 1 | s1, s3, s5, s4 | 180.23 | 9 | ~180 |
| 2 | s1, s3, s5, s4 | 191.78 | 8 | ~160 |
| 3 | s1, s3, s5, s4 | 185.29 | 9 | ~180 |
| 4 | s1, s3, s5, s4 | 190.81 | 9 | ~180 |
| 5 | s1, s3, s5, s4 | 180.63 | 9 | ~180 |
| **Mean** | | **185.75** | | **~176** |

Raw data: `stage3_multi_ovs_raw.csv` (parsed summary),
`stage3_multi_ovs_agent_rep{1..5}.log` (full JSON-lines agent event logs),
`stage3_multi_ovs_ping_rep{1..5}.log` (raw `ping` output).

**Independent corroboration from packet-level data.** The ping-gap column is
not read from the agent's own instrumentation: it comes from the ICMP
sequence numbers actually observed in the ping process's stdout, which ran
as an independent process on a different host reacting only to real
data-plane packet loss. In every repetition, exactly one gap appears in the
`icmp_seq` sequence, sized 8-9 missing packets at the ping's fixed 20 ms
send interval (Mininet/iputils `ping` does not print a line for a lost
packet, so a gap in the printed sequence numbers is the only signal of loss
available from this log; the true outage duration lies between
`missing_count` and `missing_count + 1` probe intervals, i.e. roughly
160-200 ms for a gap of 8-9 at 20 ms spacing) -- consistent with the
agent-reported repair-action time to within the ping interval's own
quantization. This is real, independently-collected evidence that the
reported repair timing reflects an actual, measurable interruption in the
end-to-end data path, not only an internal log timestamp.

No `ping_statistics` summary line is present in the raw logs: the harness
transfers each log file 5 s after fault injection to keep the per-repetition
loop bounded, before the 200-packet/20 ms ping (4 s nominal run time,
extended somewhat by the outage itself) has necessarily finished printing
its own summary and while it may still be running in the background on the
VM; this does not affect the gap analysis above, which only depends on the
`icmp_seq` numbers already printed by the time of transfer, not on the
process having exited.

This is a functional demonstration across five repetitions of a specific
scenario -- a single remote edge, fully owned by a second OVS instance, that
the agent has no local OVSDB connection to -- confirming consistent,
repeatable detection-and-repair behaviour with a mean repair-action time
(185.75 ms) in the same range as the single-host figure reported in Section
7.1 (157.67 ms), not a claim that cross-host repair is as fast as or faster
than local repair: this experiment does not isolate or control for the GRE
tunnel/TCP round-trip cost, and n=5 is reported as means and per-run values
only, per Section 6.5's stated replication policy.

## Claim boundary

This closes evidence-gate item 3 for the case actually built and measured:
two independent OVS instances, connected by a real GRE tunnel, with the
agent opening a remote OVSDB monitor connection and routing remote flow
calls correctly for a single edge it shares no local connection with. It
does not establish:

- Behaviour with more than two independent OVS instances, or more than one
  simultaneously-failing remote edge.
- Behaviour if a remote OVSDB or OpenFlow TCP connection itself drops
  mid-run -- there is no reconnect/resynchronization logic for a *remote*
  monitor connection, any more than Section 4.4's already-documented gap
  covers the local case (Section 8.3's monitor-subprocess-reconnect item).
- Performance under real (non-emulated) network latency between OVS
  instances -- the GRE tunnel here runs over a host-only virtual subnet
  between two VMs on the same physical machine, not a real WAN or even a
  real LAN link between separate hosts.
- Scale: the topology measured (4 switches split 1/3 across two hosts) is
  smaller than even the single-host diamond's scale claim already
  acknowledges as unaddressed (Section 8.3, "Single topology, single
  scale").
- Security or authentication of the remote OVSDB/OpenFlow TCP connections:
  both `ptcp:` listeners in this testbed are unauthenticated plaintext TCP,
  acceptable for a controlled testbed but not evaluated as, or claimed to
  be, a production-ready remote-management channel.
