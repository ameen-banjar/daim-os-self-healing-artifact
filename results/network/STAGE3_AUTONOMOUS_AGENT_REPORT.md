# Stage 3 Autonomous Link-Recovery Agent Report

Date: 18 July 2026
Environment: Ubuntu 24.04 ARM64, Open vSwitch 3.3.4, Mininet 2.3.0
Evidence level: `measured_emulation_autonomous_agent`

## What this closes

`STAGE3_REPORT.md`'s `local_repair`/`central_repair`/`two_loop` modes and
`stage3_link_recovery.py`'s `autonomous_link` mode all called a pre-written
`install_alternate()` function from inside the test harness -- the harness
knew in advance which link would fail and which flows to install/delete.
This experiment replaces that with `network/daim_link_agent.py`, an
independent, long-running process that:

1. subscribes to real OVSDB `Interface.link_state` change notifications via
   `ovsdb-client monitor` (a blocking read on a push subscription, not a
   polling loop);
2. on a down event, removes the failed edge from a declared topology graph
   and recomputes a path with breadth-first search -- the alternate path is
   computed at failure time, not hard-coded;
3. installs/withdraws the resulting flows through the existing
   `daim_ovs_flow` CLI (the DAIM OVS adapter).

`network/test_daim_link_agent.py` is a pure-logic unit test that checks the
agent's BFS path computation reproduces exactly the flow sets that were
previously hand-written in `install_primary()`/`install_alternate()`, so the
graph search is verified independently of any live network before being
trusted against a real failure.

## Test

`network/stage3_autonomous_agent.py` runs the same four-switch diamond
topology as `stage3_link_recovery.py`. It starts the topology, starts
`daim_link_agent.py` as a background process (which installs the initial
primary path itself), starts an 80-packet ping, and brings `s1-s2` down via
`net.configLinkStatus`. The harness calls no repair function; it only merges
the agent's own timestamped event log with the ping result. Five repetitions
were run.

## Result

All 5 repetitions detected the failure and completed a repair with the
correct alternate path (`s1,s3,s4`):

| Repetition | Loss | Detection | Repair action |
|---|---:|---:|---:|
| 1 | 8.75% | 2.88 ms | 152.73 ms |
| 2 | 10.0% | 5.45 ms | 156.54 ms |
| 3 | 10.0% | 5.33 ms | 178.07 ms |
| 4 | 8.75% | 2.08 ms | 151.76 ms |
| 5 | 7.5% | 2.00 ms | 149.24 ms |

Mean packet loss 9.0%, mean detection 3.55 ms, mean repair action 157.67 ms
(total failure-to-repair-complete ≈161 ms). Raw data:
`stage3_autonomous_agent_raw.csv`.

## Comparison with the scripted intervention

The mean loss here (9.0%) is close to `STAGE3_REPORT.md`'s `local_repair`
mean (8.25%) and lower than its `central_repair` mean (14.25%, with a
deliberately injected 100 ms delay). This is expected -- both this run and
`local_repair` react promptly -- but the two are not the same experiment: here
detection and path computation are performed by an independent process
reacting to a real OVSDB event it was not told to expect, not by the harness
calling `install_alternate()` immediately after injecting the fault it just
injected.

## Claim boundary

This is genuine autonomous detection and repair for a single link-down fault
on one declared topology. It does not yet demonstrate:

- topology discovery (the switch graph is a declared Python dict, not learned
  via LLDP or `DAIM_LINK_TABLE`);
- switch, controller, or multi-link failures;
- coordination between multiple agents, or conflict/rollback semantics
  (Paper 4's scope);
- an optimality or safety proof for the computed path, beyond the unit test's
  check against the two specific paths in this topology;
- self-healing at a scale beyond the four-switch diamond.

## Bugs found and fixed while building this

Two real bugs surfaced during testing, both fixed in `daim_link_agent.py`,
kept here because they were non-obvious and would otherwise recur:

1. `del-flows` match syntax does not accept `priority=...`; the agent's
   withdraw step originally reused the `add-flow` string unmodified and
   every deletion failed silently (logged as `flow_error` but not fatal).
2. `ovsdb-client monitor` is a child process that outlives its parent dying
   from SIGTERM (Python does not kill a process's children automatically).
   Repeated test runs leaked dozens of orphaned `ovsdb-client monitor`
   connections to the same `ovsdb-server`, which is a real resource leak for
   any future long-running deployment. The agent now installs a SIGTERM/SIGINT
   handler that terminates its `ovsdb-client` child before exiting.

A third issue was in the test harness, not the agent: calling
`ping.communicate()` (blocking) before draining the agent's event pipe
starved the concurrent read of the agent's output under Mininet's
host-namespace `popen`, even though the agent itself had already completed
the repair in milliseconds. The harness now reads the ping and the agent's
event stream concurrently via `select`.
