# Multi-OVS Deployment Report

Date: 14 August 2026 (updated same day after an architecture-consistency fix
found by external review -- see "Adapter-unification fix" below)
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
- `apply_flow()` -- routes each install/withdraw call, for both local and
  remote bridges, through the same DAIM-OS OVS adapter binary
  (`daim_ovs_flow`) -- see "Adapter-unification fix" below for why this is
  worded this carefully.

Two new unit tests confirmed the connection-multiplexing extension is inert
unless opted into and routes correctly when it is
(`test_default_config_uses_single_local_ovsdb_target`,
`test_multi_ovs_target_routing`); two further unit tests, added by the
adapter-unification fix below, are described there.

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
  (`ovs-vsctl set-controller sN ptcp:663N:0.0.0.0`) so the DAIM-OS OVS
  adapter can reach them directly (there is no real SDN controller in this
  experiment -- this listener is functionally equivalent to a local
  bridge-name target for `ovs-ofctl`'s purposes).
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

## Adapter-unification fix

An external review of this exact report and the manuscript's §4.4 found a
real architecture-consistency defect: the manuscript's Abstract and core
contribution state that repair paths are installed "through the existing
DAIM-OS OVS adapter", but the first version of `apply_flow()`'s multi-OVS
extension bypassed that adapter for remote bridges and called `ovs-ofctl`
directly, only using the actual `daim_ovs_flow` adapter binary for local
bridges. The remote path *worked* -- this was not a correctness bug, the
n=5 results below reproduce identically before and after -- but it broke
the paper's own claim for exactly the new experiment meant to extend it.

Before assuming a C-code change was needed, this was checked empirically:
`daim_ovs_flow`/`ovs_switch_adapter.c` (Paper 1's adapter) already forward
their target argument as an opaque string into `ovs-ofctl -O OpenFlow13
<add-flow|del-flows> <target> <flow>`, with only a length/newline check
(`valid_token`, ≤63 chars). `ovs-ofctl` itself accepts either a local
bridge name or a remote `tcp:HOST:PORT` connection target at that
position. Confirmed live, before changing any code: `daim_ovs_flow add
tcp:192.168.252.3:6636 "priority=100,in_port=1,actions=output:2"` run on
VM1, targeting VM2's `s5` OpenFlow listener, installed a real flow on VM2
(verified with `ovs-ofctl dump-flows` on VM2 immediately after); the
matching `delete` call removed it. **The adapter already supported a
remote target with zero C-code changes** -- the fix was entirely in
`daim_link_agent.py`'s `apply_flow()`, which now always calls
`daim_ovs_flow`, substituting the switch's registered remote OpenFlow
target for the bridge-name argument when one is registered in
`REMOTE_ENDPOINTS`, instead of ever calling `ovs-ofctl` directly. Two new
unit tests were added:
`test_apply_flow_routes_remote_bridge_through_adapter` (captures the
actual subprocess argv and confirms both local and remote bridges invoke
the same `daim_ovs_flow` binary, differing only in the target argument)
and `test_execute_repair_reports_partial_failure_honestly` (see below).

**Same review also found a separate, real correctness gap**, unrelated to
which binary is invoked: `install_path()`/`withdraw_path()` discarded
`apply_flow()`'s per-call success/failure entirely, so `main()` could log
`repair_installed` and advance `current_path` even if one or more
flow-mod calls had failed -- a partially-failed installation (a remote
instance unreachable, a timeout) was silently reported as a clean repair.
`execute_repair()` is new: it now returns `repair_installed` and advances
`current_path` only if *every* flow-mod call across withdraw and install
succeeded; otherwise it returns a distinct `repair_incomplete` event with
`current_path` left unchanged. This does **not** implement the two-phase
staged/commit/rollback protocol Section 5.2 specifies -- flows that did
succeed before a failure are not undone, and the network can still be left
in a mixed old/new state exactly as Section 4.6 already documents -- it
only stops the agent from claiming a success it did not achieve.

The n=5 live measurement below was re-run in full against the fixed code
(not reused from the pre-fix version) for both empirical honesty and to
check whether routing remote calls through an extra process (`daim_ovs_flow`
itself `posix_spawn`s `ovs-ofctl`, so the adapter-unified remote path now
spawns two processes per remote flow-mod call instead of one) measurably
changed repair timing. It does, modestly: mean repair-action time rose
from 185.75 ms (pre-fix, direct `ovs-ofctl`, raw data retained as
`..._pre_adapter_unification.*`) to **191.48 ms** (post-fix, through the
adapter), a difference of about 5.7 ms attributable to the extra
process-spawn hop. This is reported as the honest cost of closing the
architecture-consistency finding, not hidden or averaged away.

## Method

`stage3_multi_ovs_deployment.sh` repeats the following five times: reset
link state and flow tables on both VMs and kill any stray agent/monitor
process; start the agent fresh on VM1, logging every event to a per-rep
JSON-lines file; start a 200-packet, 20 ms-interval ping from `h1` (in its
Mininet network namespace on VM1) to `h2` (on VM2); bring both `s3-eth1`
and `s4-eth1` down on VM2 (the complete `s3-s4` edge, injected entirely on
the remote host); wait 5 s; stop the agent with SIGTERM; transfer both the
agent log and the ping output back for analysis.

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
withdrawn/installed across the local `s1` and the remote `s3`/`s4`/`s5`,
every call in every repetition succeeding -- no `repair_incomplete` event
was ever produced) &rarr; `transition_suppressed` (`interface: "s3-eth1"`,
the redundant report of the same edge, correctly absorbed by the existing
edge-keyed hold-down window) &rarr; clean SIGTERM shutdown terminating both
monitor subprocesses. No special-casing was needed anywhere in this
sequence for the multi-OVS case: edge-keying, edge-confirmation, and the
startup-snapshot fix all continued to behave exactly as they do in the
single-host measurements.

**Table.** Per-repetition repair-action timing
(`repair_start_ns`&rarr;`repair_end_ns`, the same instrumentation used for
Section 7.1's single-host figures) and independent packet-level
corroboration from the concurrent ping stream.

| Rep | Repaired path | Repair-action time (ms) | Consecutive missing pings | Outage bound (ms) |
|---:|---|---:|---:|---:|
| 1 | s1, s3, s5, s4 | 198.55 | 10 | 180–220 |
| 2 | s1, s3, s5, s4 | 194.30 | 9 | 160–200 |
| 3 | s1, s3, s5, s4 | 191.58 | 9 | 160–200 |
| 4 | s1, s3, s5, s4 | 188.30 | 9 | 160–200 |
| 5 | s1, s3, s5, s4 | 184.69 | 8 | 140–180 |
| **Mean** | | **191.48** | | |

Raw data: `stage3_multi_ovs_raw.csv` (parsed summary, post-fix),
`stage3_multi_ovs_agent_rep{1..5}.log` / `stage3_multi_ovs_ping_rep{1..5}.log`
(post-fix, current numbers above); pre-fix data retained as
`stage3_multi_ovs_{agent,ping}_rep{1..5}_pre_adapter_unification.log` and
`stage3_multi_ovs_raw_pre_adapter_unification.csv`, per this evidence set's
convention of keeping a defect's before/after data side by side rather than
overwriting it.

**Independent corroboration from packet-level data, and a corrected bound.**
The "Consecutive missing pings" column is not read from the agent's own
instrumentation: it comes from the ICMP sequence numbers actually observed
in the ping process's stdout -- an independent probe process running in
`h1`'s Mininet network namespace on VM1 (the same physical VM the agent
runs on, though a separate OS process reacting only to real data-plane
outcomes, not to the agent's internal state), whose traffic physically
traverses the inter-VM GRE path to `h2` on VM2 and back. Mininet/iputils
`ping` prints no line for a lost packet, so a gap in the printed sequence
numbers is the only signal of loss available from this log.

An earlier version of this report stated the true outage duration "lies
between `missing_count` and `missing_count + 1` probe intervals" -- this
was not the correct bound. If `N` consecutive probes are lost at a fixed
interval `Δ`, and the probes immediately before and after the gap both
succeeded, the only thing that can be inferred is that the outage started
strictly after the last successful probe and ended strictly before the
next successful one: the true outage duration `T` satisfies
`(N-1)Δ < T < (N+1)Δ`, not the tighter (and wrong) bound stated before.
For `Δ=20 ms` and the gaps observed here (8-10 missing packets), that is a
range of roughly 140-220 ms depending on the repetition (see the table's
"Outage bound" column) -- consistent with, but not a tight independent
confirmation of, the agent-reported repair-action times (184.7-198.5 ms),
which fall inside every repetition's bound. This is real, independently-
collected evidence that the reported repair timing corresponds to an
actual, measurable interruption in the end-to-end data path of a
plausible magnitude, not only an internal log timestamp -- but it should
be read as a consistency check against a wide bound, not a precise
independent measurement of the outage. A future revision should instrument
`t_last_success_before_failure`/`t_first_success_after_failure` directly
(e.g. via timestamped packet capture) for a genuine independent
service-restoration measurement, per Section 10's item on decomposing
service restoration.

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
(191.48 ms) in the same range as the single-host figure reported in Section
7.1 (157.67 ms), not a claim that cross-host repair is as fast as or faster
than local repair: this experiment does not isolate or control for the GRE
tunnel/TCP round-trip cost or the adapter's extra process-spawn hop, and
n=5 is reported as means and per-run values only, per Section 6.5's stated
replication policy.

## Claim boundary

This closes evidence-gate item 3 for the case actually built and measured:
two independent OVS instances, connected by a real GRE tunnel, with the
agent opening a remote OVSDB monitor connection and routing remote flow
calls -- now genuinely through the same DAIM-OS OVS adapter used for local
flows -- correctly for a single edge it shares no local connection with. It
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
- Recovery from a partial installation failure: `execute_repair()` now
  reports a partial failure honestly (`repair_incomplete`, `current_path`
  unchanged) instead of misreporting it as success, but it still does not
  roll back flows that succeeded before a failure, or retry -- that is
  Section 5.2's two-phase protocol, not yet implemented.
