# Stage 3 fault protocol

Two fault classes are measured independently, five repetitions each.

* `controller_failure`: install flows, start a local control-plane TCP endpoint,
  start the 80-packet ping, stop the endpoint, and measure whether existing
  OVS rules continue forwarding. This is a data-plane persistence test; it is
  not a claim that a controller performed recovery.
* `switch_failure`: install the primary path, bring down both links incident to
  `s2`, install the alternate path, and measure packet loss and action time.
  The reconfiguration is currently scripted, so autonomous switch-failure
  detection remains a separate required experiment.

Raw output is written to `results/network/stage3_controller_switch_faults_raw.csv`.
