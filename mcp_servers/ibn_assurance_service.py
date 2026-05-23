#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Assurance Service — Live Compliance and Hybrid Vector Diagnosis

The brain of the demo. Owns continuous compliance computation against active
intents (reads ibn_telemetry, writes ibn_compliance_events) and runs the
hybrid vector + geospatial + time + structured-filter search across
ibn_knowledge_chunks to diagnose violations by surfacing semantically
similar past incidents and their proven runbooks.

Use this service when users say:
- Compliance: "how are we doing", "compliance status", "is it green",
             "show compliance for IBN-...", "fleet status",
             "current metrics at <site>", "what are the metrics",
             "check status at <site>", "how is <site> performing",
             "live status", "site performance", "current telemetry"
- Ranking:   "which store has the lowest latency", "best performing store",
             "worst performing store", "highest POS latency", "lowest latency",
             "rank stores by latency", "compare latency across stores",
             "which site is fastest", "which site is slowest"
- Diagnose:  "what happened", "diagnose", "diagnose <intent>",
             "why is it red", "explain the violation", "find similar incident"
- Apply:     "apply the runbook", "apply <runbook_id>", "apply the fix",
             "remediate"
- Runbooks:  "what runbooks are available", "list runbooks", "show runbooks",
             "which runbooks exist"
- Template:  "update the template", "patch the template",
             "fold the fix into the template"

This service does NOT submit intents, manage inventory, check feasibility,
or simulate telemetry directly — those are the other IBN services.
"""

import datetime
import json
import logging
import os
import math
from pymongo import MongoClient, DESCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("ibn_assurance_service")
logger = logging.getLogger("ibn_assurance_service")

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db                  = mongo_client["agent_registry"]
intents             = db["ibn_intents"]
sites               = db["ibn_sites"]
telemetry           = db["ibn_telemetry"]
compliance_events   = db["ibn_compliance_events"]
knowledge_chunks    = db["ibn_knowledge_chunks"]
policy_snapshots    = db["ibn_policy_snapshots"]


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


def _build_fingerprint(intent: dict, observed_pos_latency: float | None) -> str:
    """Render a natural-language symptom fingerprint for vector search."""
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

@mcp.tool()
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


@mcp.tool()
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

    observed = _latest_metric(intent_id, "pos_latency_ms")
    fingerprint = _build_fingerprint(intent, observed)

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

    def _build_pipeline(flt):
        return [
            {
                "$vectorSearch": {
                    "index":   "ibn_knowledge_index",
                    "path":    "text",
                    "query":   fingerprint,
                    "filter":  flt,
                    "numCandidates": 200,
                    "limit": 3,
                }
            },
            {
                "$project": {
                    "_id":         0, "title": 1, "site_name": 1, "ts": 1,
                    "fingerprint": 1, "runbook_id": 1, "text": 1,
                    "lng": 1, "lat": 1,
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
        f"· similarity {top['score']:.2f}",
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


@mcp.tool()
def apply_runbook(runbook_id: str, intent_id: str = None, site: str = None) -> str:
    """
    Apply a runbook to a violating intent. Records the application in the
    intent's history, writes a recovery event, and writes one fresh
    in-spec telemetry sample to clear the gauge in the dashboard.

    Args:
        runbook_id: The runbook to apply, e.g. 'RB-007'.
        intent_id:  The intent currently violating, e.g. 'IBN-005'.
        site:       Alternative: site name fragment like 'Marienplatz'.

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

    now = datetime.datetime.now()
    targets = intent.get("parsed", {}).get("targets", {})
    threshold = targets.get("pos_latency_ms", 40)
    healthy_value = max(threshold * 0.7, 22)  # restore well within target

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


@mcp.tool()
def update_template_version(template_id: str = "strict-retail-v3",
                            new_version: str = "strict-retail-v4") -> str:
    """
    Bump a segmentation template version with the runbook fix folded in.
    For the demo, this updates the policy doc in ibn_knowledge_chunks and
    flags existing intents using the old template for next-change-window
    migration. Demonstrates how institutional memory feeds back into
    preventing recurrence at sister sites.

    Args:
        template_id: The current template ID, default 'strict-retail-v3'.
        new_version: The new template ID, default 'strict-retail-v4'.
    """
    pol = knowledge_chunks.find_one({"_id": f"POL-{template_id}", "kind": "policy"})
    if not pol:
        return f"❌ Template {template_id} not found in policy catalogue."

    affected = list(intents.find({"template": template_id, "status": "active"}))

    knowledge_chunks.update_one(
        {"_id": f"POL-{template_id}"},
        {"$set": {
            "successor": f"POL-{new_version}",
            "deprecated_at": datetime.datetime.now(),
        }},
    )
    knowledge_chunks.insert_one({
        "_id": f"POL-{new_version}",
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
    })

    # Flag affected intents for migration
    for it in affected:
        intents.update_one(
            {"_id": it["_id"]},
            {"$push": {"history": {
                "ts": datetime.datetime.now(),
                "event": "migration_queued",
                "note":  f"Pending migration {template_id} → {new_version} "
                         f"in next change window.",
            }}}
        )

    return (
        f"✓ Template `{template_id}` → `{new_version}` "
        f"(EF mapping for POS class folded in).\n"
        f"  {len(affected)} active intent{'s' if len(affected) != 1 else ''} "
        f"flagged for next change window."
    )


@mcp.tool()
def list_runbooks() -> str:
    """List all runbooks available in the knowledge base."""
    rbs = list(knowledge_chunks.find({"kind": "runbook"}, {"_id": 1, "title": 1, "text": 1}))
    if not rbs:
        return "No runbooks found in the knowledge base."
    lines = [f"**{r['_id']}** — {r.get('title', '(no title)')}\n  {r.get('text', '')[:120]}…"
             for r in rbs]
    return f"**{len(rbs)} runbooks available:**\n\n" + "\n\n".join(lines)


if __name__ == "__main__":
    mcp.run()
