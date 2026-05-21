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
- `web/dtw_dashboard.py` — FastAPI + WebSocket dashboard for the Digital Twin (ACME what-if) demo, driven by Change Streams on `dtw_scenarios` (run separately on `localhost:8080`; supports `?mode=eng` and `?mode=exec`)
- `seed/ibn_seed.py` — One-shot loader for the IBN demo fixtures (sites, customers, resources, knowledge_chunks, intents). Run once before starting the demo; supports `--reset`
- `seed/dtw_seed.py` — One-shot loader for the Digital Twin demo fixtures (plans, QoS profiles, network elements, topology edges, subscribers, traffic models, knowledge chunks, sample scenarios). Run once before starting the DTW demo; supports `--reset`

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

### DTW Demo (Digital Twin — ACME Mobile what-if simulations)

A 5-service flow building a **digital twin** of ACME Mobile's HLR/HSS-relevant world in MongoDB, then letting an LLM + MCP agents run what-if simulations on it. The pitch focus is two realistic scenarios:

- **Flow A (QoS uplift):** "Raise prepaid ACME M downlink from 7.2 Mbps to 20 Mbps in NYC and LA Saturday evening — where do we bottleneck?"
- **Flow B (Policy change):** "Migrate ACME M to a new APN, update the PCRF template, and enable Canada roaming — what control-plane pressure do we expect?"

The five services live alongside the others in `mcp_servers/`:

- `dtw_plan_service` — plans, QoS profiles, subscriber samples (`describe_plan`, `get_qos_profile`, `list_plans`, `compare_qos_profiles`, `subscribers_for_plan`)
- `dtw_topology_service` — RAN + core inventory and dependency graph; wraps `$graphLookup` over `dtw_topology_edges` (`get_network_element`, `find_cells_in_market`, `traverse_dependencies`, `find_path_between`, `list_markets`)
- `dtw_traffic_service` — per-cell traffic models and load estimation by time window (`get_traffic_model`, `estimate_cell_load`, `list_time_windows`, `peak_hours_for_market`)
- `dtw_scenario_service` — NL → structured what-if scenario (`gpt-4o`); lifecycle (`create_scenario`, `list_scenarios`, `get_scenario`, `cancel_scenario`)
- `dtw_simulation_service` — the **hero**. `simulate_qos_change` runs `$graphLookup` for scope + per-cell load projection from `dtw_traffic_models` + hybrid `$vectorSearch` against `dtw_knowledge_chunks` for analogous past scenarios — in one tool call. `simulate_roaming_change` does the Flow B control-plane variant. Also `diff_scenarios`, `get_simulation_result`

**The WOW combo** in `dtw_simulation_service.simulate_qos_change`: $graphLookup walks the dependency tree from `plan_ACME_M` downstream through QoS → cells → eNBs → SGW → PGW, while a hybrid `$vectorSearch` against `dtw_knowledge_chunks.text` (with structured pre-filters on `segment`, `market`, `kind`) surfaces semantically similar past incidents and their mitigation runbooks. Graph for *operational structure*, vector for *institutional memory* — both in one Atlas store, both invoked at simulate-time.

**Atlas Vector Search index required.** The `dtw_knowledge_index` on `dtw_knowledge_chunks` must be created in the Atlas UI before the hybrid query works. The seed script prints the exact JSON config; the index needs `text` configured with auto-embedding (`voyage-3-large`, 1024 dims) and `kind`, `segment`, `market`, `plan_id`, `ts`, `lng`, `lat` as filter fields. If the index isn't ready, `simulate_qos_change` still runs and persists graph + load projections — only the "similar past scenarios" panel will be empty.

**Dashboard modes.** The DTW dashboard reads `?mode=` from the URL. `eng` (default) shows the raw aggregation pipeline and the graph-walk edge list; `exec` hides those and renders only narrative panels with a similarity bar (no numerical score).

**Demo collections** in `agent_registry`: `dtw_markets`, `dtw_plans`, `dtw_qos_profiles`, `dtw_subscribers`, `dtw_network_elements` (polymorphic — HSS/HLR/MME/SGW/PGW/eNodeB/Cell in one collection), `dtw_topology_edges` (the dependency graph), `dtw_traffic_models`, `dtw_scenarios`, `dtw_knowledge_chunks` (vector-indexed in Atlas UI).

## Key Dependencies

- `mcp` — Model Context Protocol client/server framework
- `openai` — AsyncOpenAI client for chat completions (orchestrator uses `gpt-4o` by default for ReAct + `gpt-4o-mini` for routing/enrichment validation)
- `voyageai` — used directly only by `restaurant_guide` (`voyage-3-large` for ad-hoc embedding); the main `vector_index` does its own auto-embedding inside Atlas
- `pymongo` — MongoDB async driver
- `rich` — Terminal UI rendering
- `httpx` — Async HTTP for broadcast notifications

Dependencies are managed via `requirements.in` (source) and `requirements.txt` (pinned). To update: edit `requirements.in` then recompile with `pip-compile`.
