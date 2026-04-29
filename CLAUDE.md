# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A multi-agent AI orchestration demo for multi-industry use cases. The system uses a ReAct-based orchestrator that dynamically discovers MCP (Model Context Protocol) services, routes queries via MongoDB Atlas Vector Search, maintains conversation memory, and invokes tools across pluggable domain services.

## Setup & Running

**Required environment variables:**
```bash
export OPENAI_API_KEY="<your openai api token>"
export MONGODB_URI="<your mdb connection string>"
export OPENAI_MODEL="gpt-4o"  # optional, defaults to gpt-4o
```

**Install and run:**
```bash
python -m venv <dir>
source <dir>/bin/activate
pip install -r requirements.txt
python main.py
```

**Watch live agent activity (separate terminal):**
```bash
curl --no-progress-meter -N https://notify.bjjl.dev/receive | grep -v '^:'
```

There is no test suite or linter configured — this is a prototype/demo project.

## Architecture

### Entry Points
- `main.py` — Interactive CLI loop; supports `status`, `memory`, `exit`, or natural language queries
- `agents/orchestrator.py` — `OrchestratorAgent` class; the core brain
- `mcp_servers/*.py` — Pluggable FastMCP service modules

### Orchestrator Flow (`agents/orchestrator.py`)

1. **Service Registry Sync** (`_sync_registry`): On startup, scans `mcp_servers/` for `.py` files, computes file hashes, generates embeddings via Voyage AI, and syncs with MongoDB `agent_registry.mcp_services`. Detects new, changed, and deleted services.

2. **Semantic Routing** (`_select_service`): Uses MongoDB Atlas Vector Search on service description embeddings. High confidence (>0.8) → use directly; medium confidence → LLM validation; low confidence → fall back to `self.last_service` (session stickiness).

3. **Follow-up Detection** (`_enrich_query_if_needed`): Determines if a short query is a follow-up to the previous exchange; enriches it with prior context if so.

4. **Server Activation** (`_activate_servers`): Launches MCP servers as subprocesses via `uv run` (StdioServerParameters), establishing stdio-based `ClientSession` connections managed by an `AsyncExitStack`.

5. **ReAct Loop** (`process_query`): Iterates up to 5 times. Collects tools from all active sessions (prefixed as `{service_name}__{tool_name}`), calls `session.call_tool()` with extracted arguments, and appends results to conversation history.

6. **Live Broadcast**: Posts colored status tags (BOOTSTRAP, QUERY, REACT, ROUTING, ACTION, RESULT, CRITIC, ERROR) to `https://notify.bjjl.dev/send`.

7. **Critic Review**: Validates responses for compliance (financial/medical disclaimers). Currently disabled structurally.

### Adding a New MCP Service

Create a new `.py` file in `mcp_servers/` using the FastMCP framework. The orchestrator auto-discovers it on next startup via hash-based change detection. The file's module-level docstring is used as the service description for semantic routing — make it specific and descriptive.

## Key Dependencies

- `mcp` — Model Context Protocol client/server framework
- `openai` — AsyncOpenAI client (used for chat completions and embeddings)
- `pymongo` — MongoDB async driver
- `rich` — Terminal UI rendering
- `httpx` — Async HTTP for broadcast notifications

Dependencies are managed via `requirements.in` (source) and `requirements.txt` (pinned). To update: edit `requirements.in` then recompile with `pip-compile`.
