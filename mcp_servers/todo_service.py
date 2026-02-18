# mcp_servers/todo_service.py

# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz

"""
SERVER: TODO List & Task Management

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
- Persistent storage (survives restarts)

Examples:
- "Add 'buy milk' to my todo list"
- "I need to call John and send that email"
- "Remind me to check the report tomorrow"
- "What's on my todo list?"
"""

import logging
import json
from pathlib import Path
from datetime import datetime
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp = FastMCP("todo_service")
logger = logging.getLogger("todo_service")

TODO_FILE = Path("/tmp/todos.json")

def _load_todos() -> dict:
    """Load todos from file"""
    if TODO_FILE.exists():
        try:
            return json.loads(TODO_FILE.read_text())
        except:
            logger.error("Failed to load todos.json, creating new")
            return {"tasks": []}
    return {"tasks": []}

def _save_todos(data: dict):
    """Save todos to file"""
    try:
        TODO_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info(f"Saved {len(data['tasks'])} tasks")
    except Exception as e:
        logger.error(f"Failed to save todos: {e}")

@mcp.tool()
def add_todo(task: str) -> str:
    """
    Add a new TODO task to the list.

    Args:
        task: Task description (what needs to be done)

    Returns:
        Confirmation message with task ID

    Example:
        add_todo("Review MongoDB slides")
        add_todo("Buy groceries")
    """
    logger.info(f"Adding task: '{task}'")

    todos = _load_todos()

    # Generate new ID
    task_id = max([t["id"] for t in todos["tasks"]], default=0) + 1

    # Create task
    new_task = {
        "id": task_id,
        "task": task,
        "completed": False,
        "created_at": datetime.now().isoformat(),
        "updated_at": None
    }

    todos["tasks"].append(new_task)
    _save_todos(todos)

    return f"✅ Added task #{task_id}: {task}"

@mcp.tool()
def list_todos(show_completed: bool = False) -> str:
    """
    List all TODO tasks.

    Args:
        show_completed: If True, also show completed tasks (default: False)

    Returns:
        Formatted list of tasks with IDs and status

    Example:
        list_todos() → Shows only active tasks
        list_todos(show_completed=True) → Shows all tasks
    """
    logger.info(f"Listing tasks (show_completed={show_completed})")

    todos = _load_todos()

    if not todos["tasks"]:
        return "📋 No tasks in your TODO list."

    # Separate active and completed
    active = [t for t in todos["tasks"] if not t["completed"]]
    completed = [t for t in todos["tasks"] if t["completed"]]

    result = []

    # Active tasks
    if active:
        result.append("📋 **Active Tasks:**")
        for task in active:
            created = datetime.fromisoformat(task["created_at"]).strftime("%Y-%m-%d %H:%M")
            result.append(f"  ○ #{task['id']}: {task['task']} (created: {created})")
    else:
        result.append("📋 No active tasks!")

    # Completed tasks (if requested)
    if show_completed and completed:
        result.append("\n✅ **Completed Tasks:**")
        for task in completed:
            created = datetime.fromisoformat(task["created_at"]).strftime("%Y-%m-%d %H:%M")
            updated = ""
            if task.get("updated_at"):
                updated_dt = datetime.fromisoformat(task["updated_at"]).strftime("%Y-%m-%d %H:%M")
                updated = f" (completed: {updated_dt})"
            result.append(f"  ✓ #{task['id']}: {task['task']} (created: {created}{updated})")

    return "\n".join(result)

@mcp.tool()
def complete_todo(task_id: int) -> str:
    """
    Mark a task as completed.

    Args:
        task_id: The ID of the task to complete (from list_todos)

    Returns:
        Confirmation message

    Example:
        complete_todo(3) → Marks task #3 as done
    """
    logger.info(f"Completing task #{task_id}")

    todos = _load_todos()

    for task in todos["tasks"]:
        if task["id"] == task_id:
            if task["completed"]:
                return f"ℹ️ Task #{task_id} is already completed."

            task["completed"] = True
            task["updated_at"] = datetime.now().isoformat()
            _save_todos(todos)

            return f"✅ Completed task #{task_id}: {task['task']}"

    return f"❌ Task #{task_id} not found. Use list_todos() to see available tasks."

@mcp.tool()
def delete_todo(task_id: int) -> str:
    """
    Permanently delete a task from the list.

    Args:
        task_id: The ID of the task to delete

    Returns:
        Confirmation message

    Example:
        delete_todo(5) → Removes task #5 permanently
    """
    logger.info(f"Deleting task #{task_id}")

    todos = _load_todos()

    # Find and store task info before deletion
    task_info = None
    for task in todos["tasks"]:
        if task["id"] == task_id:
            task_info = task["task"]
            break

    if not task_info:
        return f"❌ Task #{task_id} not found."

    # Remove task
    todos["tasks"] = [t for t in todos["tasks"] if t["id"] != task_id]
    _save_todos(todos)

    return f"🗑️ Deleted task #{task_id}: {task_info}"

@mcp.tool()
def clear_completed_todos() -> str:
    """
    Delete all completed tasks at once.

    Returns:
        Confirmation with count of deleted tasks

    Example:
        clear_completed_todos() → Removes all ✓ tasks
    """
    logger.info("Clearing all completed tasks")

    todos = _load_todos()

    completed_count = sum(1 for t in todos["tasks"] if t["completed"])

    if completed_count == 0:
        return "ℹ️ No completed tasks to clear."

    todos["tasks"] = [t for t in todos["tasks"] if not t["completed"]]
    _save_todos(todos)

    return f"🗑️ Cleared {completed_count} completed task(s)"

if __name__ == "__main__":
    logger.info("🚀 Starting TODO Service...")
    mcp.run()
