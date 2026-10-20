#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Agent-to-agent calls between the Telco OSS agents (Phase 4 of
AGENT_ENGINE_PLAN.md): the coordinator's delegation and the domain agents'
consultations. A2A only exists in the agent sandbox, so every caller is a
local tool (`app.tool(is_local=True)`), which the platform still records
in the execution log.

The demo lane travels as the `demo-prefix` custom header. A2A does not
forward it by itself, so each call passes it on explicitly, together with
`consult-depth`, which keeps consultations one level deep.
"""

import json
from typing import Any

import httpx
from agent_engine_runner_shared.context import get_current_custom_headers
from agent_engine_sdk_langgraph import App
from telco_catalog import SPECS

# Long enough for a domain agent's full turn (several tool calls); the OE
# caps A2A calls at 300 s.
A2A_TIMEOUT_S = 280

# Catalog name (ibn_agent) → Agent Engine workspace/agent name (ibn-agent).
AGENTS = {spec["name"].replace("_", "-"): spec for spec in SPECS}
DOMAIN_AGENT = {spec["domain"]: name for name, spec in AGENTS.items()}


def consult_depth() -> int:
    try:
        return int((get_current_custom_headers() or {}).get("consult-depth", "0"))
    except ValueError:
        return 0


def is_a2a_child() -> bool:
    """Called by another of our agents (we always send consult-depth)."""
    return "consult-depth" in (get_current_custom_headers() or {})


def forward_headers(depth: int) -> dict[str, str]:
    """Headers for a child call: this execution's lane, one level deeper."""
    headers = {"consult-depth": str(depth)}
    prefix = (get_current_custom_headers() or {}).get("demo-prefix")
    if prefix is not None:
        headers["demo-prefix"] = prefix
    return headers


def _agent_ids(app: App) -> dict[str, str]:
    """{agent name: agent_id} of the agents this one may call. Asked per
    call: the ids differ between local dev and the platform."""
    client = app._runtime.a2a
    return {a.name: a.agent_id for a in client.discover_agents(limit=50)} if client else {}


def call_agent(app: App, name: str, message: str, depth: int) -> dict[str, Any]:
    """Invoke agent `name` over A2A. Returns {agent, status, result|error,
    execution_id}; status is completed, failed or input-required (the
    agent paused for a NOC review)."""
    client = app._runtime.a2a
    if client is None:
        return {"agent": name, "status": "failed", "error": "A2A is not available here"}
    agent_id = _agent_ids(app).get(name)
    if not agent_id:
        return {"agent": name, "status": "failed",
                "error": f"agent {name!r} is not deployed or not allowed for this caller"}
    try:
        resp = client.invoke_agent(agent_id=agent_id, message=message, timeout=A2A_TIMEOUT_S,
                                   custom_headers=forward_headers(depth))
    except httpx.HTTPError as e:  # incl. a 401 after the 5-min A2A token lifetime
        return {"agent": name, "status": "failed", "error": f"{type(e).__name__}: {e}"}
    out: dict[str, Any] = {"agent": name, "status": resp.status,
                           "execution_id": resp.execution_id}
    if resp.result is not None:
        out["result"] = resp.result if isinstance(resp.result, str) else json.dumps(resp.result)
    if resp.error:
        out["error"] = resp.error
    return out
