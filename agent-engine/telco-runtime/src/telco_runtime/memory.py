#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Agent Engine memory for the Telco OSS agents (Phase 5 of AGENT_ENGINE_PLAN.md).

- **Taxonomic:** a Telco glossary (3GPP/TMF vocabulary plus the demo's own
  terms), shared by every user: the "agent speaks operator" beat.
- **Procedural:** the three demo flows plus the IBN runbooks and DTW
  mitigation playbooks from the knowledge chunks. Extraction adds learned
  procedures from conversations.
- **Semantic:** what the user asks to remember (the legacy
  preferences_service plane), plus the facts extraction learns.
- **Episodic:** extracted from the recorded turns by the platform.

Taxonomic extraction is off (project-config.yaml): it only re-learned the
seeded glossary from the transcripts.

Recall is one build_context call. It needs the caller's identity, which an
A2A child execution doesn't have, so the front door (the coordinator, or a
domain agent called directly) recalls and passes the block down.

All calls go through app.memory in the agent sandbox; the platform records
them as durable activities, so a replay doesn't repeat a write.
"""

from collections.abc import Callable
from typing import Any

from agent_engine_sdk_langgraph import App
from agent_engine_sdk_memory import MemoryAPIError, MemoryClientError, MemoryIdentityError

GLOSSARY_DOMAIN = "telco-oss"
GLOSSARY_VERSION = "1"  # bump to re-seed after editing GLOSSARY or FLOWS
RECALL_SOURCES = {"episodic", "semantic", "procedural", "taxonomic"}
# Recall goes into the system prompt and into every delegated message, so it
# is budgeted (the server keeps 500 of it as a formatting reserve).
RECALL_MAX_TOKENS = 2000
PREFERENCES_TOP_K = 8
# Heads the recalled block in an agent's system prompt and in delegated
# messages. Answer-style preferences are binding, the rest is background.
MEMORY_PREAMBLE = ("What the platform remembers for this user. Follow their stated "
                   "preferences on how to answer (length, format, style, units) in "
                   "your reply; use the episodes, procedures and glossary where "
                   "relevant:")

GLOSSARY: dict[str, str] = {
    # Intent-based networking and assurance
    "Intent": "A declarative statement of the outcome a customer wants from the network "
              "(e.g. POS latency under 20 ms at a store), not the configuration to get there.",
    "IBN": "Intent-Based Networking: the network is driven from intents that are checked for "
           "feasibility, turned into a plan, activated, and then continuously assured.",
    "Intent lifecycle": "submitted → feasible → planned → active (→ violated → active) → "
                        "cancelled; each step is recorded on the intent's history.",
    "Feasibility check": "Matching an intent against site inventory (access node, uplink "
                         "capacity, CPE) before any plan is proposed.",
    "Policy snapshot": "The committed plan for an intent: access node, uplink, CPE, template "
                       "and deadline, frozen at propose time.",
    "Service template": "A versioned configuration bundle (e.g. strict-retail-v3) that "
                        "provisions segmentation and QoS for a class of sites.",
    "Template folding": "Rolling a template version forward across every intent that uses it, "
                        "ideally as a canary on one site first.",
    "Assurance": "Continuous comparison of live telemetry against an intent's targets.",
    "SLO": "Service Level Objective: a measurable target, such as POS latency ≤ 40 ms or "
           "99.95% availability, that an intent must keep.",
    "SLA": "Service Level Agreement: the contractual commitment built from one or more SLOs.",
    "SLO violation": "A telemetry window in which an intent misses a target; it opens a "
                     "compliance event and a diagnosis.",
    "Compliance event": "A recorded violation or recovery of an intent's SLO, with the metric, "
                        "observed value and threshold.",
    "Runbook": "A documented remediation procedure (e.g. RB-007) applied to fix a known "
               "fault pattern; on Agent Engine it needs NOC approval.",
    "Blast radius": "How many customers, sites or intents an action can affect; actions with "
                    "a blast radius pause for NOC review.",
    "NOC": "Network Operations Centre: the engineers who review, approve and run changes.",
    "Canary rollout": "Applying a change to one site first, re-checking compliance, then "
                      "approving the rest of the fleet.",
    "POS": "Point of Sale terminals; the latency-critical service class in retail intents.",
    "Guest Wi-Fi segmentation": "Isolating customer Wi-Fi from store systems (strict = no "
                                "shared VLAN or routing domain).",
    "CPE": "Customer Premises Equipment: the router/firewall at the customer site.",
    "Access node": "The aggregation device a site connects to, e.g. AN-MUC-MAR-01.",
    "Uplink": "The site's transport link to the network (fibre, DSL, 5G), with a capacity in "
              "Mbps.",
    "PoE": "Power over Ethernet: powers cameras and kiosks over the data cable.",
    "EVPN": "Ethernet VPN: BGP-based layer-2/3 VPN used to extend segmentation across sites.",
    "VLAN": "Virtual LAN: layer-2 segmentation of traffic classes on shared equipment.",
    "DSCP": "Differentiated Services Code Point: the IP header marking that maps traffic to a "
            "queue.",
    "EF queue": "Expedited Forwarding: the strict-priority queue for latency-sensitive "
                "traffic such as POS (runbook RB-007 moves POS there).",
    "PIR": "Peak Information Rate: the burst ceiling of a shaped traffic class.",
    "CIR": "Committed Information Rate: the guaranteed bandwidth of a traffic class.",
    "Head-of-line blocking": "Latency-sensitive packets stuck behind bulk traffic in a shared "
                             "queue.",
    "Telemetry": "Time-series measurements (latency, loss, throughput) streamed per intent.",
    # Mobile core and RAN (digital twin)
    "Digital twin": "A queryable model of the operator's network (plans, QoS, topology, "
                    "traffic) used for what-if simulation before a change.",
    "What-if scenario": "A proposed change (QoS uplift, APN move, roaming) simulated on the "
                        "twin, with projected load and risks.",
    "QoS profile": "The bearer parameters of a plan: downlink/uplink rate, QCI, ARP.",
    "QCI": "QoS Class Identifier: the LTE class (1–9) that sets a bearer's priority, delay "
           "and loss budget.",
    "5QI": "5G QoS Identifier: the 5G successor of QCI.",
    "ARP": "Allocation and Retention Priority: decides which bearers are admitted or "
           "pre-empted under congestion.",
    "APN": "Access Point Name: selects the PDN gateway and data network a subscriber uses.",
    "PCRF": "Policy and Charging Rules Function: pushes QoS and charging policy to the "
            "gateways; a template change can re-apply policy to many subscribers.",
    "PCF": "Policy Control Function: the 5G core successor of PCRF.",
    "HSS": "Home Subscriber Server: the LTE subscriber database for authentication and "
           "attach; it has an attach-rate ceiling.",
    "HLR": "Home Location Register: the 2G/3G predecessor of the HSS.",
    "UDM": "Unified Data Management: the 5G core subscriber data function (HSS successor).",
    "MME": "Mobility Management Entity: the LTE control-plane node for attach, paging and "
           "handover.",
    "AMF": "Access and Mobility Management Function: the 5G successor of the MME.",
    "SGW": "Serving Gateway: the LTE user-plane anchor between eNodeB and PGW.",
    "PGW": "PDN Gateway: the LTE user-plane exit to the data network; PGW headroom limits "
           "QoS uplifts.",
    "UPF": "User Plane Function: the 5G user-plane gateway (SGW/PGW successor).",
    "eNodeB": "The LTE base station; it serves one or more cells.",
    "gNodeB": "The 5G NR base station.",
    "Cell": "One sector of a base station; cell load is the share of its capacity in use.",
    "RAN": "Radio Access Network: base stations and cells.",
    "Secondary carrier": "An additional frequency block (e.g. 20 MHz) added to an overloaded "
                         "sector (DTW-RB-001).",
    "Handover hysteresis": "The margin that prevents ping-pong handovers between cells.",
    "Roaming": "Service on a partner network; enabling a roaming partner adds signalling load "
               "on HSS and MME.",
    "Busy hour": "The time window with the highest traffic, e.g. Saturday evening.",
    "Capacity threshold": "The cell load above which a cell is flagged RED; at 100% it BLOCKs.",
    "Prepaid": "Subscribers who pay in advance; the ACME M plan is a prepaid segment.",
    # Operations frameworks
    "TM Forum": "The telecom industry body behind the Open Digital Architecture and Open APIs.",
    "ODA": "TM Forum Open Digital Architecture: component model for modular OSS/BSS.",
    "TMF921": "TM Forum Intent Management API, the standard interface for intent "
              "submission and reporting.",
    "Autonomous network level": "TM Forum's L0–L5 scale of network autonomy; closed-loop "
                                "assurance with human approval sits around L3–L4.",
    "OSS": "Operations Support Systems: inventory, provisioning, assurance and fault "
           "management.",
    "BSS": "Business Support Systems: product catalogue, ordering, billing and CRM.",
    "3GPP": "The standards body for mobile networks (LTE, 5G).",
}

def _step(step_type: str, tool: str, note: str) -> dict[str, str]:
    """A procedure step in the memory server's ProceduralStep shape."""
    return {"step_type": step_type, "content": f"Call {tool}: {note}.",
            "description": f"{tool} ({note})"}


FLOWS: list[dict[str, Any]] = [
    {
        "procedure": "ibn-store-onboarding",
        "description": "Onboard a new retail site through the IBN intent lifecycle.",
        "content": "Submit the intent with the customer's wording verbatim, check "
                   "feasibility against the site's inventory, propose the plan (snapshot), "
                   "then activate it after NOC approval. Assurance starts at activation.",
        "steps": [
            _step("instruction", "submit_intent", "raw_text verbatim, site as named"),
            _step("instruction", "check_feasibility", "against the site's inventory"),
            _step("instruction", "propose_plan", "snapshots the plan"),
            _step("human_input", "activate_plan", "pauses for NOC approval"),
        ],
        "allowed_tools": ["submit_intent", "check_feasibility", "propose_plan", "activate_plan"],
        "trigger_conditions": ["open a new store", "onboard a site", "new intent"],
        "tags": ["ibn", "onboarding"],
    },
    {
        "procedure": "ibn-violation-response",
        "description": "Respond to an SLO violation on an active intent.",
        "content": "Read compliance, diagnose the violation with the hybrid vector search "
                   "(similar incidents and runbooks near the site), propose the best "
                   "runbook, apply it after NOC approval, and confirm compliance is restored.",
        "steps": [
            _step("instruction", "get_compliance", "find the violated SLO"),
            _step("instruction", "diagnose_violation", "similar incidents and runbooks"),
            _step("human_input", "apply_runbook", "pauses for NOC approval"),
            _step("validation", "get_compliance", "confirm the intent is green again"),
        ],
        "allowed_tools": ["get_compliance", "diagnose_violation", "apply_runbook"],
        "trigger_conditions": ["SLO violated", "latency too high", "intent red"],
        "tags": ["ibn", "assurance"],
    },
    {
        "procedure": "dtw-qos-what-if",
        "description": "Simulate a QoS change on the ACME digital twin before rolling it out.",
        "content": "Create the scenario from the request (plan, target rate, markets, time "
                   "window), confirm the parameters, run simulate_qos_change, then report "
                   "cells over the capacity threshold, core elements at risk, similar past "
                   "scenarios and their mitigation runbooks.",
        "steps": [
            _step("instruction", "create_scenario", "plan, target rate, markets, window"),
            _step("instruction", "simulate_qos_change", "graph scope, load, similar cases"),
            _step("instruction", "get_simulation_result", "report hot cells and runbooks"),
        ],
        "allowed_tools": ["create_scenario", "simulate_qos_change", "get_simulation_result"],
        "trigger_conditions": ["raise downlink", "QoS uplift", "where do we bottleneck"],
        "tags": ["dtw", "what-if"],
    },
]


def runbook_procedures(runbooks: list[dict]) -> list[dict[str, Any]]:
    """Procedures from the knowledge chunks' runbooks (routing.runbooks_for_memory)."""
    return [{
        "procedure": rb["_id"].lower(),
        "description": rb.get("title") or rb["_id"],
        "content": rb.get("text") or "",
        "tags": [rb.get("domain", ""), "runbook"],
        "trigger_conditions": [rb.get("title") or rb["_id"]],
    } for rb in runbooks]


_seeded = False


def ensure_seeded(app: App, runbooks: Callable[[], list[dict]]) -> int:
    """Seed the glossary and procedures once (per memory store and
    GLOSSARY_VERSION). `runbooks` fetches the knowledge-chunk runbooks
    (only called when seeding). Returns the number of entries written, 0
    if they were already there."""
    global _seeded
    if _seeded:
        return 0
    marker = f"glossary-v{GLOSSARY_VERSION}"
    # visibility="org": the marker is shared, whichever user seeded it.
    if app.memory.get_taxonomic_term(GLOSSARY_DOMAIN, marker, visibility="org"):
        _seeded = True
        return 0
    # Both are create-only: a glossary term that exists comes back as a 409
    # (unacknowledged, not raised), and updating a procedure fails on the
    # platform (update_procedural rejects trigger_conditions), so existing
    # procedures are kept as they are.
    written, failed = 0, []
    for term, definition in GLOSSARY.items():
        result = app.memory.save_taxonomic(
            domain=GLOSSARY_DOMAIN, term=term, definition=definition,
            visibility="org", metadata={"source": "telco-oss-seed"})
        if getattr(result, "acknowledged", result):
            written += 1
        elif not app.memory.get_taxonomic_term(GLOSSARY_DOMAIN, term, visibility="org"):
            failed.append(term)
    for proc in FLOWS + runbook_procedures(runbooks()):
        if app.memory.get_procedure(proc["procedure"], visibility="org"):
            continue
        result = app.memory.save_procedure(**proc, visibility="org",
                                           extraction_source="telco-oss-seed")
        if getattr(result, "acknowledged", result):
            written += 1
        else:
            failed.append(proc["procedure"])
    # Without the marker the next start seeds what is still missing.
    if failed:
        print(f"⚠️ memory seed incomplete, retried next start: {failed}")
        return written
    # Written last, so an interrupted seed is retried in full.
    app.memory.save_taxonomic(domain=GLOSSARY_DOMAIN, term=marker,
                              definition=f"Seed marker: {len(GLOSSARY)} terms, "
                                         f"{len(FLOWS)} flows plus the runbooks.",
                              visibility="org")
    _seeded = True
    return written


def recall(app: App, query: str) -> str:
    """The user's relevant memory for this request ('' when none, or when
    the execution has no user identity, as in an A2A child)."""
    try:
        ctx = app.memory.build_context(query, enabled_sources=set(RECALL_SOURCES),
                                       max_tokens=RECALL_MAX_TOKENS)
    except (MemoryIdentityError, MemoryClientError, MemoryAPIError) as e:
        print(f"⚠️ memory recall skipped: {type(e).__name__}: {e}")
        return ""
    text = getattr(ctx, "formatted_context", ctx)
    return text.strip() if isinstance(text, str) else ""


def save_preference(app: App, label: str, text: str) -> bool:
    result = app.memory.save_semantic(text=text, label=label, source="noc-coordinator",
                                      metadata={"kind": "preference"})
    return bool(getattr(result, "acknowledged", result))


def list_preferences(app: App) -> list[str]:
    """What memory holds about this user: the preferences they stated and
    what extraction learned. Not filtered on the `kind` metadata, because
    extraction supersedes a saved preference with its own version."""
    hits = app.memory.search_semantic("how the user wants answers, their role and focus",
                                      top_k=PREFERENCES_TOP_K)
    return list(dict.fromkeys(h.content for h in hits if h.content))
