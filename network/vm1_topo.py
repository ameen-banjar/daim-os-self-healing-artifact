#!/usr/bin/env python3
"""VM1 half of the multi-OVS testbed: h1 -- s1, with s1 later given a GRE
tunnel port to VM2's s3. Stays running (does not exit) so the agent and
fault-injection commands can be driven against it from other shells."""
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.topo import Topo


class VM1Topo(Topo):
    def build(self):
        s1 = self.addSwitch("s1", protocols="OpenFlow13", failMode="secure")
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        self.addLink(h1, s1)


def main():
    setLogLevel("warning")
    net = Mininet(topo=VM1Topo(), controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.start()
    print("VM1 topology up: h1 -- s1", flush=True)
    for intf in net["s1"].intfList():
        print(f"  s1 port {net['s1'].ports[intf]}: {intf.name}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
