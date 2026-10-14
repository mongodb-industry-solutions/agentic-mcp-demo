"""Tool definitions for the Telco OSS Phase 0 spike.

Two kinds of tools:

- list_sites: a read-only port of ibn_inventory_service.list_sites, reading
  the shared IBN reference fixtures through DEMO_MONGODB_URI. The runtime's
  own state (checkpoints, executions, memory) lives on MONGODB_URI, which
  stays a separate connection on purpose.
- probe_*: report what the sandbox a tool runs in can actually see (custom
  headers, user identity, secrets). These answer the AGENT_ENGINE_PLAN.md
  open questions and get deleted once Phase 0 is written up.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_engine_runner_shared.context import get_current_custom_headers
from agent_engine_sdk_langgraph import App
from pymongo import MongoClient

_SECRETS_OF_INTEREST = ("MONGODB_URI", "DEMO_MONGODB_URI", "LLM_API_KEY", "VOYAGE_API_KEY")

_demo_client: MongoClient | None = None


def _demo_db():
    """Lazily connect to the demo cluster; tool bodies run only on demand."""
    global _demo_client
    if _demo_client is None:
        _demo_client = MongoClient(os.environ["DEMO_MONGODB_URI"])
    return _demo_client["agent_registry"]


def _probe(app: App, sandbox: str) -> str:
    try:
        headers = get_current_custom_headers()
    except Exception as exc:  # noqa: BLE001 - the failure mode is the finding
        headers = {"<error>": repr(exc)}
    try:
        user_id = app.get_current_user_id()
    except Exception as exc:  # noqa: BLE001
        user_id = f"<error> {exc!r}"
    return json.dumps(
        {
            "sandbox": sandbox,
            "custom_headers": headers,
            "user_id": user_id,
            "secrets_present": {k: bool(os.environ.get(k)) for k in _SECRETS_OF_INTEREST},
        },
        indent=2,
    )


def register(app: App) -> None:
    @app.tool(is_local=False)
    def get_current_date(timezone_name: str = "UTC") -> str:
        """Return today's date in ISO 8601 format for an IANA timezone."""
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return json.dumps(
                {
                    "error": f"Unknown timezone: {timezone_name}",
                    "hint": "Use an IANA timezone like America/Los_Angeles.",
                }
            )
        return datetime.now(timezone).date().isoformat()

    @app.tool(is_local=False)
    def list_sites() -> str:
        """List all sites in the IBN inventory with their status and address."""
        docs = list(_demo_db()["ibn_sites"].find({}).sort("name", 1))
        if not docs:
            return "No sites in inventory."
        lines = [f"**{len(docs)} site{'s' if len(docs) != 1 else ''}:**\n"]
        for s in docs:
            status_emoji = {"active": "🟢", "provisioning": "🛠️ ", "decommissioned": "⊗"}.get(
                s.get("status"), "•"
            )
            lines.append(
                f"- **{s['name']}** {status_emoji} {s.get('status', '—')}\n"
                f"  {s.get('address', '—')}"
            )
        return "\n".join(lines)

    @app.tool(is_local=False)
    def probe_tool_sandbox() -> str:
        """Diagnostics: report custom headers, user identity and secrets visible in the
        tool sandbox. Call only when explicitly asked to probe the tool sandbox."""
        return _probe(app, "tool")

    @app.tool(is_local=True)
    def probe_agent_sandbox() -> str:
        """Diagnostics: report custom headers, user identity and secrets visible in the
        agent sandbox. Call only when explicitly asked to probe the agent sandbox."""
        return _probe(app, "agent")

    @app.tool(is_local=False)
    def save_user_info(info_key: str, info_value: str) -> str:
        """Save a piece of information about the user to long-term memory.

        Args:
            info_key: What the information represents, such as name, location, or interests.
            info_value: The value to store.
        """
        user_id = app.get_current_user_id()
        result = app.memory.save_semantic(
            text=f"User {info_key}: {info_value}",
            label=f"{user_id}_{info_key}",
            source="agent_telco_oss_spike",
            metadata={"type": "user_info", "info_key": info_key},
            user_id=user_id,
        )
        if getattr(result, "acknowledged", result):
            return json.dumps({"status": "saved", "info_key": info_key, "user_id": user_id})
        return json.dumps({"status": "error", "error": "Memory is not enabled"})

    @app.tool(is_local=False)
    def recall_user_info() -> str:
        """Recall stored information about the current user from long-term memory."""
        user_id = app.get_current_user_id()
        context = app.memory.build_context(
            query=f"user profile and information for user {user_id}",
            user_id=user_id,
        )
        formatted = getattr(context, "formatted_context", context)
        memory_context = formatted if isinstance(formatted, str) else ""
        if not memory_context:
            return json.dumps({"found": False, "message": "No stored information found"})
        return json.dumps({"found": True, "profile": memory_context, "user_id": user_id})
