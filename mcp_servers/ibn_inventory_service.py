#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Inventory Service — Sites, Resources, and Topology

Read-only inventory surface for the Intent-Based Networking demo. Owns
queries against ibn_sites and ibn_resources. Heterogeneous resource
documents (access nodes, uplinks, CPEs, segmentation templates) live side
by side under one collection — the document-model selling point. Includes
geospatial lookups via 2dsphere indexes for "find spare resources nearby"
queries.

Use this service when users say:
- Sites:    "list sites", "show all stores", "where are our locations",
           "what sites do we have"
- Resources: "what resources are available at <site>", "show inventory at <site>",
           "list access nodes / uplinks / CPEs at <site>", "topology of <site>"
- Geo:     "any spare resources near <site>", "nearby capacity",
           "failover candidates within Xkm"

This service does NOT submit intents, check feasibility, manage the
lifecycle of services, compute compliance, or run telemetry.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.ibn import inventory  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("ibn_inventory_service", inventory.tools)
