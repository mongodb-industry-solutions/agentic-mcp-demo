#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Runtime context shared by every oss_tools module: the MongoDB client, the
per-session collection prefix, and the tool collector.

The same tool functions run in two runtimes:

- **Legacy MCP subprocesses** (mcp_servers/*.py shims, spawned per browser
  session with DEMO_PREFIX in their environment).
- **Atlas Agent Engine sandboxes**, where one process serves many sessions
  and the shell sends the prefix per request as the custom header
  `X-Mdb-Agent-Engine-Custom-Demo-Prefix` (arrives as `demo-prefix`).

So the prefix can no longer be bound at import time. `collection()` returns
a proxy that resolves `<prefix><name>` on every use. Tool bodies keep
calling `intents.find(...)` exactly as before.
"""

import os
import re

from pymongo import MongoClient

DB_NAME = "agent_registry"

# Mirrors web/shell.py: _TOKEN_RE (^[a-z0-9]{8,32}$) wrapped as s_<token>_.
# The header is caller-supplied, so anything else is rejected rather than
# allowed to address another lane's collections.
_PREFIX_RE = re.compile(r"^(s_[a-z0-9]{8,32}_)?$")

_client: MongoClient | None = None

try:  # Only present inside Atlas Agent Engine sandboxes.
    from agent_engine_runner_shared.context import (
        get_current_custom_headers,
        get_current_execution_id,
        get_current_session_id,
        get_current_workspace_id,
    )
except ImportError:  # Legacy MCP subprocess / CLI.
    get_current_custom_headers = None


def demo_prefix() -> str:
    """The current session's collection prefix ('' → shared default lane)."""
    if get_current_custom_headers is not None:
        prefix = (get_current_custom_headers() or {}).get("demo-prefix")
        if prefix is not None:
            if not _PREFIX_RE.match(prefix):
                raise ValueError(f"invalid demo-prefix header: {prefix!r}")
            return prefix
    return os.environ.get("DEMO_PREFIX", "")


def execution_context() -> dict:
    """The Agent Engine execution this tool call belongs to ({} on the
    legacy runtime): lets a reviewer, or the coordinator, find and resume
    the suspended execution behind an approval."""
    if get_current_custom_headers is None:
        return {}
    ids = {"execution_id": get_current_execution_id(), "session_id": get_current_session_id(),
           "workspace_id": get_current_workspace_id()}
    ctx = {k: v for k, v in ids.items() if v}
    # The platform doesn't keep custom headers across a pause, so whoever
    # resumes the run must resend them (the lane, and consult-depth, which
    # tells the graph it runs as an A2A child).
    headers = get_current_custom_headers() or {}
    ctx["headers"] = {k: headers[k] for k in ("demo-prefix", "consult-depth") if k in headers}
    return ctx


def db():
    """The demo database, on a lazily created process-wide client."""
    global _client
    if _client is None:
        _client = MongoClient(os.environ["MONGODB_URI"])
    return _client[DB_NAME]


class _Collection:
    """Stand-in for a pymongo Collection that re-resolves its name (and the
    session prefix, when mutable) on every attribute access."""

    __slots__ = ("_name", "_mutable")

    def __init__(self, name: str, mutable: bool):
        self._name = name
        self._mutable = mutable

    def resolve(self):
        name = demo_prefix() + self._name if self._mutable else self._name
        return db()[name]

    def __getattr__(self, attr):
        return getattr(self.resolve(), attr)

    def __repr__(self):
        return f"<oss_tools collection {self._name!r} mutable={self._mutable}>"


def collection(name: str, mutable: bool = False) -> _Collection:
    """A collection handle. `mutable=True` marks per-session demo data
    (prefixed); reference fixtures stay shared and unprefixed."""
    return _Collection(name, mutable)


class ToolSet(list):
    """Ordered list of a module's tool functions. `@tools.tool` registers a
    function without binding it to a runtime; mcp_servers/ shims hand the
    list to FastMCP, and Agent Engine agents wrap each function with
    `@app.tool()`. `@tools.tool(timeout_s=…)` raises the Agent Engine tool
    timeout (30 s by default) for a tool that waits."""

    def tool(self, fn=None, *, timeout_s: int | None = None):
        def register(fn):
            if timeout_s:
                fn.timeout_s = timeout_s
            self.append(fn)
            return fn
        return register(fn) if fn else register
