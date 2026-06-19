#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

# Live DTW (Digital Twin) demo dashboard — driven by MongoDB Change Streams
# over dtw_scenarios. Shows the result of QoS-uplift and policy-change
# simulations as the simulation service writes them.
#
# Run:  python web/dtw_dashboard.py
# Then: http://localhost:8080
#       http://localhost:8080/?mode=exec   (executive view)
#       http://localhost:8080/?mode=eng    (engineer view, default)
#       http://localhost:8080/?session=<token>   (Phase B: watch one
#         browser session's prefixed dtw_scenarios — the web shell hands
#         out this link with its session token. No token → default lane.)

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pymongo import AsyncMongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("dtw_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

# Phase B: dtw_scenarios is per-session (prefixed s_<token>_); dtw_markets
# is reference data → shared. See MULTI_SESSION_PLAN.md.
_TOKEN_RE = re.compile(r"^[a-z0-9]{8,32}$")
SESSION_IDLE_TTL_SEC = 120


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
    data = json.dumps(_serializable(msg))
    dead = []
    for ws in session.clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session.clients.discard(ws)


# ─── State queries (dtw_scenarios scoped by prefix; markets shared) ─────────

async def all_scenarios(db, pfx):
    cur = db[pfx + "dtw_scenarios"].find({}).sort("submitted_at", -1)
    return [d async for d in cur]


async def all_markets(db):
    cur = db["dtw_markets"].find({}).sort("_id", 1)   # reference → shared
    return [d async for d in cur]


async def build_snapshot(db, pfx):
    """Full state package sent on WebSocket connect."""
    scns    = await all_scenarios(db, pfx)
    markets = await all_markets(db)
    focused = scns[0] if scns else None
    return {
        "type":      "snapshot",
        "scenarios": [_serializable(s) for s in scns],
        "markets":   [_serializable(m) for m in markets],
        "focused":   _serializable(focused) if focused else None,
    }


# ─── Change-stream watcher (one per session) ────────────────────────────────

async def watch_scenarios(db, session: DashSession):
    pfx = session.prefix
    log.info(f"scenario watcher started (prefix={pfx!r})")
    coll = db[pfx + "dtw_scenarios"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] in ("insert", "update", "replace"):
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast(session, {"type": "scenario_update",
                                                      "doc": _serializable(doc)})
        except Exception as e:
            log.warning(f"scenario stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


# ─── Session lifecycle ──────────────────────────────────────────────────────

async def _get_or_start_session(db, prefix: str) -> DashSession:
    async with _guard:
        sess = _sessions.get(prefix)
        if sess is None:
            sess = DashSession(prefix=prefix, last_activity=time.monotonic())
            sess.tasks = [asyncio.create_task(watch_scenarios(db, sess))]
            _sessions[prefix] = sess
            log.info(f"session lane started (prefix={prefix!r}, "
                     f"{len(_sessions)} active)")
        return sess


async def _reaper_loop():
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
    log.info("Dashboard ready — http://localhost:8080")
    yield
    reaper.cancel()
    async with _guard:
        for sess in _sessions.values():
            for t in sess.tasks:
                t.cancel()
        _sessions.clear()
    await client.close()


app = FastAPI(lifespan=lifespan)

HTML_PATH = Path(__file__).parent / "dtw.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    if not HTML_PATH.exists():
        return HTMLResponse(
            "<h1>dtw.html missing</h1><p>Expected at " + str(HTML_PATH) + "</p>",
            status_code=500,
        )
    return HTML_PATH.read_text()


@app.get("/snapshot/{scenario_id}")
async def scenario_snapshot(scenario_id: str, session: str = ""):
    db  = app.state.db
    pfx = _prefix(session)
    doc = await db[pfx + "dtw_scenarios"].find_one({"_id": scenario_id})
    if not doc:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serializable(doc))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    pfx = _prefix(ws.query_params.get("session"))
    session = await _get_or_start_session(app.state.db, pfx)
    session.clients.add(ws)
    session.last_activity = time.monotonic()
    log.info(f"client connected (prefix={pfx!r}, {len(session.clients)} tab(s))")
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
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
