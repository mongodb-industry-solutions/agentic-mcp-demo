#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Shared shell-history storage in MongoDB.

The terminal shell (main.py) and the web shell (web/shell.py) both keep a
cursor-up command history. Until now that history was a file in $HOME
(written by Python's readline). Putting it in MongoDB means:

  • History is shared across machines and across web/terminal sessions
    against the same Atlas cluster — no more file-sync question.
  • A workstation reinstall doesn't lose the history.
  • The demo's "Atlas is the agent's entire data plane" story extends
    to user input history as well.

Collection: agent_registry.agent_history
Doc shape:  {"_id": ObjectId, "text": str, "source": "terminal"|"web",
             "ts": datetime}

Reads use _id-desc sort (ObjectId is monotonic by creation time, so this is
correct without an explicit ts index). For demo-scale corpora (~10k entries)
no extra index is needed.

The module migrates the legacy ~/.agentic_demo_history file on first use
if the collection is empty — preserves the demo's existing history when
this upgrade lands. After migration the file is left intact as a backup;
nothing reads it anymore.
"""

import datetime
import os
from pathlib import Path

from pymongo import MongoClient


_LEGACY_FILE = Path(os.path.expanduser("~/.agentic_demo_history"))

# Lazily-initialised module-level connection so both shells share the same
# client + collection handle within a process. The Mongo driver pools
# automatically, so single-collection-per-process is the right granularity.
_client: MongoClient | None = None
_coll = None
_migration_attempted = False


def _conn():
    global _client, _coll
    if _coll is None:
        _client = MongoClient(os.environ["MONGODB_URI"])
        _coll = _client["agent_registry"]["agent_history"]
    return _coll


def _maybe_migrate_from_file(coll) -> None:
    """First-run migration: if the collection is empty and the legacy
    readline history file exists, import its entries (in order) so the
    upgrade lands without losing history."""
    global _migration_attempted
    if _migration_attempted:
        return
    _migration_attempted = True
    if coll.count_documents({}) > 0:
        return
    if not _LEGACY_FILE.exists():
        return

    # Decode via readline so the V2 escapes (\040, \042 …) are handled
    # correctly. readline is a process-wide singleton; we save and restore
    # its current state so we don't disturb anything else in the process.
    import readline
    before_n = readline.get_current_history_length()
    saved = [readline.get_history_item(i)
             for i in range(1, before_n + 1)] if before_n else []
    readline.clear_history()
    try:
        readline.read_history_file(str(_LEGACY_FILE))
    except Exception:
        # Restore previous state
        for item in saved:
            if item:
                readline.add_history(item)
        return

    n = readline.get_current_history_length()
    items = [readline.get_history_item(i) for i in range(1, n + 1)]

    # Restore process readline state to whatever it was
    readline.clear_history()
    for item in saved:
        if item:
            readline.add_history(item)

    items = [it for it in items if it]
    if not items:
        return

    # Spread synthetic timestamps so _id-desc ordering is preserved.
    base = datetime.datetime.now() - datetime.timedelta(days=30)
    docs = [{
        "text":   item,
        "source": "migration",
        "ts":     base + datetime.timedelta(seconds=i),
    } for i, item in enumerate(items)]
    coll.insert_many(docs)
    print(f"[history] migrated {len(docs)} entries "
          f"from {_LEGACY_FILE} → agent_registry.agent_history")


def read_recent(limit: int = 500) -> list[str]:
    """Return the most-recent `limit` entries, newest first."""
    coll = _conn()
    _maybe_migrate_from_file(coll)
    try:
        cur = coll.find({}, {"_id": 0, "text": 1}).sort("_id", -1).limit(limit)
        return [d["text"] for d in cur if d.get("text")]
    except Exception as e:
        print(f"[history] read failed: {e}")
        return []


def append(text: str, source: str = "unknown") -> None:
    """Append one entry. Skips back-to-back duplicates (same as readline's
    HIST_IGNOREDUPS)."""
    text = (text or "").strip()
    if not text:
        return
    coll = _conn()
    try:
        last = coll.find_one({}, {"_id": 0, "text": 1}, sort=[("_id", -1)])
        if last and last.get("text") == text:
            return
        coll.insert_one({
            "text":   text,
            "source": source,
            "ts":     datetime.datetime.now(),
        })
    except Exception as e:
        print(f"[history] append failed: {e}")
