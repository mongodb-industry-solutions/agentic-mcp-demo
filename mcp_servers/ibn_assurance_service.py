#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Assurance Service — Live Compliance and Hybrid Vector Diagnosis

The brain of the demo. Owns continuous compliance computation against active
intents (reads ibn_telemetry, writes ibn_compliance_events) and runs the
hybrid vector + geospatial + time + structured-filter search across
ibn_knowledge_chunks to diagnose violations by surfacing semantically
similar past incidents and their proven runbooks.

Use this service when users say:
- Compliance: "how are we doing", "compliance status", "is it green",
             "show compliance for IBN-...", "fleet status",
             "current metrics at <site>", "what are the metrics",
             "check status at <site>", "how is <site> performing",
             "live status", "site performance", "current telemetry"
- Ranking:   "which store has the lowest latency", "best performing store",
             "worst performing store", "highest POS latency", "lowest latency",
             "rank stores by latency", "compare latency across stores",
             "which site is fastest", "which site is slowest"
- Diagnose:  "what happened", "diagnose", "diagnose <intent>",
             "why is it red", "explain the violation", "find similar incident"
- Apply:     "apply the runbook", "apply <runbook_id>", "apply the fix",
             "remediate"
- Runbooks:  "what runbooks are available", "list runbooks", "show runbooks",
             "which runbooks exist"
- Template:  "update the template", "patch the template",
             "fold the fix into the template"

This service does NOT submit intents, manage inventory, check feasibility,
or simulate telemetry directly — those are the other IBN services.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.ibn import assurance  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("ibn_assurance_service", assurance.tools)
