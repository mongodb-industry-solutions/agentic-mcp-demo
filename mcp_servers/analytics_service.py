#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Analytics Service — read-only observability over the orchestrator's
routing decisions.

The orchestrator writes one document per process_query call to
`agent_registry.routing_decisions`, capturing what Stage 1, Stage 2,
the memory layer, and the ReAct loop did. This service exposes
aggregations on top of that collection so the user (or the agent
itself) can ask:

- "What's my LLM tie-break rate today?"
- "Which routing decisions were slow?"
- "Which queries resulted in no tool calls (likely routing misses)?"
- "Which services are getting routed to the most?"
- "How often does the memory layer recall facts?"

Use this service when users say:
- Routing stats:   "how is the routing performing", "routing summary",
                   "show me routing metrics", "what's my tie-break rate"
- Slow routes:    "any slow routings", "which decisions are slow",
                  "routing latency"
- Misses:         "any routing misses", "queries with no tool calls",
                  "what failed to route"
- Service usage:  "which services are used most", "service distribution",
                  "service usage stats"

This service is READ-ONLY over routing_decisions. The orchestrator owns
writes. It does not orchestrate, classify, or modify anything else.
"""

import datetime
import logging
import os

from pymongo import MongoClient
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("analytics_service")
logger        = logging.getLogger("analytics_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
decisions     = db["routing_decisions"]


def _window(hours: int) -> dict:
    """Build a $match stage that scopes the aggregation to the last `hours`."""
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=max(1, hours))
    return {"$match": {"ts": {"$gte": cutoff}}}


def _ms_to_str(ms) -> str:
    if ms is None:
        return "—"
    try:
        ms = float(ms)
    except Exception:
        return str(ms)
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{int(ms)}ms"


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def routing_summary(hours: int = 24) -> str:
    """
    Overall routing health: turn count, Stage 1 method breakdown, Stage 2
    winner-path breakdown, LLM tie-break rate, latency percentiles, and the
    fraction of turns that landed in zero tool calls (routing misses).

    Args:
        hours: Look-back window in hours (default 24, max 720).
    """
    hours = max(1, min(720, int(hours or 24)))
    pipeline = [
        _window(hours),
        {"$group": {
            "_id":            None,
            "turns":          {"$sum": 1},
            "tool_calls":     {"$sum": {"$ifNull": ["$outcome.tool_calls_count", 0]}},
            "no_service":     {"$sum": {"$cond": ["$outcome.no_services_found", 1, 0]}},
            "memory_recall":  {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$memory.recalled_count", 0]}, 0]}, 1, 0]}},
            "replay_count":   {"$sum": {"$cond": ["$outcome.had_replay_recipe", 1, 0]}},
            "max_iter_hit":   {"$sum": {"$cond": ["$outcome.max_iterations_hit", 1, 0]}},
            "avg_total_ms":   {"$avg": "$outcome.duration_ms"},
            "p50_total_ms":   {"$percentile": {
                "input": "$outcome.duration_ms", "p": [0.5], "method": "approximate"}},
            "p95_total_ms":   {"$percentile": {
                "input": "$outcome.duration_ms", "p": [0.95], "method": "approximate"}},
        }},
    ]
    try:
        agg = list(decisions.aggregate(pipeline))
    except Exception as e:
        return f"⚠ aggregation failed: {e}"
    if not agg:
        return f"No routing decisions recorded in the last {hours}h."
    r = agg[0]
    n = r["turns"]

    # Stage 1 + Stage 2 method breakdowns
    s1 = list(decisions.aggregate([
        _window(hours),
        {"$group": {"_id": "$stage1.method", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]))
    s2 = list(decisions.aggregate([
        _window(hours),
        {"$group": {"_id": "$stage2.method", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]))

    def _pct(num, denom):
        return f"{(100.0 * num / denom):.1f}%" if denom else "—"

    llm_tiebreak_n = sum(x["n"] for x in s2 if "llm" in (x["_id"] or ""))

    lines = [
        f"## Routing analytics — last {hours}h",
        f"- Turns:               **{n}**",
        f"- Total tool calls:    {r['tool_calls']}  ({r['tool_calls']/n:.2f} per turn)",
        f"- Memory recall rate:  {_pct(r['memory_recall'], n)}",
        f"- Replay turns:        {r['replay_count']}  ({_pct(r['replay_count'], n)})",
        f"- LLM tie-break rate:  {_pct(llm_tiebreak_n, n)} _(target: keep low)_",
        f"- Routing misses:      {r['no_service']}  ({_pct(r['no_service'], n)})",
        f"- Max-iterations hit:  {r['max_iter_hit']}  ({_pct(r['max_iter_hit'], n)})",
        "",
        f"**Latency:** avg {_ms_to_str(r['avg_total_ms'])} · "
        f"p50 {_ms_to_str((r['p50_total_ms'] or [None])[0])} · "
        f"p95 {_ms_to_str((r['p95_total_ms'] or [None])[0])}",
        "",
        "### Stage 1 methods",
    ]
    for row in s1:
        lines.append(f"- `{row['_id'] or '—'}`: {row['n']}  ({_pct(row['n'], n)})")
    lines.append("")
    lines.append("### Stage 2 winner-path methods")
    for row in s2:
        lines.append(f"- `{row['_id'] or '—'}`: {row['n']}  ({_pct(row['n'], n)})")
    return "\n".join(lines)


@mcp.tool()
def routing_misses(hours: int = 24, limit: int = 10) -> str:
    """
    List queries that produced zero tool calls (suggesting a routing miss
    or an LLM that refused to act on the selected services). Useful for
    surfacing where the routing pipeline needs tuning.

    Args:
        hours: Look-back window in hours (default 24).
        limit: Max queries to return (default 10, max 50).
    """
    hours = max(1, min(720, int(hours or 24)))
    limit = max(1, min(50, int(limit or 10)))
    cur = decisions.find({
        "ts": {"$gte": datetime.datetime.now() - datetime.timedelta(hours=hours)},
        "$or": [
            {"outcome.tool_calls_count": {"$lte": 0}},
            {"outcome.tool_calls_count": {"$exists": False}},
            {"outcome.no_services_found": True},
        ],
    }, {
        "_id": 0, "ts": 1, "query": 1, "workstream_id": 1,
        "stage1.method": 1, "stage1.domains_selected": 1,
        "stage2.method": 1, "stage2.winner_services": 1,
        "outcome.tool_calls_count": 1, "outcome.no_services_found": 1,
    }).sort("ts", -1).limit(limit)
    rows = list(cur)
    if not rows:
        return f"No routing misses in the last {hours}h."
    lines = [f"**{len(rows)} routing miss(es) in the last {hours}h:**"]
    for d in rows:
        ts = d.get("ts")
        ts_str = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime.datetime) else "—"
        s1 = (d.get("stage1") or {})
        s2 = (d.get("stage2") or {})
        sel = ", ".join(s1.get("domains_selected") or []) or "—"
        winner = ", ".join(s2.get("winner_services") or []) or "—"
        lines.append("")
        lines.append(f"- **{ts_str}** · `{d.get('workstream_id') or '—'}`")
        lines.append(f"  > {(d.get('query') or '')[:160]}")
        lines.append(f"  Stage 1: `{s1.get('method')}` → {sel}")
        lines.append(f"  Stage 2: `{s2.get('method')}` → {winner}")
    return "\n".join(lines)


@mcp.tool()
def slow_routing(threshold_ms: int = 5000, hours: int = 24, limit: int = 10) -> str:
    """
    List the slowest routing decisions, ranked by total turn duration.
    Use to spot which queries are spending too long in routing+ReAct.

    Args:
        threshold_ms: Minimum total turn duration in ms (default 5000).
        hours:        Look-back window in hours (default 24).
        limit:        Max queries to return (default 10).
    """
    hours = max(1, min(720, int(hours or 24)))
    limit = max(1, min(50, int(limit or 10)))
    threshold_ms = max(0, int(threshold_ms or 0))
    cur = decisions.find({
        "ts": {"$gte": datetime.datetime.now() - datetime.timedelta(hours=hours)},
        "outcome.duration_ms": {"$gte": threshold_ms},
    }, {
        "_id": 0, "ts": 1, "query": 1, "outcome.duration_ms": 1,
        "stage1.duration_ms": 1, "stage1.method": 1,
        "stage2.method": 1, "stage2.winner_services": 1,
        "outcome.tool_calls_count": 1, "outcome.iterations_used": 1,
    }).sort("outcome.duration_ms", -1).limit(limit)
    rows = list(cur)
    if not rows:
        return f"No routing decisions ≥ {threshold_ms}ms in the last {hours}h."
    lines = [f"**Slowest {len(rows)} routing decision(s) in the last {hours}h:**"]
    for d in rows:
        outcome = d.get("outcome") or {}
        s1 = d.get("stage1") or {}
        s2 = d.get("stage2") or {}
        winner = ", ".join(s2.get("winner_services") or []) or "—"
        lines.append("")
        lines.append(f"- **{_ms_to_str(outcome.get('duration_ms'))}** total · "
                     f"Stage 1 `{s1.get('method')}` ({_ms_to_str(s1.get('duration_ms'))}) "
                     f"→ Stage 2 `{s2.get('method')}` → `{winner}`")
        lines.append(f"  > {(d.get('query') or '')[:160]}")
        lines.append(f"  iterations: {outcome.get('iterations_used') or 0} · "
                     f"tool calls: {outcome.get('tool_calls_count') or 0}")
    return "\n".join(lines)


@mcp.tool()
def service_usage(hours: int = 24, limit: int = 20) -> str:
    """
    Most-frequently routed services. Useful for understanding which
    capabilities the user actually exercises vs which are dormant.

    Args:
        hours: Look-back window in hours (default 24).
        limit: Max services to return (default 20).
    """
    hours = max(1, min(720, int(hours or 24)))
    limit = max(1, min(100, int(limit or 20)))
    pipeline = [
        _window(hours),
        {"$unwind": {"path": "$stage2.winner_services", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id":  "$stage2.winner_services",
            "n":    {"$sum": 1},
            "last": {"$max": "$ts"},
        }},
        {"$sort": {"n": -1}},
        {"$limit": limit},
    ]
    try:
        rows = list(decisions.aggregate(pipeline))
    except Exception as e:
        return f"⚠ aggregation failed: {e}"
    if not rows:
        return f"No services routed to in the last {hours}h."
    total = sum(r["n"] for r in rows)
    lines = [f"**Service usage — last {hours}h** ({total} routings counted across "
             f"{len(rows)} services)"]
    for r in rows:
        last = r.get("last")
        last_str = last.strftime("%Y-%m-%d %H:%M") if isinstance(last, datetime.datetime) else "—"
        pct = 100.0 * r["n"] / total
        lines.append("")
        lines.append(f"- `{r['_id']}`: **{r['n']}** routings  ({pct:.1f}%) · last {last_str}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
