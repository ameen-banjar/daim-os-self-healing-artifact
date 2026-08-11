#!/usr/bin/env python3
"""Stage-3 link-failure/recovery emulation with two paths."""
import csv, re, subprocess, time, threading
from pathlib import Path
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "implementation/build/daim_ovs_flow"
RAW = ROOT / "results/network/stage3_link_recovery_raw.csv"
MODES = ["autonomous_link", "local_repair", "central_repair", "two_loop"]
REPETITIONS = 5

class DiamondTopo(Topo):
    def build(self):
        s1,s2,s3,s4=[self.addSwitch(f"s{i}",protocols="OpenFlow13",failMode="secure") for i in range(1,5)]
        h1=self.addHost("h1",ip="10.0.0.1/24"); h2=self.addHost("h2",ip="10.0.0.2/24")
        self.addLink(h1,s1); self.addLink(s1,s2); self.addLink(s2,s4); self.addLink(s1,s3); self.addLink(s3,s4); self.addLink(s4,h2)

def flow(action,bridge,match):
    r=subprocess.run([str(CLI),action,bridge,match],text=True,capture_output=True)
    if r.returncode: raise RuntimeError(f"{action} {bridge} {match}: {r.stderr}")

def install_primary():
    for b,m in [("s1","priority=100,in_port=1,actions=output:2"),("s2","priority=100,in_port=1,actions=output:2"),("s4","priority=100,in_port=1,actions=output:3"),("s4","priority=100,in_port=3,actions=output:1"),("s2","priority=100,in_port=2,actions=output:1"),("s1","priority=100,in_port=2,actions=output:1")]: flow("add",b,m)

def install_alternate():
    for b,m in [("s1","in_port=1"),("s2","in_port=1"),("s4","in_port=1"),("s4","in_port=3"),("s2","in_port=2"),("s1","in_port=2")]: flow("delete",b,m)
    for b,m in [("s1","priority=100,in_port=1,actions=output:3"),("s3","priority=100,in_port=1,actions=output:2"),("s4","priority=100,in_port=2,actions=output:3"),("s4","priority=100,in_port=3,actions=output:2"),("s3","priority=100,in_port=2,actions=output:1"),("s1","priority=100,in_port=3,actions=output:1")]: flow("add",b,m)

def parse_ping(output):
    sent=re.search(r"(\d+) packets transmitted",output); received=re.search(r"(\d+) received",output); loss=re.search(r"([0-9.]+)% packet loss",output)
    return {"packets_sent":int(sent.group(1)) if sent else 0,"packets_received":int(received.group(1)) if received else 0,"packet_loss_pct":float(loss.group(1)) if loss else 100.0}

def link_is_down():
    """Poll OVS state, modelling an event monitor rather than a repair call."""
    p = subprocess.run(["ovs-vsctl", "get", "Interface", "s1-eth2", "link_state"],
                       text=True, capture_output=True)
    return p.returncode == 0 and "down" in p.stdout

def run_one(mode,repetition):
    subprocess.run(["mn","-c"],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); net=Mininet(topo=DiamondTopo(),controller=None,switch=OVSSwitch,link=TCLink,autoSetMacs=True)
    try:
        net.start(); install_primary(); h1,h2=net.get("h1","h2")
        ping=h1.popen(["ping","-c","80","-i","0.02","-W","1","10.0.0.2"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True); time.sleep(0.15)
        failure_ns=time.perf_counter_ns(); net.configLinkStatus("s1","s2","down")
        if mode=="central_repair": time.sleep(0.100)
        detection_ns = failure_ns
        if mode == "autonomous_link":
            detected = threading.Event()
            def monitor():
                nonlocal detection_ns
                deadline = time.perf_counter() + 5
                while time.perf_counter() < deadline:
                    if link_is_down():
                        detection_ns = time.perf_counter_ns(); detected.set(); return
                    time.sleep(0.005)
            thread = threading.Thread(target=monitor, daemon=True); thread.start()
            detected.wait(timeout=5); thread.join(timeout=1)
            if not detected.is_set(): raise RuntimeError("autonomous monitor did not detect link failure")
        repair_start=time.perf_counter_ns(); install_alternate(); repair_end=time.perf_counter_ns(); output,_=ping.communicate(timeout=20)
        result=parse_ping(output); result.update({"evidence_level":"measured_emulation_autonomous" if mode=="autonomous_link" else "measured_emulation_intervention","mode":mode,"repetition":repetition,"failure":"s1-s2 link down","recovery_action_us":(repair_end-repair_start)/1000.0,"failure_to_action_us":(repair_start-failure_ns)/1000.0,"failure_to_detection_us":(detection_ns-failure_ns)/1000.0}); return result
    finally:
        net.stop(); subprocess.run(["mn","-c"],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def main():
    setLogLevel("warning"); rows=[]
    for mode in MODES:
        for repetition in range(1,REPETITIONS+1): print(f"stage3 mode={mode} repetition={repetition}",flush=True); rows.append(run_one(mode,repetition))
    RAW.parent.mkdir(parents=True,exist_ok=True)
    with RAW.open("w",newline="") as h: w=csv.DictWriter(h,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(f"wrote {len(rows)} rows to {RAW}")

if __name__=="__main__": main()
