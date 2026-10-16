#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Plan Service — ACME Mobile digital-twin plans, QoS profiles, subscribers.

The catalog surface of the Digital Twin demo. Owns dtw_plans, dtw_qos_profiles,
and read access to dtw_subscribers. Use this service to look up the commercial
products (ACME M, Premium, Plus5G, Connect, …), their bound QoS profiles
(max downlink/uplink Mbps, QCI, ARP, APN overrides, PCRF template refs), and
to sample subscribers for a given plan.

Use this service when users say:
- Describe:  "describe plan <id>", "show plan <id>", "what is in plan <id>"
- QoS:       "show QoS profile <id>", "details on a QoS profile",
             "what does this QoS profile look like", "QoS profile params"
- List:      "list plans", "show all plans", "what plans do we have",
             "list prepaid plans", "postpaid plans"
- Compare:   "compare two QoS profiles", "diff QoS profiles",
             "field-by-field QoS comparison"
- Sample:    "show some subscribers on plan <id>",
             "subscriber sample", "who is on plan <id>"

This service does NOT traverse the topology graph, model traffic, run
simulations, or own scenarios — those belong to the topology, traffic,
scenario, and simulation DTW services respectively.

This service is NOT about retail intents, retail sites, or compliance — that
is the IBN demo. Don't confuse `plan` (mobile data plan) with `policy
template` (provisioning template) — those are different demos.

This service operates exclusively on `plan_ACME_*` plan ids and `qos_*`
QoS profile ids. If the user's request does not name a plan or QoS
profile, this service is the wrong tool.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.dtw import plan  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("dtw_plan_service", plan.tools)
