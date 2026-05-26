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

import datetime
import json
import logging
import os

from openai import OpenAI
from pymongo import MongoClient, DESCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp                = FastMCP("dtw_simulation_service")
logger             = logging.getLogger("dtw_simulation_service")

mongo_client       = MongoClient(os.environ["MONGODB_URI"])
db                 = mongo_client["agent_registry"]
scenarios          = db["dtw_scenarios"]
plans              = db["dtw_plans"]
qos_profiles       = db["dtw_qos_profiles"]
elements           = db["dtw_network_elements"]
topology_edges     = db["dtw_topology_edges"]
traffic_models     = db["dtw_traffic_models"]
subscribers        = db["dtw_subscribers"]
knowledge_chunks   = db["dtw_knowledge_chunks"]
markets_coll       = db["dtw_markets"]

openai_client      = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
PARSE_MODEL        = os.environ.get("OPENAI_MODEL", "gpt-4o")


def _next_scenario_id() -> str:
    last = scenarios.find_one(
        {"_id": {"$regex": r"^DTW-SCN-\d+$"}},
        sort=[("_id", DESCENDING)],
    )
    return "DTW-SCN-001" if not last else f"DTW-SCN-{int(last['_id'].split('-')[-1]) + 1:03d}"


def _create_scenario_inline(text: str) -> str:
    """Parse a natural-language what-if request and persist it as a scenario.
    Returns the new scenario_id."""
    known_plans   = [p["_id"] for p in plans.find({}, {"_id": 1})]
    known_qos     = [q["_id"] for q in qos_profiles.find({}, {"_id": 1})]
    known_markets = [m["_id"] for m in markets_coll.find({}, {"_id": 1})]

    prompt = (
        f"You are a parser for telecom what-if scenarios on a digital twin. "
        f"Today is {datetime.date.today().isoformat()}.\n\n"
        f"Extract structured fields from this request:\n\n{text!r}\n\n"
        f"Known plans: {', '.join(known_plans)}\n"
        f"Known QoS profiles: {', '.join(known_qos)}\n"
        f"Known markets: {', '.join(known_markets)}\n\n"
        "Return ONLY valid JSON with this schema:\n"
        '{\n  "scenario_type": "qos_change"|"policy_change"|"other",\n'
        '  "change_set": {\n'
        '    "plan_id": string|null, "old_qos_profile_id": string|null,\n'
        '    "new_qos_profile_id": string|null,\n'
        '    "apn_change": {"from":string,"to":string}|null,\n'
        '    "pcrf_template_change": {"from":string,"to":string}|null,\n'
        '    "roaming_enable": string[]|null\n  },\n'
        '  "scope": {"markets": string[], "time_windows": string[]},\n'
        '  "summary": string\n}'
    )
    resp = openai_client.chat.completions.create(
        model=PARSE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(raw)

    sid = _next_scenario_id()
    cs  = parsed.get("change_set") or {}
    sc  = parsed.get("scope") or {}
    scenarios.insert_one({
        "_id":           sid,
        "description":   parsed.get("summary") or text[:120],
        "scenario_type": parsed.get("scenario_type") or "other",
        "raw_text":      text,
        "change_set":    cs,
        "scope":         sc,
        "status":        "submitted",
        "submitted_at":  datetime.datetime.now(),
        "history":       [{"ts": datetime.datetime.now(), "event": "submitted",
                           "note": "inline-created by simulation service"}],
        "results":       None,
    })
    logger.info(f"Inline-created scenario {sid}")
    return sid


# ─── Thresholds (per DTW-POL-002 in seed data) ─────────────────────────────

UTIL_YELLOW = 0.70
UTIL_RED    = 0.85
UTIL_BLOCK  = 0.95


def _classify(util: float) -> str:
    if util >= UTIL_BLOCK:  return "BLOCK"
    if util >= UTIL_RED:    return "RED"
    if util >= UTIL_YELLOW: return "YELLOW"
    return "GREEN"


def _serialize(doc):
    if isinstance(doc, dict):
        return {k: _serialize(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_serialize(v) for v in doc]
    if isinstance(doc, datetime.datetime):
        return doc.isoformat()
    return doc


# ─── Graph-walk: find affected cells and core elements for a scope ─────────

def _affected_cells(plan_id: str, markets: list[str]) -> list[dict]:
    """Walk plan → qos_profile (uses_qos), then subscribers (downstream-by-plan)
    to find the cells most likely to be hit. Practically: cells in the
    requested markets that traffic_models list for this plan."""
    q: dict = {"plan_id": plan_id}
    if markets:
        q["market"] = {"$in": markets}
    cell_ids: set = set()
    for tm in traffic_models.find(q, {"cells.cell_id": 1}):
        for c in tm.get("cells", []):
            cell_ids.add(c["cell_id"])
    if not cell_ids:
        return []
    return list(elements.find({"_id": {"$in": list(cell_ids)}, "type": "Cell"}))


def _graph_dependency_walk(start_id: str, max_depth: int = 4) -> list[dict]:
    """Run $graphLookup downstream from a node, return the discovered edges
    plus their depth. Used to enumerate the dependency tree for the scenario."""
    pipeline = [
        {"$match": {"from_id": start_id}},
        {"$limit": 1},
        {
            "$graphLookup": {
                "from":             "dtw_topology_edges",
                "startWith":        "$to_id",
                "connectFromField": "from_id",
                "connectToField":   "to_id",
                "as":               "walk",
                "maxDepth":         max_depth - 1,
                "depthField":       "depth",
            }
        },
        {"$project": {"_id": 0, "from_id": 1, "to_id": 1, "relation": 1, "walk": 1}},
    ]
    return list(topology_edges.aggregate(pipeline))


# ─── Per-cell load projection ──────────────────────────────────────────────

def _project_cell_load(cell: dict, old_qos: dict, new_qos: dict,
                       tm_entries: list[dict]) -> dict:
    """Project the projected vs original DL utilization on one cell, given
    matching traffic-model entries for this cell from one or more plans."""
    cap_dl = (cell.get("capacity") or {}).get("downlink_mbps") or 0
    old_max = old_qos.get("max_downlink_mbps") or 1
    new_max = new_qos.get("max_downlink_mbps") or old_max
    ratio = max(1.0, new_max / old_max)  # uplift only

    original_mbps = 0.0
    projected_mbps = 0.0
    contributing_subs = 0
    for entry in tm_entries:
        subs   = entry.get("active_subscribers_estimate", 0)
        avg    = entry.get("avg_per_user_mbps", 0.0)
        corr   = entry.get("correlation_to_qos", 0.5)
        # Old aggregate Mbps on this cell from this plan's contribution
        original_mbps += subs * avg
        # New per-user Mbps: subs that were QoS-throttled get a bump up to the
        # new cap; uncorrelated subs see less effect.
        new_user_mbps = min(new_max, avg * (1.0 + corr * (ratio - 1.0)))
        projected_mbps += subs * new_user_mbps
        contributing_subs += subs

    original_util  = (original_mbps / cap_dl) if cap_dl else 0
    # Combine with the cell's existing background utilization (other plans
    # not in scope keep loading the cell at their current level).
    background_util = (cell.get("capacity") or {}).get("current_utilization", 0.0)
    # Subtract the scope's *original* contribution (which is part of the
    # observed background) before re-adding the projected.
    isolated_bg = max(0.0, background_util - original_util)
    projected_util = isolated_bg + (projected_mbps / cap_dl if cap_dl else 0)

    return {
        "cell_id":              cell["_id"],
        "market":               cell.get("market"),
        "tech":                 cell.get("tech"),
        "capacity_dl_mbps":     cap_dl,
        "original_utilization": round(min(0.99, max(0.0, original_util + isolated_bg)), 3),
        "projected_utilization": round(min(1.0, max(0.0, projected_util)), 3),
        "contributing_subs":    contributing_subs,
        "risk":                 _classify(projected_util),
        "delta_pct":            round(100.0 * (projected_util - (original_util + isolated_bg)), 1),
    }


def _aggregate_to_enb_and_pgw(cell_projections: list[dict]) -> tuple[list, list]:
    """For each eNB that hosts an at-risk cell, collect its parent PGW via
    topology_edges and roll up cell loads."""
    if not cell_projections:
        return [], []
    cell_ids = [c["cell_id"] for c in cell_projections]
    # Cell → eNB
    enb_by_cell = {}
    for e in topology_edges.find({"from_id": {"$in": cell_ids}, "relation": "hosted_on"}):
        enb_by_cell[e["from_id"]] = e["to_id"]

    enb_ids = set(enb_by_cell.values())
    # eNB → SGW
    sgw_by_enb = {}
    for e in topology_edges.find({"from_id": {"$in": list(enb_ids)},
                                   "relation": "serves_via_s1u"}):
        sgw_by_enb[e["from_id"]] = e["to_id"]
    # SGW → PGW
    pgw_by_sgw = {}
    for e in topology_edges.find({"from_id": {"$in": list(sgw_by_enb.values())},
                                   "relation": "s5_to"}):
        pgw_by_sgw[e["from_id"]] = e["to_id"]

    # Roll-up: average projected_util per eNB and PGW, weighted by capacity
    enb_load: dict = {}
    for c in cell_projections:
        enb = enb_by_cell.get(c["cell_id"])
        if not enb:
            continue
        slot = enb_load.setdefault(enb, {"util_sum": 0.0, "count": 0, "cells": []})
        slot["util_sum"] += c["projected_utilization"]
        slot["count"]    += 1
        slot["cells"].append(c["cell_id"])

    enb_results = []
    for enb_id, slot in enb_load.items():
        enb = elements.find_one({"_id": enb_id}, {"capacity": 1, "market": 1, "vendor": 1})
        base_util = ((enb or {}).get("capacity") or {}).get("current_utilization", 0.5)
        # Projected enb util = baseline shifted by mean projected delta across hosted cells
        avg_cell_proj = slot["util_sum"] / max(1, slot["count"])
        projected = round(min(1.0, base_util * 0.5 + avg_cell_proj * 0.5), 3)
        enb_results.append({
            "neId":                  enb_id,
            "type":                   "eNodeB",
            "market":                 (enb or {}).get("market"),
            "vendor":                 (enb or {}).get("vendor"),
            "original_utilization":   base_util,
            "projected_utilization":  projected,
            "hosted_cells_in_scope":  slot["count"],
            "risk":                   _classify(projected),
        })

    pgw_load: dict = {}
    for enb_id in enb_load:
        sgw = sgw_by_enb.get(enb_id)
        pgw = pgw_by_sgw.get(sgw) if sgw else None
        if not pgw:
            continue
        slot = pgw_load.setdefault(pgw, {"enb_count": 0, "util_sum": 0.0})
        # Approximate each upstream eNB's projected util as the cell-avg above
        slot["util_sum"] += enb_load[enb_id]["util_sum"] / max(1, enb_load[enb_id]["count"])
        slot["enb_count"] += 1

    pgw_results = []
    for pgw_id, slot in pgw_load.items():
        pgw = elements.find_one({"_id": pgw_id}, {"capacity": 1, "market": 1, "vendor": 1})
        base = ((pgw or {}).get("capacity") or {}).get("current_utilization", 0.6)
        # PGW shift = small fraction of summed eNB shift (PGW aggregates many eNBs)
        projected = round(min(1.0, base + 0.18 * (slot["util_sum"] / max(1, slot["enb_count"]) - 0.5)), 3)
        pgw_results.append({
            "neId":                  pgw_id,
            "type":                   "PGW",
            "market":                 (pgw or {}).get("market"),
            "vendor":                 (pgw or {}).get("vendor"),
            "original_utilization":   base,
            "projected_utilization":  projected,
            "upstream_enbs_in_scope": slot["enb_count"],
            "risk":                   _classify(projected),
        })
    return enb_results, pgw_results


# ─── Hybrid vector + structured search for analogous past scenarios ────────

def _build_fingerprint(scenario: dict, plan: dict,
                       old_qos: dict | None, new_qos: dict | None) -> str:
    """Render an NL fingerprint of the proposed change for vector embedding."""
    cs    = scenario.get("change_set", {})
    scope = scenario.get("scope", {})
    parts = []
    parts.append(f"What-if scenario for {plan.get('name', 'a plan')} ({plan.get('segment')})")
    if old_qos and new_qos and old_qos.get("max_downlink_mbps") != new_qos.get("max_downlink_mbps"):
        parts.append(f"raising downlink QoS cap from {old_qos.get('max_downlink_mbps')} Mbps "
                     f"to {new_qos.get('max_downlink_mbps')} Mbps")
    if cs.get("apn_change"):
        parts.append(f"migrating APN from {cs['apn_change'].get('from')} to "
                     f"{cs['apn_change'].get('to')}")
    if cs.get("pcrf_template_change"):
        parts.append(f"updating PCRF template from {cs['pcrf_template_change'].get('from')} "
                     f"to {cs['pcrf_template_change'].get('to')}")
    if cs.get("roaming_enable"):
        parts.append(f"enabling roaming in {', '.join(cs['roaming_enable'])}")
    if scope.get("markets"):
        parts.append(f"in markets {', '.join(scope['markets'])}")
    if scope.get("time_windows"):
        parts.append(f"affecting time windows {', '.join(scope['time_windows'])}")
    return ", ".join(parts) + ". " + (
        "We are concerned about cell sector overload, PGW saturation during peak windows, "
        "HSS attach-rate pressure from PCRF reapplication, and impact on subscriber QoE. "
        "We need to know whether similar past changes caused issues, what mitigations worked, "
        "and which network elements to watch."
    )


def _hybrid_knowledge_search(fingerprint: str, scenario: dict) -> tuple[list, list, str]:
    """
    Run the hybrid query: $vectorSearch on knowledge_chunks.text combined with
    structured filters drawn from the scenario's change_set / scope.
    Returns (matches, pipeline_used, filter_level_label).

    Gracefully degrades from a full hybrid filter to broader ones if the
    initial result is empty (e.g. unusual market/segment combination).
    """
    cs    = scenario.get("change_set", {}) or {}
    scope = scenario.get("scope", {})       or {}
    segment = None
    if cs.get("plan_id"):
        p = plans.find_one({"_id": cs["plan_id"]}, {"segment": 1})
        segment = (p or {}).get("segment")
    markets = scope.get("markets") or []

    # Increasingly relaxed filters. The dashboard renders the chosen filter
    # level so the demo can show the fallback path.
    filters: list[tuple[str, dict | None]] = []
    if segment and markets:
        filters.append((
            f"hybrid (segment={segment} ∧ market∈{markets} ∧ kind∈[incident,runbook])",
            {"segment": {"$eq": segment},
             "market":  {"$in": markets},
             "kind":    {"$in": ["incident", "runbook"]}},
        ))
    if segment:
        filters.append((
            f"relaxed (segment={segment} ∧ kind∈[incident,runbook])",
            {"segment": {"$eq": segment},
             "kind":    {"$in": ["incident", "runbook"]}},
        ))
    filters.append((
        "broad (kind∈[incident,runbook])",
        {"kind": {"$in": ["incident", "runbook"]}},
    ))
    filters.append(("widest (no filter)", None))

    def _pipeline(flt: dict | None):
        spec = {
            "index":   "dtw_knowledge_index",
            "path":    "text",
            "query":   fingerprint,
            "numCandidates": 200,
            "limit":   5,
        }
        if flt is not None:
            spec["filter"] = flt
        return [
            {"$vectorSearch": spec},
            {"$project": {
                "_id":  0,
                "id":   "$_id",
                "kind": 1, "title": 1, "market": 1, "segment": 1,
                "plan_id": 1, "tags": 1, "ts": 1,
                "linked_runbook": 1, "text": 1,
                "score": {"$meta": "vectorSearchScore"},
            }},
        ]

    pipeline = []
    matches = []
    chosen_level = "—"
    for label, flt in filters:
        pipeline = _pipeline(flt)
        try:
            matches = list(knowledge_chunks.aggregate(pipeline))
        except Exception as e:
            logger.warning(f"vector search failed at level {label!r}: {e}")
            matches = []
        if matches:
            chosen_level = label
            break

    return matches, pipeline, chosen_level


# ─── Public tools ─────────────────────────────────────────────────────────

@mcp.tool()
def simulate_qos_change(scenario_id: str = None, text: str = None) -> str:
    """
    Run a QoS-uplift simulation (Flow A). Accepts either a pre-created
    scenario id OR a natural-language description — when text is given,
    the scenario is created inline before the simulation runs.

    Performs four steps in one tool call:
      1) Resolve plan + old/new QoS profiles from the scenario change_set.
      2) $graphLookup downstream from the plan node (cells → eNBs → SGW → PGW).
      3) Per-cell utilization projection from dtw_traffic_models + QoS ratio.
      4) Hybrid $vectorSearch + structured filter on dtw_knowledge_chunks for
         analogous past scenarios and their mitigation playbooks.

    Args:
        scenario_id: Existing scenario id, e.g. 'DTW-SCN-003'. Optional when text is given.
        text:        Natural-language what-if request (e.g. 'Raise ACME M downlink to 20 Mbps
                     in NYC Saturday evening'). Used to create the scenario inline.
    """
    if not scenario_id and text:
        scenario_id = _create_scenario_inline(text)
    if not scenario_id:
        return "❌ Provide either scenario_id or text describing the QoS change."
    s = scenarios.find_one({"_id": scenario_id})
    if not s:
        return f"❌ Scenario {scenario_id} not found."
    if s.get("status") == "cancelled":
        return f"❌ Scenario {scenario_id} is cancelled."

    cs    = s.get("change_set", {}) or {}
    scope = s.get("scope", {})       or {}
    plan_id = cs.get("plan_id")
    old_qos_id = cs.get("old_qos_profile_id")
    new_qos_id = cs.get("new_qos_profile_id")
    if not (plan_id and old_qos_id and new_qos_id):
        return ("❌ Scenario change_set is missing plan_id / old_qos_profile_id / "
                "new_qos_profile_id — required for simulate_qos_change. "
                "Use simulate_roaming_change for policy-only scenarios.")

    plan    = plans.find_one({"_id": plan_id})
    old_qos = qos_profiles.find_one({"_id": old_qos_id})
    new_qos = qos_profiles.find_one({"_id": new_qos_id})
    if not (plan and old_qos and new_qos):
        return f"❌ Could not resolve plan or QoS profiles for scenario {scenario_id}."

    markets      = scope.get("markets") or []
    time_windows = scope.get("time_windows") or []

    # Step 2: graph walk so the dashboard can show the tree.
    graph_walk_docs = _graph_dependency_walk(plan_id, max_depth=4)
    graph_edges = []
    for d in graph_walk_docs:
        graph_edges.append({"from_id": d["from_id"], "to_id": d["to_id"],
                            "relation": d.get("relation"), "depth": 0})
        for w in d.get("walk", []):
            graph_edges.append({"from_id": w["from_id"], "to_id": w["to_id"],
                                "relation": w.get("relation"),
                                "depth": w.get("depth", 0) + 1})

    # Step 3: per-cell projection.
    cells = _affected_cells(plan_id, markets)
    # Lookup matching traffic-model entries per cell for the requested windows
    tm_query: dict = {"plan_id": plan_id}
    if markets:
        tm_query["market"] = {"$in": markets}
    if time_windows:
        tm_query["time_window._id"] = {"$in": time_windows}
    tms = list(traffic_models.find(tm_query))

    entries_by_cell: dict = {}
    for tm in tms:
        for c in tm.get("cells", []):
            entries_by_cell.setdefault(c["cell_id"], []).append(c)

    cell_projections = []
    for cell in cells:
        tm_entries = entries_by_cell.get(cell["_id"], [])
        if not tm_entries:
            continue
        cell_projections.append(_project_cell_load(cell, old_qos, new_qos, tm_entries))

    # Roll-up to eNB + PGW
    enb_results, pgw_results = _aggregate_to_enb_and_pgw(cell_projections)
    cells_over_capacity = [c for c in cell_projections
                           if c["risk"] in ("RED", "BLOCK")]
    core_elements_at_risk = [e for e in (enb_results + pgw_results)
                              if e["risk"] in ("RED", "BLOCK")]

    # Step 4: hybrid vector + structured search
    fingerprint = _build_fingerprint(s, plan, old_qos, new_qos)
    matches, vector_pipeline, filter_level = _hybrid_knowledge_search(fingerprint, s)

    # Narrative summary (the LLM will further elaborate)
    summary_bits = [
        f"QoS uplift on **{plan.get('name')}** ({plan['_id']}) from "
        f"{old_qos.get('max_downlink_mbps')} → {new_qos.get('max_downlink_mbps')} Mbps DL."
    ]
    if markets:
        summary_bits.append(f"Scope: {', '.join(markets)}.")
    if time_windows:
        summary_bits.append(f"Windows: {', '.join(time_windows)}.")
    summary_bits.append(
        f"Projection: {len(cells_over_capacity)} cell(s) RED/BLOCK, "
        f"{len(core_elements_at_risk)} core element(s) at risk."
    )
    if matches:
        top = matches[0]
        summary_bits.append(
            f"Hybrid vector search ({filter_level}) found {len(matches)} analogous "
            f"past scenario(s); closest is `{top.get('id') or top.get('title','—')}` "
            f"with similarity {top.get('score', 0):.2f}."
        )
    narrative = " ".join(summary_bits)

    # Persist results
    results = {
        "computed_at":             datetime.datetime.now(),
        "fingerprint":             fingerprint,
        "graph_walk":              graph_edges[:200],  # cap for storage
        "cell_projections":        cell_projections,
        "cells_over_capacity":     cells_over_capacity,
        "enb_projections":         enb_results,
        "pgw_projections":         pgw_results,
        "core_elements_at_risk":   core_elements_at_risk,
        "similar_past_scenarios":  matches,
        "vector_filter_level":     filter_level,
        "vector_pipeline":         json.loads(json.dumps(vector_pipeline, default=str)),
        "thresholds":              {"yellow": UTIL_YELLOW, "red": UTIL_RED, "block": UTIL_BLOCK},
        "narrative_summary":       narrative,
    }
    scenarios.update_one(
        {"_id": scenario_id},
        {
            "$set":  {"status": "completed", "results": results},
            "$push": {"history": {"ts": datetime.datetime.now(),
                                  "event": "simulated",
                                  "note": f"qos_change · {filter_level}"}},
        },
    )

    # Build the LLM-facing summary
    lines = [
        f"## 🧮 Simulation result for {scenario_id}",
        narrative,
        "",
    ]
    if cells_over_capacity:
        lines.append("### 🔴 Cells over capacity threshold")
        for c in cells_over_capacity[:10]:
            lines.append(
                f"- `{c['cell_id']}` · {c.get('market')} · "
                f"{int(100*c['original_utilization'])}% → "
                f"{int(100*c['projected_utilization'])}% "
                f"({'+' if c['delta_pct'] >= 0 else ''}{c['delta_pct']:.1f}pp) "
                f"· {c['contributing_subs']} subs · **{c['risk']}**"
            )
        if len(cells_over_capacity) > 10:
            lines.append(f"  … and {len(cells_over_capacity) - 10} more.")
        lines.append("")
    else:
        lines.append("### 🟢 No cells crossed the RED threshold")
        lines.append("")

    if core_elements_at_risk:
        lines.append("### ⚠️  Core elements at risk")
        for e in core_elements_at_risk:
            lines.append(
                f"- `{e['neId']}` · {e['type']} · {e.get('market')} · "
                f"{int(100*e['original_utilization'])}% → "
                f"{int(100*e['projected_utilization'])}% · **{e['risk']}**"
            )
        lines.append("")

    if matches:
        lines.append(f"### 🔍 Similar past scenarios — `{filter_level}`")
        for m in matches[:3]:
            ts = m.get("ts")
            ts_str = ts.strftime("%Y-%m-%d") if isinstance(ts, datetime.datetime) else (ts or "—")
            lines.append(f"- **{m.get('title', '—')}** · {m.get('market', '—')} · "
                         f"{ts_str} · similarity {m.get('score', 0):.2f}")
            if m.get("linked_runbook"):
                lines.append(f"  Runbook: `{m['linked_runbook']}`")
        lines.append("")

    rec_runbooks = list({m.get("linked_runbook") for m in matches if m.get("linked_runbook")})
    if rec_runbooks:
        lines.append("### 📘 Recommended mitigation runbooks (from past scenarios)")
        for rb_id in rec_runbooks:
            rb = knowledge_chunks.find_one({"_id": rb_id})
            if rb:
                lines.append(f"- `{rb['_id']}` — {rb.get('title')}")

    return "\n".join(lines)


@mcp.tool()
def simulate_roaming_change(scenario_id: str = None, text: str = None) -> str:
    """
    Run a control-plane simulation for an APN / PCRF / roaming-enable
    scenario (Flow B). Projects HSS query-rate and attach-rate impact and
    surfaces analogous past scenarios via the hybrid vector search.

    Pass either scenario_id (pre-created) or text (free-form description of
    the change — the scenario is created inline). You do not need to call
    dtw_scenario_service first.

    Args:
        scenario_id: Pre-existing scenario id (DTW-SCN-###), if available.
        text:        Natural-language description of the APN/PCRF/roaming
                     change (e.g. "Migrate ACME M to new APN, update PCRF
                     template, enable Canada roaming"). Used when no
                     scenario_id is provided.
    """
    if not scenario_id and text:
        scenario_id = _create_scenario_inline(text)
    if not scenario_id:
        return "❌ Provide either scenario_id or text describing the roaming/policy change."

    s = scenarios.find_one({"_id": scenario_id})
    if not s:
        return f"❌ Scenario {scenario_id} not found."

    cs    = s.get("change_set", {}) or {}
    scope = s.get("scope", {}) or {}
    plan_id = cs.get("plan_id")

    if not (cs.get("apn_change") or cs.get("pcrf_template_change") or cs.get("roaming_enable")):
        return ("❌ Scenario does not contain an APN, PCRF, or roaming change — "
                "use simulate_qos_change for QoS scenarios.")

    plan = plans.find_one({"_id": plan_id}) if plan_id else None

    # Subscriber count affected
    sub_q: dict = {"plan_id": plan_id} if plan_id else {}
    if scope.get("markets"):
        sub_q["home_market"] = {"$in": scope["markets"]}
    affected_subs = subscribers.count_documents(sub_q)

    # HSS load delta heuristics
    hss_delta_pct = 0.0
    notes = []
    if cs.get("apn_change"):
        hss_delta_pct += 12.0
        notes.append("APN migration triggers per-session policy reapplication on HSS.")
    if cs.get("pcrf_template_change"):
        hss_delta_pct += 18.0
        notes.append("PCRF template change causes bulk policy push — expect attach-rate spike.")
    if cs.get("roaming_enable"):
        hss_delta_pct += 8.0 * len(cs["roaming_enable"])
        notes.append(f"Roaming enable in {len(cs['roaming_enable'])} country(ies) "
                     "drives roaming-partner HSS queries.")

    # HSS instances in scope
    hss_q: dict = {"type": "HSS"}
    if scope.get("markets"):
        hss_q["market"] = {"$in": scope["markets"]}
    hss_units = list(elements.find(hss_q))
    hss_projections = []
    for hss in hss_units:
        base = (hss.get("capacity") or {}).get("current_utilization", 0.6)
        projected = round(min(1.0, base * (1.0 + hss_delta_pct / 100.0)), 3)
        hss_projections.append({
            "neId":                 hss["_id"],
            "type":                 "HSS",
            "market":               hss.get("market"),
            "original_utilization": base,
            "projected_utilization": projected,
            "risk":                 _classify(projected),
        })

    # Hybrid vector search (reuse, no per-cell load)
    fingerprint = _build_fingerprint(s, plan or {"name": plan_id or "?", "segment": "?"},
                                     None, None)
    matches, vector_pipeline, filter_level = _hybrid_knowledge_search(fingerprint, s)

    core_at_risk = [h for h in hss_projections if h["risk"] in ("RED", "BLOCK")]
    narrative = (
        f"Control-plane scenario on **{(plan or {}).get('name', plan_id or '?')}**. "
        f"~{affected_subs} subscriber(s) in scope. "
        f"Projected HSS load shift: +{hss_delta_pct:.0f}%. "
        f"{len(core_at_risk)} HSS unit(s) projected RED/BLOCK. "
        f"Hybrid vector search ({filter_level}) found {len(matches)} analogous "
        "past scenario(s)."
    )

    results = {
        "computed_at":             datetime.datetime.now(),
        "fingerprint":             fingerprint,
        "scenario_kind":           "roaming_policy",
        "affected_subscribers":    affected_subs,
        "hss_delta_pct":           hss_delta_pct,
        "hss_projections":         hss_projections,
        "core_elements_at_risk":   core_at_risk,
        "similar_past_scenarios":  matches,
        "vector_filter_level":     filter_level,
        "vector_pipeline":         json.loads(json.dumps(vector_pipeline, default=str)),
        "notes":                   notes,
        "thresholds":              {"yellow": UTIL_YELLOW, "red": UTIL_RED, "block": UTIL_BLOCK},
        "narrative_summary":       narrative,
    }
    scenarios.update_one(
        {"_id": scenario_id},
        {
            "$set":  {"status": "completed", "results": results},
            "$push": {"history": {"ts": datetime.datetime.now(),
                                  "event": "simulated",
                                  "note": f"roaming_change · {filter_level}"}},
        },
    )

    lines = [
        f"## 🧮 Roaming/Policy simulation for {scenario_id}",
        narrative,
        "",
        f"**Subscribers in scope:** {affected_subs}",
        f"**Projected HSS load shift:** +{hss_delta_pct:.0f}%",
        "",
    ]
    if notes:
        lines.append("**Control-plane impact notes:**")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")
    if hss_projections:
        lines.append("**HSS unit projections:**")
        for h in hss_projections:
            lines.append(f"- `{h['neId']}` · {h.get('market')} · "
                         f"{int(100*h['original_utilization'])}% → "
                         f"{int(100*h['projected_utilization'])}% · **{h['risk']}**")
        lines.append("")
    if matches:
        lines.append(f"### 🔍 Similar past scenarios — `{filter_level}`")
        for m in matches[:3]:
            ts = m.get("ts")
            ts_str = ts.strftime("%Y-%m-%d") if isinstance(ts, datetime.datetime) else (ts or "—")
            lines.append(f"- **{m.get('title', '—')}** · {m.get('market', '—')} · {ts_str} · "
                         f"similarity {m.get('score', 0):.2f}")
            if m.get("linked_runbook"):
                lines.append(f"  Runbook: `{m['linked_runbook']}`")
    return "\n".join(lines)


@mcp.tool()
def diff_scenarios(scenario_id_a: str, scenario_id_b: str) -> str:
    """
    Compare the results of two completed scenarios. Highlights differences
    in projected utilization and at-risk element counts.

    Args:
        scenario_id_a: Baseline scenario id.
        scenario_id_b: Comparison scenario id.
    """
    a = scenarios.find_one({"_id": scenario_id_a})
    b = scenarios.find_one({"_id": scenario_id_b})
    if not a or not b:
        return f"❌ Could not load both scenarios ({scenario_id_a}, {scenario_id_b})."
    if not a.get("results") or not b.get("results"):
        return "❌ Both scenarios must be simulated before diffing."

    ar = a["results"]
    br = b["results"]
    lines = [
        f"## Diff: `{scenario_id_a}` vs `{scenario_id_b}`",
        "",
        f"| Metric | {scenario_id_a} | {scenario_id_b} |",
        "|---|---|---|",
        f"| cells over capacity | {len(ar.get('cells_over_capacity') or [])} | {len(br.get('cells_over_capacity') or [])} |",
        f"| core elements at risk | {len(ar.get('core_elements_at_risk') or [])} | {len(br.get('core_elements_at_risk') or [])} |",
        f"| similar past scenarios | {len(ar.get('similar_past_scenarios') or [])} | {len(br.get('similar_past_scenarios') or [])} |",
        f"| filter level | {ar.get('vector_filter_level')} | {br.get('vector_filter_level')} |",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_simulation_result(scenario_id: str) -> str:
    """
    Return the persisted simulation result for a scenario as readable text.

    Args:
        scenario_id: Scenario id.
    """
    s = scenarios.find_one({"_id": scenario_id})
    if not s:
        return f"❌ Scenario {scenario_id} not found."
    r = s.get("results")
    if not r:
        return f"ℹ️  No simulation result stored yet for {scenario_id}. " \
               "Call simulate_qos_change or simulate_roaming_change."
    summary = r.get("narrative_summary") or "(no narrative)"
    return f"## Stored result for {scenario_id}\n\n{summary}"


if __name__ == "__main__":
    mcp.run()
