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

import asyncio
import datetime
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pymongo import AsyncMongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("dtw_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

clients: set[WebSocket] = set()


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

async def all_scenarios(db):
    cur = db["dtw_scenarios"].find({}).sort("submitted_at", -1)
    return [d async for d in cur]


async def latest_scenario(db):
    cur = db["dtw_scenarios"].find({}).sort("submitted_at", -1).limit(1)
    docs = [d async for d in cur]
    return docs[0] if docs else None


async def all_markets(db):
    cur = db["dtw_markets"].find({}).sort("_id", 1)
    return [d async for d in cur]


async def build_snapshot(db):
    """Full state package sent on WebSocket connect."""
    scns    = await all_scenarios(db)
    markets = await all_markets(db)
    focused = scns[0] if scns else None
    return {
        "type":      "snapshot",
        "scenarios": [_serializable(s) for s in scns],
        "markets":   [_serializable(m) for m in markets],
        "focused":   _serializable(focused) if focused else None,
    }


# ─── Change-stream watcher ────────────────────────────────────────────────

async def watch_scenarios(db):
    log.info("scenario watcher started")
    coll = db["dtw_scenarios"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] in ("insert", "update", "replace"):
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast({"type": "scenario_update",
                                             "doc": _serializable(doc)})
        except Exception as e:
            log.warning(f"scenario stream error ({e}); retrying in 2s")
            await asyncio.sleep(2)


# ─── FastAPI app ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGO_URI)
    db = client[DB_NAME]
    app.state.mongo = client
    app.state.db    = db
    tasks = [asyncio.create_task(watch_scenarios(db))]
    log.info("Dashboard ready — http://localhost:8080")
    yield
    for t in tasks:
        t.cancel()
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
async def scenario_snapshot(scenario_id: str):
    db = app.state.db
    doc = await db["dtw_scenarios"].find_one({"_id": scenario_id})
    if not doc:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serializable(doc))


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
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
