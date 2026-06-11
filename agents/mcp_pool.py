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


class McpPoolMixin:

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
                    env=os.environ.copy()
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

