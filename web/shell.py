#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

# Web shell — browser-based equivalent of main.py / Rich terminal UI.
# Run:  python web/shell.py
# Then: http://localhost:8070

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import datetime
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pymongo import MongoClient

from agents.orchestrator import OrchestratorAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("shell")

clients: set[WebSocket] = set()
_agent: OrchestratorAgent | None = None
_query_lock = asyncio.Lock()  # one query at a time, same as the CLI


async def _ws_broadcast(tag: str, msg: str):
    """Local broadcast callback — forwards orchestrator events to all WS clients."""
    data = json.dumps({"type": "broadcast", "tag": tag, "msg": msg})
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def _mongo_info() -> dict:
    uri = os.environ.get("MONGODB_URI", "")
    parsed = urlparse(uri)
    host = parsed.hostname or "?"
    user = parsed.username or "?"
    vector_idx = []
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        db = client["agent_registry"]
        for coll in db.list_collection_names():
            try:
                for idx in db[coll].list_search_indexes():
                    if idx.get("type") == "vectorSearch":
                        vector_idx.append(f"{coll}.{idx['name']}")
            except Exception:
                pass
        client.close()
    except Exception:
        pass
    return {"host": f"{user}@{host}", "indexes": vector_idx}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    _agent = OrchestratorAgent(local_broadcast=_ws_broadcast)
    await _agent.__aenter__()
    app.state.mongo_info = _mongo_info()
    log.info("Shell ready — http://localhost:8070")
    yield
    await _agent.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)
HTML_PATH = Path(__file__).parent / "shell.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    log.info(f"client connected ({len(clients)} total)")

    # Send initial info so the browser can render the banner
    info = app.state.mongo_info
    await ws.send_text(json.dumps({
        "type":    "hello",
        "host":    info["host"],
        "indexes": info["indexes"],
        "servers": list(_agent.sessions.keys()) if _agent else [],
    }))

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "query":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                async with _query_lock:
                    t0 = time.monotonic()
                    try:
                        await ws.send_text(json.dumps({"type": "thinking", "active": True}))
                        response = await _agent.process_query(text)
                        elapsed = time.monotonic() - t0
                        await ws.send_text(json.dumps({
                            "type":     "response",
                            "markdown": response or "No response.",
                            "elapsed":  round(elapsed, 1),
                        }))
                        # Refresh server list so active state updates in the UI
                        await ws.send_text(json.dumps({
                            "type":    "server_list",
                            "servers": _agent.list_servers_info(),
                        }))
                    except Exception as e:
                        await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
                    finally:
                        await ws.send_text(json.dumps({"type": "thinking", "active": False}))

            elif msg.get("type") == "server_list":
                servers = _agent.list_servers_info() if _agent else []
                await ws.send_text(json.dumps({"type": "server_list", "servers": servers}))

            elif msg.get("type") == "server_add":
                name   = (msg.get("name")        or "").strip()
                desc   = (msg.get("description") or "").strip()
                code   = (msg.get("source_code") or "").strip()
                result = await _agent.add_server(name, desc, code) if _agent else "❌ Agent not ready."
                servers = _agent.list_servers_info() if _agent else []
                await ws.send_text(json.dumps({"type": "server_add_result",
                                               "message": result, "servers": servers}))

            elif msg.get("type") == "server_remove":
                name   = (msg.get("name") or "").strip()
                result = await _agent.remove_server(name) if _agent else "❌ Agent not ready."
                servers = _agent.list_servers_info() if _agent else []
                await ws.send_text(json.dumps({"type": "server_remove_result",
                                               "message": result, "servers": servers}))

            elif msg.get("type") == "command":
                cmd = msg.get("cmd", "")
                if cmd == "status":
                    servers = list(_agent.sessions.keys()) if _agent else []
                    await ws.send_text(json.dumps({"type": "status", "servers": servers}))
                elif cmd == "memory":
                    memories = []
                    try:
                        client = MongoClient(os.environ["MONGODB_URI"])
                        docs = list(
                            client["agent_registry"]["episodic_memories"]
                            .find({}, {"_id": 0, "text": 1, "category": 1,
                                       "createdAt": 1, "is_temporary": 1})
                            .limit(10)
                        )
                        for d in docs:
                            ts = d.get("createdAt")
                            memories.append({
                                "ts":       ts.isoformat()[:19] if isinstance(ts, datetime.datetime) else "—",
                                "text":     d.get("text", ""),
                                "category": d.get("category", ""),
                                "type":     "Temporary" if d.get("is_temporary") else "Permanent",
                            })
                        client.close()
                    except Exception as e:
                        memories = [{"error": str(e)}]
                    await ws.send_text(json.dumps({"type": "memory", "rows": memories}))

    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        log.info(f"client disconnected ({len(clients)} total)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8070, log_level="info")
