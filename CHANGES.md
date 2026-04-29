# CHANGES.md

## 2026-04-02

### Added `CLAUDE.md`
Initial project documentation for Claude Code, covering setup, environment variables,
orchestrator flow, MCP service inventory, MongoDB collections, and dependency management.

### Architecture Review
Identified six fundamental flaws in the system architecture:
1. Synchronous `requests` blocking the async event loop in `_broadcast`
2. Critic review permanently hardcoded to `APPROVED` (dead code)
3. MCP servers killed and respawned on every query (no session reuse)
4. No user identity in the memory service (global shared memory pool)
5. `recall_memories` performs a full collection scan with LLM-based filtering (does not scale)
6. Vector search routing depends on invisible Atlas-side embedding configuration

### Fix: Async broadcast (`agents/orchestrator.py`)
Replaced `import requests` with `import httpx`. Changed `_broadcast` from a synchronous
method to `async def`, using a persistent `httpx.AsyncClient` instance (initialized in
`__init__`, closed in `__aexit__`). Added `await` to all 35 active call sites. The event
loop is no longer blocked during broadcasts; the persistent client also reuses the
underlying TCP connection across calls.

### Fix: MCP server session pooling (`agents/orchestrator.py`)
`_activate_servers` previously tore down all server processes and rebuilt the exit stack
on every query. It now skips servers already present in `self.sessions`, starting only
servers that are not yet running. The shared `AsyncExitStack` accumulates all server
contexts for the lifetime of the agent and is only closed in `__aexit__`. Subprocess
startup cost is paid once per server, and servers remain alive across queries.

As a companion change, the `openai_tools` list in `process_query` is now built by
iterating `matches` (the servers selected for the current query) rather than all entries
in `self.sessions`, keeping the LLM's tool list scoped to the current intent even though
unrelated servers remain alive in the pool.

## 2026-04-03

### New MCP server: `portfolio_service.py`
Full investment portfolio manager backed by `agent_registry.portfolio` in MongoDB.

Tools: `add_position` (by ISIN), `add_position_by_name` (by company name + optional
currency), `update_position`, `delete_position`, `list_portfolio`, `refresh_prices`.

Price and name resolution uses Yahoo Finance (search → chart). ISIN resolution uses a
3-stage fallback: (1) Yahoo Finance chart meta, (2) Yahoo Finance v7 quote endpoint,
(3) OpenAI `gpt-4o-mini` — reliable for all major publicly traded securities worldwide.
Lookup prefers `longname` over `shortname` to avoid truncation artifacts (e.g. trailing
" S" on German stocks).

Position lookup (`_find_position`) tries, in order: exact ISIN, ticker regex (so "BMW"
matches "BMW.DE"), name substring — enabling natural references like "update BMW to 25"
or "delete MongoDB".

`list_portfolio(currency="EUR")` normalises mixed-currency portfolios to a single target
currency using live rates from Frankfurter (api.frankfurter.app, free, ECB-sourced).
Exchange rates are cached for 5 minutes to avoid a network call on every listing.

Module docstring covers natural-language add/update/delete/view/refresh phrasings used
for semantic routing, including name-based variants like "BMW quantity now 25".

### Performance optimisations (`agents/orchestrator.py`, `mcp_servers/portfolio_service.py`)
Targeted the most expensive operations on the per-query critical path:

**Tool list cache** — `list_tools()` results are cached in `self.tool_cache` (keyed by
server name) after the first call and reused on all subsequent queries. Cache entry is
invalidated when a server is (re)started. Combined with `asyncio.gather` to fetch any
cache misses in parallel across sessions. Saves ~100 ms per already-running server.

**Self-contained query heuristic** — follow-up detection (`_needs_context_enrichment`,
a `gpt-4o-mini` call) is skipped when the query starts with an action verb (`list`,
`show`, `add`, `update`, `delete`, `change`, `refresh`, `what`, `how`, …). Saves
~300 ms for the majority of operational queries.

**Memory service de-injection** — the unconditional force-append of `memory_service`
to every query's match list has been removed. The memory service is now routed via
vector search like every other service and will only be activated when the query is
genuinely about preferences, personal facts, or memory operations. This eliminates
4–5 internal `gpt-4o-mini` calls (`_generate_search_perspectives` + perspective
evaluations) that `recall_memories` was making on every single query regardless of
relevance. Saves ~1 000–1 500 ms on transactional queries.

**Exchange rate cache** — `_get_eur_rates()` in `portfolio_service.py` caches the
Frankfurter response for 300 seconds. Saves ~300 ms on all `list_portfolio` calls
after the first within the cache window.

### Investigation: `restaurant_guide` "Connection closed" error
`restaurant_guide.py` imports `voyageai` at module level, but `voyageai` is absent from
`requirements.in` and `requirements.txt`. The server crashes on startup with
`ModuleNotFoundError`, producing the "Connection closed" error. This is **not a
regression** from the session-pooling fix — the failure occurs identically under both
the old (teardown-per-query) and new (pooled) code paths. Root cause: `voyageai` was
never added to the requirements when the restaurant guide was written.

### Async MongoDB driver (`agents/orchestrator.py`)
Replaced `pymongo.MongoClient` (synchronous) with `pymongo.AsyncMongoClient`
(native async, available since PyMongo 4.5). All collection operations —
`find`, `find_one`, `aggregate`, `insert_one`, `update_one`, `delete_one` —
are now awaited, so the event loop is no longer blocked during database I/O.
`_sync_registry` uses `async for` to iterate the cursor from `find()`, and
`_semantic_search` uses `.to_list()` on the aggregation cursor.

### Eliminated per-query MongoDB round-trips (`agents/orchestrator.py`)
`_semantic_search` now projects `description` alongside `server_name` and
`score`. Previously, `_route_query` issued up to 5 individual `find_one`
calls to re-fetch descriptions for the LLM validation prompt — these are
now eliminated entirely.

### Removed dead critic review (`agents/orchestrator.py`)
The `_critic_review` method, its structured function schema, the two
per-query broadcast calls ("Reviewing…" / "Approved ✓"), and the full
rejection/retry branch have been removed. The critic was hardcoded to
`["APPROVED"]` and had been structurally disabled since the architecture
review.

### Parallel follow-up detection and routing (`agents/orchestrator.py`)
`_needs_context_enrichment` (a `gpt-4o-mini` call) and `_route_query`
(vector search + optional LLM validation) now run concurrently via
`asyncio.gather` when follow-up detection is needed. If the query turns
out *not* to be a follow-up (~80 % of ambiguous cases), the optimistic
routing result is used directly — saving the full sequential latency of
the enrichment call. Only when enrichment fires is a second routing call
made with the enriched query.

### System prompt moved to module constant (`agents/orchestrator.py`)
The ~2 K-token `system_msg` string is now a module-level `_SYSTEM_PROMPT`
constant instead of being rebuilt inside `process_query` on every call.

### Removed `_enrich_for_routing` heuristic (`agents/orchestrator.py`)
The TODO-list routing bias (`[Multiple tasks to add to TODO list]` prefix)
has been removed. It force-routed comma-separated or "I need to" queries
toward `todo_service` regardless of actual intent, biasing the vector
search embedding. Routing now relies entirely on semantic search and LLM
validation.

### Gap-based routing confidence (`agents/orchestrator.py`)
Replaced the fixed absolute threshold (`score > 0.8`) with a relative
gap check: the top candidate is used directly when it scores above 0.65
**and** leads the runner-up by more than 0.05. This avoids unnecessary
LLM validation calls for queries that clearly map to one service (e.g.
"what is my portfolio" scoring 0.788 vs 0.689) while still falling
through to LLM disambiguation when candidates are genuinely close.
