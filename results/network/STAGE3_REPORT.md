# Stage 3 link-recovery report

Date: 18 July 2026  
Environment: Ubuntu 24.04 ARM64, Open vSwitch 3.3.4, Mininet 2.3.0  
Evidence: `measured_emulation_intervention`

Fifteen runs used a four-switch diamond topology with an alternate path. During
an 80-packet ping, the `s1-s2` link was brought down. The repair action then
installed alternate-path rules in one of three scripted modes: immediate
`local_repair`, delayed `central_repair` (100 ms), and immediate `two_loop`.

| Mode | Runs | Mean loss | Mean failure-to-action | Mean repair-action time |
|---|---:|---:|---:|---:|
| local_repair | 5 | 8.25% | 2.91 ms | 148.91 ms |
| two_loop | 5 | 8.75% | 2.51 ms | 150.38 ms |
| central_repair | 5 | 14.25% | 106.40 ms | 152.65 ms |

These results show the expected cost of the injected 100-ms delay and confirm
that the alternate path can be exercised. They do not establish autonomous
failure detection, failed-switch/controller recovery, or a general self-healing
advantage. Those require event-driven detection, additional fault classes,
repeated seeds, and a persistent controller baseline.
