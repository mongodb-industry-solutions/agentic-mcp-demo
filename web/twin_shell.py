#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

# The web shell on Atlas Agent Engine, for the twin (AGENT_ENGINE_PLAN.md,
# Phase 8a). The same page as web/shell.py, but a turn goes to the deployed
# noc-coordinator through `agentengine invoke` with the presenter's CLI
# login, and the Agent Log comes from the platform's traces on the agents'
# cluster (mdb_store_<project>.traces, a change stream): the coordinator's
# routing, its A2A delegations, every tool call of the domain agents.
#
# NOC approvals: a blast-radius action pauses the domain agent that the
# coordinator delegated to (A2A can't resume a child). Its review doc in
# agent_approvals names the paused execution and session; a change stream
# turns it into an approval card, and the decision resumes that session with
# `agentengine invoke --resume`, keyed by the interrupt id from the
# platform's GET /executions/{id}.
#
# Single presenter, default lane, like the twin dashboards: the CLI can't
# send the demo-prefix header, and the invoke runs as the presenter. So it
# only serves loopback clients. Run it with bin/twin.sh start (:8170).
# web/shell.py, the Kanopy shell, doesn't import this module.

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import datetime
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pymongo import MongoClient

from web import seed_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("twin_shell")

DEMO_DB = "agent_registry"
ROOT = Path(__file__).resolve().parent.parent

# The deployed coordinator in JeffN (explicit, so the CLI's current context
# doesn't matter; the same defaults as agent-engine/autopilot.py).
PROJECT_ID = os.environ.get("TWIN_AE_PROJECT_ID", "6aa4558bb19c96d52970c253")
TARGET = {
    "--base-url": os.environ.get("TWIN_AE_BASE_URL", "https://agentengine.mongodb.com"),
    "--org-id": os.environ.get("TWIN_AE_ORG_ID", "6a99bbc5fa6869c45dee52b6"),
    "--project-id": PROJECT_ID,
    "--workspace-id": os.environ.get("TWIN_COORDINATOR_WORKSPACE_ID", "ws-6ad1060d6189a50fdc472a65"),
}
INVOKE_TIMEOUT = "10m"
AUTH_RACE = ("session expired", "refresh token")   # see autopilot.py
AUTH_RETRIES, AUTH_RETRY_S = 2, 3
BUSY = "currently busy"     # 503: every sandbox is held (or the runtime is rolling)
BUSY_RETRIES, BUSY_RETRY_S = 3, 5
# The CLI couldn't reach the platform (its workspace lookup, or DNS) and
# sent nothing, so repeating is safe, also for a resume.
UNREACHABLE = ("resolve workspace features", "nodename nor servname", "no such host")
NET_RETRIES, NET_RETRY_S = 2, 10
AGENTS = ["noc-coordinator", "ibn-agent", "dtw-agent"]
AGENT_WORKSPACES = {   # for the approval cards; the domain agents pause
    os.environ.get("TWIN_IBN_WORKSPACE_ID", "ws-6ad0efe66189a50fdc461f8d"): "ibn-agent",
    os.environ.get("TWIN_DTW_WORKSPACE_ID", "ws-6ad0efe7ea67bce171583766"): "dtw-agent",
    TARGET["--workspace-id"]: "noc-coordinator",
}
_IBN_WS, _DTW_WS = (next(w for w, a in AGENT_WORKSPACES.items() if a == n)
                    for n in ("ibn-agent", "dtw-agent"))
# The agent that owns each reviewed action (oss_tools.approvals): the review
# doc names the paused execution and session, but the tool sandbox doesn't
# know its workspace id (get_current_workspace_id() is empty there).
ACTION_WORKSPACE = {
    "activate_plan": _IBN_WS, "apply_runbook": _IBN_WS, "update_template_version": _IBN_WS,
    "cancel_intent": _IBN_WS, "delete_scenario": _DTW_WS, "delete_all_scenarios": _DTW_WS,
}
# The CLI login's token, for the one call the CLI has no command for:
# GET /executions/{id} (the interrupt id of a paused run).
AUTH_FILE = Path.home() / ".agentengine" / "auth.json"

# Our three workspaces and the subscriber-id patterns come from the
# guardrail (Phase 6). It redacts the agents' answers; the log shows tool
# results, which it never sees, so the log redacts them the same way.
_GUARDRAIL = json.loads((ROOT / "agent-engine/guardrails/subscriber-ids.json").read_text())
WORKSPACES = set(_GUARDRAIL["workspace_ids"])
_REDACT = [re.compile(p["value"]) for p in _GUARDRAIL["config"]["match_patterns"]]

# The coordinator's internal tools; its routing comes from the route span.
_INTERNAL_TOOLS = {"record_routing", "rank_agents", "runbooks_for_memory", "pending_reviews"}
_REORDER_S = 1.0   # spans arrive in batches; emit them in end-time order

_clients: set[WebSocket] = set()
_agent_names: dict[str, str] = {}   # workspace id → agent, learned from the spans
# A resumed run replays its earlier steps, and each replayed tool call is
# traced again (same execution, tool, input and output, no call id): logged
# once. Keyed per execution, the oldest dropped first.
_seen_tools: dict[str, set] = {}
_SEEN_EXECUTIONS = 200
_turn_lock = asyncio.Lock()          # one turn (or reset) at a time
_ae_session: str | None = None       # the coordinator session, reused across turns


def _redact(text: str) -> str:
    for p in _REDACT:
        text = p.sub("[BLOCKED]", text)
    return text


def _preview(text: str, max_lines: int = 3, max_chars: int = 250) -> str:
    """The legacy shell's RESULT preview (mcp_pool._format_result_preview)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    result = " │ ".join(lines[:max_lines])
    if len(result) > max_chars:
        return result[:max_chars - 1] + "…"
    return result + (" …" if len(lines) > max_lines else "")


async def _send_all(msg: dict) -> None:
    data = json.dumps(msg)
    for ws in list(_clients):
        try:
            await ws.send_text(data)
        except Exception:
            _clients.discard(ws)


async def _send(ws: WebSocket, msg: dict) -> None:
    """To one page; a page that has closed meanwhile is dropped."""
    try:
        await ws.send_text(json.dumps(msg))
    except Exception:
        _clients.discard(ws)


async def _broadcast(tag: str, msg: str) -> None:
    await _send_all({"type": "broadcast", "tag": tag, "msg": msg})


# ── Agent Log from the traces ─────────────────────────────────────────────

def _json(value) -> dict:
    try:
        v = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _replayed(span: dict) -> bool:
    attrs = span.get("attributes") or {}
    if attrs.get("openinference.span.kind") != "TOOL":
        return False
    execution = span.get("execution_id") or attrs.get("execution.id")
    if not execution:
        return False
    seen = _seen_tools.setdefault(execution, set())
    if len(_seen_tools) > _SEEN_EXECUTIONS:
        _seen_tools.pop(next(iter(_seen_tools)))
    key = (attrs.get("tool.name"), attrs.get("input.value"), attrs.get("output.value"))
    if key in seen:
        return True
    seen.add(key)
    return False


def _span_lines(span: dict) -> list[tuple[str, str]]:
    """(tag, text) Agent Log lines for one finished span, in the legacy
    shell's vocabulary."""
    attrs, agent = span.get("attributes") or {}, (span.get("resource") or {}).get("service.name", "?")
    secs = (span.get("duration_ns") or 0) / 1e9
    failed = (span.get("status") or {}).get("code") == "ERROR"
    name = span.get("name")
    if name == "route":
        turn = _json(attrs.get("output.value")).get("turn") or {}
        stage1 = turn.get("stage1") or {}
        lines = []
        if stage1:
            domains = ", ".join(stage1.get("domains_selected") or []) or "none"
            lines.append(("ROUTING", f"Stage 1 → {domains} ({stage1.get('method')})"))
        mem = turn.get("memory") or {}
        if mem.get("recalled_chars"):
            lines.append(("MEMORY", f"Platform memory recalled ({mem['recalled_chars']:,} chars)"))
        if mem.get("preference_saved"):
            lines.append(("MEMORY", f"Preference saved: {mem['preference_saved']}"))
        ranked = (turn.get("agent_cards") or {}).get("ranked") or []
        if ranked:
            lines.append(("ROUTING", "Agent cards ($vectorSearch): "
                          + ", ".join(f"{r['agent']} {r['score']:.3f}" for r in ranked)))
        subtasks = (turn.get("multi") or {}).get("subtasks") or {}
        for a in turn.get("dispatched_agents") or []:
            lines.append(("DISPATCH", f"noc-coordinator → {a} (A2A)"
                          + (f": {subtasks[a]}" if a in subtasks else "")))
        return lines
    if name == "a2a.invoke_agent" and agent != "noc-coordinator":
        target = _agent_names.get(attrs.get("a2a.target_agent_id"), "another agent")
        return [("DISPATCH", f"[{agent}] → {target} (A2A consult, {secs:.1f} s)")]
    if attrs.get("openinference.span.kind") == "AGENT" and name == "aer.execute":
        tokens = attrs.get("llm.token_count.total")
        return [("ERROR" if failed else "AGENT",
                 f"[{agent}] {'failed' if failed else 'finished'} in {secs:.1f} s"
                 + (f" · {int(tokens):,} tokens" if tokens else ""))]
    tool = attrs.get("tool.name")
    if attrs.get("openinference.span.kind") == "TOOL" and tool and tool not in _INTERNAL_TOOLS:
        if tool == "delegate":
            return []   # the route span and the child's own spans tell it
        out = _json(attrs.get("output.value"))
        content = str((out.get("data") or {}).get("content", "")) or attrs.get("output.value", "")
        lines = [("ACTION", f"  [{agent}] {tool} ({secs:.1f} s)")]
        if content:
            lines.append(("ERROR" if failed else "RESULT", _preview(str(content))))
        return lines
    return []


_SPAN_FILTER = [{"$match": {"operationType": "insert", "$or": [
    {"fullDocument.name": {"$in": ["route", "a2a.invoke_agent", "aer.execute"]}},
    {"$expr": {"$eq": [{"$getField": {"field": "openinference.span.kind",
                                      "input": "$fullDocument.attributes"}}, "TOOL"]}},
]}}]


def _watch(db: str, coll: str, pipeline: list, on_doc, stop: threading.Event) -> None:
    """Blocking change stream (a thread): on_doc(fullDocument) per change."""
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        while not stop.is_set():
            try:
                with client[db][coll].watch(pipeline, full_document="updateLookup",
                                            max_await_time_ms=1000) as stream:
                    while not stop.is_set():
                        change = stream.try_next()
                        if change is not None and change.get("fullDocument"):
                            on_doc(change["fullDocument"])
            except Exception as e:
                log.warning(f"{coll} watch: {e}; retrying")
                stop.wait(3)
    finally:
        client.close()


def _watch_traces(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, stop: threading.Event) -> None:
    def on_span(span: dict) -> None:
        if (span.get("resource") or {}).get("agentic_platform.workspace_id") in WORKSPACES:
            loop.call_soon_threadsafe(queue.put_nowait, span)
    _watch(f"mdb_store_{PROJECT_ID}", "traces", _SPAN_FILTER, on_span, stop)


async def _log_pump(queue: asyncio.Queue) -> None:
    """Spans → Agent Log lines, reordered by end time within a short window."""
    while True:
        batch = [await queue.get()]
        deadline = time.monotonic() + _REORDER_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                batch.append(await asyncio.wait_for(queue.get(), left))
            except TimeoutError:
                break
        for span in batch:
            res = span.get("resource") or {}
            _agent_names[res.get("agentic_platform.workspace_id")] = res.get("service.name", "?")
        for span in sorted(batch, key=lambda s: s.get("end_time_ns") or 0):
            if _replayed(span):
                continue
            try:
                for tag, text in _span_lines(span):
                    await _broadcast(tag, _redact(text))
            except Exception as e:
                log.warning(f"span {span.get('name')}: {e}")


# ── Turns ─────────────────────────────────────────────────────────────────

async def _invoke(text: str | None, session_id: str | None, workspace_id: str | None = None,
                  resume: dict | None = None) -> dict:
    """One `agentengine invoke`: a message (stdin) to the coordinator, or a
    resume_map for a paused session of `workspace_id`."""
    cmd = ["agentengine", "invoke", "--json", "--quiet", "--timeout", INVOKE_TIMEOUT]
    for flag, value in {**TARGET, **({"--workspace-id": workspace_id} if workspace_id else {})}.items():
        cmd += [flag, value]
    if session_id:
        cmd += ["--session", session_id]
    if resume:
        cmd += ["--resume", json.dumps(resume)]
    auth_left, busy_left, net_left = AUTH_RETRIES, BUSY_RETRIES, NET_RETRIES
    while True:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL if resume else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate(None if resume else (text or "").encode())
        try:
            out = json.loads(stdout)
        except ValueError:
            text_err = (stderr or stdout).decode(errors="replace").strip()
            # The CLI's own "Error: …" line, not its hints and flag echo.
            lines = [ln for ln in text_err.splitlines() if ln.startswith("Error:")]
            out = {"success": False, "error": (lines[-1] if lines else text_err)[-500:]}
        err = out.get("error") or ""
        if auth_left and any(s in err for s in AUTH_RACE):
            auth_left -= 1
            await asyncio.sleep(AUTH_RETRY_S)
            continue
        if busy_left and BUSY in err:
            busy_left -= 1
            await _broadcast("INFO", f"Agent Engine is at capacity; retrying in {BUSY_RETRY_S} s")
            await asyncio.sleep(BUSY_RETRY_S)
            continue
        if net_left and any(s in err for s in UNREACHABLE):
            net_left -= 1
            await _broadcast("INFO", f"Can't reach Agent Engine; retrying in {NET_RETRY_S} s")
            await asyncio.sleep(NET_RETRY_S)
            continue
        out["ok"] = proc.returncode == 0 and str(out.get("success")).lower() == "true"
        return out


async def _turn(ws: WebSocket, text: str) -> None:
    global _ae_session
    await _broadcast("QUERY", text[:300])
    await _broadcast("AGENT", "→ noc-coordinator on Atlas Agent Engine"
                     + (f" (session {_ae_session[:8]}…)" if _ae_session else " (new session)"))
    t0 = time.monotonic()
    out = await _invoke(text, _ae_session)
    if not out["ok"] and _ae_session and "session" in (out.get("error") or "").lower():
        await _broadcast("INFO", f"Session {_ae_session[:8]}… is gone; starting a new one")
        _ae_session = None
        out = await _invoke(text, None)
    elapsed = round(time.monotonic() - t0, 1)
    if not out["ok"]:
        err = out.get("error") or "the invoke failed"
        await _broadcast("ERROR", err[:300])
        await ws.send_text(json.dumps({"type": "error", "message": f"Agent Engine: {err}"}))
        return
    _ae_session = out.get("session_id") or _ae_session
    await ws.send_text(json.dumps({"type": "response", "elapsed": elapsed,
                                   "markdown": out.get("response") or "No response."}))
    await _broadcast("INFO", f"✓ noc-coordinator answered in {elapsed} s "
                             f"(execution {str(out.get('execution_id'))[:8]}…)")


# ── NOC approvals ─────────────────────────────────────────────────────────

_PAUSED = {"suspended", "resuming"}


def _approvals(client: MongoClient):
    return client[DEMO_DB]["agent_approvals"]   # the default lane


def _workspace(doc: dict) -> str | None:
    return doc.get("workspace_id") or ACTION_WORKSPACE.get(doc.get("action"))


def _card(doc: dict) -> dict:
    """An agent_approvals doc as the page's approval card."""
    card = {k: doc.get(k) for k in (
        "action", "title", "summary", "details", "blast_radius", "allowed_decisions", "canary",
        "status", "decision", "reviewer_notes", "decided_by")}
    card["approval_id"] = doc["_id"]
    card["agent"] = AGENT_WORKSPACES.get(_workspace(doc), _workspace(doc) or "?")
    card["resumable"] = bool(doc.get("session_id") and doc.get("execution_id"))
    return json.loads(_redact(json.dumps(card, default=str)))


def _pending_cards() -> list[dict]:
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        return [_card(d) for d in _approvals(client).find({"status": "pending"}).sort("seq", 1)]
    finally:
        client.close()


def _watch_approvals(loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
    def on_doc(doc: dict) -> None:
        card = _card(doc)
        asyncio.run_coroutine_threadsafe(_send_all({"type": "approval", "review": card}), loop)
        if card["status"] == "pending":
            text = f"⏸ {card['approval_id']} awaits NOC approval on {card['agent']}: {card['title']}"
        elif card["status"] == "expired":
            text = f"⌛ {card['approval_id']} expired ({card['reviewer_notes']}): {card['title']}"
        elif card["decided_by"] not in (None, "noc"):
            text = f"✓ {card['approval_id']} approved by {card['decided_by']}: {card['title']}"
        else:
            return
        asyncio.run_coroutine_threadsafe(_broadcast("AGENT", text), loop)
    _watch(DEMO_DB, "agent_approvals", [{"$match": {"operationType": {"$in": ["insert", "update"]}}}],
           on_doc, stop)


async def _api(method: str, path: str, *, timeout: float = 20, **kw) -> httpx.Response:
    """An Agent Engine API call with the CLI login's token (a 401 makes the
    CLI refresh it, then the call is repeated once)."""
    url = f"{TARGET['--base-url']}/api/v1/projects/{PROJECT_ID}{path}"
    headers = kw.pop("headers", {})
    for attempt in range(2):
        creds = json.loads(await asyncio.to_thread(AUTH_FILE.read_text))["credentials"]
        token = creds[TARGET["--base-url"]]["access_token"]
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"},
                                     **kw)
        if r.status_code != 401 or attempt:
            return r
        proc = await asyncio.create_subprocess_exec(
            "agentengine", "status", "--json", "--quiet",
            *[a for f, v in TARGET.items() for a in (f, v)],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    return r


async def _execution(execution_id: str, workspace_id: str) -> dict:
    """GET /executions/{id}."""
    r = await _api("GET", f"/executions/{execution_id}", params={"workspace_id": workspace_id})
    if r.status_code == 404:
        return {"status": "gone"}
    r.raise_for_status()
    ex = r.json().get("execution")
    return _json(ex) if isinstance(ex, str) else (ex or {})


def _interrupt_id(execution: dict, approval_id: str) -> str | None:
    """The stable interrupt id (suspend_context.interrupts[].id) whose
    payload is this review."""
    items = (execution.get("suspend_context") or {}).get("interrupts") or []
    for it in items:
        if _json(it.get("value")).get("approval_id") == approval_id:
            return it.get("id")
    return items[0].get("id") if len(items) == 1 and not _json(items[0].get("value")) else None


def _expire(approval_id: str, status: str) -> None:
    """A review whose paused run is gone: close it and clear its markers."""
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        coll = _approvals(client)
        doc = coll.find_one_and_update(
            {"_id": approval_id, "status": "pending"},
            {"$set": {"status": "expired", "decided_by": "twin-shell",
                      "reviewer_notes": f"the paused run is {status}",
                      "decided_at": datetime.datetime.now()}})
        for name, ids in ((doc or {}).get("targets") or {}).items():
            if ids:
                client[DEMO_DB][name].update_many({"_id": {"$in": ids}},
                                                  {"$unset": {"pending_approval": ""}})
    finally:
        client.close()


async def _decide(approval_id: str, decision: str, notes: str) -> None:
    """Resume the paused agent with the NOC decision."""
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        doc = await asyncio.to_thread(_approvals(client).find_one, {"_id": approval_id})
    finally:
        client.close()
    if not doc or doc.get("status") != "pending":
        raise ValueError(f"{approval_id} is not pending any more")
    if decision not in (doc.get("allowed_decisions") or []):
        raise ValueError(f"{decision!r} isn't allowed for {approval_id}")
    workspace_id = _workspace(doc)
    if not (doc.get("execution_id") and workspace_id and doc.get("session_id")):
        raise ValueError(f"{approval_id} doesn't name the paused run")
    agent = AGENT_WORKSPACES.get(workspace_id, workspace_id)
    await _broadcast("ACTION", f"NOC decision: {decision} {approval_id}"
                     + (f" ({notes})" if notes else "") + f" → resuming {agent}")
    execution = await _execution(doc["execution_id"], workspace_id)
    status = execution.get("status") or "unknown"
    if status in ("pending", "running"):
        raise ValueError(f"{agent} is still running ({status}); decide once it has paused")
    if status not in _PAUSED:
        await asyncio.to_thread(_expire, approval_id, status)
        raise ValueError(f"the paused run behind {approval_id} is {status}: it can't be resumed. "
                         "Ask again to get a new review.")
    interrupt_id = _interrupt_id(execution, approval_id)
    if not interrupt_id:
        raise ValueError(f"no interrupt for {approval_id} on execution {doc['execution_id'][:8]}…")
    t0 = time.monotonic()
    # The CLI resumes (its exact request is what the platform accepts) but
    # can't resend the run's custom headers; the graph keeps its path
    # without them (telco_runtime.a2a.message_depth). The lane header is
    # lost too, which is fine for the twin's default lane.
    out = await _invoke(None, doc["session_id"], workspace_id=workspace_id,
                        resume={interrupt_id: {"decision": decision, "reviewer_notes": notes}})
    elapsed = round(time.monotonic() - t0, 1)
    if not out["ok"]:
        err = f"resume failed: {out.get('error') or 'the invoke failed'}"
        # A resume the runtime rejects (a replay conflict) fails the run:
        # expire the review now, not on the next click.
        status = "unknown"
        try:
            for _ in range(3):
                await asyncio.sleep(2)
                status = (await _execution(doc["execution_id"], workspace_id)).get("status") or "unknown"
                if status not in ("pending", "running", "resuming"):
                    break
        except Exception as e:
            log.warning(f"{approval_id}: execution status after the failed resume: {e}")
        if status not in _PAUSED | {"pending", "running", "unknown"}:
            await asyncio.to_thread(_expire, approval_id, status)
            err += f". The run is {status} now, so {approval_id} has expired; ask again for a new review."
        raise ValueError(err)
    answer = out.get("response") or (f"⏸ {agent} paused again for a NOC review."
                                     if out.get("suspend_context") else "No response.")
    # To every page: the cards are shared, and the deciding page may be gone.
    await _send_all({"type": "response", "elapsed": elapsed, "markdown": answer,
                     "title": f"🤖 {agent} after {approval_id} ({decision})"})
    await _broadcast("INFO", f"✓ {agent} resumed after {approval_id} in {elapsed} s")


def _history_coll(client: MongoClient):
    # The default lane's agent_history on the twin (Reset clears it). Not
    # agents/history.py: that one imports the legacy terminal history file
    # into an empty collection.
    return client[DEMO_DB]["agent_history"]


def _history(limit: int = 500) -> list[str]:
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        return [d["text"] for d in _history_coll(client).find({}, {"_id": 0, "text": 1})
                .sort("_id", -1).limit(limit) if d.get("text")]
    finally:
        client.close()


def _remember(text: str) -> None:
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        coll = _history_coll(client)
        last = coll.find_one({}, {"_id": 0, "text": 1}, sort=[("_id", -1)])
        if not last or last.get("text") != text:
            coll.insert_one({"text": text, "source": "twin-shell", "ts": datetime.datetime.now()})
    finally:
        client.close()


def _mongo_info() -> dict:
    parsed = urlparse(os.environ.get("MONGODB_URI", ""))
    indexes = []
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=3000)
    try:
        db = client[DEMO_DB]
        for coll in db.list_collection_names():
            try:
                indexes += [f"{coll}.{i['name']}" for i in db[coll].list_search_indexes()
                            if i.get("type") == "vectorSearch"]
            except Exception:
                pass
    except Exception:
        pass
    finally:
        client.close()
    return {"host": f"{parsed.username or '?'}@{parsed.hostname or '?'}", "indexes": indexes}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mongo_info = await asyncio.to_thread(_mongo_info)
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_traces, args=(asyncio.get_running_loop(), queue, stop),
                               daemon=True)
    watcher.start()
    threading.Thread(target=_watch_approvals, args=(asyncio.get_running_loop(), stop),
                     daemon=True).start()
    pump = asyncio.create_task(_log_pump(queue))
    log.info("Twin shell ready on Atlas Agent Engine (noc-coordinator)")
    yield
    stop.set()
    pump.cancel()
    await asyncio.gather(pump, return_exceptions=True)


app = FastAPI(lifespan=lifespan)
HTML_PATH = Path(__file__).parent / "shell.html"
HELP_PATH = Path(__file__).parent / "help.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    return HELP_PATH.read_text()


def _agent_rows() -> list[dict]:
    return [{"name": a, "active": True, "origin": "engine"} for a in AGENTS]


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Every turn runs as the presenter's CLI login: loopback only.
    if not ws.client or ws.client.host not in ("127.0.0.1", "::1"):
        await ws.close(code=1008)
        return
    await ws.accept()
    _clients.add(ws)
    global _ae_session
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            typ = msg.get("type")

            if typ == "init":
                info = app.state.mongo_info
                # The page works without its history and cards (the cluster
                # unreachable, DNS): the chat still goes to the coordinator.
                try:
                    history = await asyncio.to_thread(_history)
                    cards = await asyncio.to_thread(_pending_cards)
                except Exception as e:
                    log.warning(f"init: {e}")
                    history, cards = [], []
                await ws.send_text(json.dumps({
                    "type": "hello", "runtime": "agent_engine", "session": "twin",
                    "host": info["host"], "indexes": info["indexes"], "servers": AGENTS,
                    "history": history,
                    "ibn_url": os.environ.get("TWIN_IBN_DASHBOARD_URL", "http://localhost:8160"),
                    "dtw_url": os.environ.get("TWIN_DTW_DASHBOARD_URL", "http://localhost:8180"),
                }))
                for card in cards:
                    await ws.send_text(json.dumps({"type": "approval", "review": card}))

            elif typ == "query":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await asyncio.to_thread(_remember, text)
                if _turn_lock.locked():
                    await ws.send_text(json.dumps({"type": "error", "message":
                        "Busy: the previous turn is still running. Try again in a moment."}))
                    continue
                async with _turn_lock:
                    await ws.send_text(json.dumps({"type": "thinking", "active": True}))
                    try:
                        await _turn(ws, text)
                    except Exception as e:
                        log.exception("turn failed")
                        await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
                    finally:
                        await ws.send_text(json.dumps({"type": "thinking", "active": False}))

            elif typ == "approval_decision":
                approval_id = str(msg.get("approval_id") or "")
                # Waits for a running turn: the card can arrive mid-turn.
                async with _turn_lock:
                    await _send(ws, {"type": "thinking", "active": True})
                    try:
                        await _decide(approval_id, str(msg.get("decision") or ""),
                                      str(msg.get("notes") or "").strip()[:500])
                    except Exception as e:
                        log.warning(f"decision on {approval_id}: {e}")
                        await _broadcast("ERROR", str(e)[:300])
                        await _send(ws, {"type": "approval_failed", "approval_id": approval_id,
                                         "message": str(e)})
                    finally:
                        await _send(ws, {"type": "thinking", "active": False})

            elif typ == "reset_demo":
                if _turn_lock.locked():
                    await ws.send_text(json.dumps({"type": "reset_done", "ok": False, "message":
                        "Busy: a turn or reset is running. Try again in a moment."}))
                    continue

                async def _emit(line: str):
                    await ws.send_text(json.dumps({"type": "reset_progress", "line": line}))

                async with _turn_lock:
                    await ws.send_text(json.dumps({"type": "reset_started"}))
                    try:
                        await seed_runner.reset_session("", DEMO_DB, _emit)
                        _ae_session = None
                        await ws.send_text(json.dumps({"type": "reset_done", "ok": True, "message":
                            "The twin's demo data has been reset to a clean slate, and the next "
                            "turn starts a new Agent Engine session. Platform memory stays (there "
                            "is no delete-by-user API)."}))
                    except Exception as e:
                        log.exception("reset failed")
                        await ws.send_text(json.dumps({"type": "reset_done", "ok": False,
                                                       "message": f"Reset failed: {e}"}))

            elif typ == "server_list":
                await ws.send_text(json.dumps({"type": "server_list", "servers": _agent_rows()}))

            elif typ == "workstreams_request":
                await ws.send_text(json.dumps({"type": "workstreams", "list": []}))

            elif typ == "command":
                if msg.get("cmd") == "status":
                    await ws.send_text(json.dumps({"type": "status", "servers": AGENTS}))
                else:   # preferences / memory: platform memory, ask the coordinator
                    await ws.send_text(json.dumps({"type": "memory", "rows": [{
                        "ts": "—", "text": "Memory lives in Agent Engine: ask the coordinator "
                                           "“what do you remember about me?”",
                        "category": "platform", "type": "—"}]}))
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8170, log_level="info")
