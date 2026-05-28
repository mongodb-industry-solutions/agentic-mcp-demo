#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Scenario Service — What-if scenario lifecycle for the digital twin demo.

The submit-and-track surface of the Digital Twin demo. Owns dtw_scenarios.
Captures natural-language what-if requests ("raise prepaid M to 20 Mbps in
NYC and LA on Saturday night"), parses them into a structured change_set +
scope via gpt-4o, and tracks lifecycle status. The actual numerical
simulation is performed by the simulation service.

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

import datetime
import json
import logging
import os
import re

from pymongo import MongoClient, DESCENDING
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("dtw_scenario_service")
logger        = logging.getLogger("dtw_scenario_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
scenarios     = db["dtw_scenarios"]
plans         = db["dtw_plans"]
qos_profiles  = db["dtw_qos_profiles"]
markets_coll  = db["dtw_markets"]

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
PARSE_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o")


def _next_scenario_id() -> str:
    last = scenarios.find_one(
        {"_id": {"$regex": r"^DTW-SCN-\d+$"}},
        sort=[("_id", DESCENDING)],
    )
    if not last:
        return "DTW-SCN-001"
    n = int(last["_id"].split("-")[-1])
    return f"DTW-SCN-{n + 1:03d}"


def _known_plans() -> list[str]:
    return [p["_id"] for p in plans.find({}, {"_id": 1})]


def _known_qos() -> list[str]:
    return [q["_id"] for q in qos_profiles.find({}, {"_id": 1})]


def _known_markets() -> list[str]:
    return [m["_id"] for m in markets_coll.find({}, {"_id": 1})]


def _resolve_plan_id(hint: str) -> str | None:
    if not hint:
        return None
    if plans.find_one({"_id": hint}):
        return hint
    doc = plans.find_one({"name": {"$regex": hint, "$options": "i"}})
    return doc["_id"] if doc else None


def _resolve_qos_id(hint: str) -> str | None:
    if not hint:
        return None
    if qos_profiles.find_one({"_id": hint}):
        return hint
    doc = qos_profiles.find_one({"name": {"$regex": hint, "$options": "i"}})
    return doc["_id"] if doc else None


def _resolve_market_id(hint: str) -> str | None:
    if not hint:
        return None
    if markets_coll.find_one({"_id": hint}):
        return hint
    doc = markets_coll.find_one({"name": {"$regex": hint, "$options": "i"}})
    if doc:
        return doc["_id"]
    h = hint.lower()
    for m in _known_markets():
        if m.lower().startswith(h):
            return m
    return None


def _parse_natural_language(text: str) -> dict:
    """Use gpt-4o to extract a structured what-if scenario from natural language."""
    today = datetime.date.today().isoformat()
    prompt = (
        f"You are a parser for telecom what-if scenarios on a digital twin. "
        f"Today is {today}.\n\n"
        f"Extract structured fields from this request:\n\n{text!r}\n\n"
        f"Known plans: {', '.join(_known_plans())}\n"
        f"Known QoS profiles: {', '.join(_known_qos())}\n"
        f"Known markets: {', '.join(_known_markets())}\n\n"
        "Return ONLY valid JSON, no prose, with this schema:\n"
        "{\n"
        '  "scenario_type": "qos_change" | "policy_change" | "subscriber_shift" | "other",\n'
        '  "change_set": {\n'
        '    "plan_id": string | null,\n'
        '    "old_qos_profile_id": string | null,\n'
        '    "new_qos_profile_id": string | null,\n'
        '    "apn_change": { "from": string, "to": string } | null,\n'
        '    "pcrf_template_change": { "from": string, "to": string } | null,\n'
        '    "roaming_enable": string[] | null      // country codes\n'
        "  },\n"
        '  "scope": {\n'
        '    "markets": string[],          // subset of known markets, [] = all\n'
        '    "time_windows": string[]      // e.g. ["Saturday_20_23"], [] = all known\n'
        "  },\n"
        '  "summary": string                // one short sentence\n'
        "}\n\n"
        "Use null/[] for fields not mentioned. Do not invent plan or market ids "
        "outside the known lists. If a user says '7.2 to 20 Mbps', map to "
        "qos_prepaid_7_2 and qos_prepaid_20 if those exist. Prefer 'Saturday_20_23' "
        "when the user mentions Saturday evening/night."
    )
    resp = openai_client.chat.completions.create(
        model=PARSE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def _format_scenario_card(s: dict) -> str:
    status = s.get("status", "—")
    status_emoji = {
        "submitted": "📝",
        "scoped":    "🎯",
        "simulated": "🧮",
        "completed": "✓",
        "cancelled": "⊗",
    }.get(status, "•")

    cs   = s.get("change_set") or {}
    scope = s.get("scope") or {}
    bits = []
    if cs.get("plan_id"):
        bits.append(f"plan {cs['plan_id']}")
    if cs.get("old_qos_profile_id") and cs.get("new_qos_profile_id"):
        bits.append(f"QoS {cs['old_qos_profile_id']} → {cs['new_qos_profile_id']}")
    if cs.get("apn_change"):
        bits.append(f"APN {cs['apn_change'].get('from')} → {cs['apn_change'].get('to')}")
    if cs.get("roaming_enable"):
        bits.append(f"roam +{','.join(cs['roaming_enable'])}")

    lines = [f"**{s['_id']}** · {status_emoji} {status} · {s.get('scenario_type', '—')}"]
    if s.get("description"):
        lines.append(f"  {s['description']}")
    if bits:
        lines.append(f"  Δ: {' · '.join(bits)}")
    if scope.get("markets") or scope.get("time_windows"):
        scope_bits = []
        if scope.get("markets"):
            scope_bits.append(f"markets: {', '.join(scope['markets'])}")
        if scope.get("time_windows"):
            scope_bits.append(f"windows: {', '.join(scope['time_windows'])}")
        lines.append("  " + " · ".join(scope_bits))
    return "\n".join(lines)


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def create_scenario(text: str) -> str:
    """
    Submit a new natural-language what-if scenario. The service parses the
    request via LLM, extracts change_set (plan, QoS old→new, APN/roaming
    changes) and scope (markets, time windows), and stores it in
    dtw_scenarios with status='submitted'.

    Next step: call simulate_qos_change (or simulate_roaming_change) from
    the simulation service with the returned scenario_id.

    Args:
        text: The user's natural-language what-if request.
    """
    parsed = _parse_natural_language(text)

    # Light id-normalization in case the LLM produced near-misses
    cs = parsed.get("change_set") or {}
    cs["plan_id"]             = _resolve_plan_id(cs.get("plan_id") or "")  or cs.get("plan_id")
    cs["old_qos_profile_id"]  = _resolve_qos_id (cs.get("old_qos_profile_id") or "")  or cs.get("old_qos_profile_id")
    cs["new_qos_profile_id"]  = _resolve_qos_id (cs.get("new_qos_profile_id") or "")  or cs.get("new_qos_profile_id")
    sc = parsed.get("scope") or {}
    sc["markets"] = [m for m in (sc.get("markets") or []) if _resolve_market_id(m)] \
                    or (sc.get("markets") or [])

    # Reject inputs that don't actually describe a what-if change.
    # Without this guard, imperative commands ("run simulation", "show me
    # results") get parsed into empty scenarios with scenario_type='other'
    # and pollute dtw_scenarios.
    has_change = bool(
        cs.get("plan_id") or cs.get("new_qos_profile_id")
        or cs.get("apn_change") or cs.get("pcrf_template_change")
        or cs.get("roaming_enable")
    )
    if not has_change:
        return (
            "❌ This doesn't look like a what-if scenario description.\n\n"
            "A scenario must name a plan/QoS/APN/PCRF/roaming change, e.g.\n"
            "  • 'raise prepaid ACME M downlink to 20 Mbps in NYC'\n"
            "  • 'enable roaming for plan_ACME_Premium in Canada'\n\n"
            "If you intended to RUN a simulation on an existing scenario, "
            "use dtw_simulation_service.simulate_qos_change(scenario_id) "
            "instead. If you want to see the latest scenarios, call "
            "list_scenarios()."
        )

    sid = _next_scenario_id()
    doc = {
        "_id":            sid,
        "description":    parsed.get("summary") or text[:120],
        "scenario_type":  parsed.get("scenario_type") or "other",
        "raw_text":       text,
        "change_set":     cs,
        "scope":          sc,
        "status":         "submitted",
        "submitted_at":   datetime.datetime.now(),
        "history":        [{"ts": datetime.datetime.now(), "event": "submitted",
                            "note": "parsed from natural language"}],
        "results":        None,
    }
    scenarios.insert_one(doc)
    logger.info(f"Created scenario {sid}")

    lines = [
        f"## 📝 Scenario {sid} — awaiting confirmation",
        f"",
        f"**Type:** {doc['scenario_type']}",
        f"**Description:** {doc['description']}",
        f"",
        f"**Parsed parameters — please verify:**",
    ]
    if cs.get("plan_id"):
        lines.append(f"- Plan: `{cs['plan_id']}`")
    if cs.get("old_qos_profile_id") and cs.get("new_qos_profile_id"):
        lines.append(f"- QoS profile: `{cs['old_qos_profile_id']}` → `{cs['new_qos_profile_id']}`")
    elif cs.get("new_qos_profile_id"):
        lines.append(f"- New QoS profile: `{cs['new_qos_profile_id']}`")
    if cs.get("apn_change"):
        lines.append(f"- APN: `{cs['apn_change'].get('from')}` → `{cs['apn_change'].get('to')}`")
    if cs.get("pcrf_template_change"):
        lines.append(f"- PCRF template: `{cs['pcrf_template_change'].get('from')}` → `{cs['pcrf_template_change'].get('to')}`")
    if cs.get("roaming_enable"):
        lines.append(f"- Roaming enable: {', '.join(cs['roaming_enable'])}")
    if sc.get("markets"):
        lines.append(f"- Markets: {', '.join(sc['markets'])}")
    if sc.get("time_windows"):
        lines.append(f"- Time windows: {', '.join(sc['time_windows'])}")
    lines += [
        f"",
        f"If the parameters look correct, say **'run the simulation'**.",
        f"To adjust, say e.g. **'change {sid} to 50 Mbps'** or "
        f"**'add LA to the scope'** — then simulate.",
    ]
    return "\n".join(lines)


@mcp.tool()
def update_scenario(modification: str, scenario_id: str = None) -> str:
    """
    Apply a natural-language modification to an existing submitted scenario
    before running the simulation. Re-parses the original request with the
    change applied and updates change_set + scope in place.

    If scenario_id is omitted, targets the most-recently submitted scenario
    (i.e. 'the last one', 'the current scenario').

    Args:
        modification: What to change, in plain language — e.g.
                      "raise downlink to 50 Mbps", "NYC only",
                      "change time window to Sunday morning",
                      "add LA to the scope".
        scenario_id:  Scenario to update. Defaults to the most recent
                      submitted (not yet simulated) scenario.
    """
    if scenario_id:
        s = scenarios.find_one({"_id": scenario_id})
    else:
        s = scenarios.find_one(
            {"status": "submitted"},
            sort=[("submitted_at", DESCENDING)],
        )
    if not s:
        return ("❌ No submitted scenario found. "
                "Create one first with a what-if request.")
    if s.get("status") not in ("submitted",):
        return (f"❌ Scenario {s['_id']} is already '{s.get('status')}' "
                f"and cannot be modified. Submit a new scenario instead.")

    # Focused amendment LLM: give it the EXISTING scenario as JSON and the
    # user's modification, ask for the updated JSON. This is much more
    # reliable than re-parsing "original + amendment" as one block, which
    # the LLM tended to anchor on the original (e.g. keeping new_qos =
    # qos_prepaid_20 even when the user said "change to 15 Mbps").
    original_cs = s.get("change_set") or {}
    original_sc = s.get("scope") or {}
    amend_prompt = (
        "You are amending an existing what-if scenario on a telecom digital "
        "twin. Apply the user's modification to the EXISTING change_set and "
        "scope, leaving fields untouched when the modification is silent on "
        "them. Return ONLY the COMPLETE updated JSON — same schema as the "
        "input, every field included.\n\n"
        f"Known plans: {', '.join(_known_plans())}\n"
        f"Known QoS profiles: {', '.join(_known_qos())}\n"
        f"Known markets: {', '.join(_known_markets())}\n\n"
        f"Existing change_set: {json.dumps(original_cs)}\n"
        f"Existing scope:      {json.dumps(original_sc)}\n\n"
        f"User modification: {modification!r}\n\n"
        "Schema:\n"
        "{\n"
        '  "change_set": {\n'
        '    "plan_id": string | null,\n'
        '    "old_qos_profile_id": string | null,\n'
        '    "new_qos_profile_id": string | null,\n'
        '    "apn_change": { "from": string, "to": string } | null,\n'
        '    "pcrf_template_change": { "from": string, "to": string } | null,\n'
        '    "roaming_enable": string[] | null\n'
        "  },\n"
        '  "scope": {\n'
        '    "markets": string[],\n'
        '    "time_windows": string[]\n'
        "  },\n"
        '  "summary": string\n'
        "}\n\n"
        "Examples of correct amendments:\n"
        "- modification 'change to 15 Mbps' + existing new_qos_profile_id "
        "'qos_prepaid_20' → new_qos_profile_id 'qos_prepaid_15'.\n"
        "- modification 'NYC only' + existing scope.markets "
        "['NYC_Metro','LA_Metro'] → scope.markets ['NYC_Metro'].\n"
        "- modification 'add Chicago' + existing scope.markets "
        "['NYC_Metro'] → scope.markets ['NYC_Metro','Chicago_Metro'].\n"
        "- modification 'change to Sunday morning' + existing "
        "scope.time_windows ['Saturday_20_23'] → "
        "scope.time_windows ['Sunday_06_12']."
    )
    resp = openai_client.chat.completions.create(
        model=PARSE_MODEL,
        messages=[{"role": "user", "content": amend_prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    parsed = json.loads(resp.choices[0].message.content)

    cs = parsed.get("change_set") or {}
    cs["plan_id"]            = _resolve_plan_id(cs.get("plan_id") or "") or cs.get("plan_id")
    cs["old_qos_profile_id"] = _resolve_qos_id(cs.get("old_qos_profile_id") or "") or cs.get("old_qos_profile_id")
    cs["new_qos_profile_id"] = _resolve_qos_id(cs.get("new_qos_profile_id") or "") or cs.get("new_qos_profile_id")
    # Belt-and-braces: if the amendment LLM dropped a field that the
    # original had, restore it from the original.
    for k, v in original_cs.items():
        if cs.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
            cs[k] = v

    sc = parsed.get("scope") or {}
    sc["markets"] = ([m for m in sc.get("markets", []) if _resolve_market_id(m)]
                     or sc.get("markets") or [])
    for k, v in original_sc.items():
        if sc.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
            sc[k] = v

    # Substitution detection: when the user requested a specific Mbps value
    # but the LLM mapped to the nearest available profile, surface the
    # substitution explicitly. Silently rounding "change to 19 Mbps" down to
    # qos_prepaid_18 (18 Mbps) is the kind of dishonest UX that makes users
    # think the system is broken.
    substitution_note = ""
    # The TARGET rate in an amendment is the LAST Mbps mention — "change
    # from 7.2 Mbps to 19 Mbps" must capture 19, not 7.2. Use findall and
    # take the final match.
    mbps_matches = re.findall(r"(\d+(?:\.\d+)?)\s*mbps", modification, re.I)
    if mbps_matches and cs.get("new_qos_profile_id"):
        requested_rate = float(mbps_matches[-1])
        profile = qos_profiles.find_one({"_id": cs["new_qos_profile_id"]})
        actual_rate = (profile or {}).get("max_downlink_mbps")
        if actual_rate is not None and abs(requested_rate - actual_rate) > 0.05:
            # Find all prepaid profiles for the available-tiers hint
            available = sorted([
                p.get("max_downlink_mbps")
                for p in qos_profiles.find(
                    {"_id": {"$regex": r"^qos_prepaid_"}},
                    {"max_downlink_mbps": 1})
                if p.get("max_downlink_mbps")
            ])
            substitution_note = (
                f"⚠️ Requested **{requested_rate:g} Mbps** has no exact QoS "
                f"profile — using nearest match `{cs['new_qos_profile_id']}` "
                f"(**{actual_rate:g} Mbps**). Available prepaid tiers: "
                f"{', '.join(f'{r:g}' for r in available)} Mbps."
            )

    scenarios.update_one(
        {"_id": s["_id"]},
        {
            "$set": {
                "change_set":  cs,
                "scope":       sc,
                "description": parsed.get("summary") or s["description"],
            },
            "$push": {"history": {
                "ts":    datetime.datetime.now(),
                "event": "updated",
                "note":  modification,
            }},
        },
    )

    lines = [
        f"## ✏️ Scenario {s['_id']} updated",
        f"",
        f"**Modification applied:** {modification}",
        f"",
        f"**Updated parameters — please verify:**",
    ]
    if cs.get("plan_id"):
        lines.append(f"- Plan: `{cs['plan_id']}`")
    if cs.get("old_qos_profile_id") and cs.get("new_qos_profile_id"):
        lines.append(f"- QoS profile: `{cs['old_qos_profile_id']}` → `{cs['new_qos_profile_id']}`")
    elif cs.get("new_qos_profile_id"):
        lines.append(f"- New QoS profile: `{cs['new_qos_profile_id']}`")
    if cs.get("apn_change"):
        lines.append(f"- APN: `{cs['apn_change'].get('from')}` → `{cs['apn_change'].get('to')}`")
    if cs.get("roaming_enable"):
        lines.append(f"- Roaming enable: {', '.join(cs['roaming_enable'])}")
    if sc.get("markets"):
        lines.append(f"- Markets: {', '.join(sc['markets'])}")
    if sc.get("time_windows"):
        lines.append(f"- Time windows: {', '.join(sc['time_windows'])}")
    if substitution_note:
        lines += ["", substitution_note]
    lines += [
        f"",
        f"Say **'run the simulation'** when ready, or describe another change.",
    ]
    return "\n".join(lines)


@mcp.tool()
def list_scenarios(status_filter: str = None) -> str:
    """
    List scenarios, optionally filtered by status. Call with NO arguments
    to see all scenarios ("what scenarios exist", "what's in the queue",
    "show all what-ifs", "current scenarios"). Only pass a status_filter
    when the user explicitly asks for a specific status (e.g. "show only
    submitted scenarios", "list completed ones").

    Args:
        status_filter: Optional. 'submitted' (awaiting simulation),
                       'completed', 'cancelled'. Omit for all scenarios.
    """
    q = {"status": status_filter} if status_filter else {}
    docs = list(scenarios.find(q).sort("submitted_at", DESCENDING))
    if not docs:
        scope = f" with status '{status_filter}'" if status_filter else ""
        return f"No scenarios found{scope}."
    header = f"**{len(docs)} scenario{'s' if len(docs) != 1 else ''}" + \
             (f" with status '{status_filter}'" if status_filter else "") + ":**"
    return header + "\n\n" + "\n\n".join(_format_scenario_card(s) for s in docs)


@mcp.tool()
def get_scenario(scenario_id: str) -> str:
    """
    Get full details for one scenario: raw request, parsed change_set, scope,
    lifecycle history, and a summary of simulation results (if any).

    Args:
        scenario_id: Scenario id, e.g. 'DTW-SCN-003'.
    """
    s = scenarios.find_one({"_id": scenario_id})
    if not s:
        return f"❌ Scenario {scenario_id} not found."

    lines = [
        f"## Scenario {s['_id']}",
        f"**Status:** {s.get('status')}",
        f"**Type:** {s.get('scenario_type')}",
        f"**Submitted:** {s.get('submitted_at').strftime('%Y-%m-%d %H:%M') if s.get('submitted_at') else '—'}",
        "",
        f"**Description:** {s.get('description', '—')}",
        "",
        "**Verbatim request:**",
        f"> {s.get('raw_text', '—')}",
        "",
        "**Change set:**",
    ]
    cs = s.get("change_set") or {}
    if cs.get("plan_id"):                 lines.append(f"- plan: {cs['plan_id']}")
    if cs.get("old_qos_profile_id"):      lines.append(f"- old QoS: {cs['old_qos_profile_id']}")
    if cs.get("new_qos_profile_id"):      lines.append(f"- new QoS: {cs['new_qos_profile_id']}")
    if cs.get("apn_change"):              lines.append(f"- APN: {cs['apn_change'].get('from')} → {cs['apn_change'].get('to')}")
    if cs.get("pcrf_template_change"):    lines.append(f"- PCRF: {cs['pcrf_template_change'].get('from')} → {cs['pcrf_template_change'].get('to')}")
    if cs.get("roaming_enable"):          lines.append(f"- roaming enable: {', '.join(cs['roaming_enable'])}")

    sc = s.get("scope") or {}
    lines.append("")
    lines.append("**Scope:**")
    lines.append(f"- markets: {', '.join(sc.get('markets') or []) or '(all)'}")
    lines.append(f"- time windows: {', '.join(sc.get('time_windows') or []) or '(all)'}")

    results = s.get("results")
    if results:
        lines.append("")
        lines.append("**Simulation result summary:**")
        cells = results.get("cells_over_capacity") or []
        cores = results.get("core_elements_at_risk") or []
        lines.append(f"- {len(cells)} cell(s) over capacity threshold")
        lines.append(f"- {len(cores)} core element(s) at risk")
        if results.get("similar_past_scenarios"):
            lines.append(f"- {len(results['similar_past_scenarios'])} similar past scenario(s) "
                         "from hybrid vector search")
        if results.get("narrative_summary"):
            lines.append("")
            lines.append(results["narrative_summary"])
    else:
        lines.append("")
        lines.append("**Results:** not yet simulated. "
                     f"Call simulate_qos_change('{s['_id']}') to compute.")

    history = s.get("history") or []
    if history:
        lines.append("")
        lines.append("**History:**")
        for h in history:
            ts = h["ts"].strftime("%Y-%m-%d %H:%M") if isinstance(h.get("ts"), datetime.datetime) else h.get("ts", "—")
            lines.append(f"- {ts} · {h.get('event')} · {h.get('note', '')}")
    return "\n".join(lines)


@mcp.tool()
def cancel_scenario(scenario_id: str, reason: str = "user request") -> str:
    """
    Soft-cancel a scenario. Sets status='cancelled' and records the reason —
    the document is RETAINED for audit. Use delete_scenario to remove the
    document entirely.

    Args:
        scenario_id: Scenario id.
        reason:      Optional reason recorded in history.
    """
    s = scenarios.find_one({"_id": scenario_id})
    if not s:
        return f"❌ Scenario {scenario_id} not found."
    if s.get("status") == "cancelled":
        return f"ℹ️  Scenario {scenario_id} already cancelled."
    scenarios.update_one(
        {"_id": scenario_id},
        {
            "$set": {"status": "cancelled"},
            "$push": {"history": {"ts": datetime.datetime.now(),
                                  "event": "cancelled", "note": reason}},
        },
    )
    return f"⊗ Scenario {scenario_id} cancelled. Reason: {reason}"


@mcp.tool()
def delete_scenario(scenario_id: str) -> str:
    """
    Hard-delete a scenario document. Removes it from the dtw_scenarios
    collection entirely so it no longer appears on the dashboard.
    Distinct from cancel_scenario which only marks status='cancelled'.

    Use this when the user says "delete scenario X", "remove scenario X",
    "wipe scenario X", or similar.

    Args:
        scenario_id: Scenario id to delete.
    """
    r = scenarios.delete_one({"_id": scenario_id})
    if r.deleted_count == 0:
        return f"❌ Scenario {scenario_id} not found."
    return f"🗑️  Scenario {scenario_id} deleted."


@mcp.tool()
def delete_all_scenarios() -> str:
    """
    Hard-delete every scenario document — used to reset state between demo
    runs. Removes the documents entirely; the dashboard's Change Stream
    fires and the scenarios disappear from the UI.

    Use this when the user says "delete all scenarios", "wipe all
    scenarios", "clear scenarios", "reset scenarios", or similar.
    """
    r = scenarios.delete_many({})
    return (f"🗑️  Hard-deleted {r.deleted_count} scenario document(s). "
            f"The dtw_scenarios collection is now empty.")


if __name__ == "__main__":
    mcp.run()
