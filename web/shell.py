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
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pymongo import MongoClient

from agents.orchestrator import OrchestratorAgent
from agents import history as shell_history
from web import seed_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("shell")

clients: set[WebSocket] = set()
_agent: OrchestratorAgent | None = None
# One exclusive operation at a time. A query and a re-seed must never
# overlap: the seed drops the very collections a tool call reads. The
# CLI is implicitly single-user; this lock makes the web shell match it.
_query_lock = asyncio.Lock()

# Phase-A: a single shared demo database, exactly like the CLI seeders.
# Phase-B (MULTI_SESSION_PLAN.md) replaces this with a per-browser-session
# database name resolved from the connection's session token, so one
# user's reset can't wipe another's demo. Centralised here so the reset
# path already routes through a single indirection point.
DEMO_DB = "agent_registry"


def _demo_db_for(session_token: str | None) -> str:
    """Resolve the demo database for a session. Phase-A ignores the token
    and returns the shared DB; the signature is the Phase-B seam."""
    return DEMO_DB


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


# Shell history lives in MongoDB (agent_registry.agent_history). See
# agents/history.py — both terminal and web shells share the same
# collection, no file involved.


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


async def _watch_workstreams():
    """Push a minimal 'workstream_update' message to all clients whenever
    an agent_workstreams doc is inserted or updated. The client uses this
    as a refresh trigger — it re-fetches the full list when the tab is
    visible. We don't try to push the full document here; the list is
    bounded (sort + limit on the read), so re-fetching is cheap and
    correct."""
    from pymongo import AsyncMongoClient
    aclient = AsyncMongoClient(os.environ["MONGODB_URI"])
    coll = aclient["agent_registry"]["agent_workstreams"]
    while True:
        try:
            stream = await coll.watch(full_document="updateLookup")
            async with stream:
                async for change in stream:
                    if change["operationType"] in ("insert", "update", "replace", "delete", "invalidate"):
                        doc = change.get("fullDocument") or {}
                        msg = json.dumps({
                            "type": "workstream_update",
                            "ws_id": doc.get("_id"),
                        })
                        for ws in list(clients):
                            try:
                                await ws.send_text(msg)
                            except Exception:
                                pass
        except Exception as e:
            log.warning(f"workstream stream error ({e}); retrying in 3s")
            await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    _agent = OrchestratorAgent(local_broadcast=_ws_broadcast)
    await _agent.__aenter__()
    app.state.mongo_info = _mongo_info()
    ws_watch_task = asyncio.create_task(_watch_workstreams())
    log.info("Shell ready — http://localhost:8070")
    yield
    ws_watch_task.cancel()
    await asyncio.gather(ws_watch_task, return_exceptions=True)
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
    # Per-connection session token. Phase-A only echoes it (and tags reset
    # logs with it); Phase-B routes each session to its own demo database
    # via _demo_db_for(session_token). Generated server-side now so the
    # protocol and client already carry it.
    session_token = uuid.uuid4().hex[:12]
    log.info(f"client connected — session {session_token} ({len(clients)} total)")

    # Send initial info so the browser can render the banner. `history` is
    # pulled from the agent_history MongoDB collection — the same store
    # the terminal shell (main.py) reads/writes — so cursor-up in the
    # browser walks back through queries typed in either UI on any host.
    info = app.state.mongo_info
    await ws.send_text(json.dumps({
        "type":    "hello",
        "host":    info["host"],
        "indexes": info["indexes"],
        "servers": list(_agent.sessions.keys()) if _agent else [],
        "history": shell_history.read_recent(),
        "session": session_token,
    }))

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "query":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                # Persist to the shared MongoDB history collection so
                # cursor-up works across web + terminal sessions on any
                # host and survives restarts.
                shell_history.append(text, source="web")

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

            elif msg.get("type") == "reset_demo":
                # Re-seed the demo data (IBN + DTW) to its pristine
                # fixture state — the browser equivalent of running both
                # seed scripts with --reset. Destructive: drops and
                # rebuilds the ibn_*/dtw_* collections.
                #
                # Held under _query_lock so it cannot interleave with a
                # query mid-tool-call (which would read half-dropped
                # collections). Phase-A assumes a single user, so this
                # lock is sufficient; Phase-B isolates per session DB.
                if _query_lock.locked():
                    await ws.send_text(json.dumps({
                        "type": "reset_done", "ok": False,
                        "message": "Busy — another query or reset is "
                                   "running. Try again in a moment."}))
                    continue

                async def _emit(line: str):
                    await ws.send_text(json.dumps(
                        {"type": "reset_progress", "line": line}))

                async with _query_lock:
                    db_name = _demo_db_for(session_token)
                    log.info(f"reset_demo — session {session_token} "
                             f"→ db {db_name}")
                    await ws.send_text(json.dumps(
                        {"type": "reset_started"}))
                    try:
                        await seed_runner.reset_and_seed(db_name, _emit)
                        # Fresh data means the agent's in-memory turn
                        # context (conversation tail, current workstream,
                        # sticky domain/service) now points at rows that
                        # no longer exist — clear it so the next query
                        # starts clean. The persisted agent_workstreams /
                        # agent_memories collections are intentionally
                        # left intact (seed --reset never touched them);
                        # whether a reset should also clear those, and
                        # per-session, is a Phase-B decision.
                        if _agent is not None:
                            _agent.conversation_history = []
                            _agent.current_workstream_id = None
                            _agent.last_domain = None
                            _agent.last_service = None
                        await ws.send_text(json.dumps({
                            "type": "reset_done", "ok": True,
                            "message": "Demo data reset to a clean "
                                       "slate. Vector indexes rebuild in "
                                       "~30–90s."}))
                        await ws.send_text(json.dumps({
                            "type":    "server_list",
                            "servers": _agent.list_servers_info()
                                       if _agent else []}))
                    except Exception as e:
                        log.exception("reset_demo failed")
                        await ws.send_text(json.dumps({
                            "type": "reset_done", "ok": False,
                            "message": f"Reset failed: {e}"}))

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

            elif msg.get("type") == "workstreams_request":
                # Read directly from agent_registry.agent_workstreams.
                # The orchestrator writes; the dashboard only reads.
                rows = []
                try:
                    client = MongoClient(os.environ["MONGODB_URI"])
                    cur = (client["agent_registry"]["agent_workstreams"]
                            .find({}, {"_id": 1, "title": 1, "domain": 1,
                                       "state": 1, "entities": 1, "summary": 1,
                                       "last_activity": 1, "opened_at": 1,
                                       "tool_calls": 1,
                                       "memories_extracted": 1,
                                       "memories_extracted_count": 1})
                            .sort("last_activity", -1).limit(50))
                    for d in cur:
                        for k in ("last_activity", "opened_at"):
                            v = d.get(k)
                            if hasattr(v, "isoformat"):
                                d[k] = v.isoformat()
                        # Strip embedded timestamps inside tool_calls for JSON
                        d["tool_calls"] = [{
                            "ts":      (c.get("ts").isoformat()
                                        if hasattr(c.get("ts"), "isoformat")
                                        else c.get("ts")),
                            "service": c.get("service"),
                            "tool":    c.get("tool"),
                            "result":  c.get("result", "")[:200],
                        } for c in (d.get("tool_calls") or [])]
                        rows.append(d)
                    client.close()
                except Exception as e:
                    log.warning(f"workstreams_request failed: {e}")
                await ws.send_text(json.dumps({"type": "workstreams", "list": rows}))

            elif msg.get("type") == "ws_memories_request":
                ws_id = (msg.get("workstream_id") or "").strip()
                mems = []
                if ws_id:
                    try:
                        client = MongoClient(os.environ["MONGODB_URI"])
                        cur = (client["agent_registry"]["agent_memories"]
                               .find({"workstream_id": ws_id},
                                     {"_id": 1, "text": 1, "category": 1,
                                      "confidence": 1, "tier": 1, "recall_count": 1,
                                      "entities": 1, "extracted_at": 1})
                               .sort("_id", 1))
                        for d in cur:
                            ts = d.get("extracted_at")
                            mems.append({
                                "id":          str(d["_id"]),
                                "text":        d.get("text", ""),
                                "category":    d.get("category", ""),
                                "confidence":  d.get("confidence", 0),
                                "tier":        d.get("tier", "extracted"),
                                "recall_count": d.get("recall_count", 0),
                                "entities":    d.get("entities") or [],
                                "extracted_at": ts.isoformat()[:19] if hasattr(ts, "isoformat") else "—",
                            })
                        client.close()
                    except Exception as e:
                        log.warning(f"ws_memories_request failed: {e}")
                await ws.send_text(json.dumps({
                    "type": "ws_memories",
                    "workstream_id": ws_id,
                    "memories": mems,
                }))

            elif msg.get("type") == "command":
                cmd = msg.get("cmd", "")
                if cmd == "status":
                    servers = list(_agent.sessions.keys()) if _agent else []
                    await ws.send_text(json.dumps({"type": "status", "servers": servers}))
                elif cmd in ("memory", "preferences"):
                    memories = []
                    try:
                        client = MongoClient(os.environ["MONGODB_URI"])
                        docs = list(
                            client["agent_registry"]["user_preferences"]
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
