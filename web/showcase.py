#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The dashboards' "Data model" and "Hero queries" popups (GET /api/showcase).

Everything shown is real: documents come from the visitor's lane (or the
shared reference data), index definitions and time series options are read
from Atlas, and the pipelines are built by the same functions the agents'
tools call (oss_tools). A pipeline the lane has already run is shown as it
ran (LIVE); otherwise it is built for the lane's own data (EXAMPLE).

Each entry:
  model:   {id, title, subtitle, collection, docs: [...], extras: [{label, value}],
            caption, callouts: [{find, label}]}
  queries: {id, title, subtitle, collection, op: aggregate|watch, pipeline,
            options?, source: live|example, ran_at?, note?, caption, callouts}
`callouts` mark the first rendered line containing `find` with a number;
the page lists the numbers with their labels under the code.
"""

import datetime
import json
import re
from pathlib import Path

from bson import ObjectId

from oss_tools.dtw import simulation as dtw_sim
from oss_tools.dtw import traffic as dtw_traffic
from oss_tools.ibn import assurance as ibn_assurance
from oss_tools.ibn import autonomy, inventory, learning, slo

ROOT = Path(__file__).resolve().parent.parent
_GUARDRAIL = json.loads((ROOT / "agent-engine/guardrails/subscriber-ids.json").read_text())
_REDACT = [re.compile(p["value"]) for p in _GUARDRAIL["config"]["match_patterns"]]

MAX_ITEMS = 3        # array items shown before "… N more"
MAX_TEXT = 220       # characters of a long string
_HIDDEN = re.compile(r"embedding|vector", re.I)   # auto-embedding fields


def _view(value, depth: int = 0):
    """A document made readable: dates and ids tagged for the page
    ($date/$oid, rendered as ISODate()/ObjectId()), long arrays and strings
    cut, subscriber ids redacted, embeddings left out."""
    if isinstance(value, dict):
        return {k: _view(v, depth + 1) for k, v in value.items() if not _HIDDEN.search(k)}
    if isinstance(value, list):
        items = [_view(v, depth + 1) for v in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"… {len(value) - MAX_ITEMS} more")
        return items
    if isinstance(value, datetime.datetime):
        return {"$date": value.replace(microsecond=0).isoformat()}
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    if isinstance(value, str):
        for rx in _REDACT:
            value = rx.sub("[BLOCKED]", value)
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    return value


def _full(value):
    """A pipeline as it runs: tagged like _view, never cut."""
    if isinstance(value, dict):
        return {k: _full(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_full(v) for v in value]
    if isinstance(value, datetime.datetime):
        return {"$date": value.replace(microsecond=0).isoformat()}
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    return value


async def _one(coll, query=None, sort=None, projection=None):
    try:
        return await coll.find_one(query or {}, projection, sort=sort)
    except Exception:
        return None


async def _search_index(coll, name: str):
    try:
        cursor = await coll.list_search_indexes(name)
        for ix in await cursor.to_list(1):
            return {"name": ix.get("name"), "type": ix.get("type"),
                    "status": ix.get("status"),
                    "definition": ix.get("latestDefinition")}
    except Exception:
        return None


async def _geo_index(coll):
    try:
        for name, ix in (await coll.index_information()).items():
            if any(kind == "2dsphere" for _, kind in ix.get("key", [])):
                return {"name": name, "key": dict(ix["key"])}
    except Exception:
        return None


async def _ts_options(db, name: str):
    try:
        cursor = await db.list_collections(filter={"name": name})
        for info in await cursor.to_list(1):
            return {"name": name, "timeseries": info.get("options", {}).get("timeseries")}
    except Exception:
        return None


def _extras(*pairs):
    return [{"label": label, "value": value} for label, value in pairs if value]


async def _lane_or_shared(db, pfx: str, base: str):
    """The lane's collection when it holds data, else the shared one (the
    default lane): the dashboards show either."""
    if pfx and await _one(db[pfx + base]) is not None:
        return db[pfx + base]
    return db[base]


# ─── IBN ────────────────────────────────────────────────────────────────────

async def ibn(db, pfx: str) -> dict:
    intents = await _lane_or_shared(db, pfx, "ibn_intents")
    intent = (await _one(intents, {"status": {"$in": ["active", "violated"]}}, sort=[("_id", 1)])
              or await _one(intents, sort=[("_id", 1)]) or {})
    site = await _one(db["ibn_sites"], {"_id": intent.get("site_id")}) or await _one(db["ibn_sites"]) or {}
    resource = await _one(db["ibn_resources"], {"site_id": site.get("_id")}) or await _one(db["ibn_resources"])
    telemetry = await _lane_or_shared(db, pfx, "ibn_telemetry")
    sample = await _one(telemetry, sort=[("ts", -1)])
    chunk = await _one(db["ibn_knowledge_chunks"], {"kind": "incident", "lane": {"$exists": False}})
    incidents = db[pfx + "ibn_incidents"]
    incident = await _one(incidents, sort=[("opened_at", -1)])
    policy = autonomy.merge(await _one(db[pfx + "ibn_autonomy_policy"], {"_id": autonomy.POLICY_ID}))

    model = [
        {"id": "intent", "title": "Customer intent", "subtitle": "nested document",
         "collection": intents.name, "docs": [_view(intent)],
         "caption": "One document per intent: the request in the customer's words, the parsed "
                    "SLOs and services, the lifecycle and the NOC review marker. Reading an "
                    "intent takes one lookup, with no joins.",
         "callouts": [{"find": "raw_text", "label": "the request as typed"},
                      {"find": "targets", "label": "parsed SLOs, nested"},
                      {"find": "status", "label": "lifecycle, watched by change streams"}]},
        {"id": "site", "title": "Sites and resources", "subtitle": "GeoJSON + 2dsphere",
         "collection": "ibn_sites · ibn_resources", "docs": [_view(site), _view(resource)],
         "extras": _extras(("2dsphere index on ibn_resources", await _geo_index(db["ibn_resources"])),
                           ("2dsphere index on ibn_sites", await _geo_index(db["ibn_sites"]))),
         "caption": "Every site and access node carries its location as GeoJSON with a 2dsphere "
                    "index, so \"spare capacity within 20 km\" is one $geoNear stage.",
         "callouts": [{"find": "Point", "label": "GeoJSON point"},
                      {"find": "2dsphere", "label": "geospatial index"}]},
        {"id": "telemetry", "title": "Telemetry", "subtitle": "time series collection",
         "collection": telemetry.name, "docs": [_view(sample)] if sample else [],
         "extras": _extras(("collection options", await _ts_options(db, telemetry.name))),
         "caption": "Four metrics per site every few seconds land in a time series collection: "
                    "columnar buckets per intent and metric, compressed, with the SLO monitor's "
                    "p95 computed in the database.",
         "callouts": [{"find": "timeField", "label": "time field"},
                      {"find": "metaField", "label": "series key (intent, metric)"},
                      {"find": "granularity", "label": "bucket size"}]},
        {"id": "knowledge", "title": "Knowledge base", "subtitle": "vector search, auto-embedded",
         "collection": "ibn_knowledge_chunks", "docs": [_view(chunk)] if chunk else [],
         "extras": _extras(("Atlas Vector Search index",
                            await _search_index(db["ibn_knowledge_chunks"], "ibn_knowledge_index"))),
         "caption": "Past incidents and runbooks as text. Atlas embeds `text` with Voyage AI on "
                    "write and at query time; kind, time, location, customer and site are filter "
                    "fields inside the same index.",
         "callouts": [{"find": "autoEmbed", "label": "Atlas embeds the text itself"},
                      {"find": "voyage", "label": "embedding model"},
                      {"find": "\"filter\"", "label": "pre-filter fields in the index"}]},
        {"id": "incident", "title": "Incident and autonomy", "subtitle": "TMF642 alarm, policy as data",
         "collection": f"{pfx}ibn_incidents · {pfx}ibn_autonomy_policy",
         "docs": ([_view(incident)] if incident else []) + [_view(policy)],
         "caption": "The SLO monitor opens an incident with a TMF642-style alarm; every stage of "
                    "the self-healing loop is appended to its timeline. The autonomy envelope "
                    "(L3/L4, thresholds, deny list) is a document the agents read, not a prompt."
                    + ("" if incident else " Start a morning rush to see an incident."),
         "callouts": [{"find": "alarm", "label": "TMF642 alarm"},
                      {"find": "timeline", "label": "the loop's stages, appended"},
                      {"find": "level:", "label": "autonomy level L3/L4"}]},
    ]

    # Hero queries
    lane = learning.lane_filter(pfx)
    event = await _one(db[pfx + "ibn_compliance_events"], {"kind": "diagnose"}, sort=[("ts", -1)])
    if event and event.get("pipeline"):
        diag = {"pipeline": event["pipeline"], "source": "live", "ran_at": _full(event.get("ts")),
                "note": f"{event.get('intent_id')} · filter level: {event.get('filter_level')}"}
    else:
        lng, lat = (site.get("location") or {}).get("coordinates", [0, 0])
        level, flt = ibn_assurance.diagnose_filters(lng, lat)[0]
        fingerprint = ibn_assurance._build_fingerprint(intent, None) if intent else "POS latency spike"
        diag = {"pipeline": _full(ibn_assurance.diagnose_pipeline(fingerprint, flt, lane)),
                "source": "example", "note": f"{intent.get('_id', '')} at {site.get('name', '')} · {level}"}
    since = datetime.datetime.now() - datetime.timedelta(seconds=20)
    queries = [
        {"id": "diagnose", "title": "Diagnose", "subtitle": "vector + kind + time + geo",
         "collection": "ibn_knowledge_chunks", "op": "aggregate", **diag,
         "caption": "One $vectorSearch stage finds the most similar past incident, pre-filtered "
                    "inside the index by kind, time window and a geo box around the site. "
                    "Elsewhere this takes pgvector, PostGIS, TimescaleDB and a join in the "
                    "application.",
         "callouts": [{"find": "$vectorSearch", "label": "meaning: similar symptoms"},
                      {"find": "kind:", "label": "structure: incidents only"},
                      {"find": "ts:", "label": "time: the last 180 days"},
                      {"find": "lng:", "label": "geo: a box around the site"},
                      {"find": "$match", "label": "only this lane's learned incidents"}]},
        {"id": "spare", "title": "Nearby spare", "subtitle": "$geoNear",
         "collection": "ibn_resources", "op": "aggregate", "source": "example",
         "note": f"around {site.get('name', 'the site')}, 20 km",
         "pipeline": _full(inventory.nearby_spare_pipeline(
             site.get("location") or {"type": "Point", "coordinates": [0, 0]}, site.get("_id"), 20)),
         "caption": "Failover planning: access nodes at other sites, nearest first, with the "
                    "distance computed by the 2dsphere index and the filter in the same stage.",
         "callouts": [{"find": "$geoNear", "label": "nearest first, 2dsphere"},
                      {"find": "maxDistance", "label": "radius in metres"},
                      {"find": "query:", "label": "structured filter, same stage"}]},
        {"id": "slo", "title": "SLO monitor", "subtitle": "p95 over a time window",
         "collection": telemetry.name, "op": "aggregate", "source": "example",
         "note": "runs every 2 s per lane while a dashboard is open",
         "pipeline": _full(slo.window_pipeline(since, intent.get("_id"))),
         "caption": "The SLO monitor asks the time series collection for p95, mean and count per "
                    "intent and metric over the last 20 seconds; three windows over the SLO open "
                    "an incident.",
         "callouts": [{"find": "ts:", "label": "the last 20 s"},
                      {"find": "$percentile", "label": "p95, computed in the database"},
                      {"find": "meta.metric", "label": "grouped by the series key"}]},
        {"id": "stream", "title": "Live dashboard", "subtitle": "change streams",
         "collection": intents.name, "op": "watch", "source": "example",
         "note": "one stream per collection the page shows", "pipeline": [],
         "options": {"fullDocument": "updateLookup"},
         "caption": "This page has no polling: change streams on intents, compliance events, "
                    "incidents and the autonomy policy push every write to the browser as it "
                    "happens.",
         "callouts": [{"find": ".watch(", "label": "subscribe to every change"},
                      {"find": "updateLookup", "label": "with the whole document"}]},
    ]
    return {"model": model, "queries": queries}


# ─── DTW ────────────────────────────────────────────────────────────────────

async def dtw(db, pfx: str) -> dict:
    elements = db["dtw_network_elements"]
    cell = await _one(elements, {"type": "Cell"})
    pgw = await _one(elements, {"type": "PGW"})
    scenarios = await _lane_or_shared(db, pfx, "dtw_scenarios")
    scenario = (await _one(scenarios, {"results": {"$exists": True}}, sort=[("results.computed_at", -1)])
                or await _one(scenarios, sort=[("_id", -1)]) or {})
    cs, scope = scenario.get("change_set") or {}, scenario.get("scope") or {}
    plan_id = cs.get("plan_id") or "plan_ACME_M"
    plan = await _one(db["dtw_plans"], {"_id": plan_id}) or await _one(db["dtw_plans"]) or {}
    qos = await _one(db["dtw_qos_profiles"], {"_id": plan.get("current_qos_profile_id")}) or await _one(db["dtw_qos_profiles"])
    edges = [e async for e in db["dtw_topology_edges"].find({}).limit(3)]
    markets = scope.get("markets") or ["NYC_Metro"]
    tm = await _one(db["dtw_traffic_models"], {"plan_id": plan.get("_id"), "market": markets[0]}) \
        or await _one(db["dtw_traffic_models"])
    chunk = await _one(db["dtw_knowledge_chunks"], {"kind": "incident"})

    model = [
        {"id": "elements", "title": "Network elements", "subtitle": "one collection, many shapes",
         "collection": "dtw_network_elements", "docs": [_view(cell), _view(pgw)],
         "caption": "HSS, HLR, MME, SGW, PGW, eNodeB and Cell live in one collection, each with "
                    "the fields its type needs: no table per element type, no nullable columns.",
         "callouts": [{"find": "\"Cell\"", "label": "a radio cell"},
                      {"find": "\"PGW\"", "label": "a core gateway, other fields"},
                      {"find": "capacity", "label": "type-specific capacity"}]},
        {"id": "topology", "title": "Topology", "subtitle": "edges for $graphLookup",
         "collection": "dtw_topology_edges", "docs": [_view(e) for e in edges],
         "caption": "The dependency graph as small edge documents (plan → QoS → cell → eNodeB → "
                    "SGW → PGW). $graphLookup walks it recursively inside the database.",
         "callouts": [{"find": "from_id", "label": "edge source"},
                      {"find": "to_id", "label": "edge target"},
                      {"find": "relation", "label": "edge type"}]},
        {"id": "plan", "title": "Plan and QoS", "subtitle": "reference data",
         "collection": "dtw_plans · dtw_qos_profiles", "docs": [_view(plan), _view(qos)],
         "caption": "Tariff plans and the QoS profiles they reference: what a what-if scenario "
                    "changes.",
         "callouts": [{"find": "segment", "label": "prepaid / postpaid"},
                      {"find": "max_downlink_mbps", "label": "the cap a scenario raises"}]},
        {"id": "traffic", "title": "Traffic model", "subtitle": "embedded per-cell array",
         "collection": "dtw_traffic_models", "docs": [_view(tm)] if tm else [],
         "caption": "One document per plan, market and time window, with the per-cell load "
                    "embedded: the simulation reads a window in one lookup.",
         "callouts": [{"find": "time_window", "label": "the window, embedded"},
                      {"find": "cells", "label": "per-cell estimates, embedded"}]},
        {"id": "knowledge", "title": "Knowledge base", "subtitle": "vector search, auto-embedded",
         "collection": "dtw_knowledge_chunks", "docs": [_view(chunk)] if chunk else [],
         "extras": _extras(("Atlas Vector Search index",
                            await _search_index(db["dtw_knowledge_chunks"], "dtw_knowledge_index"))),
         "caption": "Past changes, incidents and runbooks. Atlas embeds `text` with Voyage AI; "
                    "segment, market, plan and kind are filter fields in the same index.",
         "callouts": [{"find": "autoEmbed", "label": "Atlas embeds the text itself"},
                      {"find": "voyage", "label": "embedding model"},
                      {"find": "\"filter\"", "label": "pre-filter fields in the index"}]},
        {"id": "scenario", "title": "What-if scenario", "subtitle": "lifecycle + embedded result",
         "collection": scenarios.name, "docs": [_view(scenario)] if scenario else [],
         "caption": "A scenario carries its change set, scope, status and, once simulated, its "
                    "whole result embedded: the dashboard redraws from one change-stream event."
                    + ("" if scenario else " Create a scenario in the chat to see one."),
         "callouts": [{"find": "change_set", "label": "what changes"},
                      {"find": "scope", "label": "where and when"},
                      {"find": "results", "label": "the simulation, embedded"}]},
    ]

    results = scenario.get("results") or {}
    if results.get("vector_pipeline"):
        hybrid = {"pipeline": results["vector_pipeline"], "source": "live",
                  "ran_at": _full(results.get("computed_at")),
                  "note": f"{scenario.get('_id')} · filter level: {results.get('vector_filter_level')}"}
    else:
        level, flt = dtw_sim.knowledge_filters(plan.get("segment"), markets)[0]
        fp = dtw_sim._build_fingerprint(scenario or {"scope": {"markets": markets}}, plan, None, None)
        hybrid = {"pipeline": _full(dtw_sim.knowledge_pipeline(fp, flt)), "source": "example",
                  "note": f"{plan.get('name', plan_id)} · {level}"}
    walk_live = bool(results)
    queries = [
        {"id": "walk", "title": "Dependency walk", "subtitle": "$graphLookup",
         "collection": "dtw_topology_edges", "op": "aggregate",
         "source": "live" if walk_live else "example",
         "ran_at": _full(results.get("computed_at")) if walk_live else None,
         "note": f"downstream from {plan_id}, 4 levels",
         "pipeline": _full(dtw_sim.graph_walk_pipeline(plan_id, max_depth=4)),
         "caption": "From the plan down to the gateways in one stage: $graphLookup follows the "
                    "edges recursively and records each hop's depth. The blast radius of a "
                    "change, without a graph database next to the operational one.",
         "callouts": [{"find": "$graphLookup", "label": "recursive walk, one stage"},
                      {"find": "connectFromField", "label": "follow to_id …"},
                      {"find": "connectToField", "label": "… into the next edge's from_id"},
                      {"find": "depthField", "label": "hop count per element"}]},
        {"id": "similar", "title": "Similar past changes", "subtitle": "vector + segment + market",
         "collection": "dtw_knowledge_chunks", "op": "aggregate", **hybrid,
         "caption": "The same simulation asks the knowledge base for past changes like this one, "
                    "pre-filtered by segment, market and kind: graph for structure, vectors for "
                    "institutional memory, in one database.",
         "callouts": [{"find": "$vectorSearch", "label": "meaning: similar changes"},
                      {"find": "segment:", "label": "same customer segment"},
                      {"find": "market:", "label": "same markets"},
                      {"find": "kind:", "label": "incidents and runbooks"}]},
        {"id": "peak", "title": "Peak hours", "subtitle": "aggregation over embedded arrays",
         "collection": "dtw_traffic_models", "op": "aggregate", "source": "example",
         "note": f"market {markets[0]}",
         "pipeline": _full(dtw_traffic.peak_hours_pipeline(markets[0])),
         "caption": "Which time windows are busiest in a market: unwind the embedded per-cell "
                    "estimates and sum them per window, in the database.",
         "callouts": [{"find": "$unwind", "label": "open the embedded cells"},
                      {"find": "$sum", "label": "active subscribers per window"},
                      {"find": "$sort", "label": "busiest first"}]},
        {"id": "stream", "title": "Live dashboard", "subtitle": "change streams",
         "collection": scenarios.name, "op": "watch", "source": "example",
         "note": "the page's only data feed", "pipeline": [],
         "options": {"fullDocument": "updateLookup"},
         "caption": "This page has no polling: a change stream on the scenarios pushes every "
                    "create, update and simulation result to the browser as it happens.",
         "callouts": [{"find": ".watch(", "label": "subscribe to every change"},
                      {"find": "updateLookup", "label": "with the whole document"}]},
    ]
    return {"model": model, "queries": queries}
