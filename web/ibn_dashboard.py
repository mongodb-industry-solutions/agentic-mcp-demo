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
#       http://localhost:8060/?session=<token>   (Phase B: watch one
#         browser session's prefixed data — the web shell hands out this
#         link with its own session token so the dashboard mirrors the
#         exact lane the user is driving. No token → the shared/default
#         lane, as before.)

import asyncio
import datetime
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pymongo import AsyncMongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("ibn_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

# Phase B: a browser session's mutable collections are prefixed
# s_<token>_ (see MULTI_SESSION_PLAN.md). The dashboard watches one
# session's prefixed collections so it shows exactly what that user is
# doing in the web shell. ibn_sites is reference data → always shared.
_TOKEN_RE = re.compile(r"^[a-z0-9]{8,32}$")
SESSION_IDLE_TTL_SEC = 120  # tear a session's watchers down this long after its last tab leaves


def _prefix(token: str | None) -> str:
    token = (token or "").strip().lower()
    return f"s_{token}_" if _TOKEN_RE.match(token) else ""


@dataclass
class DashSession:
    prefix: str
    clients: set = field(default_factory=set)
    tasks: list = field(default_factory=list)
    last_activity: float = 0.0


_sessions: dict[str, DashSession] = {}   # keyed by prefix ("" = default lane)
_guard = asyncio.Lock()


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


async def broadcast(session: DashSession, msg: dict):
    """Send to the tabs watching ONE session only."""
    data = json.dumps(_serializable(msg))
    dead = []
    for ws in session.clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session.clients.discard(ws)


# ─── State queries (all scoped by prefix; ibn_sites stays shared) ───────────

async def latest_focused_intent(db, pfx):
    coll = db[pfx + "ibn_intents"]
    cur  = coll.find({}).sort("submitted_at", -1).limit(1)
    docs = [d async for d in cur]
    return docs[0] if docs else None


async def latest_plan_for(db, pfx, intent_id):
    coll = db[pfx + "ibn_policy_snapshots"]
    cur  = coll.find({"intent_id": intent_id}).sort("snapshot_at", -1).limit(1)
    docs = [d async for d in cur]
    return docs[0] if docs else None


async def site_for(db, site_id):
    if not site_id: return None
    return await db["ibn_sites"].find_one({"_id": site_id})   # reference → shared


async def all_intents(db, pfx):
    cur = db[pfx + "ibn_intents"].find({}).sort("submitted_at", -1)
    return [d async for d in cur]


async def recent_telemetry(db, pfx, intent_id, seconds=120):
    cutoff = datetime.datetime.now() - datetime.timedelta(seconds=seconds)
    cur = db[pfx + "ibn_telemetry"].find(
        {"meta.intent_id": intent_id, "ts": {"$gte": cutoff}}
    ).sort("ts", 1)
    return [d async for d in cur]


async def recent_compliance_events(db, pfx, intent_id, limit=8):
    cur = db[pfx + "ibn_compliance_events"].find(
        {"intent_id": intent_id}
    ).sort("ts", -1).limit(limit)
    docs = [d async for d in cur]
    return list(reversed(docs))


async def build_snapshot(db, pfx):
    """Full state package sent on WebSocket connect."""
    focused = await latest_focused_intent(db, pfx)
    intents_list = await all_intents(db, pfx)

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
        plan = await latest_plan_for(db, pfx, focused["_id"])
        snap["plan"] = _serializable(plan)
        snap["site"] = _serializable(await site_for(db, focused.get("site_id")))
        snap["telemetry"] = [_serializable(t) for t in await recent_telemetry(db, pfx, focused["_id"])]
        snap["events"] = [_serializable(e) for e in await recent_compliance_events(db, pfx, focused["_id"])]
    return snap


# ─── Change stream watchers (one set per session) ───────────────────────────

async def watch_intents(db, session: DashSession):
    pfx = session.prefix
    log.info(f"intent watcher started (prefix={pfx!r})")
    coll = db[pfx + "ibn_intents"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] in ("insert", "update", "replace"):
                        doc = change.get("fullDocument")
                        if doc:
                            site = await site_for(db, doc.get("site_id"))
                            plan = await latest_plan_for(db, pfx, doc["_id"])
                            await broadcast(session, {
                                "type": "intent_update",
                                "doc":  _serializable(doc),
                                "site": _serializable(site),
                                "plan": _serializable(plan),
                            })
        except Exception as e:
            log.warning(f"intent stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


async def watch_compliance(db, session: DashSession):
    pfx = session.prefix
    log.info(f"compliance watcher started (prefix={pfx!r})")
    coll = db[pfx + "ibn_compliance_events"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] == "insert":
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast(session, {"type": "compliance_event",
                                                      "doc": _serializable(doc)})
        except Exception as e:
            log.warning(f"compliance stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


async def poll_telemetry(db, session: DashSession, interval_seconds: float = 1.0):
    """
    Poll telemetry at 1Hz instead of using Change Streams. Atlas exposes
    time-series collections as views over the underlying buckets collection,
    and `collection.watch()` rejects views — polling is the simpler and
    sufficient approach for the demo's update cadence.
    """
    pfx = session.prefix
    log.info(f"telemetry poller started (prefix={pfx!r}, {interval_seconds:.1f}s)")
    last_count = 0
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            focused = await latest_focused_intent(db, pfx)
            if not focused:
                continue
            samples = await recent_telemetry(db, pfx, focused["_id"], seconds=120)
            if len(samples) == last_count and samples:
                continue
            last_count = len(samples)
            await broadcast(session, {
                "type":      "telemetry",
                "intent_id": focused["_id"],
                "samples":   [_serializable(s) for s in samples],
            })
        except Exception as e:
            log.warning(f"telemetry poll error ({e})")
            await asyncio.sleep(2)


async def live_telemetry_writer(db, session: DashSession, interval_seconds: float = 2.0):
    """
    Write one telemetry sample per active intent every interval_seconds,
    into THIS session's prefixed telemetry collection. Keeps the gauge bar
    alive and visibly fluctuating during the demo. Skips violated intents
    so the spike stays visible until diagnosed.
    """
    pfx = session.prefix
    log.info(f"live telemetry writer started (prefix={pfx!r}, {interval_seconds:.1f}s)")
    intents_coll   = db[pfx + "ibn_intents"]
    telemetry_coll = db[pfx + "ibn_telemetry"]
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


async def watch_plans(db, session: DashSession):
    pfx = session.prefix
    log.info(f"plan watcher started (prefix={pfx!r})")
    coll = db[pfx + "ibn_policy_snapshots"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] == "insert":
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast(session, {"type": "plan_update",
                                                      "doc": _serializable(doc)})
        except Exception as e:
            log.warning(f"plan stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


# ─── Session lifecycle ──────────────────────────────────────────────────────

async def _get_or_start_session(db, prefix: str) -> DashSession:
    """Look up (or lazily start the watcher set for) a session lane."""
    async with _guard:
        sess = _sessions.get(prefix)
        if sess is None:
            sess = DashSession(prefix=prefix, last_activity=time.monotonic())
            sess.tasks = [
                asyncio.create_task(watch_intents(db, sess)),
                asyncio.create_task(watch_compliance(db, sess)),
                asyncio.create_task(poll_telemetry(db, sess)),
                asyncio.create_task(watch_plans(db, sess)),
                asyncio.create_task(live_telemetry_writer(db, sess)),
            ]
            _sessions[prefix] = sess
            log.info(f"session lane started (prefix={prefix!r}, "
                     f"{len(_sessions)} active)")
        return sess


async def _reaper_loop():
    """Tear down a session's watcher tasks once its last tab has been gone
    past the idle TTL, so abandoned sessions don't leak change streams."""
    while True:
        await asyncio.sleep(30)
        now = time.monotonic()
        async with _guard:
            for prefix, sess in list(_sessions.items()):
                if sess.clients:
                    continue
                if now - sess.last_activity < SESSION_IDLE_TTL_SEC:
                    continue
                for t in sess.tasks:
                    t.cancel()
                _sessions.pop(prefix, None)
                log.info(f"session lane reaped (prefix={prefix!r}, "
                         f"{len(_sessions)} remain)")


# ─── FastAPI app ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGO_URI)
    app.state.mongo = client
    app.state.db    = client[DB_NAME]
    reaper = asyncio.create_task(_reaper_loop())
    log.info("Dashboard ready — http://localhost:8060")
    yield
    reaper.cancel()
    async with _guard:
        for sess in _sessions.values():
            for t in sess.tasks:
                t.cancel()
        _sessions.clear()
    await client.close()


app = FastAPI(lifespan=lifespan)

HTML_PATH = Path(__file__).parent / "ibn.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.get("/snapshot/{intent_id}")
async def intent_snapshot(intent_id: str, session: str = ""):
    """Per-intent snapshot for tab switching — scoped to the session lane."""
    db  = app.state.db
    pfx = _prefix(session)
    intent = await db[pfx + "ibn_intents"].find_one({"_id": intent_id})
    if not intent:
        return JSONResponse({"error": "not found"}, status_code=404)
    plan    = await latest_plan_for(db, pfx, intent_id)
    site    = await site_for(db, intent.get("site_id"))
    samples = await recent_telemetry(db, pfx, intent_id)
    events  = await recent_compliance_events(db, pfx, intent_id)
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
    pfx = _prefix(ws.query_params.get("session"))
    session = await _get_or_start_session(app.state.db, pfx)
    session.clients.add(ws)
    session.last_activity = time.monotonic()
    log.info(f"client connected (prefix={pfx!r}, "
             f"{len(session.clients)} tab(s))")

    try:
        snap = await build_snapshot(app.state.db, pfx)
        await ws.send_text(json.dumps(_serializable(snap)))
    except Exception as e:
        log.error(f"snapshot send failed: {e}")

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session.clients.discard(ws)
        session.last_activity = time.monotonic()
        log.info(f"client disconnected (prefix={pfx!r}, "
                 f"{len(session.clients)} tab(s) remain)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8060, log_level="info")
