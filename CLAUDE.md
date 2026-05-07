# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A multi-agent AI orchestration demo for multi-industry use cases. The system uses a ReAct-based orchestrator that dynamically discovers MCP (Model Context Protocol) services, routes queries via MongoDB Atlas Vector Search, maintains conversation memory, and invokes tools across pluggable domain services.

## Setup & Running

**Required environment variables:**
```bash
export OPENAI_API_KEY="<your openai api token>"
export MONGODB_URI="<your mdb connection string>"
export VOYAGE_API_KEY="<your voyage api token>"   # used by restaurant_guide service
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
curl -sN https://notify.bjjl.dev/receive | sed -n 's/^data: //p'
```

There is no test suite or linter configured — this is a prototype/demo project.

## Architecture

### Entry Points
- `main.py` — Interactive CLI loop; supports `status`, `memory`, `exit`, or natural language queries
- `agents/orchestrator.py` — `OrchestratorAgent` class; the core brain
- `mcp_servers/*.py` — Pluggable FastMCP service modules
- `web/portfolio_dashboard.py` — FastAPI + WebSocket dashboard for the portfolio service, driven by MongoDB Change Streams (run separately on `localhost:8050`)
- `web/ibn_dashboard.py` — FastAPI + WebSocket dashboard for the Intent-Based Networking demo, driven by Change Streams across `ibn_intents`, `ibn_compliance_events`, `ibn_telemetry`, `ibn_policy_snapshots` (run separately on `localhost:8060`; supports `?mode=eng` and `?mode=exec`)
- `seed/ibn_seed.py` — One-shot loader for the IBN demo fixtures (sites, customers, resources, knowledge_chunks, intents). Run once before starting the demo; supports `--reset`

### Orchestrator Flow (`agents/orchestrator.py`)

1. **Service Registry Sync** (`_sync_registry`): On startup, scans `mcp_servers/` for `.py` files, computes file hashes, and syncs `{server_name, description, file_hash, last_seen}` docs into MongoDB `agent_registry.mcp_services`. Detects new, changed, and deleted services. Embedding of the `description` field is performed automatically by the Atlas Vector Search index (`vector_index`) — the orchestrator passes raw text in `$vectorSearch.query`, and Atlas embeds it on the fly.

2. **Semantic Routing** (`_route_query`): Runs `$vectorSearch` against `vector_index` (top 5 candidates with score). Decision tree:
   - **Clear winner** — `best_score > 0.65` *and* score gap to runner-up `> 0.03` → return top match without LLM.
   - **Session stickiness** — only when `use_stickiness=True` (set after follow-up detection) and `best_score < 0.6` and `self.last_service` exists → reuse last service.
   - **Otherwise (medium confidence)** — LLM validation: gpt-4o-mini picks from the top 5 by description, may return multiple comma-separated services or `NONE`.

3. **Follow-up Detection** (`_needs_context_enrichment`): For short queries (< 5 words) that don't start with a self-contained verb (`list`, `show`, `add`, …), asks gpt-4o-mini whether the current query is a follow-up to the previous one. If yes, the query is prepended with the prior user message before re-routing with `use_stickiness=True`. Routing and enrichment-detection run concurrently via `asyncio.gather`.

4. **Server Activation** (`_activate_servers`): Launches MCP servers as subprocesses via `uv run` (StdioServerParameters), establishing stdio-based `ClientSession` connections managed by an `AsyncExitStack`. Sessions are reused across queries.

5. **ReAct Loop** (`process_query`): Iterates up to 5 times. Collects tools from all active sessions (prefixed as `{service_name}__{tool_name}`), calls `session.call_tool()` with extracted arguments, and appends results to the message list. Tool definitions are cached per session in `self.tool_cache`.

6. **Live Broadcast**: Posts colored status tags (`BOOTSTRAP`, `QUERY`, `AGENT`, `ROUTING`, `ACTION`, `RESULT`, `ERROR`) to `https://notify.bjjl.dev/send` for the live-feed viewer.

### Adding a New MCP Service

Create a new `.py` file in `mcp_servers/` using the FastMCP framework. The orchestrator auto-discovers it on next startup via hash-based change detection. The file's module-level docstring is used as the service description for semantic routing — make it specific and descriptive.

### IBN Demo (Intent-Based Networking)

A 5-service flow demonstrating Atlas as the operational memory + decision layer between customer intent, network reality, and automated assurance. The five services live alongside the others in `mcp_servers/`:

- `ibn_intent_service` — NL → structured intent (`gpt-4o`), lifecycle management (submit / list / get / cancel)
- `ibn_inventory_service` — sites, resources, topology; geospatial `find_nearby_spare` via 2dsphere
- `ibn_feasibility_service` — match intent against inventory (`check_feasibility`, `propose_plan`, `activate_plan`)
- `ibn_assurance_service` — compliance computation + the **hybrid vector diagnose query** (`diagnose_violation`); also `apply_runbook` and `update_template_version`
- `ibn_telemetry_simulator` — push-button violation injection (`inject_event`, `seed_baseline`, `reset_telemetry`)

**The WOW query** lives in `ibn_assurance_service.diagnose_violation` — a single `$vectorSearch` aggregation stage that combines semantic similarity over `ibn_knowledge_chunks.text`, structured equality on `kind`, a `$gte` time filter on `ts`, and a numeric bounding box on `lng`/`lat` (a 2dsphere isn't used here because Atlas Vector Search filters don't support `$geoWithin`; we precompute lng/lat fields on the chunks and box-filter instead). All four modalities pre-filter the vector search inside the index — this is the architectural beat where Atlas separates from a Postgres + pgvector + PostGIS + TimescaleDB stack.

**Atlas Vector Search index required.** The `ibn_knowledge_index` index on `ibn_knowledge_chunks` must be created in the Atlas UI before the diagnose tool works. The seed script prints the exact JSON config when you run it; the index needs `text` configured as a vector field with auto-embedding (e.g. `voyage-3-large`, 1024 dims) and `kind`, `ts`, `lng`, `lat`, `customer`, `site_id` as filter fields.

**Dashboard modes.** The IBN dashboard reads `?mode=` from the URL. `eng` (default) shows the raw aggregation pipeline modal during diagnose and the parsed-JSON intent block; `exec` hides those, replaces them with prose callouts and a similarity bar without numerical score. Same chat backbone, two render styles for two audiences.

**Demo collections** in `agent_registry`: `ibn_customers`, `ibn_sites` (with `2dsphere` index), `ibn_resources` (with `2dsphere`), `ibn_intents`, `ibn_knowledge_chunks` (vector-indexed in Atlas UI), `ibn_policy_snapshots`, `ibn_compliance_events`, `ibn_telemetry` (a Time Series collection — created by the seed script).

## Key Dependencies

- `mcp` — Model Context Protocol client/server framework
- `openai` — AsyncOpenAI client for chat completions (orchestrator uses `gpt-4o` by default for ReAct + `gpt-4o-mini` for routing/enrichment validation)
- `voyageai` — used directly only by `restaurant_guide` (`voyage-3-large` for ad-hoc embedding); the main `vector_index` does its own auto-embedding inside Atlas
- `pymongo` — MongoDB async driver
- `rich` — Terminal UI rendering
- `httpx` — Async HTTP for broadcast notifications

Dependencies are managed via `requirements.in` (source) and `requirements.txt` (pinned). To update: edit `requirements.in` then recompile with `pip-compile`.
