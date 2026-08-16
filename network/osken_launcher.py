#!/usr/bin/env python3
"""Minimal os_ken app launcher, since the pip-distributed os-ken==4.2.1
wheel ships no cmd/manager.py console-script entry point (verified: no
os_ken/cmd/ package, no entry_points.txt, confirmed empirically on
daim-lab). This replicates the standard AppManager + OpenFlowController
bring-up sequence any os_ken/ryu manager script performs.
"""
import sys

from os_ken.base import app_manager
from os_ken.controller import controller
from os_ken.lib import hub

app_mgr = app_manager.AppManager.get_instance()
app_mgr.load_apps([sys.argv[1]])
contexts = app_mgr.create_contexts()
services = []
services.extend(app_mgr.instantiate_apps(**contexts))

services.append(hub.spawn(controller.OpenFlowController()))
hub.joinall(services)
