#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Intent Service — Customer Intent Lifecycle Management

The customer-facing surface of the Intent-Based Networking demo. Owns the
ibn_intents collection. Records customer business intents for retail
network services (POS priority, guest segmentation, availability targets,
deadlines) and tracks lifecycle state (submitted → feasible → planned →
active → violated → closed). Field extraction from natural language is
performed by the calling domain agent (Phase 4 of MULTI_AGENT_PLAN.md) —
this service receives structured fields plus the verbatim raw text and
performs only data operations (site resolution, persistence).

Use this service when users say:
- Submit:   "I'm opening a new <store> at <location>", "new branch", "submit intent",
           "new connectivity request", "we need POS / guest WiFi / camera at ..."
- List:    "show all intents", "list active intents", "show all active intents",
           "what's running", "fleet status"
- Detail:  "get intent <id>", "show details for IBN-...", "intent details"
- Cancel:  "cancel intent <id>", "withdraw intent", "stop intent"

This service does NOT check feasibility, allocate resources, manage
inventory, compute compliance, or simulate telemetry — those belong to the
feasibility, inventory, assurance, and telemetry services respectively.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.ibn import intent  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("ibn_intent_service", intent.tools)
