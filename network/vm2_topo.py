#!/usr/bin/env python3
"""VM2 half of the multi-OVS testbed: s3 -- s4 -- h2 (primary), s3 -- s5 -- s4
(alternate), all local to this VM/OVSDB instance. s3 later gets a GRE tunnel
port to VM1's s1. Stays running so the agent (on VM1) and fault-injection
commands (on this VM) can be driven against it from other shells."""
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo


class VM2Topo(Topo):
    def build(self):
        s3 = self.addSwitch("s3", protocols="OpenFlow13", failMode="secure")
        s4 = self.addSwitch("s4", protocols="OpenFlow13", failMode="secure")
        s5 = self.addSwitch("s5", protocols="OpenFlow13", failMode="secure")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        self.addLink(s3, s4)   # primary remote-edge under test
        self.addLink(s3, s5)   # alternate path leg 1
        self.addLink(s5, s4)   # alternate path leg 2
        self.addLink(s4, h2)


def main():
    setLogLevel("warning")
    net = Mininet(topo=VM2Topo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.start()
    print("VM2 topology up: s3 -- s4 -- h2, s3 -- s5 -- s4", flush=True)
    for name in ("s3", "s4", "s5"):
        sw = net[name]
        for intf in sw.intfList():
            print(f"  {name} port {sw.ports[intf]}: {intf.name}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
