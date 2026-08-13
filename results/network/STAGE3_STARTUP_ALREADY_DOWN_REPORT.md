# Startup State Synchronization Report

Date: 13 August 2026
Environment: Multipass Ubuntu 24.04 LTS ARM64 VM (`daim-lab`), Open vSwitch 3.3.4,
Mininet 2.3.0 -- same environment as the other live-network measurements
in this evidence set.
Evidence level: `measured_emulation_startup_already_down`

## What this closes

A code review of `daim_link_agent.py` (not a test failure, and not
something the existing live-network measurements happened to exercise)
found that `_parse_monitor_line()` only ever matched OVSDB rows whose
`action` field was `"new"`. This is correct for real transitions, but not
for the very first line `ovsdb-client monitor` sends: OVSDB reports a
subscribed table's *current contents* as its initial reply, with every one
of those rows carrying `action=="initial"`, not `"new"`. This was verified
empirically against a real `ovsdb-server` in the VM (not assumed from
documentation): a fresh `ovsdb-client monitor Interface name,link_state
--format=json` against a live OVS bridge sends exactly one row per
interface with `"action":"initial"` on its first line, and only later,
genuine transitions send an `"old"`/`"new"` row pair.

The consequence: if a monitored interface was already `down` when the
agent started (or restarted after a crash, deploy, or planned bounce), the
agent had no way to find out. `main()` always computed and installed its
initial path assuming `down_edges = set()`, unconditionally. If the
already-down interface never produced a further transition -- true by
definition, since its state was not changing -- nothing would ever correct
the agent's belief that the network was fully up.

## The fix

- `_parse_monitor_line(line, actions=("new",))` -- extended with an
  `actions` parameter so the same parser can extract either `"new"` rows
  (the default, unchanged behaviour for the ongoing event stream) or
  `"initial"` rows (used once, at startup).
- `read_initial_snapshot(proc, timeout=10.0)` -- new. Reads exactly the
  first line the monitor subprocess sends, parses its `"initial"` rows, and
  returns `{interface_name: link_state}` for every *monitored* interface
  reported in it. A `MONITORED_INTERFACES` entry missing from this snapshot
  is treated as a fatal misconfiguration in `main()`, not silently defaulted.
- `down_edges_from_snapshot(snapshot)` -- new, pure helper turning that
  snapshot into the initial `down_edges` set; pulled out on its own so the
  same logic `main()` runs is exercised directly by a unit test.
- `main()` restructured: starts the monitor subprocess first, reads and
  applies the initial snapshot (seeding both `down_edges` and the
  persistent `interface_state` dict from the edge-confirmation fix), and
  only *then* computes and installs the agent's first path -- instead of
  always starting from an assumed-clean `down_edges = set()`.

Four new regression tests (`test_parse_monitor_line_initial_vs_new`,
`test_read_initial_snapshot_reflects_already_down_interface`,
`test_startup_detects_already_down_edge`,
`test_startup_rejects_unexpected_link_state`) cover the parser distinction,
the snapshot-reading function against a real OS pipe, the downstream
path-selection logic, and rejection of an unrecognised `link_state` value
(found by review: `down_edges_from_snapshot` originally treated anything
other than `"down"` as implicitly up, including an empty string or unknown
value OVS documents `Interface.link_state` as capable of carrying -- fixed
to require exactly `"up"` or `"down"`, raising otherwise). `main()` was
also wrapped so a fatal startup path (missing interface, no viable initial
path, or an unrecognised `link_state`) still terminates the monitor
subprocess rather than leaking it, matching the existing SIGTERM/SIGINT
handler's cleanup for the signal-driven case. All 10 unit tests pass (the 6
from the prior two fixes, unmodified, plus these 4).

## A bug in this report's own verification harness

A second code review, of `stage3_startup_already_down.py` itself, found
that its original `ping_had_loss` field was computed with
`"0% packet loss" not in ping_output`, a substring check -- and `"100%
packet loss"` contains `"0% packet loss"` as a substring (the trailing
`"0%"` of `"100%"`), so the 100%-loss pre-fix run was silently reported as
`ping_had_loss: false` in the raw stored JSON. The script's `correct` field
was unaffected, because it never used `ping_had_loss` in its own success
condition (it already correctly failed the pre-fix run on the wrong
`initial_path`/`down_edges` fields), and neither did the figure-generation
script, which parsed the numeric loss percentage directly out of
`ping_output_tail` rather than through this boolean -- so Figure 7's 100%
vs. 0% headline numbers were never wrong. The harness itself was fixed with
a proper `parse_ping_loss_pct()` function (with its own regression test,
`test_stage3_startup_already_down.py`, reproducing the exact "100%
packet loss" string that triggered the bug), and `correct` now also
requires `ping_loss_pct == 0.0` for the fixed-agent run explicitly, rather
than only checking the agent's own reported path/edges.

## Live-network result

`stage3_startup_already_down.py` brings the `s1`-`s2` link down with
`net.configLinkStatus` *before* the agent process is even started, then
reads the agent's own `agent_started` log line and immediately runs a
20-packet ping with no fault injected during the run -- the fault is
already present at the moment the agent starts.

Run three times against the current (fixed) agent as a robustness check
(all three produced the identical `CORRECT` result, `0%` loss), and once,
for direct comparison, against the unmodified pre-fix agent from the
`v0.4.0` artifact release:

| | Initial path installed | Edge s1-s2 detected down at startup? | Ping packet loss |
|---|---|---:|---:|
| **Before (v0.4.0, buggy)** | `s1, s2, s4` (through the dead link) | no | **100%** |
| **After (fixed)** | `s1, s3, s4` (correct alternate) | yes | **0%** |

The pre-fix agent installed the primary path straight through the
already-dead `s1`-`s2` link and stayed that way indefinitely: the ping ran
to completion with `20 packets transmitted, 0 received, 100% packet loss`,
and nothing in the pre-fix agent's design would ever have corrected this
without a further transition on `s1-eth2`/`s2-eth1`, which by construction
was never going to arrive. The fixed agent's `agent_started` log line
reports `down_edges: [["s1", "s2"]]` and `initial_path: ["s1", "s3",
"s4"]`, and the ping ran with `0% packet loss` from the first packet.

This is a functional before/after correctness demonstration, not a
statistical performance estimate -- a link is either correctly detected as
down at startup or it is not -- so no inferential statistics are applied;
the fixed-agent repetitions are reported to show the result is repeatable,
not to support a percentile or significance claim.

## Claim boundary

This closes initial topology-state synchronization for a *fresh* agent
process, on the single-link, single-topology scenario tested: an interface
already down when the agent starts is now correctly detected from the
OVSDB initial snapshot and routed around before the first flow is
installed. It does not establish: behaviour when *multiple* monitored
interfaces are down simultaneously at startup in more complex topologies;
behaviour if the initial snapshot itself is delivered across more than one
line (not observed in this environment, but not proven impossible on a
larger table); a full *process restart* where OVS already holds flow state
installed by a previous agent instance (this experiment starts a fresh
testbed, it does not simulate an agent restarting against a live prior
deployment, and the current implementation has no reconciliation logic for
pre-existing flows beyond recomputing and reinstalling a path over whatever
is already there); or reconnect/resynchronization behaviour if the
`ovsdb-client monitor` subprocess dies and is restarted while the agent's
own process keeps running, which remains a separate, unaddressed
reliability gap.
