#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
MCP session pool — launches MCP servers as stdio subprocesses
and manages the reused ClientSession instances.

Phase 0 extraction from the former monolithic OrchestratorAgent
(see MULTI_AGENT_PLAN.md). Method bodies are unchanged; they share
state with the composition root in orchestrator.py via mixin `self`.
"""

import asyncio
import os
import json
import ast
import re
import httpx
import datetime
import hashlib
import tempfile
import time
from pathlib import Path
from contextlib import AsyncExitStack
from typing import List, Dict
from watchfiles import awatch
from pymongo import AsyncMongoClient, ReturnDocument
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI


# Tool schemas are identical across browser sessions and across runs, so
# cache them process-wide once learned. This lets an agent PRESENT a whole
# domain's tools to the LLM without keeping every server's subprocess
# alive — a server is only spawned persistently when one of its tools is
# actually called (see ensure_active). Keyed by server name → list of
# OpenAI tool dicts.
_TOOL_SCHEMA_CACHE: Dict[str, List[Dict]] = {}


class McpPoolMixin:

    def _child_env(self) -> dict:
        """Env for a launched MCP server — carries this orchestrator's
        demo_prefix so the server's mutable collections land in the right
        per-session namespace (empty → shared lane)."""
        env = os.environ.copy()
        env["DEMO_PREFIX"] = getattr(self, "demo_prefix", "")
        return env

    @staticmethod
    def _schemas_from(name: str, t_list) -> List[Dict]:
        return [
            {"type": "function", "function": {
                "name": f"{name}__{t.name}",
                "description": t.description,
                "parameters": t.inputSchema,
            }}
            for t in t_list.tools
        ]

    async def tool_schemas_for(self, name: str) -> List[Dict]:
        """OpenAI tool schemas for one server, cached process-wide.

        If the server is already running here, read from its live session.
        Otherwise spawn it TRANSIENTLY (spawn → list_tools → shut down)
        purely to learn the schemas — no persistent subprocess is kept.
        This is what lets a domain agent show all of its tools to the LLM
        while only the servers it actually invokes get activated."""
        cached = _TOOL_SCHEMA_CACHE.get(name)
        if cached is not None:
            return cached

        if name in self.sessions:
            try:
                t_list = await self.sessions[name].list_tools()
                _TOOL_SCHEMA_CACHE[name] = self._schemas_from(name, t_list)
                return _TOOL_SCHEMA_CACHE[name]
            except Exception:
                pass

        matches = self._resolve_server_paths([name])
        if not matches:
            return []
        params = StdioServerParameters(
            command="uv", args=["run", matches[0]["path"]],
            env=self._child_env())
        schemas: List[Dict] = []
        try:
            # Transient: entered and exited within this call (same task),
            # so no long-lived subprocess and no cross-task anyio scope.
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                t_list = await session.list_tools()
                schemas = self._schemas_from(name, t_list)
        except Exception as e:
            # Never cache a failure: a missing `uv`, a busy machine, or a
            # transient spawn error would otherwise disable this server for
            # the whole process lifetime, so every later query in the
            # domain reports "no tools" long after the cause is gone.
            print(f"⚠️ schema harvest for {name} failed: {e}", flush=True)
            return []
        _TOOL_SCHEMA_CACHE[name] = schemas
        return schemas

    async def ensure_active(self, name: str) -> bool:
        """Lazily spawn one server into this orchestrator's persistent
        pool if it isn't already running. Returns True if usable. Called
        right before a tool on that server is invoked, so subprocesses
        only exist for servers the agent actually uses this session."""
        if name in self.sessions:
            return True
        await self._activate_servers(self._resolve_server_paths([name]))
        return name in self.sessions

    async def _activate_servers(self, servers: List[Dict]):
        for srv in servers:
            name = srv["server_name"]
            if name in self.sessions:
                continue  # already running, reuse

            path = srv["path"]

            try:
                params = StdioServerParameters(
                    command="uv",
                    args=["run", path],
                    env=self._child_env()
                )
                read, write = await self.exit_stack.enter_async_context(stdio_client(params))
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                self.sessions[name] = session
                self.tool_cache.pop(name, None)  # invalidate stale cache on (re)start
                await self._broadcast("AGENT", f"✓ Activated: {name}")

            except Exception as e:
                print(f"  ❌ {name} failed: {e}")
                await self._broadcast("ERROR",
                    f"❌ Failed to activate {name}: {type(e).__name__}: {e}")

    def _resolve_server_paths(self, service_names: List[str]) -> List[Dict]:
        """Resolve service names to launchable paths — local filesystem
        first, then the cloud temp dir (same resolution order as the
        legacy routing path in process_query)."""
        matches = []
        for service_name in service_names:
            local_path = self.server_dir / f"{service_name}.py"
            cloud_path = self.temp_dir   / f"{service_name}.py"

            if local_path.exists():
                matches.append({"server_name": service_name,
                                "path": str(local_path.absolute())})
            elif cloud_path.exists():
                matches.append({"server_name": service_name,
                                "path": str(cloud_path.absolute())})
            else:
                print(f"⚠️ {service_name} not found locally or in cloud "
                      f"temp dir, skipping")
        return matches

    async def _call_tool_locked(self, server_name: str, tool: str, args: dict):
        """Serialize tool calls per stdio session. MCP ClientSessions are
        shared across the orchestrator and all domain agents; interleaved
        call_tool requests on one stdio pipe must not overlap once agents
        run concurrently (Phase 1+ of MULTI_AGENT_PLAN.md). Locks are
        created lazily — the event loop is single-threaded, so setdefault
        is race-free."""
        lock = self.session_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            return await self.sessions[server_name].call_tool(tool, args)

    def _format_result_preview(self, text: str, max_lines: int = 3, max_chars: int = 250) -> str:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        preview = lines[:max_lines]
        truncated = len(lines) > max_lines
        result = " │ ".join(preview)
        if len(result) > max_chars:
            result = result[:max_chars - 1] + "…"
        elif truncated:
            result += " …"
        return result

