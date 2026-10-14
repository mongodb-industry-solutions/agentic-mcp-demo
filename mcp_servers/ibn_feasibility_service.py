#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Feasibility Service — Match Intent to Inventory and Plan Activation

Validates whether a submitted intent can be honored at its target site,
generates a concrete service plan from available resources, and transitions
the intent through feasible → planned → active states. Stores plan
snapshots in ibn_policy_snapshots so the dashboard can render them.

Use this service when users say:
- Feasibility: "is this feasible", "can we do this", "check feasibility for IBN-...",
              "verify intent <id>", "is the <intent> achievable"
- Plan:       "show the proposed plan", "plan for IBN-...", "what's the plan",
              "propose plan for <intent>"
- Activate:   "activate <intent>", "activate it", "go live", "make it active",
              "deploy intent <id>"

This service does NOT submit new intents (use intent service), list inventory
(use inventory service), compute compliance (use assurance service), or run
telemetry (use telemetry simulator).
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.ibn import feasibility  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("ibn_feasibility_service", feasibility.tools)
