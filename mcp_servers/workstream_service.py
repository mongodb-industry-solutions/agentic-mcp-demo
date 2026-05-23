#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Workstream Service — the agent's own activity log.

Read-only surface over agent_workstreams, the orchestrator's short-term
working memory. Each workstream represents a coherent thread of activity
(opening a store, running a what-if simulation, managing a TODO list) with
its domain, entities, running summary, and tool-call audit trail. The
orchestrator writes; this service exposes read tools so the user can ask
the agent itself questions like:

- "What workstreams are currently open?"
- "What did we work on yesterday?"
- "Show me the history of WS-2026-05-23-001."
- "What workstreams involved Marienplatz?"

Use this service when users say:
- Activity:     "what have we done", "what did you do yesterday",
                "what have we been working on", "recent activity"
- List:         "what workstreams are open", "list active sessions",
                "what threads are running", "show all workstreams"
- Detail:       "show workstream WS-...", "details on the Marienplatz one",
                "history of <workstream>"
- Search:       "any workstreams about Munich", "find threads involving X",
                "what workstreams concern the QoS uplift"
- Close:        "close that workstream", "we're done with Marienplatz",
                "mark WS-... completed"    (state change, doc kept)
- Delete:       "delete WS-...", "remove workstream", "purge completed
                workstreams", "wipe all completed workstreams"
                (DB removal — gone forever)

This service does NOT submit intents, run simulations, manage TODOs, or do
anything domain-specific — those are the actual demo services. It manages
the orchestrator's own audit log: read, close, delete.

Important distinction:
- close_workstream sets state=completed (kept in DB, memory extraction
  still runs, can be inspected later).
- delete_workstream / delete_completed_workstreams REMOVE documents from
  agent_workstreams entirely and cascade-delete the workstream's
  extracted memories from agent_memories. There is no undo.

When the user asks to remove / purge / wipe / clear multiple completed
workstreams at once, prefer delete_completed_workstreams (single bulk
operation) over iterating delete_workstream — it avoids the orchestrator's
5-iteration ReAct cap.
"""

import datetime
import logging
import os
from pymongo import MongoClient, DESCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("workstream_service")
logger        = logging.getLogger("workstream_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
workstreams   = db["agent_workstreams"]
memories      = db["agent_memories"]


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime.datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts) if ts else "—"


def _fmt_card(ws: dict, *, full: bool = False) -> str:
    state_emoji = {"open": "🟢", "paused": "⏸", "completed": "✓",
                   "cancelled": "⊗"}.get(ws.get("state", ""), "•")
    lines = [
        f"**{ws['_id']}** · {state_emoji} {ws.get('state', '?')} · "
        f"{ws.get('domain', '—')}"
    ]
    if ws.get("title"):
        lines.append(f"  {ws['title']}")
    last = ws.get("last_activity")
    opened = ws.get("opened_at")
    if last:
        lines.append(f"  last activity: {_fmt_ts(last)}")
    if opened and opened != last:
        lines.append(f"  opened: {_fmt_ts(opened)}")
    n_calls = len(ws.get("tool_calls") or [])
    if n_calls:
        lines.append(f"  tool calls: {n_calls}")
    entities = ws.get("entities") or []
    if entities:
        lines.append(f"  entities: {', '.join(entities[:8])}"
                     + (" …" if len(entities) > 8 else ""))
    summary = (ws.get("summary") or "").strip()
    if summary:
        snippet = summary if full else (summary[:300] + ("…" if len(summary) > 300 else ""))
        lines.append("")
        lines.append(f"  📝 {snippet}")
    return "\n".join(lines)


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_workstreams(state: str = None, limit: int = 10) -> str:
    """
    List workstreams, optionally filtered by state. Returns a recency-sorted
    summary of each.

    Args:
        state:  Optional state filter ('open', 'paused', 'completed', 'cancelled').
                If omitted, returns all states with 'open' first.
        limit:  Max workstreams to return (default 10, max 50).
    """
    limit = max(1, min(50, int(limit or 10)))
    q = {"state": state} if state else {}
    docs = list(workstreams.find(q).sort("last_activity", DESCENDING).limit(limit))
    if not docs:
        scope = f" with state '{state}'" if state else ""
        return f"No workstreams found{scope}."

    by_state: dict = {}
    for d in docs:
        by_state.setdefault(d.get("state", "?"), []).append(d)

    header = f"**{len(docs)} workstream(s)" + \
             (f" with state '{state}'" if state else "") + ":**"
    blocks = [header]
    for st in ("open", "paused", "completed", "cancelled"):
        group = by_state.get(st)
        if not group:
            continue
        blocks.append("")
        blocks.append(f"### {st.upper()} ({len(group)})")
        for ws in group:
            blocks.append("")
            blocks.append(_fmt_card(ws))
    # Any unexpected states fall through here
    for st, group in by_state.items():
        if st in ("open", "paused", "completed", "cancelled"):
            continue
        blocks.append("")
        blocks.append(f"### {st.upper()} ({len(group)})")
        for ws in group:
            blocks.append("")
            blocks.append(_fmt_card(ws))
    return "\n".join(blocks)


@mcp.tool()
def get_workstream(workstream_id: str) -> str:
    """
    Show the full record for a workstream: title, domain, entities,
    summary, and the full tool-call history.

    Args:
        workstream_id: Workstream id, e.g. 'WS-2026-05-23-001'.
    """
    ws = workstreams.find_one({"_id": workstream_id})
    if not ws:
        return f"❌ Workstream {workstream_id} not found."
    lines = [_fmt_card(ws, full=True)]
    calls = ws.get("tool_calls") or []
    if calls:
        lines.append("")
        lines.append(f"**Tool-call history ({len(calls)}):**")
        for c in calls[-30:]:
            ts = _fmt_ts(c.get("ts"))
            result_snip = (c.get("result") or "").replace("\n", " ")[:80]
            lines.append(f"  {ts} · `{c.get('service')}__{c.get('tool')}` · "
                         f"{result_snip}…")
    return "\n".join(lines)


@mcp.tool()
def recall_recent_activity(days: int = 1) -> str:
    """
    Summarize what's been done across all workstreams in the last N days.

    Args:
        days: Look-back window in days (default 1, max 30).
    """
    days = max(1, min(30, int(days or 1)))
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    docs = list(workstreams.find(
        {"last_activity": {"$gte": cutoff}}
    ).sort("last_activity", DESCENDING))
    if not docs:
        return f"No workstream activity in the last {days} day(s)."

    # Group by date
    by_day: dict = {}
    for d in docs:
        ts = d.get("last_activity")
        key = ts.date().isoformat() if isinstance(ts, datetime.datetime) else "—"
        by_day.setdefault(key, []).append(d)

    lines = [f"**Activity in the last {days} day{'s' if days != 1 else ''}:** "
             f"{len(docs)} workstream(s) touched."]
    for day in sorted(by_day.keys(), reverse=True):
        lines.append("")
        lines.append(f"### {day}")
        for ws in by_day[day]:
            n_calls = len(ws.get("tool_calls") or [])
            domain = ws.get("domain", "—")
            title = ws.get("title", "(untitled)")
            lines.append(f"- **{ws['_id']}** [{domain}] · {title} "
                         f"({n_calls} tool call{'s' if n_calls != 1 else ''})")
            summary = (ws.get("summary") or "").strip()
            if summary:
                lines.append(f"  · {summary[:200]}{'…' if len(summary) > 200 else ''}")
    return "\n".join(lines)


@mcp.tool()
def find_workstreams_about(text: str, limit: int = 5) -> str:
    """
    Search for workstreams whose summary, title, or entities match the
    given text. Uses Atlas Vector Search on the summary field if the
    `workstream_vector_index` is configured; otherwise falls back to a
    plain regex match on title + entities.

    Args:
        text:   Free-text search query (e.g. 'Munich store', 'QoS uplift').
        limit:  Max results (default 5, max 20).
    """
    limit = max(1, min(20, int(limit or 5)))

    # Try vector search first (best results); fall back if index not ready.
    matches = []
    try:
        cursor = workstreams.aggregate([
            {"$vectorSearch": {
                "index":   "workstream_vector_index",
                "path":    "summary",
                "query":   text,
                "numCandidates": 50,
                "limit":   limit,
            }},
            {"$project": {
                "_id": 1, "title": 1, "domain": 1, "state": 1,
                "entities": 1, "summary": 1, "last_activity": 1,
                "tool_calls": 1,
                "score": {"$meta": "vectorSearchScore"},
            }},
        ])
        matches = list(cursor)
    except Exception:
        matches = []

    if not matches:
        # Plain text fallback
        regex = {"$regex": text, "$options": "i"}
        cursor = workstreams.find({
            "$or": [
                {"title":    regex},
                {"summary":  regex},
                {"entities": regex},
            ]
        }).sort("last_activity", DESCENDING).limit(limit)
        matches = list(cursor)
        mode = "regex"
    else:
        mode = "vector"

    if not matches:
        return f"No workstreams match {text!r}."

    lines = [f"**{len(matches)} match(es) for {text!r}** _(via {mode} search)_"]
    for ws in matches:
        lines.append("")
        score = ws.get("score")
        if score is not None:
            lines.append(_fmt_card(ws) + f"\n  similarity: {score:.3f}")
        else:
            lines.append(_fmt_card(ws))
    return "\n".join(lines)


@mcp.tool()
def close_workstream(workstream_id: str, note: str = "completed") -> str:
    """
    Mark a workstream as completed. Use when the user signals a thread is
    done ('we're done with Marienplatz', 'close that workstream').

    Args:
        workstream_id: Workstream id to close.
        note:          Optional closing note.
    """
    ws = workstreams.find_one({"_id": workstream_id})
    if not ws:
        return f"❌ Workstream {workstream_id} not found."
    if ws.get("state") == "completed":
        return f"ℹ️  Workstream {workstream_id} already completed."
    workstreams.update_one(
        {"_id": workstream_id},
        {"$set": {
            "state": "completed",
            "closed_at": datetime.datetime.now(),
            "close_note": note,
        }},
    )
    return (f"✓ Workstream {workstream_id} marked completed. Note: {note}\n"
            f"  Long-term memory extraction will run automatically — "
            f"watch for the [MEMORY] broadcast lines.")


@mcp.tool()
def delete_workstream(workstream_id: str, cascade_memories: bool = True) -> str:
    """
    REMOVE a workstream document from the database entirely. NOT the same
    as close_workstream — this is a true DELETE, the document is gone.
    Use when the user says 'delete WS-...', 'remove that workstream',
    'wipe WS-...'.

    By default, also deletes any extracted memories associated with the
    workstream (agent_memories.workstream_id == workstream_id), so the
    audit trail and the knowledge it produced disappear together. Pass
    cascade_memories=False to keep the memories as orphans.

    Args:
        workstream_id:    Workstream id to delete, e.g. 'WS-2026-05-23-001'.
        cascade_memories: When True (default), also delete the
                          workstream's extracted memories from
                          agent_memories.
    """
    ws = workstreams.find_one({"_id": workstream_id}, {"_id": 1, "state": 1, "title": 1})
    if not ws:
        return f"❌ Workstream {workstream_id} not found."
    mem_deleted = 0
    if cascade_memories:
        res = memories.delete_many({"workstream_id": workstream_id})
        mem_deleted = res.deleted_count
    workstreams.delete_one({"_id": workstream_id})
    title = ws.get("title") or "(untitled)"
    extra = f"; also removed {mem_deleted} associated memorie(s)" \
        if mem_deleted else ""
    return f"🗑 Workstream {workstream_id} ({title!r}) deleted{extra}."


@mcp.tool()
def delete_completed_workstreams(cascade_memories: bool = True) -> str:
    """
    BULK delete every workstream currently in state='completed'. Single
    server-side operation — avoids the orchestrator's 5-iteration ReAct
    cap that would otherwise stop a delete-one-at-a-time loop after the
    fifth workstream.

    Use when the user says 'delete all completed workstreams', 'purge
    completed workstreams', 'wipe completed workstreams', 'clear all
    finished workstreams'.

    By default, also cascade-deletes the agent_memories extracted from
    each removed workstream.

    Args:
        cascade_memories: When True (default), also delete the
                          associated extracted memories.
    """
    # Snapshot ids first so we can cascade and report counts cleanly.
    ids = [d["_id"] for d in workstreams.find(
        {"state": "completed"}, {"_id": 1})]
    if not ids:
        return "No completed workstreams to delete."
    mem_deleted = 0
    if cascade_memories:
        mres = memories.delete_many({"workstream_id": {"$in": ids}})
        mem_deleted = mres.deleted_count
    wres = workstreams.delete_many({"_id": {"$in": ids}})
    extra = f"; also removed {mem_deleted} associated memorie(s)" \
        if mem_deleted else ""
    return (f"🗑 Deleted {wres.deleted_count} completed workstream(s) "
            f"in one bulk operation{extra}. Ids: "
            f"{', '.join(ids[:10])}"
            + (f" … (+{len(ids) - 10} more)" if len(ids) > 10 else ""))


# ─── Long-term memory tools ────────────────────────────────────────────────
#
# agent_memories is populated by the orchestrator's background extraction
# task when a workstream closes. These tools expose read access so the
# user can ask the agent "what do you remember about X?" explicitly.

_TIER_EMOJI = {"core": "⭐", "extracted": "💎", "decayed": "🍂"}


def _fmt_memory(m: dict, score: float | None = None) -> str:
    tier = m.get("tier") or "extracted"
    tier_badge = f"{_TIER_EMOJI.get(tier, '•')} {tier.upper()}"
    bits = [f"`{m.get('_id','?')}`",
            tier_badge,
            f"_{m.get('category','fact')}_",
            f"[{m.get('domain','—')}]"]
    n = int(m.get("recall_count") or 0)
    if n:
        bits.append(f"recalled {n}×")
    if score is not None:
        bits.append(f"similarity {score:.3f}")
    elif m.get("confidence") is not None:
        bits.append(f"confidence {float(m['confidence']):.2f}")
    line = " · ".join(bits)
    ents = ", ".join((m.get("entities") or [])[:6])
    src  = m.get("workstream_id", "—")
    body = f"  {m.get('text','')}".strip()
    extra = []
    if ents:
        extra.append(f"entities: {ents}")
    extra.append(f"from {src}")
    return f"{line}\n  {body}\n  _{' · '.join(extra)}_"


@mcp.tool()
def recall_facts(text: str = "", domain: str = None, limit: int = 5) -> str:
    """
    Recall reusable facts extracted from closed workstreams. Uses Atlas
    Vector Search on the memory text when the `agent_memories_index` is
    configured; otherwise falls back to entity/domain match.

    Use this when the user says:
      - "what do you remember about <X>?"
      - "any past learnings about <topic>?"
      - "recall facts about Marienplatz" / "things you learned about Alpenmarkt"

    Args:
        text:   Free-text query — what topic / entity to recall about.
                Empty = list recent memories regardless of topic.
        domain: Optional filter by domain (ibn, dtw, todo, …).
        limit:  Max results (default 5, max 20).
    """
    limit = max(1, min(20, int(limit or 5)))
    query = (text or "").strip()
    matches = []
    mode = "—"

    if query:
        try:
            vs_spec = {
                "index":   "agent_memories_index",
                "path":    "text",
                "query":   query,
                "numCandidates": 50,
                "limit":   limit,
            }
            if domain:
                vs_spec["filter"] = {"domain": {"$eq": domain}}
            cursor = memories.aggregate([
                {"$vectorSearch": vs_spec},
                {"$project": {
                    "_id": 1, "text": 1, "category": 1, "domain": 1,
                    "entities": 1, "confidence": 1, "workstream_id": 1,
                    "extracted_at": 1, "tier": 1, "recall_count": 1,
                    "last_recalled_at": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }},
            ])
            matches = list(cursor)
            mode = "vector"
        except Exception:
            matches = []

    if not matches:
        # Fallback: regex / domain match / recency
        q: dict = {}
        if domain:
            q["domain"] = domain
        if query:
            q["$or"] = [
                {"text":     {"$regex": query, "$options": "i"}},
                {"entities": {"$regex": query, "$options": "i"}},
            ]
        matches = list(memories.find(q).sort("extracted_at", -1).limit(limit))
        mode = "regex/recency" if query else "recency"

    if not matches:
        scope = f" matching {query!r}" if query else ""
        return f"No facts recalled{scope}. (Memories are extracted when " \
               "workstreams close — close some completed work first.)"

    lines = [f"**{len(matches)} fact(s)** _(via {mode})_"]
    for m in matches:
        lines.append("")
        lines.append(_fmt_memory(m, score=m.get("score")))
    return "\n".join(lines)


@mcp.tool()
def list_memories(limit: int = 20) -> str:
    """
    List the most-recently extracted memories across all workstreams.

    Args:
        limit: Max memories to show (default 20, max 100).
    """
    limit = max(1, min(100, int(limit or 20)))
    rows = list(memories.find({}).sort("extracted_at", -1).limit(limit))
    if not rows:
        return ("No memories yet. Memories are extracted automatically when "
                "workstreams transition to state=completed.")
    # Tier-first grouping: core knowledge surfaces at the top, decayed
    # facts at the bottom so the structure of the memory plane is visible.
    tier_order = ["core", "extracted", "decayed"]
    by_tier: dict = {t: [] for t in tier_order}
    for m in rows:
        by_tier.setdefault(m.get("tier") or "extracted", []).append(m)
    n_core    = len(by_tier.get("core")      or [])
    n_extr    = len(by_tier.get("extracted") or [])
    n_decayed = len(by_tier.get("decayed")   or [])
    lines = [
        f"**{len(rows)} memor{'ies' if len(rows) != 1 else 'y'}** — "
        f"⭐ {n_core} core · 💎 {n_extr} extracted · 🍂 {n_decayed} decayed",
    ]
    for t in tier_order:
        bucket = by_tier.get(t) or []
        if not bucket:
            continue
        lines.append("")
        lines.append(f"### {_TIER_EMOJI.get(t, '•')} {t.upper()} ({len(bucket)})")
        for m in bucket:
            lines.append("")
            lines.append(_fmt_memory(m))
    return "\n".join(lines)


@mcp.tool()
def forget_memory(memory_id: str) -> str:
    """
    Delete a memory by id. Use when the user explicitly disavows a fact
    ('forget that, it was wrong').

    Args:
        memory_id: Memory id, e.g. 'MEM-2026-05-23-001-02'.
    """
    res = memories.delete_one({"_id": memory_id})
    if res.deleted_count == 0:
        return f"❌ Memory {memory_id} not found."
    return f"✓ Memory {memory_id} forgotten."


if __name__ == "__main__":
    mcp.run()
