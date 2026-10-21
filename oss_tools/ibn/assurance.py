#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Assurance Service — Live Compliance and Hybrid Vector Diagnosis

Tool implementations behind mcp_servers/ibn_assurance_service.py (the FastMCP shim
whose docstring drives semantic routing) and the Atlas Agent Engine
agents. Collection handles resolve the per-session prefix on every
call, see oss_tools/_ctx.py.
"""

import datetime
import json
import logging
import math
import re
import time
from pymongo import DESCENDING
from oss_tools._ctx import ToolSet, collection, demo_prefix
from oss_tools.ibn import autonomy, learning, rush, slo


tools  = ToolSet()
logger = logging.getLogger("ibn_assurance_service")

# Per-session isolation (Phase B): mutable collections carry the session
# prefix (oss_tools/_ctx.py; empty → bare names); reference collections
# stay shared.
# ibn_knowledge_chunks stays SHARED even though update_template_version
# inserts a policy doc into it — keeping it shared avoids a per-session
# vector index; that write is rare and additive (see MULTI_SESSION_PLAN).
# Learned incidents (S3) are lane-tagged instead (oss_tools/ibn/learning.py).
intents           = collection("ibn_intents", mutable=True)            # mutable
sites             = collection("ibn_sites")                            # reference
telemetry         = collection("ibn_telemetry", mutable=True)          # mutable
compliance_events = collection("ibn_compliance_events", mutable=True)  # mutable
knowledge_chunks  = collection("ibn_knowledge_chunks")                 # shared (see above)
policy_snapshots  = collection("ibn_policy_snapshots", mutable=True)   # mutable
incidents         = collection("ibn_incidents", mutable=True)          # mutable (self-healing)


def _km_to_degrees(km: float, lat: float) -> tuple[float, float]:
    """Approximate degree offsets for a given km radius at a given latitude."""
    dlat = km / 111.0
    dlng = km / (111.0 * max(math.cos(math.radians(lat)), 0.001))
    return dlat, dlng


def _resolve_site(site_hint: str) -> dict | None:
    """Resolve a site by name using Atlas Search fuzzy text, with regex fallback."""
    if not site_hint:
        return None
    try:
        results = list(sites.aggregate([
            {"$search": {
                "index": "ibn_sites_search",
                "text": {
                    "query": site_hint,
                    "path":  "name",
                    "fuzzy": {"maxEdits": 1, "prefixLength": 2},
                },
            }},
            {"$limit": 1},
        ]))
        if results:
            return results[0]
    except Exception:
        pass
    # Fallback: regex token matching when the Search index is not yet available
    direct = sites.find_one({"name": {"$regex": site_hint, "$options": "i"}})
    if direct:
        return direct
    tokens = [t for t in site_hint.split() if len(t) >= 3]
    if not tokens:
        return None
    result = sites.find_one(
        {"$and": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )
    return result or sites.find_one(
        {"$or": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )


def _resolve_intent(intent_id: str = None, site: str = None) -> dict | None:
    """
    Find the intent to operate on. Accepts either:
      - an exact intent_id like 'IBN-005'
      - a site-name fragment like 'Marienplatz' (picks the active/violated intent there)
      - a value passed in intent_id that doesn't look like an IBN-id (falls
        back to site-name resolution — covers the LLM passing the wrong arg)
    """
    if intent_id:
        doc = intents.find_one({"_id": intent_id})
        if doc:
            return doc
        # Maybe the LLM passed a site name as intent_id — fall back to site lookup
        site_doc = _resolve_site(intent_id)
        if site_doc:
            return intents.find_one({
                "site_id": site_doc["_id"],
                "status":  {"$in": ["active", "violated"]},
            })
        return None
    if site:
        site_doc = _resolve_site(site)
        if not site_doc:
            return None
        return intents.find_one({
            "site_id": site_doc["_id"],
            "status":  {"$in": ["active", "violated"]},
        })
    return None


def _latest_metric(intent_id: str, metric: str, window_seconds: int = 300) -> float | None:
    """Return the latest value of a metric in the recent window, or None."""
    cutoff = datetime.datetime.now() - datetime.timedelta(seconds=window_seconds)
    doc = telemetry.find_one(
        {"meta.intent_id": intent_id, "meta.metric": metric, "ts": {"$gte": cutoff}},
        sort=[("ts", DESCENDING)],
    )
    return doc["value"] if doc else None


def _last_known_metric(intent_id: str, metric: str) -> tuple[float, datetime.datetime] | tuple[None, None]:
    """Return the most recent (value, ts) ever recorded, ignoring age."""
    doc = telemetry.find_one(
        {"meta.intent_id": intent_id, "meta.metric": metric},
        sort=[("ts", DESCENDING)],
    )
    return (doc["value"], doc["ts"]) if doc else (None, None)


def _measured(intent_id: str) -> dict | None:
    """What the SLO monitor measured: the open incident's window, else the
    last slo.WINDOW_S of telemetry. None without uplink data (inject_event
    only writes POS latency), so that path keeps the template fingerprint."""
    incident = incidents.find_one({"intent_id": intent_id, "open": True})
    if incident and (incident.get("measured") or {}).get("uplink_util_avg_pct") is not None:
        return incident["measured"]
    since = datetime.datetime.now() - datetime.timedelta(seconds=slo.WINDOW_S)
    metrics = slo.measurements(list(telemetry.aggregate(
        slo.window_pipeline(since, intent_id)))).get(intent_id) or {}
    if "uplink_util_pct" not in metrics or slo.LATENCY not in metrics:
        return None
    return slo.measured_summary(metrics)


def _build_fingerprint(intent: dict, observed_pos_latency: float | None) -> str:
    """Render a natural-language symptom fingerprint for vector search
    (the template used when there are no measurements, see _measured)."""
    site_name = "site"
    site = sites.find_one({"_id": intent.get("site_id")}) if intent.get("site_id") else None
    if site:
        site_name = site["name"]
    targets = intent.get("parsed", {}).get("targets", {})
    threshold = targets.get("pos_latency_ms", 40)
    obs = observed_pos_latency or threshold + 5
    seg = targets.get("segmentation", "strict")
    return (
        f"{site_name} retail branch reports POS payment terminal latency reaching "
        f"{obs:.0f} milliseconds during the morning customer rush, exceeding the "
        f"{threshold} millisecond intent SLA. {seg.capitalize()} guest segmentation "
        f"policy is active with isolated VLANs for guest WiFi, camera uplink and POS. "
        f"Uplink utilization remains low — bandwidth is not saturated. CPE diagnostics "
        f"show no faults. Symptom signature suggests queue scheduling collision rather "
        f"than physical-layer or capacity issue."
    )


# ─── Tools ─────────────────────────────────────────────────────────────────

@tools.tool
def get_compliance(intent_id: str = None, site: str = None) -> str:
    """
    Report compliance status and current metrics. With an intent_id or site
    name, returns the live SLO state for that intent (latest metric vs target,
    current status). Without arguments, returns a fleet summary across all
    active intents.

    Args:
        intent_id: Optional intent ID. Takes precedence over site.
        site:      Site name fragment (e.g. "Stuttgart", "Alpenmarkt Stuttgart").
                   Used to look up the active intent at that site.
    """
    if not intent_id and site:
        resolved = _resolve_intent(site=site)
        if resolved:
            intent_id = resolved["_id"]
        else:
            return f"❌ No active intent found for site '{site}'."

    if intent_id:
        intent = intents.find_one({"_id": intent_id})
        if not intent:
            return f"❌ Intent {intent_id} not found."

        targets = intent.get("parsed", {}).get("targets", {})
        latest_pos = _latest_metric(intent_id, "pos_latency_ms")
        threshold  = targets.get("pos_latency_ms")
        site = sites.find_one({"_id": intent.get("site_id")})
        status = intent.get("status", "—")

        if latest_pos is None:
            return (
                f"**{intent_id}** · {site['name'] if site else '—'} · status {status}\n"
                f"  No telemetry samples yet — intent may have been activated very "
                f"recently."
            )

        within = (threshold is None) or (latest_pos <= threshold)
        emoji  = "🟢" if within else "🔴"
        lines = [
            f"**{intent_id}** · {site['name'] if site else '—'} · {emoji} "
            f"{'compliant' if within else 'VIOLATED'}",
            f"  POS latency: {latest_pos:.1f}ms (target ≤ {threshold}ms)",
        ]
        if not within:
            recent = compliance_events.find_one(
                {"intent_id": intent_id, "kind": "violation"},
                sort=[("ts", DESCENDING)],
            )
            if recent:
                lines.append(f"  Violation since: {recent['ts'].strftime('%H:%M:%S')}")
                lines.append(f"  Run diagnose_violation('{intent_id}') for root cause.")
        return "\n".join(lines)

    # ── Fleet summary ──
    active = list(intents.find({"status": {"$in": ["active", "violated"]}}))
    if not active:
        return "No active intents."

    now = datetime.datetime.now()
    rows = []
    for it in active:
        t         = it.get("parsed", {}).get("targets", {})
        threshold = t.get("pos_latency_ms")
        # Compliance uses a 5-min live window; display uses last-known ever.
        live    = _latest_metric(it["_id"], "pos_latency_ms", window_seconds=300)
        val, ts = _last_known_metric(it["_id"], "pos_latency_ms")
        site_doc  = sites.find_one({"_id": it.get("site_id")})
        site_name = site_doc["name"] if site_doc else "—"
        if live is None:
            violated = False          # no live signal → not actively violated
        elif threshold and live > threshold:
            violated = True
        else:
            violated = False
        age_sec = int((now - ts).total_seconds()) if ts else None
        rows.append({
            "it": it, "site_name": site_name,
            "live": live, "val": val, "ts": ts, "age_sec": age_sec,
            "threshold": threshold, "violated": violated,
        })

    green_rows = [r for r in rows if not r["violated"]]
    red_rows   = [r for r in rows if r["violated"]]

    lines = [f"**Fleet compliance:** {len(green_rows)}/{len(rows)} green", ""]

    def _age_str(age_sec: int | None) -> str:
        if age_sec is None:
            return ""
        if age_sec < 60:
            return f"{age_sec}s ago"
        if age_sec < 3600:
            return f"{age_sec // 60}m ago"
        return f"{age_sec // 3600}h ago"

    def _row_line(r):
        it, name = r["it"], r["site_name"]
        val, thr, age_sec = r["val"], r["threshold"], r["age_sec"]
        live = r["live"]
        emoji = "🔴" if r["violated"] else "🟢"
        if val is None:
            metric_str = "no telemetry yet"
        else:
            utilisation = f"{int(100 * val / thr)}% of SLA" if thr else ""
            freshness   = "" if (live is not None) else f" · last seen {_age_str(age_sec)}"
            metric_str  = (
                f"POS {val:.1f}ms / {thr}ms SLA  ({utilisation}){freshness}"
                if thr else f"POS {val:.1f}ms{freshness}"
            )
        return f"- {emoji} **{it['_id']}** · {name} · {metric_str}"

    if red_rows:
        lines.append("**🔴 Violations:**")
        for r in red_rows:
            lines.append(_row_line(r))
            ev = compliance_events.find_one(
                {"intent_id": r["it"]["_id"], "kind": "violation"},
                sort=[("ts", DESCENDING)],
            )
            if ev:
                lines.append(f"  ↳ violation since {ev['ts'].strftime('%H:%M:%S')} "
                             f"— run diagnose_violation('{r['it']['_id']}')")
        lines.append("")

    if green_rows:
        lines.append("**🟢 Compliant:**")
        for r in green_rows:
            lines.append(_row_line(r))

    return "\n".join(lines)


@tools.tool
def diagnose_violation(intent_id: str = None, site: str = None,
                       radius_km: float = 10, lookback_days: int = 180) -> str:
    """
    Diagnose the current violation by running the WOW query — a single
    aggregation pipeline against ibn_knowledge_chunks that combines:
      • semantic similarity ($vectorSearch on text)
      • structured filter (kind = 'incident')
      • time window (last N days)
      • geospatial bounding box (within radius_km of the affected site)

    Returns the closest matching past incident (if any) with its proven
    runbook, similarity score, and side-by-side fingerprint comparison.
    Records the diagnose event in ibn_compliance_events so the dashboard
    can render the pipeline and the AHA card.

    Args:
        intent_id:     The intent that is currently violating, e.g. 'IBN-005'.
        site:          Alternative: site-name fragment like 'Marienplatz';
                       the tool will pick the active/violated intent there.
        radius_km:     Geo radius for filtering past incidents (default 10).
        lookback_days: How far back to search in days (default 180).

    Pass either intent_id or site. If both are absent, the tool returns an
    error.
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return (f"❌ No matching intent found for "
                f"intent_id={intent_id!r}, site={site!r}.")

    intent_id = intent["_id"]  # canonicalize after resolution
    site_doc  = sites.find_one({"_id": intent.get("site_id")})
    if not site_doc or not site_doc.get("location"):
        return f"❌ Site for {intent_id} has no coordinates."

    lng, lat = site_doc["location"]["coordinates"]
    dlat, dlng = _km_to_degrees(radius_km, lat)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=lookback_days)

    measured = _measured(intent_id)
    if measured:
        targets = intent.get("parsed", {}).get("targets", {})
        fingerprint = slo.fingerprint_text(
            site_doc["name"], targets.get("pos_latency_ms", 40),
            targets.get("segmentation", "strict"), measured)
    else:
        fingerprint = _build_fingerprint(intent, _latest_metric(intent_id, "pos_latency_ms"))

    # Try the full filter first (the WOW: vector + kind + time + geo box).
    # If empty (likely an index-config issue or geo box too tight), gracefully
    # relax: drop geo, then drop time. Report which level produced results so
    # the demo never dead-ends and we still show what filtering happened.
    filter_full = {
        "kind": {"$eq": "incident"},
        "ts":   {"$gte": cutoff},
        "lng":  {"$gte": lng - dlng, "$lte": lng + dlng},
        "lat":  {"$gte": lat - dlat, "$lte": lat + dlat},
    }
    filter_no_geo = {"kind": {"$eq": "incident"}, "ts": {"$gte": cutoff}}
    filter_kind   = {"kind": {"$eq": "incident"}}

    # Learned incidents (S3) carry their lane; the index has no lane
    # field, so other lanes' are dropped after the search.
    lane = learning.lane_filter(demo_prefix())

    def _build_pipeline(flt):
        return [
            {
                "$vectorSearch": {
                    "index":   "ibn_knowledge_index",
                    "path":    "text",
                    "query":   fingerprint,
                    "filter":  flt,
                    "numCandidates": 200,
                    "limit": 10,
                }
            },
            {"$match": lane},
            {"$limit": 3},
            {
                "$project": {
                    "_id":         0, "title": 1, "site_name": 1, "ts": 1,
                    "fingerprint": 1, "runbook_id": 1, "text": 1,
                    "lng": 1, "lat": 1, "learned": 1,
                    "score":       {"$meta": "vectorSearchScore"},
                }
            },
        ]

    matches = []
    pipeline = _build_pipeline(filter_full)
    filter_level = "full (vector + kind + time + geo)"
    try:
        matches = list(knowledge_chunks.aggregate(pipeline))
        if not matches:
            pipeline = _build_pipeline(filter_no_geo)
            filter_level = "relaxed: dropped geo box (kind + time only)"
            matches = list(knowledge_chunks.aggregate(pipeline))
        if not matches:
            pipeline = _build_pipeline(filter_kind)
            filter_level = "broad: dropped time + geo (kind only)"
            matches = list(knowledge_chunks.aggregate(pipeline))
    except Exception as e:
        return (
            f"❌ Vector search failed: {e}\n"
            f"  Make sure the Atlas Vector Search index 'ibn_knowledge_index' is "
            f"built on ibn_knowledge_chunks. See seed/ibn_seed.py output for the "
            f"index config."
        )

    # Persist a diagnose event so the dashboard can render the pipeline + result
    event_doc = {
        "intent_id":   intent_id,
        "kind":        "diagnose",
        "ts":          datetime.datetime.now(),
        "fingerprint": fingerprint,
        "fingerprint_source": "measured" if measured else "template",
        "measured":    measured,
        "pipeline":    json.loads(json.dumps(pipeline, default=str)),
        "filter_level": filter_level,
        "matches":     [
            {**m, "ts": m["ts"].isoformat() if isinstance(m.get("ts"), datetime.datetime) else m.get("ts")}
            for m in matches
        ],
        "site_name":   site_doc["name"],
        "radius_km":   radius_km,
    }
    compliance_events.insert_one(event_doc)
    if matches:
        # The self-healing incident's timeline (web/ibn_assurance.py).
        top = matches[0]
        incidents.update_one({"intent_id": intent_id, "open": True}, {
            "$set": {"alarm.probable_cause": "congestion" if measured else "unknown",
                     "alarm.root_cause": slo.diagnosis(measured) if measured else None,
                     "diagnosis": {"top_match": top.get("title"), "runbook_id": top.get("runbook_id"),
                                   "similarity": round(top.get("score", 0), 3),
                                   "filter_level": filter_level}},
            "$push": {"timeline": {"ts": datetime.datetime.now(), "stage": "diagnose",
                                   "by": "diagnose_violation",
                                   "note": f"{top.get('site_name')}: {top.get('title')} "
                                           f"(similarity {top.get('score', 0):.2f}, "
                                           f"runbook {top.get('runbook_id') or '—'})"}},
        })

    if not matches:
        return (
            f"⚠️  Diagnose query returned no matches at any filter level for "
            f"{site_doc['name']}. Even the broadest fallback (kind=incident only) "
            f"returned zero — almost certainly an Atlas Vector Search index "
            f"issue. Verify the index 'ibn_knowledge_index' exists, is Active, "
            f"and was created with `kind` declared as a filter field."
        )

    top = matches[0]
    lines = [
        f"## 🔍 Diagnosis for {intent_id} at {site_doc['name']}",
        f"_Filter level used: {filter_level}_",
        "",
        f"**Top match — {top['site_name']}, "
        f"{top['ts'].strftime('%Y-%m-%d') if isinstance(top['ts'], datetime.datetime) else top['ts']}** "
        f"· similarity {top['score']:.2f}"
        + (" · learned by the self-healing loop" if top.get("learned") else ""),
        "",
        f"> {top['title']}",
        "",
    ]

    fp = top.get("fingerprint") or {}
    if fp:
        lines.append("**Fingerprint comparison:**")
        lines.append("```")
        lines.append(f"  trigger        : {fp.get('trigger', '—')}")
        lines.append(f"  segmentation   : {fp.get('segmentation', '—')}")
        lines.append(f"  link util      : {fp.get('link_util_pct', '—')}%")
        lines.append(f"  latency observed: {fp.get('latency_ms_observed', '—')}ms")
        lines.append(f"  threshold      : {fp.get('latency_ms_threshold', '—')}ms")
        lines.append("```")
        lines.append("")

    if top.get("runbook_id"):
        lines.append(f"**Recommended runbook:** `{top['runbook_id']}`")
        rb = knowledge_chunks.find_one({"_id": top["runbook_id"]})
        if rb:
            lines.append(f"  *{rb.get('title')}*")
            actions = rb.get("actions") or []
            if actions:
                lines.append("")
                for a in actions:
                    lines.append(f"  {a['step']}. {a['command']}")
            lines.append("")
            lines.append(f"  Run apply_runbook('{intent_id}', '{top['runbook_id']}') "
                         f"to remediate.")

    if len(matches) > 1:
        lines.append("")
        lines.append("**Other candidates:**")
        for m in matches[1:]:
            lines.append(
                f"  - {m['site_name']} "
                f"({m['ts'].strftime('%Y-%m-%d') if isinstance(m['ts'], datetime.datetime) else m['ts']}) "
                f"· similarity {m['score']:.2f}"
            )

    return "\n".join(lines)


@tools.tool
def apply_runbook(runbook_id: str, intent_id: str = None, site: str = None,
                  autonomous: bool = False) -> str:
    """
    Apply a runbook to a violating intent. Records the application in the
    intent's history, writes a recovery event, and writes one fresh
    in-spec telemetry sample to clear the gauge in the dashboard.

    Args:
        runbook_id: The runbook to apply, e.g. 'RB-007'.
        intent_id:  The intent currently violating, e.g. 'IBN-005'.
        site:       Alternative: site name fragment like 'Marienplatz'.
        autonomous: Only in autonomous mode (no human in the loop). The
                    autonomy envelope is checked again here: outside it,
                    nothing is applied and the incident goes to the NOC.
                    Inside it, the runbook is applied and recovery is left
                    to the SLO monitor to verify (call await_recovery).

    Pass either intent_id or site.
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return (f"❌ No matching intent found for "
                f"intent_id={intent_id!r}, site={site!r}.")
    intent_id = intent["_id"]

    runbook = knowledge_chunks.find_one({"_id": runbook_id, "kind": "runbook"})
    if not runbook:
        return f"❌ Runbook {runbook_id} not found."

    if autonomous:
        return _apply_autonomously(intent, runbook)

    now = datetime.datetime.now()
    targets = intent.get("parsed", {}).get("targets", {})
    threshold = targets.get("pos_latency_ms", 40)
    # Restore well within target. (The old 22 ms floor left targets under
    # ~31 ms, e.g. the 20 ms Marienplatz onboarding, still violated.)
    healthy_value = round(threshold * 0.7, 1)

    # Restore: a fresh good telemetry sample
    telemetry.insert_one({
        "ts":   now,
        "meta": {"intent_id": intent_id, "site_id": intent.get("site_id"),
                 "metric": "pos_latency_ms"},
        "value": healthy_value,
    })

    # Recovery compliance event
    compliance_events.insert_one({
        "intent_id":   intent_id,
        "kind":        "recovery",
        "ts":          now,
        "runbook_id":  runbook_id,
        "metric":      "pos_latency_ms",
        "observed":    healthy_value,
        "threshold":   threshold,
        "justification": f"Runbook {runbook_id} applied; metric returned within SLO.",
    })

    # Update intent: history + status (back to active if was violated)
    new_status = "active" if intent.get("status") == "violated" else intent.get("status")
    intents.update_one(
        {"_id": intent_id},
        {
            "$set":  {"status": new_status},
            "$push": {"history": {
                "ts": now, "event": "runbook_applied",
                "runbook_id": runbook_id,
                "note": runbook.get("title", ""),
            }},
            "$inc":  {"version": 1},
        },
    )

    return (
        f"✓ Runbook **{runbook_id}** applied to {intent_id}.\n"
        f"  *{runbook.get('title')}*\n"
        f"  POS latency now {healthy_value:.1f}ms (target ≤ {threshold}ms). "
        f"Compliance restored."
    )


def _apply_autonomously(intent: dict, runbook: dict) -> str:
    """apply_runbook(autonomous=True): re-check the envelope here (the
    model's choice never widens it), then apply without asserting recovery:
    the intent's history is what the rush model reads (rush.mitigated), so
    latency falls in the data and the SLO monitor closes the incident."""
    incident = incidents.find_one({"intent_id": intent["_id"], "open": True})
    verdict = autonomy.evaluate(intent, runbook["_id"], incident)
    autonomy.record(verdict)
    if verdict["decision"] != "allow":
        return (f"⛔ {runbook['_id']} NOT applied: outside the autonomy envelope "
                f"({verdict['policy']}, level {verdict['level']}). Escalated to the NOC:\n"
                + "\n".join(f"  - {r}" for r in verdict["reasons"]))
    now = datetime.datetime.now()
    intents.update_one({"_id": intent["_id"]}, {
        "$push": {"history": {"ts": now, "event": "runbook_applied", "runbook_id": runbook["_id"],
                              "by": autonomy.AUTONOMOUS_BY, "incident_id": incident["_id"],
                              "note": runbook.get("title", "")}},
        "$inc": {"version": 1},
    })
    incidents.update_one({"_id": incident["_id"]}, {
        "$set": {"status": "mitigating", "autonomous_act_at": now,
                 "action": {"runbook_id": runbook["_id"], "at": now, "by": autonomy.AUTONOMOUS_BY,
                            "policy": verdict["policy"]}},
        "$push": {"timeline": {"ts": now, "stage": "act", "by": autonomy.AUTONOMOUS_BY,
                               "note": f"applied {runbook['_id']}: {runbook.get('title', '')} "
                                       f"({verdict['policy']})"}},
    })
    return (f"✓ Runbook **{runbook['_id']}** applied autonomously to {intent['_id']} "
            f"({verdict['policy']}).\n  *{runbook.get('title')}*\n"
            f"  Recovery is not assumed: call await_recovery('{intent['_id']}') and let the "
            f"SLO monitor verify it.")


@tools.tool
def check_autonomy(runbook_id: str, intent_id: str = None, site: str = None) -> str:
    """
    Autonomous mode only: may the system apply this runbook on its own, or
    must a NOC engineer decide? Checks the lane's autonomy policy (level
    L3/L4, change freeze, deny list, rate limit) and the open incident's
    recorded diagnosis (the top match must name this runbook with
    similarity at or above the policy minimum, and the runbook must have
    fixed a past incident). Run diagnose_violation first.

    Returns JSON: decision ("allow" or "escalate"), the policy, every
    check with its result, and the reasons for an escalation. On
    "escalate", apply nothing: report the diagnosis and the reasons.

    Args:
        runbook_id: The runbook the diagnosis recommends, e.g. 'RB-007'.
        intent_id:  The violating intent, e.g. 'IBN-005'.
        site:       Alternative: site name fragment like 'Marienplatz'.
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return json.dumps({"error": f"No matching intent found for intent_id={intent_id!r}, "
                                    f"site={site!r}."})
    incident = incidents.find_one({"intent_id": intent["_id"], "open": True})
    verdict = autonomy.evaluate(intent, runbook_id, incident)
    autonomy.record(verdict)
    return json.dumps(verdict, default=str)


AWAIT_MAX_S = 75


@tools.tool(timeout_s=AWAIT_MAX_S + 15)
def await_recovery(intent_id: str = None, site: str = None, timeout_s: int = 60) -> str:
    """
    Autonomous mode only: wait until the SLO monitor (not the agent)
    declares the intent's incident recovered, after a runbook was applied.
    On timeout the incident is escalated to the NOC.

    Args:
        intent_id: The intent, e.g. 'IBN-005'.
        site:      Alternative: site name fragment like 'Marienplatz'.
        timeout_s: How long to wait (default 60, at most 75).
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return (f"❌ No matching intent found for "
                f"intent_id={intent_id!r}, site={site!r}.")
    incident = incidents.find_one({"intent_id": intent["_id"]}, sort=[("opened_at", DESCENDING)])
    if not incident:
        return f"❌ No incident recorded for {intent['_id']}."
    wait_s = min(max(int(timeout_s or 60), 5), AWAIT_MAX_S)
    deadline = time.monotonic() + wait_s
    while incident.get("open") and time.monotonic() < deadline:
        time.sleep(2)
        incident = incidents.find_one({"_id": incident["_id"]}) or {}
    if not incident.get("open"):
        verify = next((s for s in reversed(incident.get("timeline") or [])
                       if s.get("stage") == "verify"), {})
        return (f"✓ {intent['_id']} recovered, verified by the SLO monitor: "
                f"{verify.get('note', 'incident closed')}. "
                f"Time to recover {incident.get('mttr_s', '—')} s.")
    now = datetime.datetime.now()
    note = f"not recovered {wait_s} s after the action: escalated to the NOC"
    incidents.update_one({"_id": incident["_id"], "open": True}, {
        "$set": {"status": "escalated", "escalated_at": now},
        "$push": {"timeline": {"ts": now, "stage": "verify", "by": "await_recovery", "note": note}},
    })
    return f"⏱ {intent['_id']} {note}. The incident stays open."


def _next_version(template_id: str) -> str:
    """strict-retail-v3 → strict-retail-v4 (the update_template_version default)."""
    m = re.search(r"v(\d+)$", template_id)
    return f"{template_id[:m.start()]}v{int(m.group(1)) + 1}" if m else f"{template_id}-next"


@tools.tool
def propose_prevention(intent_id: str = None, site: str = None) -> str:
    """
    Autonomous mode only, after await_recovery reported a recovery: propose
    protecting the sister sites before their next rush. Finds the other
    live intents on the same segmentation template that don't have the fix
    yet, and the knowledge base's evidence for the runbook, then records a
    canary proposal (update_template_version for one sister site) on the
    incident and marks the sister intents. It changes nothing on the
    network: the proposal changes the fleet, so a NOC engineer decides.

    Args:
        intent_id: The intent that just recovered, e.g. 'IBN-001'.
        site:      Alternative: site name fragment like 'Hamburg'.
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return (f"❌ No matching intent found for "
                f"intent_id={intent_id!r}, site={site!r}.")
    incident = incidents.find_one({"intent_id": intent["_id"]}, sort=[("opened_at", DESCENDING)])
    if not incident or incident.get("open") or not incident.get("action"):
        return (f"❌ Nothing to propose for {intent['_id']}: it needs an incident the system "
                f"fixed on its own and the SLO monitor closed.")
    if incident.get("prevention"):
        return _prevention_text(intent["_id"], incident["prevention"])

    runbook_id = incident["action"]["runbook_id"]
    template = intent.get("template")
    now = datetime.datetime.now()
    if runbook_id not in rush.MITIGATING_RUNBOOKS or not template:
        prevention = {"status": "none", "runbook_id": runbook_id, "proposed_at": now,
                      "reason": f"{runbook_id} is a fix for this site, not for its template"}
    else:
        sisters = [it for it in intents.find(
            {"template": template, "status": {"$in": ["active", "violated"]},
             "_id": {"$ne": intent["_id"]}}).sort("_id", 1) if not rush.mitigated(it)]
        names = {s["_id"]: s["name"] for s in sites.find(
            {"_id": {"$in": [it.get("site_id") for it in sisters]}}, {"name": 1})}
        evidence = list(knowledge_chunks.find(
            {"kind": "incident", "runbook_id": runbook_id, "outcome": {"$ne": "failed"},
             **learning.lane_filter(demo_prefix())},
            {"site_name": 1, "learned": 1}).sort("ts", 1))
        new_version = _next_version(template)
        sister_rows = [{"intent_id": it["_id"], "site": names.get(it.get("site_id"), it.get("site_id"))}
                       for it in sisters]
        prevention = {
            "status": "proposed" if sisters else "none", "runbook_id": runbook_id,
            "template": template, "new_version": new_version, "proposed_at": now,
            "proposed_by": autonomy.AUTONOMOUS_BY, "sisters": sister_rows,
            "evidence": {"incidents": len(evidence),
                         "sites": sorted({e["site_name"] for e in evidence}),
                         "learned": sum(1 for e in evidence if e.get("learned"))},
        }
        if sisters:
            prevention["canary"] = sister_rows[0]
            prevention["action"] = {"tool": "update_template_version",
                                    "arguments": {"template_id": template, "new_version": new_version,
                                                  "intent_ids": [sisters[0]["_id"]]}}
        else:
            prevention["reason"] = f"every live intent on {template} already has {runbook_id}"

    res = incidents.update_one({"_id": incident["_id"], "prevention": {"$exists": False}}, {
        "$set": {"prevention": prevention},
        "$push": {"timeline": {"ts": now, "stage": "prevent", "by": autonomy.AUTONOMOUS_BY,
                               "note": _prevention_note(prevention)}},
    })
    if res.modified_count and prevention["status"] == "proposed":
        canary = prevention["canary"]["intent_id"]
        for row in prevention["sisters"]:
            intents.update_one({"_id": row["intent_id"]}, {"$set": {"proposed_change": {
                "incident_id": incident["_id"], "action": "update_template_version",
                "title": f"{template} → {prevention['new_version']}"
                         + (" (canary)" if row["intent_id"] == canary else " (after the canary)"),
                "proposed_at": now, "awaiting": "NOC decision"}}})
    return _prevention_text(intent["_id"], prevention)


def _prevention_note(p: dict) -> str:
    if p["status"] != "proposed":
        return f"no fleet change proposed: {p['reason']}"
    return (f"proposed {p['template']} → {p['new_version']} as a canary at {p['canary']['site']}, "
            f"then {len(p['sisters']) - 1} more site(s); awaiting the NOC")


def _prevention_text(intent_id: str, p: dict) -> str:
    if p["status"] != "proposed":
        return f"No sister-site change proposed for {intent_id}: {p['reason']}."
    ev = p["evidence"]
    args = p["action"]["arguments"]
    return (
        f"📋 Proposed, NOT applied (a fleet change: the NOC decides).\n"
        f"  Evidence: {p['runbook_id']} resolved {ev['incidents']} incident(s) at "
        f"{', '.join(ev['sites'])} ({ev['learned']} learned by this loop): a pattern of template "
        f"{p['template']}, not of one site.\n"
        f"  At risk before the next rush: {', '.join(s['site'] for s in p['sisters'])}.\n"
        f"  Proposal: {p['template']} → {p['new_version']} as a canary at {p['canary']['site']} "
        f"({p['canary']['intent_id']}), the rest after it holds through a rush.\n"
        f"  To approve, a NOC engineer asks for update_template_version("
        f"template_id='{args['template_id']}', new_version='{args['new_version']}', "
        f"intent_ids=['{args['intent_ids'][0]}'])."
    )


@tools.tool
def update_template_version(template_id: str = "strict-retail-v3",
                            new_version: str = "strict-retail-v4",
                            intent_ids: list[str] = None) -> str:
    """
    Bump a segmentation template version with the runbook fix folded in.
    For the demo, this updates the policy doc in ibn_knowledge_chunks and
    flags existing intents using the old template for next-change-window
    migration. Demonstrates how institutional memory feeds back into
    preventing recurrence at sister sites.

    Args:
        template_id: The current template ID, default 'strict-retail-v3'.
        new_version: The new template ID, default 'strict-retail-v4'.
        intent_ids:  Optional. Flag only these intents (a staged/canary
                     rollout); default is every active intent on the
                     template. Repeat calls are safe.
    """
    pol = knowledge_chunks.find_one({"_id": f"POL-{template_id}", "kind": "policy"})
    if not pol:
        return f"❌ Template {template_id} not found in policy catalogue."

    query = {"template": template_id, "status": "active"}
    if intent_ids:
        query["_id"] = {"$in": list(intent_ids)}
    affected = list(intents.find(query))

    knowledge_chunks.update_one(
        {"_id": f"POL-{template_id}"},
        {"$set": {
            "successor": f"POL-{new_version}",
            "deprecated_at": datetime.datetime.now(),
        }},
    )
    # Upsert: ibn_knowledge_chunks is shared across sessions, and a staged
    # rollout calls this more than once.
    knowledge_chunks.update_one({"_id": f"POL-{new_version}"}, {"$setOnInsert": {
        "kind": "policy",
        "title": f"Segmentation template {new_version}",
        "ts": datetime.datetime.now(),
        "lng": None, "lat": None,
        "supersedes": f"POL-{template_id}",
        "text": (
            f"Segmentation template {new_version}. Successor to {template_id}. "
            "Adds EF queue mapping for POS class (DSCP 46), addressing the "
            "head-of-line blocking pattern observed in chain-wide retail "
            "deployments. POS class PIR uplifted by 30% in default profile. "
            "All other VLAN isolation, inter-VLAN routing, and firewall egress "
            f"rules carried forward unchanged from {template_id}."
        ),
    }}, upsert=True)

    # Flag affected intents for migration (this decides a self-healing
    # proposal for them, if there was one: propose_prevention)
    for it in affected:
        intents.update_one(
            {"_id": it["_id"]},
            {"$push": {"history": {
                "ts": datetime.datetime.now(),
                "event": "migration_queued",
                "note":  f"Pending migration {template_id} → {new_version} "
                         f"in next change window.",
            }}, "$unset": {"proposed_change": ""}}
        )
    if affected:
        incidents.update_many(
            {"prevention.status": "proposed",
             "prevention.canary.intent_id": {"$in": [it["_id"] for it in affected]}},
            {"$set": {"prevention.status": "approved",
                      "prevention.approved_at": datetime.datetime.now()}})

    return (
        f"✓ Template `{template_id}` → `{new_version}` "
        f"(EF mapping for POS class folded in).\n"
        f"  {len(affected)} active intent{'s' if len(affected) != 1 else ''} "
        f"flagged for next change window."
    )


@tools.tool
def list_runbooks() -> str:
    """List all runbooks available in the knowledge base."""
    rbs = list(knowledge_chunks.find({"kind": "runbook"}, {"_id": 1, "title": 1, "text": 1}))
    if not rbs:
        return "No runbooks found in the knowledge base."
    lines = [f"**{r['_id']}** — {r.get('title', '(no title)')}\n  {r.get('text', '')[:120]}…"
             for r in rbs]
    return f"**{len(rbs)} runbooks available:**\n\n" + "\n\n".join(lines)
