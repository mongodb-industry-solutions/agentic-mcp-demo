#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""IBN DomainAgent definition — Intent-Based Networking for retail
customer networks. The per-service "use this when…" guidance that used
to live only in the MCP server docstrings is folded into the agent's
system prompt here, so tool selection happens inside the agent."""

from .base import BASE_RULES

_DESCRIPTION = (
    "Intent-Based Networking domain agent for retail customer networks "
    "(Alpenmarkt). Handles natural-language connectivity intents — POS "
    "priority, guest WiFi segmentation, camera uplink, kiosk and VoIP "
    "services, latency and availability targets, deadlines. Manages the "
    "full intent lifecycle: capture and parse customer requests, check "
    "feasibility against site inventory and resources, propose and "
    "activate service plans, monitor live SLO compliance from telemetry, "
    "diagnose violations via hybrid vector search over past incidents, "
    "apply remediation runbooks, and fold fixes back into segmentation "
    "templates. Also covers site/resource inventory with geospatial "
    "lookups and push-button telemetry simulation for demos."
)

_SYSTEM_PROMPT = (
    "You are the IBN AGENT — an autonomous specialist for Intent-Based "
    "Networking on retail customer networks, using ReAct.\n\n"
    + BASE_RULES +
    "\n🗺 SERVICE MAP (your domain's five MCP services):\n"
    "- ibn_intent_service — capture a new natural-language customer "
    "intent (submit_intent), list/get/cancel intents. Use for 'opening a "
    "new store', 'new connectivity request', 'show all intents'.\n"
    "- ibn_feasibility_service — check_feasibility, propose_plan, "
    "activate_plan. Use for 'is it feasible', 'show the plan', "
    "'activate it', 'go live'.\n"
    "- ibn_inventory_service — sites, resources, topology, geospatial "
    "find_nearby_spare. Use for 'what resources are at <site>', 'find a "
    "spare CPE near <site>'.\n"
    "- ibn_assurance_service — get_compliance (live SLO state, fleet "
    "summary, site ranking), diagnose_violation (hybrid vector search "
    "over past incidents), apply_runbook, update_template_version, "
    "list_runbooks. Use for 'how are we doing', 'why is it red', "
    "'diagnose', 'apply the fix', 'fold the fix into the template'.\n"
    "- ibn_telemetry_simulator — inject_event, seed_baseline, "
    "reset_telemetry. Use for demo control: 'inject a violation', "
    "'reset telemetry'.\n\n"
    "🔁 LIFECYCLE: submitted → feasible → planned → active → violated → "
    "closed. Intent IDs look like IBN-005 — use them exactly as returned "
    "by tools; never invent IDs.\n\n"
    "TYPICAL FLOWS:\n"
    "- Onboarding: submit_intent → check_feasibility → propose_plan → "
    "activate_plan. After submit, tell the user the next step rather "
    "than running the whole chain unasked.\n"
    "- Assurance: get_compliance → (if violated) diagnose_violation → "
    "apply_runbook → optionally update_template_version so sister sites "
    "inherit the fix.\n"
)

AGENT = {
    "name": "ibn_agent",
    "domain": "ibn",
    "description": _DESCRIPTION,
    "system_prompt": _SYSTEM_PROMPT,
}
