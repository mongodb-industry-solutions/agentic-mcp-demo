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
import io
import os
from contextlib import redirect_stdout
from typing import Awaitable, Callable

import seed.ibn_seed as ibn_seed
import seed.dtw_seed as dtw_seed
from pymongo import MongoClient


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
