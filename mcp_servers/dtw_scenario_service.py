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

import datetime
import logging
import os
import re

from pymongo import MongoClient, DESCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("dtw_scenario_service")
logger        = logging.getLogger("dtw_scenario_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
# Per-session isolation (Phase B): mutable collections are prefixed with
# DEMO_PREFIX (empty → bare names); reference collections stay shared.
_PFX          = os.environ.get("DEMO_PREFIX", "")
scenarios     = db[_PFX + "dtw_scenarios"]  # mutable → session-scoped
plans         = db["dtw_plans"]             # reference → shared
qos_profiles  = db["dtw_qos_profiles"]      # reference → shared
markets_coll  = db["dtw_markets"]           # reference → shared


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
    """Resolve a market hint to a canonical id. Id matching (exact, then
    prefix) runs BEFORE any name matching, and the name regex is anchored
    at a word boundary — an unanchored substring match sent 'LA' to
    'Da-LLA-s-Fort Worth' instead of LA_Metro ('Los Angeles Metro'
    doesn't even contain the substring 'la')."""
    if not hint:
        return None
    if markets_coll.find_one({"_id": hint}):
        return hint
    h = hint.lower()
    known = _known_markets()
    for m in known:                       # case-insensitive exact id
        if m.lower() == h:
            return m
    for m in known:                       # id prefix: 'LA' → 'LA_Metro'
        if m.lower().startswith(h):
            return m
    doc = markets_coll.find_one(          # word-anchored name match:
        {"name": {"$regex": rf"\b{re.escape(hint)}",  # 'New York' → NYC_Metro
                  "$options": "i"}})
    if doc:
        return doc["_id"]
    return None


def _resolve_qos_hint(hint: str) -> tuple[str | None, str]:
    """Resolve a QoS hint to a profile id. Accepts exact ids, name
    fragments, and numeric rates ('19', '19 Mbps'). When a numeric rate
    has no exact profile, the nearest one is substituted and an explicit
    note is returned — silently rounding '19 Mbps' down to an 18 Mbps
    profile is the kind of dishonest UX that makes users think the
    system is broken. Returns (qos_id_or_passthrough, substitution_note)."""
    if not hint:
        return None, ""
    if qos_profiles.find_one({"_id": hint}):
        return hint, ""
    doc = qos_profiles.find_one(
        {"name": {"$regex": re.escape(hint), "$options": "i"}})
    if doc:
        return doc["_id"], ""
    m = re.search(r"(\d+(?:\.\d+)?)", str(hint))
    if m:
        rate = float(m.group(1))
        exact = qos_profiles.find_one({"max_downlink_mbps": rate})
        if exact:
            return exact["_id"], ""
        candidates = [p for p in qos_profiles.find(
            {"max_downlink_mbps": {"$exists": True, "$ne": None}})]
        if candidates:
            nearest = min(candidates,
                          key=lambda p: abs(p["max_downlink_mbps"] - rate))
            available = sorted(p["max_downlink_mbps"] for p in candidates)
            note = (
                f"⚠️ Requested **{rate:g} Mbps** has no exact QoS profile "
                f"— using nearest match `{nearest['_id']}` "
                f"(**{nearest['max_downlink_mbps']:g} Mbps**). Available "
                f"tiers: {', '.join(f'{r:g}' for r in available)} Mbps."
            )
            return nearest["_id"], note
    return hint, ""  # passthrough — keeps the hint visible downstream


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
def create_scenario(raw_text: str, scenario_type: str,
                    plan: str = None, old_qos: str = None,
                    new_qos: str = None,
                    apn_from: str = None, apn_to: str = None,
                    pcrf_from: str = None, pcrf_to: str = None,
                    roaming_enable: list[str] = None,
                    markets: list[str] = None,
                    time_windows: list[str] = None,
                    summary: str = None) -> str:
    """
    Submit a new what-if scenario with STRUCTURED fields. The calling
    agent extracts the fields from the user's natural-language request
    (Phase 4: parsing lives in the domain agent, not here); this service
    resolves plan/QoS/market hints against the twin's catalog (numeric
    rates like '19 Mbps' map to the nearest profile with an explicit
    substitution note) and stores the scenario with status='submitted'.

    Next step: call simulate_qos_change (or simulate_roaming_change) from
    the simulation service once the user confirms the parameters.

    Args:
        raw_text:       The user's what-if request VERBATIM (stored for audit).
        scenario_type:  'qos_change' | 'policy_change' | 'subscriber_shift'
                        | 'other'.
        plan:           Plan id or name hint, e.g. 'plan_ACME_M' or 'ACME M'.
        old_qos:        Current QoS profile — id, name, or rate ('7.2').
        new_qos:        Target QoS profile — id, name, or rate ('20 Mbps').
        apn_from:       Current APN (policy changes).
        apn_to:         Target APN (policy changes).
        pcrf_from:      Current PCRF template ref (policy changes).
        pcrf_to:        Target PCRF template ref (policy changes).
        roaming_enable: Country codes/names to enable roaming in.
        markets:        Market hints, e.g. ['NYC', 'LA'] — resolved to
                        canonical ids. Empty = all markets.
        time_windows:   Window ids, e.g. ['Saturday_20_23'] for Saturday
                        evening. Empty = all known windows.
        summary:        One short sentence describing the what-if.
                        Omit anything the user did not mention; never
                        invent values.
    """
    old_qos_id, note_old = _resolve_qos_hint(old_qos or "")
    new_qos_id, note_new = _resolve_qos_hint(new_qos or "")
    cs = {
        "plan_id":            _resolve_plan_id(plan or "") or plan,
        "old_qos_profile_id": old_qos_id,
        "new_qos_profile_id": new_qos_id,
        "apn_change": ({"from": apn_from, "to": apn_to}
                       if (apn_from or apn_to) else None),
        "pcrf_template_change": ({"from": pcrf_from, "to": pcrf_to}
                                 if (pcrf_from or pcrf_to) else None),
        "roaming_enable": roaming_enable or None,
    }
    sc = {
        "markets": ([_resolve_market_id(m) or m for m in (markets or [])]),
        "time_windows": time_windows or [],
    }
    substitution_note = " ".join(n for n in (note_old, note_new) if n)
    parsed = {"scenario_type": scenario_type, "summary": summary}

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
        "description":    parsed.get("summary") or raw_text[:120],
        "scenario_type":  parsed.get("scenario_type") or "other",
        "raw_text":       raw_text,
        "change_set":     cs,
        "scope":          sc,
        "status":         "submitted",
        "submitted_at":   datetime.datetime.now(),
        "history":        [{"ts": datetime.datetime.now(), "event": "submitted",
                            "note": "structured submit by domain agent"}],
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
    if substitution_note:
        lines += ["", substitution_note]
    lines += [
        f"",
        f"If the parameters look correct, say **'run the simulation'**.",
        f"To adjust, say e.g. **'change {sid} to 50 Mbps'** or "
        f"**'add LA to the scope'** — then simulate.",
    ]
    return "\n".join(lines)


@mcp.tool()
def update_scenario(modification: str, scenario_id: str = None,
                    plan: str = None, old_qos: str = None,
                    new_qos: str = None,
                    apn_from: str = None, apn_to: str = None,
                    pcrf_from: str = None, pcrf_to: str = None,
                    roaming_enable: list[str] = None,
                    markets: list[str] = None,
                    time_windows: list[str] = None,
                    summary: str = None) -> str:
    """
    Apply a modification to an existing submitted scenario before
    running the simulation. The calling agent computes the UPDATED field
    values itself (Phase 4: amendment reasoning lives in the domain
    agent, which also has the conversation context) and passes ONLY the
    fields that change — each provided field REPLACES the stored value
    wholesale (e.g. to add LA when markets is ['NYC_Metro'], pass
    markets=['NYC_Metro', 'LA_Metro']). Unspecified fields stay
    untouched.

    If scenario_id is omitted, targets the most-recently submitted
    scenario (i.e. 'the last one', 'the current scenario').

    Args:
        modification:   The user's amendment in plain language — recorded
                        in the scenario history, e.g. "raise downlink to
                        50 Mbps", "NYC only".
        scenario_id:    Scenario to update. Defaults to the most recent
                        submitted (not yet simulated) scenario.
        plan:           New plan id/name hint, only if it changes.
        old_qos:        New value for the current-QoS field — id, name,
                        or rate ('7.2').
        new_qos:        New value for the target-QoS field — id, name, or
                        rate ('15 Mbps'). Rates without an exact profile
                        map to the nearest one with an explicit
                        substitution note.
        apn_from:       New source APN, only if it changes.
        apn_to:         New target APN, only if it changes.
        pcrf_from:      New source PCRF template ref, only if it changes.
        pcrf_to:        New target PCRF template ref, only if it changes.
        roaming_enable: COMPLETE new country list, if it changes.
        markets:        COMPLETE new market list, if it changes.
        time_windows:   COMPLETE new window list, if it changes.
        summary:        Updated one-sentence description, if it changes.
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

    cs = dict(s.get("change_set") or {})
    sc = dict(s.get("scope") or {})
    substitution_note = ""

    if plan is not None:
        cs["plan_id"] = _resolve_plan_id(plan) or plan
    if old_qos is not None:
        qid, note = _resolve_qos_hint(old_qos)
        cs["old_qos_profile_id"] = qid
        substitution_note = note or substitution_note
    if new_qos is not None:
        qid, note = _resolve_qos_hint(new_qos)
        cs["new_qos_profile_id"] = qid
        substitution_note = note or substitution_note
    if apn_from is not None or apn_to is not None:
        prev = cs.get("apn_change") or {}
        cs["apn_change"] = {"from": apn_from or prev.get("from"),
                            "to":   apn_to or prev.get("to")}
    if pcrf_from is not None or pcrf_to is not None:
        prev = cs.get("pcrf_template_change") or {}
        cs["pcrf_template_change"] = {"from": pcrf_from or prev.get("from"),
                                      "to":   pcrf_to or prev.get("to")}
    if roaming_enable is not None:
        cs["roaming_enable"] = roaming_enable or None
    if markets is not None:
        sc["markets"] = [_resolve_market_id(m) or m for m in markets]
    if time_windows is not None:
        sc["time_windows"] = time_windows

    scenarios.update_one(
        {"_id": s["_id"]},
        {
            "$set": {
                "change_set":  cs,
                "scope":       sc,
                "description": summary or s["description"],
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
    if cs.get("pcrf_template_change"):
        lines.append(f"- PCRF template: `{cs['pcrf_template_change'].get('from')}` → `{cs['pcrf_template_change'].get('to')}`")
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
