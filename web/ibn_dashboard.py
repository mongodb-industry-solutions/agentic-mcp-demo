#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

# Live IBN demo dashboard — driven by MongoDB Change Streams over
# ibn_intents, ibn_compliance_events, ibn_telemetry, ibn_policy_snapshots.
#
# Run:  python web/ibn_dashboard.py
# Then: http://localhost:8060
#       http://localhost:8060/?mode=exec   (executive view)
#       http://localhost:8060/?mode=eng    (engineer view, default)

import asyncio
import datetime
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pymongo import AsyncMongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("ibn_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

clients: set[WebSocket] = set()


def _serializable(doc):
    """Recursively convert MongoDB types to JSON-serializable forms."""
    if isinstance(doc, dict):
        return {k: _serializable(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_serializable(v) for v in doc]
    if hasattr(doc, "isoformat"):
        return doc.isoformat()
    if hasattr(doc, "__class__") and doc.__class__.__name__ == "ObjectId":
        return str(doc)
    return doc


async def broadcast(msg: dict):
    data = json.dumps(_serializable(msg))
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# ─── State queries ─────────────────────────────────────────────────────────

async def latest_focused_intent(db):
    """Pick the intent most relevant for display: most-recent submission."""
    coll = db["ibn_intents"]
    cur  = coll.find({}).sort("submitted_at", -1).limit(1)
    docs = [d async for d in cur]
    return docs[0] if docs else None


async def latest_plan_for(db, intent_id):
    coll = db["ibn_policy_snapshots"]
    cur  = coll.find({"intent_id": intent_id}).sort("snapshot_at", -1).limit(1)
    docs = [d async for d in cur]
    return docs[0] if docs else None


async def site_for(db, site_id):
    if not site_id: return None
    return await db["ibn_sites"].find_one({"_id": site_id})


async def all_intents(db):
    cur = db["ibn_intents"].find({}).sort("submitted_at", -1)
    return [d async for d in cur]


async def recent_telemetry(db, intent_id, seconds=120):
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(seconds=seconds)
    cur = db["ibn_telemetry"].find(
        {"meta.intent_id": intent_id, "ts": {"$gte": cutoff}}
    ).sort("ts", 1)
    return [d async for d in cur]


async def recent_compliance_events(db, intent_id, limit=8):
    cur = db["ibn_compliance_events"].find(
        {"intent_id": intent_id}
    ).sort("ts", -1).limit(limit)
    docs = [d async for d in cur]
    return list(reversed(docs))


async def build_snapshot(db):
    """Full state package sent on WebSocket connect."""
    focused = await latest_focused_intent(db)
    intents_list = await all_intents(db)

    snap = {
        "type":    "snapshot",
        "intents": [_serializable(i) for i in intents_list],
        "focused": _serializable(focused) if focused else None,
        "plan":    None,
        "site":    None,
        "telemetry": [],
        "events":  [],
    }
    if focused:
        plan = await latest_plan_for(db, focused["_id"])
        snap["plan"] = _serializable(plan)
        snap["site"] = _serializable(await site_for(db, focused.get("site_id")))
        snap["telemetry"] = [_serializable(t) for t in await recent_telemetry(db, focused["_id"])]
        snap["events"] = [_serializable(e) for e in await recent_compliance_events(db, focused["_id"])]
    return snap


# ─── Change stream watchers ────────────────────────────────────────────────

async def watch_intents(db):
    log.info("intent watcher started")
    coll = db["ibn_intents"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] in ("insert", "update", "replace"):
                        doc = change.get("fullDocument")
                        if doc:
                            site = await site_for(db, doc.get("site_id"))
                            plan = await latest_plan_for(db, doc["_id"])
                            await broadcast({
                                "type": "intent_update",
                                "doc":  _serializable(doc),
                                "site": _serializable(site),
                                "plan": _serializable(plan),
                            })
        except Exception as e:
            log.warning(f"intent stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


async def watch_compliance(db):
    log.info("compliance watcher started")
    coll = db["ibn_compliance_events"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] == "insert":
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast({"type": "compliance_event",
                                             "doc": _serializable(doc)})
        except Exception as e:
            log.warning(f"compliance stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


async def poll_telemetry(db, interval_seconds: float = 1.0):
    """
    Poll telemetry at 1Hz instead of using Change Streams. Atlas exposes
    time-series collections as views over the underlying buckets collection,
    and `collection.watch()` rejects views — polling is the simpler and
    sufficient approach for the demo's update cadence.
    """
    log.info(f"telemetry poller started ({interval_seconds:.1f}s interval)")
    last_count = 0
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            focused = await latest_focused_intent(db)
            if not focused:
                continue
            samples = await recent_telemetry(db, focused["_id"], seconds=120)
            # Skip broadcast if nothing changed since last poll
            if len(samples) == last_count and samples:
                continue
            last_count = len(samples)
            await broadcast({
                "type":      "telemetry",
                "intent_id": focused["_id"],
                "samples":   [_serializable(s) for s in samples],
            })
        except Exception as e:
            log.warning(f"telemetry poll error ({e})")
            await asyncio.sleep(2)


async def live_telemetry_writer(db, interval_seconds: float = 2.0):
    """
    Write one telemetry sample per active intent every interval_seconds.
    Keeps the gauge bar alive and visibly fluctuating during the demo.
    Skips violated intents so the spike stays visible until diagnosed.
    """
    log.info(f"live telemetry writer started ({interval_seconds:.1f}s interval)")
    intents_coll  = db["ibn_intents"]
    telemetry_coll = db["ibn_telemetry"]
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            cur = intents_coll.find({"status": "active"})
            active = [d async for d in cur]
            if not active:
                continue
            now = datetime.datetime.now()
            docs = []
            for intent in active:
                targets   = (intent.get("parsed") or {}).get("targets") or {}
                threshold = targets.get("pos_latency_ms", 40)
                lo = max(15, threshold - 18)
                hi = max(20, threshold - 8)
                docs.append({
                    "ts":   now,
                    "meta": {"intent_id": intent["_id"],
                             "site_id":   intent.get("site_id"),
                             "metric":    "pos_latency_ms"},
                    "value": round(random.uniform(lo, hi), 1),
                })
            await telemetry_coll.insert_many(docs)
        except Exception as e:
            log.warning(f"live telemetry writer error ({e})")
            await asyncio.sleep(2)


async def watch_plans(db):
    log.info("plan watcher started")
    coll = db["ibn_policy_snapshots"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] == "insert":
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast({"type": "plan_update",
                                             "doc": _serializable(doc)})
        except Exception as e:
            log.warning(f"plan stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


# ─── FastAPI app ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGO_URI)
    db = client[DB_NAME]
    app.state.mongo = client
    app.state.db    = db

    tasks = [
        asyncio.create_task(watch_intents(db)),
        asyncio.create_task(watch_compliance(db)),
        asyncio.create_task(poll_telemetry(db)),
        asyncio.create_task(watch_plans(db)),
        asyncio.create_task(live_telemetry_writer(db)),
    ]
    log.info("Dashboard ready — http://localhost:8060")
    yield
    for t in tasks:
        t.cancel()
    await client.close()


app = FastAPI(lifespan=lifespan)

HTML_PATH = Path(__file__).parent / "ibn.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.get("/snapshot/{intent_id}")
async def intent_snapshot(intent_id: str):
    """Per-intent snapshot for tab switching — returns intent, plan, site, telemetry, events."""
    db = app.state.db
    intent = await db["ibn_intents"].find_one({"_id": intent_id})
    if not intent:
        return JSONResponse({"error": "not found"}, status_code=404)
    plan    = await latest_plan_for(db, intent_id)
    site    = await site_for(db, intent.get("site_id"))
    samples = await recent_telemetry(db, intent_id)
    events  = await recent_compliance_events(db, intent_id)
    return JSONResponse(_serializable({
        "intent":    intent,
        "plan":      plan,
        "site":      site,
        "telemetry": samples,
        "events":    events,
    }))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    log.info(f"client connected ({len(clients)} total)")

    try:
        snap = await build_snapshot(app.state.db)
        await ws.send_text(json.dumps(_serializable(snap)))
    except Exception as e:
        log.error(f"snapshot send failed: {e}")

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        log.info(f"client disconnected ({len(clients)} total)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8060, log_level="info")
