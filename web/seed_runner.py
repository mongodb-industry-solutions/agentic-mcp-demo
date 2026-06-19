#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Programmatic demo re-seed — the `python seed/ibn_seed.py --reset` +
`python seed/dtw_seed.py --reset` sequence, callable from the web shell
and streamed live to the browser.

The seed scripts already expose their work as discrete phase functions
(reset / ensure_indexes / insert_all / … each taking a `db` handle), so
we drive those directly rather than shelling out — one shared client,
and we can intercept their print() output line-by-line.

Phase-B seam: every entry point takes an explicit `db_name`. Today the
web shell always passes the shared "agent_registry"; a future
session-scoped variant passes "agent_registry__<session>" and nothing
else here changes. See MULTI_SESSION_PLAN.md.
"""

import asyncio
import copy
import datetime
import io
import os
import random
from contextlib import redirect_stdout
from typing import Awaitable, Callable

import seed.ibn_seed as ibn_seed
import seed.dtw_seed as dtw_seed
from pymongo import ASCENDING, DESCENDING, MongoClient


# The mutable demo collections — the only ones a browser session needs
# its own copy of (everything else is read-only reference data, shared
# across sessions). Must match the DEMO_PREFIX-prefixed collections in
# the MCP servers. See MULTI_SESSION_PLAN.md.
SESSION_MUTABLE_BASES = [
    "ibn_intents",
    "ibn_telemetry",            # time-series
    "ibn_compliance_events",
    "ibn_policy_snapshots",
    "dtw_scenarios",
]


# The full reset+seed pipeline as (label, fn) pairs. fn(db) mirrors one
# step of the CLI seeders' main(); order matches them exactly.
_IBN_PHASES = [
    ("IBN · drop collections",     ibn_seed.reset),
    ("IBN · telemetry timeseries", ibn_seed.ensure_telemetry_timeseries),
    ("IBN · indexes",              ibn_seed.ensure_indexes),
    ("IBN · fixtures",             ibn_seed.insert_all),
    ("IBN · baseline telemetry",   ibn_seed.seed_baseline_telemetry),
    ("IBN · vector index",         ibn_seed.create_vector_index),
]
_DTW_PHASES = [
    ("DTW · drop collections", dtw_seed.reset),
    ("DTW · indexes",          dtw_seed.ensure_indexes),
    ("DTW · fixtures",         dtw_seed.insert_all),
    ("DTW · vector index",     dtw_seed.create_vector_index),
]


def _run_phases(db, emit) -> None:
    """Synchronous: run every phase against `db`, forwarding each phase's
    stdout to emit(line). Raises on the first phase that fails so the
    caller can surface it."""
    class _LineTee(io.TextIOBase):
        def write(self, s):
            for line in s.splitlines():
                if line.strip():
                    emit(line.rstrip())
            return len(s)

    tee = _LineTee()
    for label, fn in (_IBN_PHASES + _DTW_PHASES):
        emit(f"━━ {label} ━━")
        with redirect_stdout(tee):
            fn(db)
    emit("✅ Re-seed complete — fresh demo data is live.")


async def reset_and_seed(
    db_name: str,
    emit: Callable[[str], Awaitable[None]],
) -> None:
    """Run the blocking reset+seed in a worker thread and stream its
    output to the async `emit` callback as lines are produced.

    Args:
        db_name: target database (Phase-A: always "agent_registry").
        emit:    async callable invoked once per progress line.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def sync_emit(line: str) -> None:
        # Called from the worker thread — hop back onto the event loop.
        loop.call_soon_threadsafe(queue.put_nowait, line)

    def blocking() -> None:
        client = MongoClient(os.environ["MONGODB_URI"])
        try:
            _run_phases(client[db_name], sync_emit)
        finally:
            client.close()
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    worker = asyncio.create_task(asyncio.to_thread(blocking))
    while True:
        line = await queue.get()
        if line is None:
            break
        await emit(line)
    await worker  # re-raise any exception from the worker thread


# ── Phase B: per-session lane seeding (prefixed mutable collections) ──────

def _seed_session_sync(db, prefix: str, emit) -> None:
    """Drop and re-seed ONLY this session's mutable collections (prefixed)
    to their pristine fixture state. Shared reference data and the Atlas
    vector indexes on the *_knowledge_chunks collections are never
    touched. Runs against the shared agent_registry database — the Atlas
    credential can drop collections there (it cannot drop databases),
    which is exactly why Phase B namespaces by prefix rather than by DB."""
    P = prefix

    emit(f"━━ session {prefix} · drop mutable collections ━━")
    for base in SESSION_MUTABLE_BASES:
        db[P + base].drop()
        # A time-series collection leaves a system.buckets sibling.
        if base == "ibn_telemetry":
            try:
                db.drop_collection("system.buckets." + P + base)
            except Exception:
                pass
    emit("    dropped: " + ", ".join(P + b for b in SESSION_MUTABLE_BASES))

    emit(f"━━ session {prefix} · telemetry timeseries ━━")
    db.create_collection(
        P + "ibn_telemetry",
        timeseries={"timeField": "ts", "metaField": "meta",
                    "granularity": "seconds"},
    )
    emit(f"    created {P}ibn_telemetry (time-series)")

    emit(f"━━ session {prefix} · fixtures ━━")
    # deepcopy so pymongo's _id injection doesn't mutate the shared
    # module-level fixture lists across repeated seeds.
    db[P + "ibn_intents"].insert_many(copy.deepcopy(ibn_seed.INTENTS))
    db[P + "ibn_policy_snapshots"].insert_many(
        copy.deepcopy(ibn_seed.POLICY_SNAPSHOTS))
    emit(f"    {len(ibn_seed.INTENTS)} intents, "
         f"{len(ibn_seed.POLICY_SNAPSHOTS)} policy snapshots")
    # dtw_scenarios + ibn_compliance_events start empty (populated at
    # runtime by tool calls), so nothing to insert.

    emit(f"━━ session {prefix} · baseline telemetry ━━")
    telem = db[P + "ibn_telemetry"]
    now = datetime.datetime.now()
    n = 120
    total = 0
    for intent in ibn_seed.INTENTS:
        if intent.get("status") != "active":
            continue
        threshold = intent["parsed"]["targets"].get("pos_latency_ms", 40)
        lo = max(15, threshold - 18)
        hi = max(20, threshold - 8)
        telem.insert_many([
            {"ts": now - datetime.timedelta(seconds=n - 1 - i),
             "meta": {"intent_id": intent["_id"],
                      "site_id":   intent["site_id"],
                      "metric":    "pos_latency_ms"},
             "value": round(random.uniform(lo, hi), 1)}
            for i in range(n)
        ])
        total += n
    emit(f"    seeded {total} baseline telemetry samples")

    emit(f"━━ session {prefix} · indexes ━━")
    try:
        db[P + "ibn_intents"].create_index([("status", ASCENDING)],
                                           name="intent_status")
        db[P + "ibn_intents"].create_index([("site_id", ASCENDING)],
                                           name="intent_site")
        db[P + "ibn_compliance_events"].create_index(
            [("intent_id", ASCENDING), ("ts", -1)],
            name="compliance_by_intent_ts")
        db[P + "dtw_scenarios"].create_index([("status", ASCENDING)],
                                             name="scenario_status")
    except Exception as e:
        emit(f"    ⚠ index ensure skipped: {e}")
    emit(f"✅ session {prefix} re-seeded (mutable collections only; "
         f"shared reference + vector indexes untouched).")


async def reset_session(prefix: str, db_name: str,
                        emit: Callable[[str], Awaitable[None]]) -> None:
    """Async wrapper around _seed_session_sync — streams progress lines
    to emit. Used by the web shell's per-session Reset button."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def sync_emit(line: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, line)

    def blocking() -> None:
        client = MongoClient(os.environ["MONGODB_URI"])
        try:
            _seed_session_sync(client[db_name], prefix, sync_emit)
        finally:
            client.close()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    worker = asyncio.create_task(asyncio.to_thread(blocking))
    while True:
        line = await queue.get()
        if line is None:
            break
        await emit(line)
    await worker


async def ensure_session_seeded(prefix: str, db_name: str) -> bool:
    """Seed a session's lane on first use. No-op (returns False) if the
    lane already has data; otherwise seeds it and returns True. Silent —
    no progress streaming (called at connection time, not on demand)."""
    def blocking() -> bool:
        client = MongoClient(os.environ["MONGODB_URI"])
        try:
            db = client[db_name]
            if db[prefix + "ibn_intents"].count_documents({}, limit=1):
                return False
            _seed_session_sync(db, prefix, lambda _l: None)
            return True
        finally:
            client.close()
    return await asyncio.to_thread(blocking)
