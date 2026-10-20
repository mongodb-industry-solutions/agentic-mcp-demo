#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Topology Service — Digital twin RAN + core inventory and dependency graph.

The network-inventory surface of the Digital Twin demo. Owns dtw_network_elements
(polymorphic: HSS, HLR, MME, SGW, PGW, eNodeB, Cell — one collection, one
schema-per-type) and dtw_topology_edges (the dependency graph). Use this
service to inspect network elements, find what cells exist in a market, and
walk the dependency graph from any starting node via $graphLookup.

Use this service when users say:
- Element:   "show network element <id>", "describe <network-element-id>",
             "look up <ne_*>", "get the HSS / SGW / PGW record"
- Cells:     "list cells in <market>", "show cells in a market",
             "what cells does an eNB host"
- Traverse:  "what depends on a network element", "trace dependencies",
             "show the dependency graph from <id>",
             "graph walk from <id>", "topology traversal"
- Markets:   "list markets", "what markets do we cover",
             "geographic footprint"

The traversal tools wrap MongoDB `$graphLookup` over dtw_topology_edges so
the LLM never has to construct the aggregation itself.

This service does NOT own plans, QoS profiles, traffic models, scenarios, or
simulations — those are the plan, traffic, scenario, and simulation DTW
services respectively. It does NOT serve the IBN retail demo's site/resource
inventory either — that lives in ibn_inventory_service.

This service operates exclusively on dependency-graph identifiers of the
shape `ne_HSS_*`, `ne_HLR_*`, `ne_MME_*`, `ne_SGW_*`, `ne_PGW_*`,
`ne_eNB_*`, `cell_*`, or `plan_ACME_*`. If the user's request does not
reference one of those id shapes, this service is the wrong tool.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.dtw import topology  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("dtw_topology_service", topology.tools)
