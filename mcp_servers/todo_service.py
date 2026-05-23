# mcp_servers/todo_service.py

# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz

"""
SERVER: TODO List & Task Management (MongoDB-backed)

Manage tasks, reminders, and action items for the user.

Use this service when users say:
- Explicit: "add todo", "create task", "remind me to", "add to my list", "put on my todo"
- Implicit: "I need to", "I have to", "I should", "I must", "don't forget to"
- Planning: "tomorrow I need to", "this week I should", "later I have to"
- Multiple tasks: "I need to X, Y, and Z", comma-separated action lists
- Questions: "what are my tasks", "show my todos", "what's on my list"

Capabilities:
- Add single or multiple tasks at once
- List active and completed tasks
- Mark tasks as completed
- Delete tasks permanently
- Bulk operations: clear all completed, delete every task
- Persistent storage in MongoDB (agent_registry.todos)

Important: when the user asks to remove MANY tasks at once ('delete
all my TODOs', 'wipe my list', 'clear everything', 'remove all
completed'), prefer the bulk tools (clear_completed_todos /
delete_all_todos) over iterating delete_todo(id). The orchestrator's
ReAct loop caps at 5 iterations per turn — one bulk call handles any
N tasks in a single operation.

Examples:
- "Add 'buy milk' to my todo list"
- "I need to call John and send that email"
- "Remind me to check the report tomorrow"
- "What's on my todo list?"

ID semantics:
- Task ids are sequential integers (#1, #2, ...). When the collection
  is empty (no active AND no completed), the next add starts from #1.
  Otherwise the next id is max(existing) + 1 — completed tasks count
  toward the running max, so the id space is monotonic until a true
  wipe (delete_all_todos).
"""

import datetime
import json
import logging
import os
from pathlib import Path

from pymongo import MongoClient, DESCENDING, ASCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("todo_service")
logger = logging.getLogger("todo_service")

_mongo = MongoClient(os.environ["MONGODB_URI"])
_db    = _mongo["agent_registry"]
todos  = _db["todos"]

# Ensure deterministic ordering on _id (Mongo's default for ints is fine,
# but the explicit index documents intent and speeds the max() query).
todos.create_index([("_id", ASCENDING)])
todos.create_index([("completed", ASCENDING)])

# One-shot migration from the legacy /tmp/todos.json. Runs once: if the
# file exists AND the Mongo collection is empty, copy the file contents
# into Mongo and rename the file with a .migrated suffix so it never
# triggers again. This is non-destructive on re-runs — we never
# overwrite existing Mongo data.
_LEGACY_FILE = Path("/tmp/todos.json")


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def _migrate_legacy_file_if_needed():
    if not _LEGACY_FILE.exists():
        return
    if todos.count_documents({}) > 0:
        return
    try:
        data = json.loads(_LEGACY_FILE.read_text())
        tasks = data.get("tasks") or []
        if tasks:
            docs = [{
                "_id":          t["id"],
                "task":         t.get("task", ""),
                "completed":    bool(t.get("completed", False)),
                "created_at":   _parse_dt(t.get("created_at"))
                                  or datetime.datetime.now(),
                "updated_at":   _parse_dt(t.get("updated_at")),
            } for t in tasks if "id" in t]
            if docs:
                todos.insert_many(docs)
                logger.info(f"Migrated {len(docs)} task(s) from "
                            f"{_LEGACY_FILE} to agent_registry.todos")
        migrated = _LEGACY_FILE.with_suffix(".json.migrated")
        _LEGACY_FILE.rename(migrated)
    except Exception as e:
        logger.error(f"Legacy file migration failed: {e}")


_migrate_legacy_file_if_needed()


def _next_id() -> int:
    """Sequential id: max(_id) + 1, or 1 if collection is empty."""
    last = todos.find_one(sort=[("_id", DESCENDING)], projection={"_id": 1})
    return (last["_id"] + 1) if last else 1


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime.datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return "—"


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def add_todo(task: str) -> str:
    """
    Add a new TODO task to the list.

    Args:
        task: Task description (what needs to be done)

    Returns:
        Confirmation message with task ID
    """
    logger.info(f"Adding task: {task!r}")
    task_id = _next_id()
    now = datetime.datetime.now()
    todos.insert_one({
        "_id":        task_id,
        "task":       task,
        "completed":  False,
        "created_at": now,
        "updated_at": None,
    })
    return f"✅ Added task #{task_id}: {task}"


@mcp.tool()
def list_todos(show_completed: bool = False) -> str:
    """
    List TODO tasks.

    Args:
        show_completed: If True, also include completed tasks. Default
                        False (active only).

    Returns:
        Formatted list of tasks with IDs and status. When the active
        list is empty but completed tasks exist, the response includes
        a hint so you know the slate isn't fully clean.
    """
    logger.info(f"Listing tasks (show_completed={show_completed})")
    active_cur = todos.find({"completed": False}).sort("_id", ASCENDING)
    active = list(active_cur)
    completed_count = todos.count_documents({"completed": True})

    lines = []

    if active:
        lines.append("📋 **Active Tasks:**")
        for t in active:
            lines.append(
                f"  ○ #{t['_id']}: {t['task']} "
                f"(created: {_fmt_ts(t.get('created_at'))})"
            )
    else:
        lines.append("📋 No active tasks!")
        if completed_count and not show_completed:
            lines.append(
                f"  ℹ️  {completed_count} completed task(s) hidden — call "
                f"list_todos(show_completed=True) to see them, "
                f"clear_completed_todos() to remove them, "
                f"or delete_all_todos() to wipe everything "
                f"(after which new tasks start from #1)."
            )

    if show_completed and completed_count:
        completed_cur = todos.find(
            {"completed": True}).sort("_id", ASCENDING)
        lines.append("")
        lines.append(f"✅ **Completed Tasks ({completed_count}):**")
        for t in completed_cur:
            extra = ""
            if t.get("updated_at"):
                extra = f", completed: {_fmt_ts(t['updated_at'])}"
            lines.append(
                f"  ✓ #{t['_id']}: {t['task']} "
                f"(created: {_fmt_ts(t.get('created_at'))}{extra})"
            )

    return "\n".join(lines)


@mcp.tool()
def complete_todo(task_id: int) -> str:
    """
    Mark a task as completed.

    Args:
        task_id: The ID of the task to complete (from list_todos)
    """
    logger.info(f"Completing task #{task_id}")
    t = todos.find_one({"_id": int(task_id)})
    if not t:
        return f"❌ Task #{task_id} not found."
    if t.get("completed"):
        return f"ℹ️ Task #{task_id} is already completed."
    todos.update_one(
        {"_id": int(task_id)},
        {"$set": {"completed": True,
                  "updated_at": datetime.datetime.now()}},
    )
    return f"✅ Completed task #{task_id}: {t['task']}"


@mcp.tool()
def delete_todo(task_id: int) -> str:
    """
    Permanently delete a single task from the list.

    Args:
        task_id: The ID of the task to delete
    """
    logger.info(f"Deleting task #{task_id}")
    t = todos.find_one_and_delete({"_id": int(task_id)})
    if not t:
        return f"❌ Task #{task_id} not found."
    return f"🗑️ Deleted task #{task_id}: {t['task']}"


@mcp.tool()
def clear_completed_todos() -> str:
    """
    BULK: Delete all completed tasks at once. Single-call alternative
    to iterating delete_todo(id) over each completed task — avoids
    the orchestrator's 5-iteration ReAct cap.

    Use when the user says 'clear completed', 'delete completed tasks',
    'remove finished todos', 'purge done items'.
    """
    logger.info("Clearing all completed tasks")
    res = todos.delete_many({"completed": True})
    n = res.deleted_count
    if n == 0:
        return "ℹ️ No completed tasks to clear."
    return f"🗑️ Cleared {n} completed task(s)"


@mcp.tool()
def delete_all_todos() -> str:
    """
    NUCLEAR: Delete EVERY task from the TODO list — active AND completed.
    Single-call bulk operation; avoids the orchestrator's 5-iteration cap.

    Use when the user says 'delete all TODOs', 'wipe my list', 'clear
    everything', 'remove all tasks', 'reset my TODO list'.

    Stronger than clear_completed_todos (which keeps active tasks).
    After this call, the next add_todo starts from #1.
    """
    logger.info("Deleting all tasks")
    n = todos.count_documents({})
    if n == 0:
        return "ℹ️ TODO list is already empty."
    todos.delete_many({})
    return (f"💥 Nuclear delete: removed {n} task(s). TODO list is now "
            f"empty — next add_todo will start from #1.")


if __name__ == "__main__":
    logger.info("🚀 Starting TODO Service (MongoDB-backed)…")
    mcp.run()
