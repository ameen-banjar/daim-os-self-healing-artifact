#!/bin/bash
# One-time setup for the multi-OVS deployment testbed (Section 6.8 of the
# manuscript): two independent Multipass VMs, each running its own
# ovsdb-server/ovs-vswitchd, connected by a real GRE tunnel so packets
# between them cross an actual L2 link rather than a shared bridge. This is
# not part of the automated per-repetition experiment
# (stage3_multi_ovs_deployment.sh) -- it is run once to stand the testbed up,
# and again only after a reboot/VM restart wipes live network state (OVS
# config on disk is unaffected by a VM stop/start; GRE ports, remote
# listeners, and Mininet's own process state are not).
#
# Preconditions: two Multipass Ubuntu 24.04 ARM64 VMs on the same host-only
# subnet, named daim-lab (VM1, this testbed's 192.168.252.2) and daim-lab-2
# (VM2, 192.168.252.3) -- adjust the IPs below if your subnet differs.
# vm1_topo.py and vm2_topo.py must already be running on their respective
# VMs (each started as: `sudo python3 vm1_topo.py` / `sudo python3
# vm2_topo.py`, left running in the foreground or under a persistent
# session, since each defines a topology and then sleeps forever so the
# commands below and the experiment script can be driven against it from
# other shells).
set -e

VM1_IP=192.168.252.2
VM2_IP=192.168.252.3

echo "--- VM1 (daim-lab): GRE tunnel port s1 -> VM2 ---"
multipass exec daim-lab -- sudo ovs-vsctl add-port s1 gre-to-vm2 \
  -- set interface gre-to-vm2 type=gre options:remote_ip=${VM2_IP}

echo "--- VM2 (daim-lab-2): GRE tunnel port s3 -> VM1 ---"
multipass exec daim-lab-2 -- sudo ovs-vsctl add-port s3 gre-to-vm1 \
  -- set interface gre-to-vm1 type=gre options:remote_ip=${VM1_IP}

echo "--- VM2: expose ovsdb-server over TCP so the agent (on VM1) can ---"
echo "--- open a remote OVSDB monitor connection to it              ---"
multipass exec daim-lab-2 -- sudo ovs-appctl -t ovsdb-server \
  ovsdb-server/add-remote ptcp:6640:0.0.0.0

echo "--- VM2: give s3/s4/s5 each their own passive OpenFlow listener ---"
echo "--- so apply_flow() can issue ovs-ofctl calls directly at them  ---"
echo "--- (functionally equivalent to a local bridge-name target --   ---"
echo "--- there is no real SDN controller in this experiment)         ---"
multipass exec daim-lab-2 -- sudo ovs-vsctl set-controller s3 ptcp:6634:0.0.0.0
multipass exec daim-lab-2 -- sudo ovs-vsctl set-controller s4 ptcp:6635:0.0.0.0
multipass exec daim-lab-2 -- sudo ovs-vsctl set-controller s5 ptcp:6636:0.0.0.0

echo "--- Verifying port numbers match multi_ovs_topology.json ---"
multipass exec daim-lab -- sudo ovs-ofctl -O OpenFlow13 show s1
multipass exec daim-lab-2 -- bash -c \
  "sudo ovs-ofctl -O OpenFlow13 show s3; sudo ovs-ofctl -O OpenFlow13 show s4; sudo ovs-ofctl -O OpenFlow13 show s5"

echo "Setup complete. Port numbers above must match multi_ovs_topology.json's"
echo "\"topology\" section exactly (Mininet assigns ports deterministically by"
echo "link-addition order, so a topology matching vm1_topo.py/vm2_topo.py"
echo "reproduces the same numbers every time)."
