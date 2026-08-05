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
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pymongo import MongoClient

from agents.orchestrator import OrchestratorAgent
from agents import history as shell_history
from web import seed_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("shell")

DEMO_DB = "agent_registry"

# Phase B (MULTI_SESSION_PLAN.md): each browser session gets its own
# OrchestratorAgent with a per-session collection prefix, so one user's
# created intents/scenarios — and their Reset — never touch another's.
# A single shared "bootstrap" orchestrator (prefix="") does the one-time
# global work (registry sync, agent-card sync, filesystem watcher) and
# owns the shared service catalogue; per-session orchestrators reuse it.
MAX_SESSIONS    = int(os.environ.get("DEMO_MAX_SESSIONS", "6"))
SESSION_TTL_SEC = int(os.environ.get("DEMO_SESSION_TTL_SEC", "1800"))  # 30 min idle
_TOKEN_RE = re.compile(r"^[a-z0-9]{8,32}$")

_bootstrap_agent: OrchestratorAgent | None = None
_sessions: dict[str, "Session"] = {}
_sessions_guard = asyncio.Lock()  # serialises create / reap / destroy


@dataclass
class Session:
    token: str
    prefix: str
    orch: OrchestratorAgent
    lock: asyncio.Lock                       # one query/reset at a time per session
    clients: set = field(default_factory=set)
    last_activity: float = 0.0


def _sanitize_token(t: str | None) -> str | None:
    """Accept only our own minted shape so the token is safe to embed in
    collection names; anything else → None (server mints a fresh one)."""
    t = (t or "").strip().lower()
    return t if _TOKEN_RE.match(t) else None


def _make_broadcast(token: str):
    """Per-session live-feed callback — forwards this session's
    orchestrator events only to the browser tabs on that session."""
    async def _bc(tag: str, msg: str):
        sess = _sessions.get(token)
        if not sess:
            return
        data = json.dumps({"type": "broadcast", "tag": tag, "msg": msg})
        for ws in list(sess.clients):
            try:
                await ws.send_text(data)
            except Exception:
                pass
    return _bc


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


def _drop_session_collections(prefix: str) -> None:
    """Drop every per-session collection for a prefix — the mutable demo
    set plus all agent-state planes (workstreams, memories, preferences,
    consult log, analytics). Collection drops within agent_registry are
    authorised (database drops are not — see MULTI_SESSION_PLAN.md), so
    this fully reaps a session's footprint on idle/disconnect."""
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        db = client["agent_registry"]
        bases = (seed_runner.SESSION_MUTABLE_BASES
                 + seed_runner.SESSION_STATE_BASES)
        for base in bases:
            db[prefix + base].drop()
            if base == "ibn_telemetry":
                try:
                    db.drop_collection("system.buckets." + prefix + base)
                except Exception:
                    pass
    finally:
        client.close()


async def _destroy_session_locked(token: str, drop_data: bool) -> None:
    """Tear down a session's orchestrator and (optionally) reap its data.
    Caller must hold _sessions_guard."""
    sess = _sessions.pop(token, None)
    if not sess:
        return
    try:
        await sess.orch.__aexit__(None, None, None)
    except Exception as e:
        log.warning(f"session {token} orchestrator close failed: {e}")
    if drop_data:
        try:
            await asyncio.to_thread(_drop_session_collections, sess.prefix)
        except Exception as e:
            log.warning(f"session {token} data drop failed: {e}")
    log.info(f"session {token} destroyed (drop_data={drop_data}, "
             f"{len(_sessions)} remain)")


async def _reap_idle_locked() -> None:
    """Drop sessions with no connected tabs that have been idle past the
    TTL. Caller must hold _sessions_guard."""
    now = time.monotonic()
    for token, sess in list(_sessions.items()):
        if sess.clients:
            continue
        if now - sess.last_activity < SESSION_TTL_SEC:
            continue
        await _destroy_session_locked(token, drop_data=True)


async def _get_or_create_session(token: str) -> tuple["Session | None", str | None]:
    """Resolve a session by token, creating + seeding its lane on first
    use. Returns (session, error_message)."""
    async with _sessions_guard:
        sess = _sessions.get(token)
        if sess:
            return sess, None
        await _reap_idle_locked()
        if len(_sessions) >= MAX_SESSIONS:
            return None, (f"The demo is at capacity ({MAX_SESSIONS} "
                          f"concurrent sessions). Please try again in a "
                          f"few minutes.")
        prefix = f"s_{token}_"
        # Seed the lane (idempotent) BEFORE the orchestrator starts, so
        # its workstream resume + first query see pristine demo data.
        try:
            seeded = await seed_runner.ensure_session_seeded(prefix, DEMO_DB)
        except Exception as e:
            log.exception("session lane seed failed")
            return None, f"Could not initialise demo data: {e}"
        orch = OrchestratorAgent(local_broadcast=_make_broadcast(token),
                                 demo_prefix=prefix, shared_bootstrap=False)
        await orch.__aenter__()
        sess = Session(token=token, prefix=prefix, orch=orch,
                       lock=asyncio.Lock(), last_activity=time.monotonic())
        _sessions[token] = sess
        log.info(f"session {token} created (lane {'seeded' if seeded else 'reused'}; "
                 f"{len(_sessions)}/{MAX_SESSIONS} active)")
        return sess, None


async def _reaper_loop():
    """Background: periodically reap idle, disconnected sessions."""
    while True:
        await asyncio.sleep(60)
        try:
            async with _sessions_guard:
                await _reap_idle_locked()
        except Exception as e:
            log.warning(f"reaper sweep error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bootstrap_agent
    # Bootstrap orchestrator: prefix="" (the shared default lane), does
    # the one-time global work — registry sync, agent-card sync, the
    # mcp_servers/ filesystem watcher — and owns the shared service
    # catalogue every per-session orchestrator reads. It does not serve
    # browser queries; sessions get their own prefixed orchestrators.
    _bootstrap_agent = OrchestratorAgent(local_broadcast=None,
                                         demo_prefix="", shared_bootstrap=True)
    await _bootstrap_agent.__aenter__()
    app.state.mongo_info = _mongo_info()
    reaper_task = asyncio.create_task(_reaper_loop())
    log.info(f"Shell ready — http://localhost:8070 "
             f"(max {MAX_SESSIONS} sessions, {SESSION_TTL_SEC}s idle TTL)")
    yield
    reaper_task.cancel()
    await asyncio.gather(reaper_task, return_exceptions=True)
    # Tear down all live sessions, then the bootstrap orchestrator. Don't
    # drop session data on shutdown — a restart can resume the lanes.
    async with _sessions_guard:
        for token in list(_sessions):
            await _destroy_session_locked(token, drop_data=False)
    await _bootstrap_agent.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)

HTML_PATH = Path(__file__).parent / "shell.html"
HELP_PATH = Path(__file__).parent / "help.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    """Static walkthrough of the two demo flows (IBN + DTW)."""
    return HELP_PATH.read_text()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session: Session | None = None    # bound by the first `init` message

    async def _send_workstreams():
        """Read this session's (prefixed) workstreams and push them."""
        rows = []
        try:
            client = MongoClient(os.environ["MONGODB_URI"])
            cur = (client["agent_registry"][session.prefix + "agent_workstreams"]
                    .find({}, {"_id": 1, "title": 1, "domain": 1,
                               "state": 1, "entities": 1, "summary": 1,
                               "last_activity": 1, "opened_at": 1,
                               "tool_calls": 1, "memories_extracted": 1,
                               "memories_extracted_count": 1})
                    .sort("last_activity", -1).limit(50))
            for d in cur:
                for k in ("last_activity", "opened_at"):
                    v = d.get(k)
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
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

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            typ = msg.get("type")

            # ── Session handshake: the browser sends `init` with its
            # stored token (or none) as the first message. The server
            # resolves/mints the token, creates+seeds the session lane,
            # and replies with `hello`. ────────────────────────────────
            if typ == "init":
                token = _sanitize_token(msg.get("token")) or uuid.uuid4().hex[:12]
                session, err = await _get_or_create_session(token)
                if err:
                    await ws.send_text(json.dumps({"type": "error", "message": err}))
                    await ws.close()
                    return
                session.clients.add(ws)
                session.last_activity = time.monotonic()
                log.info(f"client bound to session {token} "
                         f"({len(session.clients)} tab(s))")
                info = app.state.mongo_info
                await ws.send_text(json.dumps({
                    "type":    "hello",
                    "host":    info["host"],
                    "indexes": info["indexes"],
                    "servers": list(session.orch.sessions.keys()),
                    "history": shell_history.read_recent(prefix=session.prefix),
                    "session": token,
                    "ibn_url": os.environ.get("IBN_DASHBOARD_URL", ""),
                    "dtw_url": os.environ.get("DTW_DASHBOARD_URL", ""),
                }))
                continue

            if session is None:
                # Ignore everything until the session is established.
                continue
            session.last_activity = time.monotonic()
            agent = session.orch

            if typ == "query":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                shell_history.append(text, source="web", prefix=session.prefix)
                async with session.lock:
                    t0 = time.monotonic()
                    try:
                        await ws.send_text(json.dumps({"type": "thinking", "active": True}))
                        response = await agent.process_query(text)
                        elapsed = time.monotonic() - t0
                        await ws.send_text(json.dumps({
                            "type":     "response",
                            "markdown": response or "No response.",
                            "elapsed":  round(elapsed, 1),
                        }))
                        await ws.send_text(json.dumps({
                            "type":    "server_list",
                            "servers": agent.list_servers_info(),
                        }))
                        # The global change-stream watcher is gone (it
                        # couldn't be session-scoped); nudge this tab to
                        # refresh its own workstream list instead.
                        await ws.send_text(json.dumps(
                            {"type": "workstream_update"}))
                    except Exception as e:
                        await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
                    finally:
                        await ws.send_text(json.dumps({"type": "thinking", "active": False}))

            elif typ == "reset_demo":
                # Re-seed ONLY this session's mutable collections to their
                # pristine fixtures — other sessions are untouched. Held
                # under the session lock so it can't interleave with this
                # session's own query mid-tool-call.
                if session.lock.locked():
                    await ws.send_text(json.dumps({
                        "type": "reset_done", "ok": False,
                        "message": "Busy — another query or reset is "
                                   "running. Try again in a moment."}))
                    continue

                async def _emit(line: str):
                    await ws.send_text(json.dumps(
                        {"type": "reset_progress", "line": line}))

                async with session.lock:
                    log.info(f"reset_demo — session {session.token} "
                             f"(prefix {session.prefix})")
                    await ws.send_text(json.dumps({"type": "reset_started"}))
                    try:
                        await seed_runner.reset_session(
                            session.prefix, DEMO_DB, _emit)
                        # The session's data was just reset; clear the
                        # orchestrator's in-memory turn context so the next
                        # query starts clean.
                        agent.conversation_history = []
                        agent.current_workstream_id = None
                        agent.last_domain = None
                        agent.last_service = None
                        await ws.send_text(json.dumps({
                            "type": "reset_done", "ok": True,
                            "message": "Your demo data has been reset to a "
                                       "clean slate. Other sessions are "
                                       "unaffected."}))
                        await ws.send_text(json.dumps({
                            "type": "server_list",
                            "servers": agent.list_servers_info()}))
                        await ws.send_text(json.dumps(
                            {"type": "workstream_update"}))
                    except Exception as e:
                        log.exception("reset_demo failed")
                        await ws.send_text(json.dumps({
                            "type": "reset_done", "ok": False,
                            "message": f"Reset failed: {e}"}))

            elif typ == "server_list":
                await ws.send_text(json.dumps({
                    "type": "server_list", "servers": agent.list_servers_info()}))

            elif typ == "server_add":
                # MCP servers are a SHARED resource (one filesystem, one
                # mcp_services catalogue) — route through the bootstrap
                # orchestrator so every session sees the new server.
                name   = (msg.get("name")        or "").strip()
                desc   = (msg.get("description") or "").strip()
                code   = (msg.get("source_code") or "").strip()
                boot   = _bootstrap_agent
                result = await boot.add_server(name, desc, code) if boot else "❌ Not ready."
                await ws.send_text(json.dumps({
                    "type": "server_add_result", "message": result,
                    "servers": agent.list_servers_info()}))

            elif typ == "server_remove":
                name   = (msg.get("name") or "").strip()
                boot   = _bootstrap_agent
                result = await boot.remove_server(name) if boot else "❌ Not ready."
                await ws.send_text(json.dumps({
                    "type": "server_remove_result", "message": result,
                    "servers": agent.list_servers_info()}))

            elif typ == "workstreams_request":
                await _send_workstreams()

            elif typ == "ws_memories_request":
                ws_id = (msg.get("workstream_id") or "").strip()
                mems = []
                if ws_id:
                    try:
                        client = MongoClient(os.environ["MONGODB_URI"])
                        cur = (client["agent_registry"][session.prefix + "agent_memories"]
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

            elif typ == "command":
                cmd = msg.get("cmd", "")
                if cmd == "status":
                    await ws.send_text(json.dumps({
                        "type": "status", "servers": list(agent.sessions.keys())}))
                elif cmd in ("memory", "preferences"):
                    # Per-session: read this session's own preferences lane.
                    memories = []
                    try:
                        client = MongoClient(os.environ["MONGODB_URI"])
                        docs = list(
                            client["agent_registry"][session.prefix + "user_preferences"]
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
        if session is not None:
            session.clients.discard(ws)
            session.last_activity = time.monotonic()  # start the idle clock
            log.info(f"client left session {session.token} "
                     f"({len(session.clients)} tab(s) remain)")


if __name__ == "__main__":
    import uvicorn
    # Behind nginx, set DEMO_BIND_HOST=127.0.0.1 so the port isn't
    # directly reachable (and the Basic-Auth gate can't be bypassed).
    uvicorn.run(app, host=os.environ.get("DEMO_BIND_HOST", "0.0.0.0"),
                port=8070, log_level="info")
