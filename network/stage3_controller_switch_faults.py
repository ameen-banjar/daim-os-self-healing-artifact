#!/usr/bin/env python3
"""Control-plane loss and complete-switch failure experiments.

The control-plane run deliberately keeps installed OVS rules in place and
stops a small TCP endpoint; it therefore measures data-plane persistence, not
controller-driven recovery. The switch run removes all links to s2 and then
installs the documented alternate path.
"""
import csv, re, socket, subprocess, threading, time
from pathlib import Path
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "implementation/build/daim_ovs_flow"
RAW = ROOT / "results/network/stage3_controller_switch_faults_raw.csv"
REPETITIONS = 5

class DiamondTopo(Topo):
    def build(self):
        for i in range(1, 5): self.addSwitch(f"s{i}", protocols="OpenFlow13", failMode="secure")
        self.addHost("h1", ip="10.0.0.1/24"); self.addHost("h2", ip="10.0.0.2/24")
        for a,b in [("h1","s1"),("s1","s2"),("s2","s4"),("s1","s3"),("s3","s4"),("s4","h2")]: self.addLink(a,b)

def flow(action, bridge, match):
    p = subprocess.run([str(CLI), action, bridge, match], text=True, capture_output=True)
    if p.returncode: raise RuntimeError(p.stderr)

def primary():
    for b,m in [("s1","priority=100,in_port=1,actions=output:2"),("s2","priority=100,in_port=1,actions=output:2"),("s4","priority=100,in_port=1,actions=output:3"),("s4","priority=100,in_port=3,actions=output:1"),("s2","priority=100,in_port=2,actions=output:1"),("s1","priority=100,in_port=2,actions=output:1")]: flow("add",b,m)

def alternate():
    for b,m in [("s1","in_port=1"),("s2","in_port=1"),("s4","in_port=1"),("s4","in_port=3"),("s2","in_port=2"),("s1","in_port=2")]: flow("delete",b,m)
    for b,m in [("s1","priority=100,in_port=1,actions=output:3"),("s3","priority=100,in_port=1,actions=output:2"),("s4","priority=100,in_port=2,actions=output:3"),("s4","priority=100,in_port=3,actions=output:2"),("s3","priority=100,in_port=2,actions=output:1"),("s1","priority=100,in_port=3,actions=output:1")]: flow("add",b,m)

def ping_stats(output):
    s=re.search(r"(\d+) packets transmitted",output); r=re.search(r"(\d+) received",output); l=re.search(r"([0-9.]+)% packet loss",output)
    return int(s.group(1)), int(r.group(1)), float(l.group(1))

class Endpoint:
    def __init__(self): self.stop=threading.Event(); self.sock=socket.socket(); self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); self.sock.bind(("127.0.0.1",6653)); self.sock.listen(8); self.sock.settimeout(.1)
    def run(self):
        while not self.stop.is_set():
            try: c,_=self.sock.accept(); c.close()
            except socket.timeout: pass
    def close(self): self.stop.set(); self.sock.close()

def run_one(kind, rep):
    subprocess.run(["mn","-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ep=Endpoint() if kind=="controller_failure" else None
    net=Mininet(topo=DiamondTopo(), controller=None, switch=OVSSwitch, autoSetMacs=True)
    try:
        net.start(); primary(); h1,h2=net.get("h1","h2")
        p=h1.popen(["ping","-c","80","-i","0.02","-W","1","10.0.0.2"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True); time.sleep(.15)
        failure=time.perf_counter_ns()
        if kind=="controller_failure":
            ep.close()                 # rules remain in the switches
            action=failure; action_type="data_plane_persistence"
        else:
            for link in [("s1","s2"),("s2","s4")]: net.configLinkStatus(*link,"down")
            action_start=time.perf_counter_ns(); alternate(); action=time.perf_counter_ns(); action_type="alternate_path_reconfiguration"
        out,_=p.communicate(timeout=20); sent,received,loss=ping_stats(out)
        return {"evidence_level":"measured_emulation_fault_injection","fault":kind,"repetition":rep,"packets_sent":sent,"packets_received":received,"packet_loss_pct":loss,"failure_to_action_us":(action-failure)/1000.0,"recovery_action_us":(action-action_start)/1000.0 if kind!="controller_failure" else 0.0,"action_type":action_type}
    finally:
        if ep: ep.close()
        net.stop(); subprocess.run(["mn","-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    setLogLevel("warning"); rows=[]
    for kind in ["controller_failure","switch_failure"]:
        for rep in range(1,REPETITIONS+1): print(f"fault={kind} repetition={rep}", flush=True); rows.append(run_one(kind,rep))
    RAW.parent.mkdir(parents=True,exist_ok=True)
    with RAW.open("w",newline="") as f: w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(f"wrote {len(rows)} rows to {RAW}")

if __name__=="__main__": main()
