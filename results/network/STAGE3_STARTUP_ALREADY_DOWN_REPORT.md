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

Three new regression tests (`test_parse_monitor_line_initial_vs_new`,
`test_read_initial_snapshot_reflects_already_down_interface`,
`test_startup_detects_already_down_edge`) cover the parser distinction, the
snapshot-reading function against a real OS pipe, and the downstream
path-selection logic, respectively. All 9 unit tests pass (the 6 from the
prior two fixes, unmodified, plus these 3).

## Live-network result

`stage3_startup_already_down.py` brings the `s1`-`s2` link down with
`net.configLinkStatus` *before* the agent process is even started, then
reads the agent's own `agent_started` log line and immediately runs a
20-packet ping with no fault injected during the run -- the fault is
already present at the moment the agent starts.

Run once against the current (fixed) agent and once, for direct
comparison, against the unmodified pre-fix agent from the `v0.4.0` artifact
release:

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

This is a single deterministic repetition each, not a sampled measurement
-- the outcome (total failure vs. immediate correct routing) does not vary
run to run for this scenario, so `n=1` is reported as the demonstration it
is, not with any statistical framing.

## Claim boundary

This closes a real startup/restart correctness gap for the single-link,
single-topology scenario tested: an interface already down at agent start
is now correctly detected from the OVSDB initial snapshot and routed
around before the first flow is installed. It does not establish: behaviour
when *multiple* monitored interfaces are down simultaneously at startup in
more complex topologies; behaviour if the initial snapshot itself is
delivered across more than one line (not observed in this environment, but
not proven impossible on a larger table); or reconnect/resynchronization
behaviour if the `ovsdb-client monitor` subprocess dies and is restarted
mid-run, which remains a separate, unaddressed reliability gap.
