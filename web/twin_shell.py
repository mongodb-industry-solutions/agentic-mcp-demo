#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

# The web shell on Atlas Agent Engine (AGENT_ENGINE_PLAN.md, Phases 8a/8b).
# The same page as web/shell.py, but a turn goes to the deployed
# noc-coordinator over the Agent Engine API (web/ae_client.py), and the
# Agent Log comes from the platform's traces on the agents' cluster
# (mdb_store_<project>.traces, a change stream): the coordinator's routing,
# its A2A delegations, every tool call of the domain agents.
#
# Lanes: like the Kanopy shell, each browser session gets its own demo lane
# (s_<token>_ collections, seeded on first use, reaped when idle). Every
# invoke carries the lane as the demo-prefix custom header, which the
# coordinator forwards to the domain agents over A2A. The browser session
# also has its own coordinator session (its id is ours, so the Agent Log
# can route the trace spans to the right page) and, with a service account,
# its own memory identity (ae_client). Reset reseeds the lane and starts
# both afresh. With the presenter's CLI login instead of a service account,
# every turn runs as the presenter, so the shell then serves loopback
# clients only.
#
# NOC approvals: a blast-radius action pauses the domain agent that the
# coordinator delegated to (A2A can't resume a child). Its review doc in the
# lane's agent_approvals names the paused execution and session; a change
# stream turns it into an approval card, and the decision resumes that
# session over the API, with the lane header resent and the interrupt id
# from GET /executions/{id}.
#
# Self-healing: each session runs a responder, like the Kanopy shell's
# (web/self_healing.py): an incident the SLO monitor opens in the lane goes
# to the deployed ibn-agent with no human prompt, and the chat shows the
# run as an `autonomous` panel. The lane's autonomy policy decides what the
# agent may do (L4 where SELF_HEALING_L4=1, as bin/twin.sh sets it).
#
# Run it with bin/twin.sh start (:8170). web/shell.py, the Kanopy shell,
# doesn't import this module.

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
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pymongo import AsyncMongoClient, MongoClient

from agents import history as shell_history
from oss_tools.ibn import autonomy
from web import ae_client, seed_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("twin_shell")

DEMO_DB = "agent_registry"
ROOT = Path(__file__).resolve().parent.parent

# The deployed agents in JeffN (the same defaults as agent-engine/autopilot.py).
COORDINATOR_WS = os.environ.get("TWIN_COORDINATOR_WORKSPACE_ID", "ws-6ad1060d6189a50fdc472a65")
IBN_WS = os.environ.get("TWIN_IBN_WORKSPACE_ID", "ws-6ad0efe66189a50fdc461f8d")
DTW_WS = os.environ.get("TWIN_DTW_WORKSPACE_ID", "ws-6ad0efe7ea67bce171583766")
AGENTS = ["noc-coordinator", "ibn-agent", "dtw-agent"]
AGENT_WORKSPACES = {IBN_WS: "ibn-agent", DTW_WS: "dtw-agent", COORDINATOR_WS: "noc-coordinator"}
# The agent that owns each reviewed action (oss_tools.approvals): the review
# doc names the paused execution and session, but the tool sandbox doesn't
# know its workspace id (get_current_workspace_id() is empty there).
ACTION_WORKSPACE = {
    "activate_plan": IBN_WS, "apply_runbook": IBN_WS, "update_template_version": IBN_WS,
    "cancel_intent": IBN_WS, "delete_scenario": DTW_WS, "delete_all_scenarios": DTW_WS,
}

MAX_SESSIONS = int(os.environ.get("TWIN_MAX_SESSIONS", "6"))
SESSION_TTL_SEC = int(os.environ.get("DEMO_SESSION_TTL_SEC", "1800"))   # 30 min idle
_TOKEN_RE = re.compile(r"^[a-z0-9]{8,32}$")   # as web/shell.py
LANE_STATE = "ae_session"   # per lane: the coordinator session (and memory identity)
RESPONDER = os.environ.get("SELF_HEALING_RESPONDER", "1") != "0"
RESPONDER_BY = "ibn-agent (twin shell)"
CATCH_UP = datetime.timedelta(minutes=5)   # open incidents this young are picked up at start

# Our three workspaces and the subscriber-id patterns come from the
# guardrail (Phase 6). It redacts the agents' answers; the log shows tool
# results, which it never sees, so the log redacts them the same way.
_GUARDRAIL = json.loads((ROOT / "agent-engine/guardrails/subscriber-ids.json").read_text())
WORKSPACES = set(_GUARDRAIL["workspace_ids"])
_REDACT = [re.compile(p["value"]) for p in _GUARDRAIL["config"]["match_patterns"]]

# The coordinator's internal tools; its routing comes from the route span.
_INTERNAL_TOOLS = {"record_routing", "rank_agents", "runbooks_for_memory", "pending_reviews"}
_REORDER_S = 1.0   # spans arrive in batches; emit them in end-time order
_ORPHAN_S = 60     # a span whose trace has no known session yet waits this long


@dataclass
class Session:
    token: str
    prefix: str
    lock: asyncio.Lock                       # one turn, decision or reset at a time
    ae_session: str = ""                     # the coordinator's Agent Engine session
    clients: set = field(default_factory=set)
    last_activity: float = 0.0
    context: str = ""                        # the last self-healing report, for the next turn
    tasks: list = field(default_factory=list)   # approvals watch, self-healing responder

    @property
    def user_id(self) -> str:
        """The memory identity: one per coordinator session, so Reset
        starts a fresh one (there is no delete-by-user API)."""
        return self.ae_session


_sessions: dict[str, Session] = {}
_sessions_guard = asyncio.Lock()   # serialises create / reap
_agent_names: dict[str, str] = {}   # workspace id → agent, learned from the spans
# Span routing: an Agent Engine session we started → browser session, and a
# trace → browser session (an A2A child has its own session in the same
# trace). Both bounded, the oldest dropped first.
_session_owner: OrderedDict[str, str] = OrderedDict()
_trace_owner: OrderedDict[str, str] = OrderedDict()
_orphans: dict[str, list] = {}   # trace → [(arrived, span)] until its owner is known
_OWNER_MAX = 2000
# A resumed run replays its earlier steps, and each replayed tool call is
# traced again (same execution, tool, input and output, no call id): logged
# once. Keyed per execution, the oldest dropped first.
_seen_tools: dict[str, set] = {}
_SEEN_EXECUTIONS = 200

_aclient: AsyncMongoClient | None = None


def _adb():
    global _aclient
    if _aclient is None:
        _aclient = AsyncMongoClient(os.environ["MONGODB_URI"])
    return _aclient[DEMO_DB]


def _remember_owner(table: OrderedDict, key: str, token: str) -> None:
    table[key] = token
    table.move_to_end(key)
    while len(table) > _OWNER_MAX:
        table.popitem(last=False)


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


async def _send_all(sess: Session, msg: dict) -> None:
    """To every tab on one session; a tab that has closed is dropped."""
    data = json.dumps(msg, default=str)
    for ws in list(sess.clients):
        try:
            await ws.send_text(data)
        except Exception:
            sess.clients.discard(ws)


async def _send(ws: WebSocket, msg: dict) -> None:
    try:
        await ws.send_text(json.dumps(msg, default=str))
    except Exception:
        pass


async def _broadcast(sess: Session, tag: str, msg: str) -> None:
    await _send_all(sess, {"type": "broadcast", "tag": tag, "msg": msg})


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


# Spans with a session id (to learn which trace is whose) plus the ones the
# log renders.
_SPAN_FILTER = [{"$match": {"operationType": "insert", "$or": [
    {"fullDocument.session_id": {"$exists": True}},
    {"fullDocument.name": {"$in": ["route", "a2a.invoke_agent", "aer.execute"]}},
    {"$expr": {"$eq": [{"$getField": {"field": "openinference.span.kind",
                                      "input": "$fullDocument.attributes"}}, "TOOL"]}},
]}}]


def _watch_traces(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, stop: threading.Event) -> None:
    """Blocking change stream on the platform's traces (a thread)."""
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        while not stop.is_set():
            try:
                with client[f"mdb_store_{ae_client.PROJECT_ID}"]["traces"].watch(
                        _SPAN_FILTER, max_await_time_ms=1000) as stream:
                    while not stop.is_set():
                        change = stream.try_next()
                        span = (change or {}).get("fullDocument")
                        if span and (span.get("resource") or {}).get(
                                "agentic_platform.workspace_id") in WORKSPACES:
                            loop.call_soon_threadsafe(queue.put_nowait, span)
            except Exception as e:
                log.warning(f"traces watch: {e}; retrying")
                stop.wait(3)
    finally:
        client.close()


def _owner(span: dict) -> str | None:
    trace = span.get("trace_id")
    sid = (span.get("session_id") or "").split(":")[0]
    if trace and sid in _session_owner:
        _remember_owner(_trace_owner, trace, _session_owner[sid])
    return _trace_owner.get(trace)


async def _log_pump(queue: asyncio.Queue) -> None:
    """Spans → each session's Agent Log, reordered by end time within a
    short window. A span is routed by its trace: the trace belongs to the
    browser session that started its root session. Spans of a trace whose
    owner isn't known yet wait for it (an A2A child's spans can arrive
    before the coordinator's)."""
    while True:
        batch = [await queue.get()]
        deadline = time.monotonic() + _REORDER_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                batch.append(await asyncio.wait_for(queue.get(), left))
            except TimeoutError:
                break
        now = time.monotonic()
        for span in batch:
            res = span.get("resource") or {}
            _agent_names[res.get("agentic_platform.workspace_id")] = res.get("service.name", "?")
            _owner(span)
        ready = []
        for trace in list(_orphans):
            if trace in _trace_owner:
                ready += [s for _, s in _orphans.pop(trace)]
            elif now - _orphans[trace][0][0] > _ORPHAN_S:
                del _orphans[trace]   # not ours (the Agent Engine UI, the autopilot)
        for span in batch:
            if _owner(span):
                ready.append(span)
            elif span.get("trace_id"):
                _orphans.setdefault(span["trace_id"], []).append((now, span))
        for span in sorted(ready, key=lambda s: s.get("end_time_ns") or 0):
            sess = _sessions.get(_trace_owner.get(span.get("trace_id")))
            if not sess or _replayed(span):
                continue
            try:
                for tag, text in _span_lines(span):
                    await _broadcast(sess, tag, _redact(text))
            except Exception as e:
                log.warning(f"span {span.get('name')}: {e}")


# ── Sessions ──────────────────────────────────────────────────────────────

def _new_ae_session(token: str) -> str:
    # Our own id (letters, digits, hyphens; at most 128), so the spans of a
    # turn can be routed while it runs.
    return f"demo-{token}-{uuid.uuid4().hex[:8]}"


async def _set_ae_session(sess: Session, session_id: str) -> None:
    sess.ae_session = session_id
    _remember_owner(_session_owner, session_id, sess.token)
    await _adb()[sess.prefix + LANE_STATE].replace_one(
        {"_id": "shell"}, {"session_id": session_id, "since": datetime.datetime.now()}, upsert=True)


def _drop_lane(prefix: str) -> None:
    """Drop a reaped session's collections (as web/shell.py does)."""
    client = MongoClient(os.environ["MONGODB_URI"])
    try:
        db = client[DEMO_DB]
        for base in seed_runner.SESSION_MUTABLE_BASES + seed_runner.SESSION_STATE_BASES + [LANE_STATE]:
            db[prefix + base].drop()
            if base == "ibn_telemetry":
                try:
                    db.drop_collection("system.buckets." + prefix + base)
                except Exception:
                    pass
    finally:
        client.close()


async def _destroy_locked(token: str) -> None:
    sess = _sessions.pop(token, None)
    if not sess:
        return
    for task in sess.tasks:
        task.cancel()
    await asyncio.gather(*sess.tasks, return_exceptions=True)
    try:
        await asyncio.to_thread(_drop_lane, sess.prefix)
    except Exception as e:
        log.warning(f"session {token}: dropping the lane failed: {e}")
    log.info(f"session {token} reaped ({len(_sessions)} remain)")


async def _reap_idle_locked() -> None:
    now = time.monotonic()
    for token, sess in list(_sessions.items()):
        if not sess.clients and now - sess.last_activity >= SESSION_TTL_SEC:
            await _destroy_locked(token)


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            async with _sessions_guard:
                await _reap_idle_locked()
        except Exception as e:
            log.warning(f"reaper sweep: {e}")


async def _get_or_create_session(token: str) -> tuple[Session | None, str | None]:
    """A session by token: its lane is seeded on first use, and its
    coordinator session survives a shell restart (stored in the lane)."""
    async with _sessions_guard:
        if sess := _sessions.get(token):
            return sess, None
        await _reap_idle_locked()
        if len(_sessions) >= MAX_SESSIONS:
            return None, (f"The demo is at capacity ({MAX_SESSIONS} concurrent sessions). "
                          "Please try again in a few minutes.")
        prefix = f"s_{token}_"
        try:
            seeded = await seed_runner.ensure_session_seeded(prefix, DEMO_DB)
            state = await _adb()[prefix + LANE_STATE].find_one({"_id": "shell"})
        except Exception as e:
            log.exception("session lane seed failed")
            return None, f"Could not initialise demo data: {e}"
        sess = Session(token=token, prefix=prefix, lock=asyncio.Lock(), last_activity=time.monotonic())
        await _set_ae_session(sess, (state or {}).get("session_id") or _new_ae_session(token))
        sess.tasks.append(asyncio.create_task(_watch_approvals(sess)))
        if RESPONDER:
            sess.tasks.append(asyncio.create_task(_respond_to_incidents(sess)))
        _sessions[token] = sess
        log.info(f"session {token} created (lane {'seeded' if seeded else 'reused'}, "
                 f"{len(_sessions)}/{MAX_SESSIONS} active)")
        return sess, None


async def _watch(coll, on_doc, ops=("insert", "update")) -> None:
    """A change stream on one lane collection until cancelled; a Reset
    drops the demo collections, which ends the stream: watch again."""
    while True:
        try:
            async with await coll.watch([{"$match": {"operationType": {"$in": list(ops)}}}],
                                        full_document="updateLookup") as stream:
                async for change in stream:
                    if doc := change.get("fullDocument"):
                        await on_doc(doc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"{coll.name} watch failed ({e}); retrying in 5 s")
            await asyncio.sleep(5)


# ── Turns ─────────────────────────────────────────────────────────────────

async def _turn(sess: Session, ws: WebSocket, text: str) -> None:
    await _broadcast(sess, "QUERY", text[:300])
    await _broadcast(sess, "AGENT", f"→ noc-coordinator on Atlas Agent Engine "
                                    f"(lane {sess.prefix}, session …{sess.ae_session[-8:]})")
    message = text
    if sess.context and not re.search(r"\bdtw\b", text, re.IGNORECASE):
        # The coordinator never saw the self-healing run (it went straight to
        # ibn-agent), so a follow-up such as "apply runbook" gets its report.
        message = f"(Context: {sess.context})\n\n{text}"
    sess.context = ""

    async def retry(note: str) -> None:
        await _broadcast(sess, "INFO", note)

    t0 = time.monotonic()
    out = await ae_client.invoke(COORDINATOR_WS, message=message, session_id=sess.ae_session,
                                 lane=sess.prefix, user_id=sess.user_id, on_retry=retry)
    err = out.get("error") or ""
    if not out["ok"] and "session" in err.lower() and "busy" not in err.lower():
        await _broadcast(sess, "INFO", f"Session …{sess.ae_session[-8:]} is gone; starting a new one")
        await _set_ae_session(sess, _new_ae_session(sess.token))
        out = await ae_client.invoke(COORDINATOR_WS, message=message, session_id=sess.ae_session,
                                     lane=sess.prefix, user_id=sess.user_id, on_retry=retry)
    elapsed = round(time.monotonic() - t0, 1)
    if not out["ok"]:
        err = out.get("error") or "the invoke failed"
        await _broadcast(sess, "ERROR", err[:300])
        await _send(ws, {"type": "error", "message": f"Agent Engine: {err}"})
        return
    await _send(ws, {"type": "response", "elapsed": elapsed,
                     "markdown": out.get("response") or "No response."})
    await _broadcast(sess, "INFO", f"✓ noc-coordinator answered in {elapsed} s "
                                   f"(execution {str(out.get('execution_id'))[:8]}…)")


# ── NOC approvals ─────────────────────────────────────────────────────────

_PAUSED = {"suspended", "resuming"}


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


async def _pending_cards(sess: Session) -> list[dict]:
    cur = _adb()[sess.prefix + "agent_approvals"].find({"status": "pending"}).sort("seq", 1)
    return [_card(d) async for d in cur]


async def _watch_approvals(sess: Session) -> None:
    async def on_doc(doc: dict) -> None:
        card = _card(doc)
        await _send_all(sess, {"type": "approval", "review": card})
        if card["status"] == "pending":
            text = f"⏸ {card['approval_id']} awaits NOC approval on {card['agent']}: {card['title']}"
        elif card["status"] == "expired":
            text = f"⌛ {card['approval_id']} expired ({card['reviewer_notes']}): {card['title']}"
        elif card["decided_by"] not in (None, "noc"):
            text = f"✓ {card['approval_id']} approved by {card['decided_by']}: {card['title']}"
        else:
            return
        await _broadcast(sess, "AGENT", text)
    await _watch(_adb()[sess.prefix + "agent_approvals"], on_doc)


def _interrupt_id(execution: dict, approval_id: str) -> str | None:
    """The stable interrupt id (suspend_context.interrupts[].id) whose
    payload is this review."""
    items = (execution.get("suspend_context") or {}).get("interrupts") or []
    for it in items:
        if _json(it.get("value")).get("approval_id") == approval_id:
            return it.get("id")
    return items[0].get("id") if len(items) == 1 and not _json(items[0].get("value")) else None


async def _expire(sess: Session, approval_id: str, status: str) -> None:
    """A review whose paused run is gone: close it and clear its markers."""
    doc = await _adb()[sess.prefix + "agent_approvals"].find_one_and_update(
        {"_id": approval_id, "status": "pending"},
        {"$set": {"status": "expired", "decided_by": "twin-shell",
                  "reviewer_notes": f"the paused run is {status}",
                  "decided_at": datetime.datetime.now()}})
    for name, ids in ((doc or {}).get("targets") or {}).items():
        if ids:
            await _adb()[sess.prefix + name].update_many({"_id": {"$in": ids}},
                                                         {"$unset": {"pending_approval": ""}})


async def _decide(sess: Session, approval_id: str, decision: str, notes: str) -> None:
    """Resume the paused agent with the NOC decision."""
    doc = await _adb()[sess.prefix + "agent_approvals"].find_one({"_id": approval_id})
    if not doc or doc.get("status") != "pending":
        raise ValueError(f"{approval_id} is not pending any more")
    if decision not in (doc.get("allowed_decisions") or []):
        raise ValueError(f"{decision!r} isn't allowed for {approval_id}")
    workspace_id = _workspace(doc)
    if not (doc.get("execution_id") and workspace_id and doc.get("session_id")):
        raise ValueError(f"{approval_id} doesn't name the paused run")
    agent = AGENT_WORKSPACES.get(workspace_id, workspace_id)
    await _broadcast(sess, "ACTION", f"NOC decision: {decision} {approval_id}"
                     + (f" ({notes})" if notes else "") + f" → resuming {agent}")
    execution = await ae_client.execution(doc["execution_id"], workspace_id)
    status = execution.get("status") or "unknown"
    if status in ("pending", "running"):
        raise ValueError(f"{agent} is still running ({status}); decide once it has paused")
    if status not in _PAUSED:
        await _expire(sess, approval_id, status)
        raise ValueError(f"the paused run behind {approval_id} is {status}: it can't be resumed. "
                         "Ask again to get a new review.")
    interrupt_id = _interrupt_id(execution, approval_id)
    if not interrupt_id:
        raise ValueError(f"no interrupt for {approval_id} on execution {doc['execution_id'][:8]}…")
    # The resumed run's spans belong to this page.
    _remember_owner(_session_owner, doc["session_id"], sess.token)
    t0 = time.monotonic()
    # The platform doesn't keep custom headers across a pause: the resume
    # resends the lane. A2A depth travels in the message instead
    # (telco_runtime.a2a.message_depth).
    out = await ae_client.invoke(workspace_id, session_id=doc["session_id"], lane=sess.prefix,
                                 user_id=sess.user_id,
                                 resume_map={interrupt_id: {"decision": decision, "reviewer_notes": notes}})
    elapsed = round(time.monotonic() - t0, 1)
    if not out["ok"]:
        err = f"resume failed: {out.get('error') or 'the invoke failed'}"
        # A resume the runtime rejects (a replay conflict) fails the run:
        # expire the review now, not on the next click.
        status = "unknown"
        try:
            for _ in range(3):
                await asyncio.sleep(2)
                status = (await ae_client.execution(doc["execution_id"], workspace_id)).get("status") or "unknown"
                if status not in ("pending", "running", "resuming"):
                    break
        except Exception as e:
            log.warning(f"{approval_id}: execution status after the failed resume: {e}")
        if status not in _PAUSED | {"pending", "running", "unknown"}:
            await _expire(sess, approval_id, status)
            err += f". The run is {status} now, so {approval_id} has expired; ask again for a new review."
        raise ValueError(err)
    if out.get("execution_id") != doc["execution_id"] and not out.get("suspend_context"):
        # An invoke that started a new turn instead of resuming leaves the
        # paused execution suspended (AGENT_ENGINE_PLAN.md, Phase 3 #5).
        status = (await ae_client.execution(doc["execution_id"], workspace_id)).get("status")
        if status in _PAUSED:
            raise ValueError(f"Agent Engine didn't resume {approval_id}: the paused run is still "
                             f"{status} (execution {doc['execution_id'][:8]}…)")
    answer = out.get("response") or (f"⏸ {agent} paused again for a NOC review."
                                     if out.get("suspend_context") else "No response.")
    await _send_all(sess, {"type": "response", "elapsed": elapsed, "markdown": answer,
                           "title": f"🤖 {agent} after {approval_id} ({decision})"})
    await _broadcast(sess, "INFO", f"✓ {agent} resumed after {approval_id} in {elapsed} s")


# ── Self-healing responder ────────────────────────────────────────────────

async def _respond_to_incidents(sess: Session) -> None:
    """Watch the lane's ibn_incidents; hand every incident the SLO monitor
    opens (it runs in the IBN dashboard, so a dashboard tab must be open on
    the session) to the deployed ibn-agent."""
    incidents = _adb()[sess.prefix + "ibn_incidents"]
    running: set[asyncio.Task] = set()

    async def start(incident_id) -> None:
        # One responder per incident (the field the autopilot and the
        # Kanopy shell's responder claim with, too).
        doc = await incidents.find_one_and_update(
            {"_id": incident_id, "autopilot": {"$exists": False}},
            {"$set": {"autopilot": {"status": "running", "by": RESPONDER_BY,
                                    "started_at": datetime.datetime.now()}}})
        if doc:
            task = asyncio.create_task(_respond(sess, incidents, doc))
            running.add(task)
            task.add_done_callback(running.discard)

    async def on_insert(doc: dict) -> None:
        if doc.get("open"):
            await start(doc["_id"])

    try:
        young = datetime.datetime.now() - CATCH_UP
        async for doc in incidents.find({"open": True, "opened_at": {"$gte": young},
                                         "autopilot": {"$exists": False}}, {"_id": 1}):
            await start(doc["_id"])
        await _watch(incidents, on_insert, ops=("insert",))
    finally:
        for task in running:
            task.cancel()


async def _respond(sess: Session, incidents, incident: dict) -> None:
    head = {"type": "autonomous", "incident_id": str(incident["_id"]),
            "intent_id": incident["intent_id"], "site": incident.get("site_name") or ""}
    if sess.lock.locked():
        await _send_all(sess, {**head, "phase": "queued"})
    async with sess.lock:
        pol = autonomy.merge(await _adb()[sess.prefix + "ibn_autonomy_policy"].find_one(
            {"_id": autonomy.POLICY_ID}))
        await _send_all(sess, {**head, "phase": "started", "level": pol["level"],
                               "note": (incident.get("alarm") or {}).get("specific_problem", "SLO breach")})
        session_id = f"{sess.ae_session}-heal-{str(incident['_id'])[-8:]}"
        _remember_owner(_session_owner, session_id, sess.token)
        log.info(f"self-healing run ({sess.prefix}, {incident['intent_id']}, {pol['level']})")

        async def retry(note: str) -> None:
            await _broadcast(sess, "INFO", note)

        t0 = time.monotonic()
        try:
            out = await ae_client.invoke(IBN_WS, message=autonomy.task(incident, pol),
                                         session_id=session_id, lane=sess.prefix,
                                         user_id=sess.user_id, on_retry=retry)
        except Exception as e:
            log.exception("self-healing run failed")
            out = {"ok": False, "error": f"The self-healing run failed: {e}"}
        ok = out["ok"]
        answer = _redact((out.get("response") or out.get("error") or "").strip())
        now = datetime.datetime.now()
        await incidents.update_one({"_id": incident["_id"]}, {
            "$set": {"autopilot.status": "done" if ok else "failed", "autopilot.finished_at": now,
                     "autopilot.session_id": out.get("session_id"),
                     "autopilot.execution_id": out.get("execution_id"),
                     "autopilot.answer": answer[:2000]},
            "$push": {"timeline": {"ts": now, "stage": "report", "by": RESPONDER_BY,
                                   "note": answer.splitlines()[0][:300] if answer else "(no answer)"}},
        })
        if ok:
            sess.context = (f"with no human prompt, the self-healing run on ibn-agent just handled "
                            f"incident {incident['_id']} for {incident['intent_id']} at {head['site']} "
                            f"(autonomy {pol['level']}) and reported: {answer[:600]}")
        await _send_all(sess, {**head, "phase": "done", "ok": ok, "level": pol["level"],
                               "markdown": answer, "elapsed": round(time.monotonic() - t0, 1)})


# ── App ───────────────────────────────────────────────────────────────────

def _mongo_info() -> dict:
    parsed = urlparse(os.environ.get("MONGODB_URI", ""))
    indexes = []
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=3000)
    try:
        db = client[DEMO_DB]
        for coll in db.list_collection_names():
            if coll.startswith("s_"):
                continue
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
    threading.Thread(target=_watch_traces, args=(asyncio.get_running_loop(), queue, stop),
                     daemon=True).start()
    background = [asyncio.create_task(_log_pump(queue)), asyncio.create_task(_reaper_loop())]
    log.info("Shell ready on Atlas Agent Engine (noc-coordinator), "
             + ("service account: per-session memory" if ae_client.credentials.service_account
                else "the presenter's CLI login: loopback only"))
    yield
    stop.set()
    async with _sessions_guard:
        for sess in _sessions.values():
            for task in sess.tasks:
                task.cancel()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)


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
    # With the CLI login every turn runs as the presenter: loopback only.
    if not ae_client.credentials.service_account and (
            not ws.client or ws.client.host not in ("127.0.0.1", "::1")):
        await ws.close(code=1008)
        return
    await ws.accept()
    sess: Session | None = None   # bound by the first `init` message
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            typ = msg.get("type")

            if typ == "init":
                token = (msg.get("token") or "").strip().lower()
                if not _TOKEN_RE.match(token):
                    token = uuid.uuid4().hex[:12]
                sess, err = await _get_or_create_session(token)
                if err:
                    await _send(ws, {"type": "error", "message": err})
                    await ws.close()
                    return
                sess.clients.add(ws)
                sess.last_activity = time.monotonic()
                info = app.state.mongo_info
                # The page works without its history and cards (the cluster
                # unreachable, DNS): the chat still goes to the coordinator.
                try:
                    history = await asyncio.to_thread(shell_history.read_recent, prefix=sess.prefix)
                    cards = await _pending_cards(sess)
                except Exception as e:
                    log.warning(f"init: {e}")
                    history, cards = [], []
                await _send(ws, {
                    "type": "hello", "runtime": "agent_engine", "session": token,
                    "host": info["host"], "indexes": info["indexes"], "servers": AGENTS,
                    "history": history,
                    "ibn_url": os.environ.get("TWIN_IBN_DASHBOARD_URL", "http://localhost:8160"),
                    "dtw_url": os.environ.get("TWIN_DTW_DASHBOARD_URL", "http://localhost:8180"),
                })
                for card in cards:
                    await _send(ws, {"type": "approval", "review": card})
                continue

            if sess is None:
                continue   # nothing before the session is established
            sess.last_activity = time.monotonic()

            if typ == "query":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await asyncio.to_thread(shell_history.append, text, source="twin-shell",
                                        prefix=sess.prefix)
                if sess.lock.locked():
                    await _send(ws, {"type": "broadcast", "tag": "AGENT",
                                     "msg": "⏳ Your query waits for the turn that is running "
                                            "(a self-healing run, or another tab)."})
                async with sess.lock:
                    await _send(ws, {"type": "thinking", "active": True})
                    try:
                        await _turn(sess, ws, text)
                    except Exception as e:
                        log.exception("turn failed")
                        await _send(ws, {"type": "error", "message": str(e)})
                    finally:
                        await _send(ws, {"type": "thinking", "active": False})

            elif typ == "approval_decision":
                approval_id = str(msg.get("approval_id") or "")
                # Waits for a running turn: the card can arrive mid-turn.
                async with sess.lock:
                    await _send(ws, {"type": "thinking", "active": True})
                    try:
                        await _decide(sess, approval_id, str(msg.get("decision") or ""),
                                      str(msg.get("notes") or "").strip()[:500])
                    except Exception as e:
                        log.warning(f"decision on {approval_id}: {e}")
                        await _broadcast(sess, "ERROR", str(e)[:300])
                        await _send(ws, {"type": "approval_failed", "approval_id": approval_id,
                                         "message": str(e)})
                    finally:
                        await _send(ws, {"type": "thinking", "active": False})

            elif typ == "reset_demo":
                if sess.lock.locked():
                    await _send(ws, {"type": "reset_done", "ok": False, "message":
                        "Busy: a turn or reset is running. Try again in a moment."})
                    continue

                async def _emit(line: str):
                    await _send(ws, {"type": "reset_progress", "line": line})

                async with sess.lock:
                    await _send(ws, {"type": "reset_started"})
                    try:
                        await seed_runner.reset_session(sess.prefix, DEMO_DB, _emit)
                        await _set_ae_session(sess, _new_ae_session(sess.token))
                        sess.context = ""
                        await _send(ws, {"type": "reset_done", "ok": True, "message":
                            "Your demo lane has been reset to a clean slate, and the next turn "
                            "starts a new Agent Engine session"
                            + (" with a fresh memory identity." if ae_client.credentials.service_account
                               else ". Platform memory stays (it is the presenter's).")})
                    except Exception as e:
                        log.exception("reset failed")
                        await _send(ws, {"type": "reset_done", "ok": False,
                                         "message": f"Reset failed: {e}"})

            elif typ == "server_list":
                await _send(ws, {"type": "server_list", "servers": _agent_rows()})

            elif typ == "workstreams_request":
                await _send(ws, {"type": "workstreams", "list": []})

            elif typ == "command":
                if msg.get("cmd") == "status":
                    await _send(ws, {"type": "status", "servers": AGENTS})
                else:   # preferences / memory: platform memory, ask the coordinator
                    await _send(ws, {"type": "memory", "rows": [{
                        "ts": "—", "text": "Memory lives in Agent Engine: ask the coordinator "
                                           "“what do you remember about me?”",
                        "category": "platform", "type": "—"}]})
    except WebSocketDisconnect:
        pass
    finally:
        if sess:
            sess.clients.discard(ws)
            sess.last_activity = time.monotonic()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8170, log_level="info")
