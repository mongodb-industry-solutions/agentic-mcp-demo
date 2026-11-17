#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Traffic Service — Digital-twin traffic patterns and load estimation.

The behavioural-model surface of the Digital Twin demo. Owns dtw_traffic_models,
which captures, per plan × market × time-window, the active subscriber
estimate and per-user throughput characteristics on each cell. The simulation
service consumes these models to project load changes; this service is the
read/inspect API for them.

Use this service when users say:
- Lookup:    "show the traffic model for <plan> in <market> during <window>",
             "fetch the traffic model details", "get the load model",
             "traffic-model fixture lookup", "traffic-model record"
- Estimate:  "estimate baseline load on cell <id> in window <id>",
             "what is the current baseline utilization of cell <id>",
             "baseline cell load for time window <id>"
- Windows:   "list time windows", "what time windows do we have",
             "show traffic windows"
- Peak:      "what are the peak windows", "peak window for market <id>"

The traffic models are static fixtures — they describe the *expected* load
pattern. The simulation service applies them under a proposed change to
produce *projected* load.

This service does NOT model commercial plans (that is the plan service),
walk the dependency graph (the topology service), own scenarios, or run
simulations (the scenario and simulation services respectively).

This service is scoped exclusively to digital-twin traffic-model fixtures
referenced by `plan_ACME_*` ids, market ids like `NYC_Metro` or `LA_Metro`,
and time-window ids like `Saturday_20_23`. If the user's request does not
reference one of those identifier shapes, this service is the wrong tool.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.dtw import traffic  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("dtw_traffic_service", traffic.tools)
