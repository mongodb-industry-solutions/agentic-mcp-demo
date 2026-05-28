# CHANGES.md

## 2026-05-27

### Asymmetric voyage-4 retrieval — replaces Atlas autoEmbed for Stage 2 routing (`agents/orchestrator.py`, MongoDB `vector_index`)
Atlas `autoEmbed` does not set the `input_type` parameter when embedding documents
at index time vs. queries at search time. Voyage-4 is an asymmetric retrieval model
that prepends a different internal prompt for `input_type="query"` vs.
`input_type="document"`; without that distinction, queries and short service
descriptions collapse to the same region of vector space. Measured before the fix:
the user's exact what-if query ranked `dtw_traffic_service` at 0.504 and
`dtw_scenario_service` at 0.504 — gap of 0.0002, wrong winner. After: same query
ranks `dtw_scenario_service` at 0.707 vs. `dtw_traffic_service` at 0.703 — gap of
0.004 with the correct winner.

The orchestrator now bypasses `autoEmbed` and computes embeddings itself via the
`voyageai` SDK. `_embed_for_index` uses `input_type="document"` and is called from
`_sync_registry` on every new or changed service description; the resulting 1024-d
vector is stored on the service's `mcp_services` doc as `description_embedding`.
`_embed_for_query` uses `input_type="query"` and is called from `_semantic_search`
before every Stage 2 lookup; the result is passed as `queryVector` to
`$vectorSearch`. The Atlas `vector_index` was rebuilt from `type: autoEmbed` to
`type: vector` on `description_embedding` (1024 dims, cosine). `VOYAGE_API_KEY` is
now a required environment variable. `voyageai` was added to `requirements.in`.

Per-service embedding failures are tolerated: if voyage-4 returns an error for any
single service during `_sync_registry`, that service is skipped for this round
(broadcast warning, no partial write) and retried on the next sync. A single bad
embedding does not abort the whole startup.

### Deterministic text-match tiebreaker for Stage 2 (`agents/orchestrator.py`)
Even with asymmetric retrieval, sibling DTW services occasionally land within
~0.005 cosine of each other when the query contains shared vocabulary
("ACME", "downlink", "Mbps"). Rather than always fall through to the LLM
tie-break — which is slow (~1-2s) and stochastic — `_route_query` now runs a
deterministic text-match step between the relative-winner check and the LLM call.

`_text_match_score` tokenises both query and candidate description (stopwords
stripped, ≥2-char tokens only), then scores literal n-gram overlap: 4-gram matches
worth 16 points, 3-gram 9, 2-gram 4, single-token matches 1. The description is
also stopword-stripped so "run simulation" in the query matches "run the
simulation" in the description (the article-vs-no-article gap was the main source
of false misses). The tiebreaker fires when the top candidate has ≥3 points AND
leads the runner-up by 2× + 1 — a deterministic, sub-millisecond decision.

For the "Raise prepaid ACME M from 7.2 → 20 Mbps in NYC and LA Saturday evening"
query, this scores `dtw_scenario_service` at 63 points (matches "raise prepaid",
"20 mbps", and many tokens) vs. `dtw_plan_service` at 2 — fires decisively
without an LLM call. The LLM tie-break path is now reached only on genuinely
ambiguous queries.

### Action-vs-object guidance in the LLM tie-break prompt (`agents/orchestrator.py`)
When the LLM tie-break does run, the prompt now explicitly distinguishes the
ACTION verb (which identifies the service) from OBJECT references
(entity IDs and proper nouns, which are inputs to the action). Worked example
baked in: "run simulation for scenario DTW-SCN-003" — verb "run simulation"
identifies the simulation service; "DTW-SCN-003" is just the input. Pick the
simulation service. Resolves the failure mode where the LLM saw "scenario" in
the query and picked `dtw_scenario_service` instead of `dtw_simulation_service`.

### Skip enrichment for self-contained imperative commands (`agents/orchestrator.py`)
`_needs_context_enrichment` no longer enriches short queries that begin with an
imperative verb (`run`, `execute`, `simulate`, `show`, `list`, `inject`, `apply`,
`delete`, `change`, …). Fusing "run simulation" with the previous turn's scenario
description was diluting the command's routing signal: the enriched
"Raise ACME M downlink … run simulation" routes to `dtw_scenario_service` because
the description vocabulary dominates. Bare imperatives now bypass the LLM
follow-up check and route on their own text. The verb list is pre-compiled at
module level (`_IMPERATIVE_VERBS`).

### Trigger-phrase-only embeddings (`agents/orchestrator._extract_discriminator`)
The text that goes to voyage-4 at index time is now built from only the service
tagline plus the literal trigger phrases in the docstring's "Use this service
when" section — exposition paragraphs are excluded. Body paragraphs across
sibling services in the same domain share heavy vocabulary ("ACME", "QoS",
"plan", "scenario"), so embedding them collapses cosine distances. Each service's
embedding is now a centroid of expected user queries. Section end is detected by
blank line / non-bullet prose break / negative-scope guard.

### Trigger phrases use generic placeholders in sibling DTW services
(`dtw_plan_service.py`, `dtw_traffic_service.py`, `dtw_topology_service.py`)
Concrete entity names that overlap with what-if scenarios ("ACME M", "NYC",
"Saturday night", "7.2 to 20 Mbps") were removed from the trigger phrases of
the sibling services and replaced with generic placeholders (`<plan>`,
`<market>`, `<window>`, `<id>`). Only `dtw_scenario_service` — the service that
owns concrete what-if descriptions — keeps the entity-rich examples. This
prevents siblings from competing on the same vocabulary at Stage 2.

### DTW simulation realism — concurrency factor (`mcp_servers/dtw_simulation_service.py`)
`_project_cell_load` was producing all-saturated original utilisations (every cell
at 100%) because the traffic-model fixtures count *nominal* subscriber populations
and the per-plan demand alone often exceeded a cell's capacity. Added a
`CONCURRENCY_FACTOR = 0.08` constant applied as a multiplier when converting
nominal subscribers to actively-transmitting subscribers — ~8% concurrency, which
matches real mobile-network behaviour during peak windows. Projections now
distribute realistically: a representative QoS-uplift scenario across 16 cells
yields ~3 GREEN, 4 YELLOW, 8 RED, 1 BLOCK instead of 16 BLOCK.

### DTW graph-walk direction fix + topology coverage (`mcp_servers/dtw_simulation_service.py`, `seed/dtw_seed.py`)
`_graph_dependency_walk` had `connectFromField` and `connectToField` swapped — it
was walking upstream (finding edges pointing TO the seed) instead of downstream.
Even with the correct direction, the seed only emitted edges from
`plan → uses_qos → qos_profile` with nothing downstream of QoS — the walk
dead-ended at `qos_prepaid_7_2`. Added `build_qos_to_cell_edges` to the seed,
emitting `qos_profile → applies_to → Cell` for every (qos, cell) pair with a
`market` field for query-time scoping. The walk from `plan_ACME_M` now discovers
~236 edges across the full chain `plan → qos → cell → eNB → SGW → PGW` plus the
`eNB → MME → HSS` branch — visible on the dashboard's graph-walk panel.
`maxDepth` raised from 4 to 6 so the walk reaches PGW/HSS at the bottom.

### Coherent delta math (`mcp_servers/dtw_simulation_service.py::_project_cell_load`)
Previously, `delta_pct` was computed from the unclamped projected utilisation —
producing physically-impossible deltas like "+418pp" when demand vastly exceeded
capacity. The display utilisation was clamped to [0, 1], but the delta wasn't.
Now `delta_pct` is computed from the clamped values (max +100pp from 0), and the
overshoot is surfaced separately as `demand_factor`. A saturated cell now reads
"99% → 100% (+1pp) · demand 5.0× cap · BLOCK" — physically coherent.

### Two-step what-if flow with verification card (`mcp_servers/dtw_scenario_service.py`)
`create_scenario` now emits a verification card framed as "📝 Scenario X —
awaiting confirmation" with the parsed change_set and scope listed for the user
to review. Explicit prompt at the bottom: say 'run the simulation' to proceed,
or describe an amendment.

Added `update_scenario(modification, scenario_id=None)`: applies a
natural-language amendment to an existing submitted scenario before simulating.
Defaults to the most-recent submitted scenario when `scenario_id` is omitted, so
"change to 18 Mbps" works without an explicit ID. Implementation uses a focused
LLM call that takes the EXISTING change_set+scope as JSON plus the user's
modification, and returns the merged JSON — much more reliable than re-parsing
"original + amendment" as one block (the LLM tended to anchor on the original
and ignore the amendment).

### Substitution detection when target QoS doesn't exist exactly (`update_scenario`)
The amendment LLM previously mapped "change to 19 Mbps" silently to
`qos_prepaid_18` (nearest available) — the user thought they got 19 but the
simulation ran 18. `update_scenario` now parses "X Mbps" patterns from the
modification (capturing the LAST Mbps mention so "from 7.2 Mbps to 19 Mbps"
correctly reads 19), looks up the resolved profile's actual `max_downlink_mbps`,
and emits a clear "⚠ Requested 19 Mbps has no exact QoS profile — using nearest
match `qos_prepaid_18` (18 Mbps). Available prepaid tiers: …" line on the
verification card when a substitution happened.

### Wider prepaid QoS coverage (`seed/dtw_seed.py`, MongoDB `dtw_qos_profiles`)
Every integer downlink from 5 to 30 Mbps now has a prepaid profile
(`qos_prepaid_5` … `qos_prepaid_30`), generated from the 20 Mbps template with
proportional uplink. 28 prepaid profiles total. Removes the most common
substitution case — common amendment values like "change to 19 Mbps" now map to
exact profiles. `build_qos_to_cell_edges` emits the matching `qos→cell` graph
edges for every new profile so the graph walk works regardless of which one a
plan adopts.

### Hard-delete tools for DTW scenarios (`mcp_servers/dtw_scenario_service.py`)
`cancel_scenario` only sets `status: "cancelled"`; the document is retained for
audit. Added `delete_scenario(scenario_id)` and `delete_all_scenarios(keep_demo=True)`
that issue `delete_one` / `delete_many` against `dtw_scenarios`. "Delete all
scenarios" no longer leaves cancelled documents visible on the dashboard;
the dashboard's Change Stream fires on delete and the rows disappear.
Trigger phrases for delete operations added to the service docstring.

### Resilient simulation tools — prefer existing scenario, ignore stale text (`mcp_servers/dtw_simulation_service.py`)
`simulate_qos_change` and `simulate_roaming_change` previously accepted either
`scenario_id` or `text` with equal weight. With both options open, the agent was
passing the original NL query as `text` even when a scenario already existed —
which triggered `_create_scenario_inline` and produced a duplicate scenario with
pre-amendment parameters (e.g. simulated 20 Mbps when the user had just updated
to 15). Now the tools always prefer an existing submitted scenario:
`text=` is only used as a fallback when the collection is empty (true one-shot).
This is documented as the resolution order in both tool docstrings.

Incomplete-change_set errors now include the actual missing values
(`plan_id=…, old_qos_profile_id=None, new_qos_profile_id=…`) and point the agent
to the right next step (re-create or update_scenario), instead of a generic
refusal.

### Reject empty scenarios in `create_scenario` (`mcp_servers/dtw_scenario_service.py`)
Imperative commands like "Run the simulation" used to pass through
`create_scenario` and produce a `scenario_type="other"` document with empty
change_set — polluting `dtw_scenarios` and confusing later tool calls. Now
rejected with a clear message pointing the agent to `dtw_simulation_service` for
execution.

### Workstream domain authority (`agents/orchestrator.py`)
When Stage 1 returns a single explicit domain (e.g. "todo" for "add Hamburg
metrics to my todos"), that domain now wins over the workstream classifier
LLM's content-vocabulary-derived `domain_hint`. Without this, "add Hamburg
metrics to todos" was being merged into an open `analytics` workstream because
the classifier saw "metrics" and labelled the new WS `analytics`. Applied at
both `_create_workstream` call sites (LLM-classified path and empty-open_ws
fast-path).

### Workstream entities are a scope hint, not a restriction (`agents/orchestrator.py`)
The workstream context block injected into the ReAct system prompt now
explicitly distinguishes specific-item queries (use entities as tool args) from
fleet/all queries (call with no scope so the tool returns the full result set).
Also: a CRITICAL rule that the workstream summary describes past actions, not
live data — always call the appropriate tool for current results, even if the
summary appears to contain the answer.

### Removed query enrichment for routing (`agents/orchestrator.py`)
Previous-turn text was being concatenated into the current query and used
for Stage 1 + Stage 2 vector search ("feasibility check!" after intent
creation got enriched to "I'm opening a new Alpenmarkt store … feasibility
check!" and routed to `ibn_intent_service` because the enriched form was
95% intent vocabulary). Bare "feasibility check!" routes correctly to
`ibn_feasibility_service` with a 0.066 gap — well above the clear-winner
threshold.

Removed:
- `_needs_context_enrichment` method (LLM call that detected follow-ups)
- The enrichment branch in `process_query` (asyncio.gather of follow-up
  detection + Stage 1, the Stage 1 re-run on enriched text, the
  `query_for_routing = enriched_query` assignment)
- `_IMPERATIVE_VERBS` module constant (only used by the removed method)
- The "Follow-up detected, enriched: …" broadcast

Kept:
- The `_SELF_CONTAINED` set, renamed `needs_enrichment_check` →
  `is_short_followup` to reflect its remaining purpose: gating the
  `use_stickiness` flag on `_route_query`. Short non-imperative
  follow-ups ("yes", "do it", "and now?") still get sticky-bias toward
  `last_service` because their bare text has weak routing signal.
- Cross-turn continuity now flows entirely through (a) the `last_domain`
  sticky hint to Stage 1 and (b) the workstream context block injected
  into the ReAct system prompt — no query-text manipulation.

Net: one LLM call removed per short follow-up turn (~200ms savings), one
source of routing bias eliminated, ~80 LOC removed.

### Revert manual voyage-4 embedding pipeline; fix is `quantization: float` on autoEmbed (`agents/orchestrator.py`, `seed/*.py`, MongoDB indexes)
The earlier diagnosis was wrong. Atlas `autoEmbed` *does* pass voyage-4's
asymmetric `input_type` parameter (`document` at index time, `query` at search
time) — the actual cause of the score collapse on `mcp_services` was the
default `quantization: "scalar"` (int8), which compresses cosine scores into
a noise band for short-text vectors. The one-line fix is to set
`quantization: "float"` on every autoEmbed field, keeping full float32
precision.

Applied to all six vector indexes in the demo:
- `mcp_services / vector_index`
- `agent_workstreams / workstream_vector_index`
- `agent_memories / agent_memories_index`
- `user_preferences / user_preferences_index`
- `ibn_knowledge_chunks / ibn_knowledge_index`
- `dtw_knowledge_chunks / dtw_knowledge_index`

Updated four seed scripts (`seed/workstream_index.py`, `seed/memories_index.py`,
`seed/ibn_seed.py`, `seed/dtw_seed.py`) to include `quantization: "float"` in
every autoEmbed field. Recreated all six live indexes with the new config.

Removed the manual-embedding scaffolding the previous (wrong) diagnosis added
to the orchestrator:
- `voyageai` import, voyage Client init, `VOYAGE_API_KEY` env-var requirement
- `_embed_for_index` / `_embed_for_query` helper methods
- The per-service voyage API call loop in `_sync_registry`
- The `description_embedding` field on every `mcp_services` doc (unset via
  `update_many`)
- `_semantic_search` now passes raw `query: <text>` to `$vectorSearch` against
  the `description` field (autoEmbed handles the embedding on both sides)

Net result: ~40 lines of code removed, one dependency removed from the
orchestrator hot path (`voyage.embed` per query no longer needed), and the
score range stays just as discriminative as the manual route (0.59-0.72 with
real gaps). `voyageai` is kept in `requirements.in` because
`restaurant_guide.py` uses it for its own ad-hoc embeddings.

The text-match tiebreaker stays — it's still useful for the rare queries
where sibling services share enough vocabulary to land within ~0.005 of each
other ("Raise prepaid ACME M…" routes to `dtw_traffic_service` at 0.7029 and
`dtw_scenario_service` at 0.6955; text-match resolves it 63 vs 2).

### Removed `DTW-SCN-DEMO-A` fixture (`seed/dtw_seed.py`, `mcp_servers/dtw_scenario_service.py`)
The seeded DEMO-A scenario was leftover scaffolding from when `dtw_scenarios`
had no other population path. The two-step what-if flow creates real scenarios
on the first user turn, so the fixture added no value and was confusing during
debugging (it kept appearing in the dashboard alongside scenarios under test).
The seed no longer inserts anything into `dtw_scenarios` — the collection is
empty after seeding and is populated at runtime by `create_scenario`.
`delete_all_scenarios` simplified accordingly: dropped the `keep_demo` arg and
the regex carve-out. Now a plain `delete_many({})`.

### Code-review cleanups (`agents/orchestrator.py`, `mcp_servers/dtw_scenario_service.py`)
Four redundant `import re` statements inside methods removed (already imported at
module level). The imperative-verb regex in `_needs_context_enrichment` was
extracted to a module-level pre-compiled `_IMPERATIVE_VERBS` to avoid
recompilation on every short query. `dtw_scenario_service` gained a module-level
`import re`, replacing the `import re as _re` workaround used in
`update_scenario`.

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
