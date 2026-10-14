#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Simulation Service — Run what-if simulations on the ACME digital twin.

The hero service of the Digital Twin demo. Owns the simulation runtime that
turns a submitted scenario into structured results. Two execution paths:

  • simulate_qos_change   — QoS uplift (Flow A): graph traversal via
                            $graphLookup to find the affected dependency
                            tree (cells → eNBs → SGW → PGW), per-cell load
                            projection from traffic_models, threshold-based
                            risk flagging, then a hybrid vector + structured
                            filter query against dtw_knowledge_chunks to
                            surface analogous past scenarios with their
                            mitigation playbooks.

  • simulate_roaming_change — APN / PCRF template / roaming policy change
                              (Flow B): control-plane focused. Projects HSS
                              query-rate impact, looks up HLR-only legacy
                              subscribers, plus the same hybrid vector search.

This is the only service that combines `$graphLookup` over the dependency
graph and `$vectorSearch` over historical knowledge chunks at simulate-time —
that combination is the architectural beat the demo makes about MongoDB
Atlas as a single store for both operational graph state and embedded
semantic memory.

Use this service when users say:
- Simulate:  "run the simulation", "simulate scenario DTW-SCN-003",
             "run the simulation for DTW-SCN-001", "go ahead and simulate",
             "what does the simulation say", "project the impact",
             "compute the results", "execute the scenario"
- Diff:      "diff scenarios A and B", "compare scenario results"
- Stored:    "show me past results for scenario X"

This service does NOT submit or define new scenarios. When a user describes
a new what-if for the first time ("raise X to Y in Z", "what if we change
plan X", "model the effect of…") use dtw_scenario_service to create and
verify the scenario first — then come here to run the simulation.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.dtw import simulation  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("dtw_simulation_service", simulation.tools)
