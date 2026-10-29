#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The browser shell's self-healing responder (SELF_HEALING_PLAN.md, S5), the
legacy runtime's counterpart of the twin autopilot (agent-engine/autopilot.py).

One task per browser session watches that lane's `ibn_incidents`. For every
incident the SLO monitor opens (it runs in the IBN dashboard, so a dashboard
tab must be open on the session), it takes the session lock, so it queues
behind a turn the user is running, and runs the session's own IBN agent on
the fixed task (autonomy.TASK), with no human prompt. The run streams into
the Agent Log like any turn, the chat shows it as an `autonomous` message,
and the report lands on the incident, where the dashboard shows it.

What the agent may do is decided by the lane's autonomy policy, inside the
tools. On Kanopy, where L4 can't be set (autonomy.l4_allowed), that is L3:
diagnose and recommend, then the NOC engineer in the chat decides.
SELF_HEALING_RESPONDER=0 turns the responder off.
"""

import asyncio
import datetime
import logging
import os
import time

from pymongo import AsyncMongoClient

from agents.domain_agent import AgentContext
from oss_tools.ibn import autonomy

log = logging.getLogger("shell")

DB_NAME = "agent_registry"
BY = "ibn_agent (shell)"
CATCH_UP = datetime.timedelta(minutes=5)   # open incidents this young are picked up at start
MAX_ITERATIONS = 8                         # five tool calls and the report, plus a retry
ENABLED = os.environ.get("SELF_HEALING_RESPONDER", "1") != "0"

_client: AsyncMongoClient | None = None


def _db():
    global _client
    if _client is None:
        _client = AsyncMongoClient(os.environ["MONGODB_URI"])
    return _client[DB_NAME]


async def run(sess, send) -> None:
    """Watch one session's lane until cancelled. `sess` is web/shell.py's
    Session, `send(payload)` delivers to all of its tabs."""
    incidents = _db()[sess.prefix + "ibn_incidents"]
    running: set[asyncio.Task] = set()

    async def start(incident_id) -> None:
        # One responder per incident, even with two shell replicas.
        doc = await incidents.find_one_and_update(
            {"_id": incident_id, "autopilot": {"$exists": False}},
            {"$set": {"autopilot": {"status": "running", "by": BY,
                                    "started_at": datetime.datetime.now()}}})
        if doc:
            task = asyncio.create_task(_respond(sess, send, incidents, doc))
            running.add(task)
            task.add_done_callback(running.discard)

    try:
        while True:
            try:
                stream = await incidents.watch(
                    [{"$match": {"operationType": "insert", "fullDocument.open": True}}])
                async with stream:
                    young = datetime.datetime.now() - CATCH_UP
                    async for doc in incidents.find({"open": True, "opened_at": {"$gte": young},
                                                     "autopilot": {"$exists": False}}, {"_id": 1}):
                        await start(doc["_id"])
                    async for change in stream:
                        await start(change["documentKey"]["_id"])
                # A Reset drops the collection, which ends the stream: watch again.
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"self-healing watch ({sess.prefix}) failed ({e}); retrying in 5s")
                await asyncio.sleep(5)
    finally:
        for task in running:
            task.cancel()


async def _respond(sess, send, incidents, incident: dict) -> None:
    head = {"type": "autonomous", "incident_id": str(incident["_id"]),
            "intent_id": incident["intent_id"], "site": incident.get("site_name") or ""}
    agent = sess.orch.domain_agents.get("ibn")
    if agent is None:
        await _finish(incidents, incident, False, "No IBN agent in this shell (AGENT_MODE).")
        return
    if sess.lock.locked():
        await send({**head, "phase": "queued"})
    async with sess.lock:
        pol = autonomy.merge(await _db()[sess.prefix + "ibn_autonomy_policy"].find_one(
            {"_id": autonomy.POLICY_ID}))
        await send({**head, "phase": "started", "level": pol["level"],
                    "note": (incident.get("alarm") or {}).get("specific_problem", "SLO breach")})
        log.info(f"self-healing run (prefix={sess.prefix!r}, {incident['intent_id']}, {pol['level']})")
        t0 = time.monotonic()
        try:
            result = await agent.run(autonomy.task(incident, pol), AgentContext(),
                                     depth=1, max_iterations=MAX_ITERATIONS)
            ok, answer = result.status == "ok", result.answer
        except Exception as e:
            log.exception("self-healing run failed")
            ok, answer = False, f"The self-healing run failed: {e}"
        await _finish(incidents, incident, ok, answer)
        # Follow-ups in the chat ("apply it", "why?") continue from the report.
        orch = sess.orch
        orch.conversation_history = (orch.conversation_history + [
            {"role": "user", "content": f"(No human prompt.) The SLO monitor opened an incident "
                                        f"for {incident['intent_id']} at {head['site']}: handle "
                                        f"it autonomously."},
            {"role": "assistant", "content": answer},
        ])[-20:]
        orch.last_domain = agent.domain
        await send({**head, "phase": "done", "ok": ok, "level": pol["level"],
                    "markdown": answer, "elapsed": round(time.monotonic() - t0, 1)})


async def _finish(incidents, incident: dict, ok: bool, answer: str) -> None:
    now = datetime.datetime.now()
    await incidents.update_one({"_id": incident["_id"]}, {
        "$set": {"autopilot.status": "done" if ok else "failed", "autopilot.finished_at": now,
                 "autopilot.answer": answer[:2000]},
        "$push": {"timeline": {"ts": now, "stage": "report", "by": BY,
                               "note": answer.splitlines()[0][:300] if answer else "(no answer)"}},
    })
