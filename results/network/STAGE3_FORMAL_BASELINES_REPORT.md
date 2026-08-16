# Formal Baselines Report

Date: 16 August 2026
Environment: Ubuntu 24.04 LTS ARM64 (Multipass VM `daim-lab`), Open vSwitch 3.3.4, Mininet 2.3.0,
os-ken 2.6.0 -- exact version match to every prior live experiment in this evidence set, plus
`os-ken==2.6.0` pinned per this repository's own `experiments/environment/controller_requirements.txt`
(used for Paper 1's Stage-2 baseline controllers).
Evidence level: `measured_emulation_fast_failover_baseline` / `measured_emulation_controller_driven_baseline`

## What this closes

Section 10's remaining "formal baselines" evidence-gate item: an actual OpenFlow fast-failover-group
configuration and an actual controller-driven recovery path, both measured on the identical diamond
topology and `s1-s2` fault injection the agent's own Table 2 uses -- not the scripted
`local_repair`/`central_repair`/`two_loop` comparisons already in Section 7.1, which share the fault
injection and topology but not the detection mechanism, and were never claimed as a formal baseline.

## Part 1: Fast-failover group (`stage3_baseline_fast_failover.py`)

### Mechanism and a real limitation found before any measurement

A real OpenFlow13 `type=ff` group is pre-installed on `s1`: two buckets, `watch_port=2` (primary, to
`s2`) and `watch_port=3` (backup, to `s3`), with `s1`'s `h1`-facing flow set to `actions=group:1`. No
controller and no `daim_link_agent.py` process run at all -- OVS itself switches to the live bucket
the instant the watched port's carrier drops.

An empirical smoke test run before writing the measurement harness found a real, structural
limitation, confirmed via `ovs-ofctl dump-group-stats`: the group only reacts to the LOCAL switch's
own watched port. `s1`'s group correctly redirects `h1->h2` forward traffic to `s3` the instant its
port to `s2` drops. But `h2->h1` return traffic is forwarded by `s4` via a static flow, and `s4`'s own
directly-attached ports (to `s2`, to `s3`) never change state for this fault -- only `s1`'s (and
`s2`'s) port to EACH OTHER goes down, which `s4` cannot observe. A symmetric group at `s4` would not
help either: it would watch `s4`'s own port to `s2`, which never goes down for this specific fault.
This is not a topology-design artefact of this diamond -- it is the textbook scope of OpenFlow fast
failover (and IP Fast Reroute generally): local link/port protection, not network-wide reconvergence.

### Measurement design

Both directions are measured independently rather than only the one that recovers. Forward delivery
is observed via `tcpdump` on `h2` capturing incoming ICMP echo-requests directly (independent of
whether replies return) during a 250-packet, 20ms-interval `ping` from `h1`. Reverse delivery is a
plain `ping` from `h2` to `h1`, run after the forward probe completes. 5 repetitions.

### Results

| Rep | Forward missing (of 250) | Forward outage bound | Reverse recovered |
|---:|---:|---:|:---:|
| 1 | 8 | 140-180 ms | No (100% loss) |
| 2 | 3 | 40-80 ms | No (100% loss) |
| 3 | 9 | 160-200 ms | No (100% loss) |
| 4 | 4 | 60-100 ms | No (100% loss) |
| 5 | 0 | 0-20 ms | No (100% loss) |

**Forward direction: 5/5 recovered**, outage bound 0-200 ms across repetitions -- overlapping rather
than dramatically beating the agent's own 157.67 ms mean repair-action time, contrary to the naive
assumption that a pure-dataplane mechanism with zero control-plane round trip would be much faster.
The dominant cost here appears to be carrier-drop-to-liveness-flag propagation latency inside this
virtualised (Mininet/OVS) environment, not any software repair logic, since there is none.

**Reverse direction: 0/5 recovered** (100% packet loss in every repetition) -- not a harness defect,
the direct, measured consequence of the structural limitation above.

## Part 2: Controller-driven recovery (`stage3_baseline_controller_driven.py`)

### Mechanism

`osken_recovery_baseline_controller.py`, a new os_ken application, proactively installs the
primary-path flows (`s1,s2,s4`) on switch connect (switches given explicit, stable DPIDs 1-4),
subscribes to `EventOFPPortStatus`, and on detecting `s1`'s port to `s2` go down, pushes a hardcoded
two-path swap: delete/replace `s1`'s and `s4`'s flows, add `s3`'s previously-unused pass-through
flows, confirmed via an `OFPBarrierRequest`/`OFPBarrierReply` round trip. Unlike the fast-failover
baseline, a real controller has global topology visibility and reprograms every affected switch
(`s1` AND `s4`), so both directions are protected. Path recomputation is a hardcoded two-path swap,
not a general BFS -- representative of a scripted/hardcoded controller reaction, not a
reimplementation of `daim_link_agent.py`'s own generic algorithm. 5 repetitions, 80-packet probe at
the same interval as Table 2, for a directly matched comparison.

**A packaging gap found and worked around**: the pip-distributed `os-ken` package's latest release
(4.2.1) ships no `cmd`/manager console-script entry point (confirmed empirically: no `os_ken/cmd/`
module, no `entry_points.txt` in the installed wheel, no `osken-manager` on `PATH`). `os-ken==2.6.0`
-- the version this repository's own `controller_requirements.txt` already pins for Paper 1, with the
stated reason "Ryu 4.34 is incompatible with Ubuntu 24.04's setuptools" -- does ship it, confirmed by
installing it and finding `osken-manager` on `PATH`. A hand-written launcher (`osken_launcher.py`),
replicating the standard `AppManager`+`OpenFlowController` bring-up sequence any os_ken/ryu manager
script performs, was written and validated against the existing `osken_learning_controller.py` (a
real switch-controller OpenFlow connection, learning-switch forwarding confirmed via a 0%-loss ping)
before writing the recovery-specific application.

### Results

| Rep | Detection (ms) | Repair action (ms) | Packet loss (%) |
|---:|---:|---:|---:|
| 1 | 15.06 | 2.66 | 0.0 |
| 2 | 7.11 | 3.33 | 0.0 |
| 3 | 11.54 | 3.48 | 1.25 |
| 4 | 10.81 | 3.88 | 0.0 |
| 5 | 9.72 | 4.87 | 1.25 |
| **Mean** | **10.85** | **3.64** | **0.5** |

**5/5 repetitions correctly detected and repaired, both directions**, matching the fast-failover
baseline's forward-direction recovery but ALSO recovering the reverse direction the fast-failover
group structurally cannot.

## Comparison

| Mechanism | Detection | Repair action | Forward | Reverse |
|---|---:|---:|---|---|
| Autonomous agent (Table 2) | 3.55 ms | 157.67 ms | 9.0% mean loss, recovers | 9.0% mean loss, recovers |
| Fast-failover group | N/A (pure dataplane) | 40-200 ms outage bound | recovers, 5/5 | never recovers, 0/5 |
| Controller-driven | 10.85 ms mean | 3.64 ms mean | 0-1.25% loss, recovers | 0-1.25% loss, recovers |

The controller-driven baseline recovers FASTER than the agent on every measured metric here while
still protecting both directions, unlike the fast-failover group. This is not read as "a real SDN
controller beats DAIM-OS's own agent" in general: the controller baseline's repair logic is a
hardcoded two-path swap (6 FlowMod calls total, no dump-flows pre-check, no ambiguous-outcome
read-back, no rollback protocol, no generic BFS), not a like-for-like reimplementation of the
correctness machinery Section 5 of the manuscript describes. The comparison isolates the
OpenFlow-control-channel-round-trip cost against the agent's OVSDB-push + local-Python-BFS +
local-adapter-exec cost, on this one fault and topology -- a fair "cost of correctness" reading, not
a claim the agent is simply slower at the same task.

## What this does and does not establish

Establishes: both a real fast-failover-group configuration and a real controller-driven recovery path
are now measured on the identical topology and fault as the agent's own Table 2; a genuine, previously
undocumented structural limitation of fast-failover groups (forward-only recovery for a failure not
locally adjacent to both communicating endpoints); and a directly comparable timing baseline isolating
control-channel round-trip cost from the agent's correctness-machinery overhead. Does not establish:
behaviour on the multiple topologies of the companion topology/scale exploration (both baselines are
diamond-only); a statistically replicated dataset (n=5 here, matching Table 2's own count, not yet the
final replication count); or a fast-failover-plus-controller-fallback layered baseline (common in
production IP-FRR deployments), which would sidestep the fast-failover group's reverse-direction gap
but was not attempted here.

## Files

- `stage3_baseline_fast_failover_raw.csv`, `stage3_baseline_controller_driven_raw.csv` -- per-repetition
  raw data for both baselines.
- `experiments/network/stage3_baseline_fast_failover.py`, `stage3_baseline_controller_driven.py` --
  the two harness scripts.
- `experiments/network/osken_recovery_baseline_controller.py`, `osken_launcher.py` -- the
  controller-driven baseline's os_ken application and its launcher.
