#!/bin/bash
# Orchestrates n=5 repetitions of the multi-OVS remote-edge-failure
# experiment (Section 6.8/7.7 of the manuscript) across daim-lab (VM1:
# h1--s1, runs the agent) and daim-lab-2 (VM2: s3--s4--h2 primary,
# s3--s5--s4 alternate, all local to VM2's own OVSDB instance). Requires
# stage3_multi_ovs_setup.sh to have been run first (GRE tunnel + remote
# OVSDB/OpenFlow listeners already up) and both vm1_topo.py/vm2_topo.py
# already running.
#
# Each repetition: reset link state + flows on both VMs, start the agent
# fresh on VM1 with DAIM_TOPOLOGY_CONFIG pointing at multi_ovs_topology.json,
# ping h1->h2, inject the s3-s4 failure (both interfaces, entirely on
# VM2/remote -- the agent has no *local* OVSDB connection to either side of
# this edge), wait, stop the agent, save its JSON log + ping output.
#
# rwt (run-with-timeout) wraps every multipass-exec call that launches a
# background/detached remote process (agent start, ping start): this
# specific combination (multipass exec + remote setsid/nohup/disown) has
# been observed, on the macOS host this was developed on, to leave the
# *wrapping* multipass-exec call hanging indefinitely even though the
# remote command it launched completes and detaches correctly within under
# a second -- confirmed by inspecting the remote process/log directly while
# the local call was still blocked. The timeout only bounds the local
# wrapper call (killing it does not kill the already-detached remote
# process, verified separately); it does not kill the remote process, which
# is independently verified via its log file before the script proceeds.
# macOS ships neither `timeout` nor `gtimeout`, hence this portable bash
# implementation; on a host that has GNU coreutils this wrapper is
# unnecessary but harmless.
set -e

rwt() {
  local secs=$1; shift
  "$@" &
  local pid=$!
  local count=0
  local limit=$((secs * 2))
  while kill -0 "$pid" 2>/dev/null; do
    sleep 0.5
    count=$((count + 1))
    if [ "$count" -ge "$limit" ]; then
      kill -9 "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
  done
  wait "$pid"
}

REPS=${REPS:-5}
# PID (on VM1) of the `bash --norc --noediting -is mininet:h1` process
# Mininet spawns for h1 -- discover with:
#   multipass exec daim-lab -- bash -c "ps aux | grep 'mininet:h1' | grep -v grep"
H1PID=${H1PID:?set H1PID to the mininet:h1 shell PID on VM1}
OUTDIR=${OUTDIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../results/network" && pwd)}
mkdir -p "$OUTDIR"

for i in $(seq 1 "$REPS"); do
  echo "=== repetition $i ==="

  # Reset: link up, flows cleared, any stray agent/monitor killed.
  rwt 10 multipass exec daim-lab -- bash -c "sudo pkill -9 -f 'daim_link_agent.py' 2>/dev/null; sudo pkill -9 -f 'ovsdb-client monitor' 2>/dev/null; sleep 0.5; sudo ovs-ofctl -O OpenFlow13 del-flows s1" || true
  rwt 10 multipass exec daim-lab-2 -- bash -c "sudo ip link set s3-eth1 up; sudo ip link set s4-eth1 up; sleep 0.5; sudo ovs-ofctl -O OpenFlow13 del-flows s3; sudo ovs-ofctl -O OpenFlow13 del-flows s4; sudo ovs-ofctl -O OpenFlow13 del-flows s5" || true
  sleep 1

  # Start the agent fresh, logging to a per-rep file. Wrapped in rwt per the
  # note above; the actual start is verified below by polling the log file
  # via a separate (fast, non-hanging) multipass exec call.
  rwt 8 multipass exec daim-lab -- bash -c "cd /home/ubuntu/daim/experiments/network && sudo -E env DAIM_TOPOLOGY_CONFIG=/home/ubuntu/daim/experiments/network/multi_ovs_topology.json setsid nohup python3 daim_link_agent.py > /home/ubuntu/multi_agent_rep${i}.log 2>&1 < /dev/null &
disown
sleep 0.3
echo started" || echo "(start call timed out locally -- checking remote log directly)"

  # Wait for agent_started to appear (bounded, independent of the call above).
  for attempt in $(seq 1 20); do
    if rwt 5 multipass exec daim-lab -- grep -q agent_started /home/ubuntu/multi_agent_rep${i}.log 2>/dev/null; then
      break
    fi
    sleep 0.5
  done

  # Start ping in background on h1 (200 pkts, 20ms interval).
  rwt 8 multipass exec daim-lab -- bash -c "sudo mnexec -a $H1PID ping -c 200 -i 0.02 -W 1 10.0.0.2 > /home/ubuntu/multi_ping_rep${i}.log 2>&1 &
disown
echo ping_started" || echo "(ping start call timed out locally -- continuing)"
  sleep 0.5

  # Inject the remote-edge failure: bring down both interfaces of s3-s4 on VM2.
  rwt 10 multipass exec daim-lab-2 -- bash -c "sudo ip link set s3-eth1 down; sudo ip link set s4-eth1 down" || echo "(fault-injection call timed out locally -- verify via agent log)"
  echo "fault injected for rep $i at $(date +%s.%N)"

  # Let the ping run past the outage and settle.
  sleep 5

  # Stop the agent (SIGTERM, exercising the cleanup path).
  rwt 10 multipass exec daim-lab -- bash -c "sudo pkill -TERM -f 'daim_link_agent.py' 2>/dev/null" || true
  sleep 1

  # Pull results back.
  rwt 15 multipass transfer daim-lab:/home/ubuntu/multi_agent_rep${i}.log "$OUTDIR/stage3_multi_ovs_agent_rep${i}.log" || echo "(agent log transfer timed out locally -- retry after loop)"
  rwt 15 multipass transfer daim-lab:/home/ubuntu/multi_ping_rep${i}.log "$OUTDIR/stage3_multi_ovs_ping_rep${i}.log" || echo "(ping log transfer timed out locally -- retry after loop)"

  echo "=== repetition $i done ==="
done

echo "All repetitions complete. Results in $OUTDIR"
