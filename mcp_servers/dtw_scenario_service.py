#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Scenario Service — What-if scenario lifecycle for the digital twin demo.

The submit-and-track surface of the Digital Twin demo. Owns dtw_scenarios.
Records what-if scenarios ("raise prepaid M to 20 Mbps in NYC and LA on
Saturday night") as a structured change_set + scope and tracks lifecycle
status. Field extraction from natural language is performed by the calling
domain agent (Phase 4 of MULTI_AGENT_PLAN.md) — this service receives
structured fields plus the verbatim raw text and performs only data
operations: plan/QoS/market hint resolution (including numeric rates like
'19 Mbps' → nearest profile, with an explicit substitution note),
persistence, and lifecycle transitions. The actual numerical simulation is
performed by the simulation service.

Use this service when users say:
- Submit:   "what if we raise prepaid M to 20 Mbps in NYC",
            "raise downlink from 7.2 to 20 Mbps in NYC and LA",
            "increase QoS from X Mbps to Y Mbps — where do we bottleneck",
            "raise the cap — what is the impact", "model the effect of …",
            "I want to run a what-if", "new scenario",
            "what happens if we change APN for plan X",
            "where do we bottleneck if we raise downlink",
            "what breaks if we increase the QoS profile"
- Update:  "change the scenario to 50 Mbps", "update the scenario",
           "change the last scenario", "modify scope to NYC only",
           "change downlink target", "adjust the scenario"
- List:    "list scenarios", "show all what-ifs",
           "show submitted scenarios", "completed scenarios"
- Detail: "get scenario DTW-SCN-001", "show me the scenario",
          "scenario details"
- Cancel: "cancel scenario X", "discard scenario X"
- Delete:  "delete scenario X", "remove scenario X", "wipe scenario X",
           "delete all scenarios", "wipe all dtw scenarios",
           "clear scenarios", "reset scenarios"

This service does NOT run the actual simulation, traverse the topology, or
return load-projection results. Once a scenario is submitted, call
simulate_qos_change (or simulate_roaming_change) from the simulation
service to compute outcomes.

This service is NOT the IBN intent service — that lives in ibn_intent_service
and handles retail-network customer intents, not mobile-network what-ifs.

This service only accepts what-if requests that name a `plan_ACME_*` plan,
a `qos_*` profile, an APN, a PCRF template ref, or a roaming country. If
the user's request does not mention one of those, this service is the
wrong tool.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.dtw import scenario  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("dtw_scenario_service", scenario.tools)
