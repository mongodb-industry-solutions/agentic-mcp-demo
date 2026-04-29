#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

# Live portfolio dashboard — real-time via MongoDB Change Streams + WebSocket.
# Run:  python web/portfolio_dashboard.py
# Then: http://localhost:8050

import asyncio
import json
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pymongo import AsyncMongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("portfolio_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME = "agent_registry"
COLL_NAME = "portfolio"

# ── Connected WebSocket clients ──────────────────────────────────────────────

clients: set[WebSocket] = set()


async def broadcast(msg: dict):
    """Send a JSON message to every connected client."""
    data = json.dumps(msg, default=str)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# ── MongoDB helpers ──────────────────────────────────────────────────────────

def doc_to_dict(doc: dict) -> dict:
    """Make a MongoDB document JSON-serialisable."""
    d = dict(doc)
    d["_id"] = str(d["_id"])
    if "addedAt" in d:
        d["addedAt"] = d["addedAt"].isoformat() if hasattr(d["addedAt"], "isoformat") else str(d["addedAt"])
    return d


async def fetch_all(coll) -> list[dict]:
    """Return all portfolio positions, sorted by name."""
    cursor = coll.find({}).sort("name", 1)
    return [doc_to_dict(doc) async for doc in cursor]


# ── Change Stream watcher ────────────────────────────────────────────────────

async def watch_changes(coll):
    """Watch the portfolio collection and push changes to all WS clients."""
    log.info("Change stream watcher started")
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    op = change["operationType"]
                    log.info(f"Change detected: {op}")

                    if op in ("insert", "update", "replace"):
                        doc = change.get("fullDocument")
                        if doc:
                            await broadcast({
                                "type": "upsert",
                                "doc": doc_to_dict(doc),
                            })
                    elif op == "delete":
                        doc_id = str(change["documentKey"]["_id"])
                        await broadcast({
                            "type": "delete",
                            "id": doc_id,
                        })
                    elif op == "invalidate":
                        rows = await fetch_all(coll)
                        await broadcast({"type": "snapshot", "rows": rows})
        except Exception as e:
            log.warning(f"Change stream error ({e}), reconnecting in 2s...")
            await asyncio.sleep(2)


# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGO_URI)
    app.state.mongo = client
    coll = client[DB_NAME][COLL_NAME]
    app.state.coll = coll

    watcher = asyncio.create_task(watch_changes(coll))
    log.info("Dashboard ready — http://localhost:8050")
    yield
    watcher.cancel()
    await client.close()


app = FastAPI(lifespan=lifespan)

HTML_PATH = Path(__file__).parent / "portfolio.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.get("/api/portfolio")
async def api_portfolio():
    rows = await fetch_all(app.state.coll)
    return rows


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    log.info(f"Client connected ({len(clients)} total)")

    # Send current snapshot immediately
    try:
        rows = await fetch_all(app.state.coll)
        log.info(f"Sending snapshot with {len(rows)} rows")
        await ws.send_text(json.dumps({"type": "snapshot", "rows": rows}, default=str))
    except Exception as e:
        log.error(f"Failed to send snapshot: {e}")

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        log.info(f"Client disconnected ({len(clients)} total)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8050, log_level="info")
