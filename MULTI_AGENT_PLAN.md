# Multi-Agent Refactor — Implementation Plan

Goal: evolve from *one orchestrator → N MCP servers* to *one shell → N domain
agents → pool of thin MCP servers*, without breaking the demos at any point.

## Ground truth (current state)

- `agents/orchestrator.py` (3,395 lines) is a monolith holding six concerns:
  registry sync, two-stage routing, workstream layer, memory layer
  (extraction/recall/promotion/decay), the ReAct loop, and MCP session
  management (one `AsyncExitStack`, sessions reused across queries).
- `main.py` is already a thin shell — it only calls `agent.process_query()`.
- LLM calls inside MCP servers today: `ibn_intent_service` (NL→intent parse),
  `dtw_scenario_service` (NL→scenario parse + amendment), and
  `dtw_simulation_service` (inline scenario creation only). The other seven
  demo services are pure data/action layers already.
- Routing today returns a *server list*; the ReAct loop sees a flat namespace
  of `{service}__{tool}` tools.

## Target architecture

```
main.py (CLI)  /  web shells
      │
SHELL (coordinator)
  - Stage 1 domain classification → selects AGENT(s), not servers
  - workstream + memory layers (unchanged, shared)
  - parallel dispatch (asyncio.gather) + cross-agent synthesis
      │                     │
  IBN AGENT             DTW AGENT          (singletons stay direct
  own ReAct loop        own ReAct loop      tools of the shell:
  own system prompt     own system prompt   preferences, restaurant,
  owns NL→intent parse  owns NL→scenario    analytics, …)
      │                     │
      └──────── MCP TOOL POOL (thin, no LLM calls) ────────┐
  ibn_intent · ibn_inventory · ibn_feasibility · ibn_assurance ·
  ibn_telemetry · dtw_plan · dtw_topology · dtw_traffic ·
  dtw_scenario · dtw_simulation
      │
  MongoDB Atlas (registry, workstreams, memories, prefs,
                 agent cards, agent-to-agent transcript)
```

Granularity decision: **two domain agents, not ten micro-agents.** Each
domain's five services share collections, vocabulary, and lifecycle — one
agent per domain gets a focused context window without fragmenting a single
user task across multiple LLM loops. The per-service "Use this service
when…" docstrings become *tool-selection guidance inside the agent's
system prompt* instead of orchestrator-level routing fodder.

---

## Phase 0 — Decompose the monolith (no behavior change) ✅ DONE 2026-06-11

Pure mechanical extraction; the demo must run identically afterwards.
Completed: byte-identical member extraction verified against HEAD;
bootstrap + end-to-end query smoke-tested against live Atlas. See the
2026-06-11 entry in CHANGES.md for details. One deviation from the table
below: the routing-decision helpers (`_decision_set`, `_persist_decision`,
…) went to `router.py` (routing analytics) rather than getting their own
module, and `list_servers_info` went to `registry.py`.

Split `agents/orchestrator.py` into a package:

| New module | Moves from orchestrator.py |
|---|---|
| `agents/broadcast.py` | `Colors`, `TITLE_COLORS`, `_broadcast` (as a class `Broadcaster`) |
| `agents/registry.py` | `_sync_registry`, `_watch_servers`, `_extract_docstring`, `_extract_discriminator`, `_compute_file_hash`, `_infer_domain`, `add_server`, `remove_server` |
| `agents/router.py` | `_classify_domain`, `_semantic_search`, `_route_query`, `_list_domains`, `_text_match_score`, `_is_session_continuation` |
| `agents/memory.py` | `_recall_memories`, `_recall_preferences`, `_mark_memories_recalled`, `_extract_memories`, `_decay_memories_sweep`, `_memory_decay_loop`, `_extract_backlog`, promote/decay knobs |
| `agents/workstreams.py` | `_classify_workstream`, `_create/close/attach/update…`, closure-cue + replay-recipe helpers |
| `agents/mcp_pool.py` | `_activate_servers`, `sessions`, `tool_cache`, exit-stack lifecycle |
| `agents/react.py` | the ReAct iteration loop extracted from `process_query` into a reusable `run_react(client, model, system_prompt, tools, query, on_tool_call) -> str` |

`OrchestratorAgent` stays as the composition root with the same public
surface (`process_query`, `list_servers_info`, context manager), so
`main.py` and the web shells don't change.

Acceptance: run both demos end-to-end (IBN violation flow, DTW Flow A);
live feed and dashboards behave identically.

## Phase 1 — Introduce `DomainAgent` ✅ DONE 2026-06-11

Completed — see the 2026-06-11 Phase 1 entry in CHANGES.md. Deviations
from the sketch below: agent specs are plain dicts in `agents/catalog/`
(not YAML); both IBN and DTW agents are registered (the `AGENT_MODE`
flag defaults to IBN-only dispatch, `all` enables both); `AgentResult`
carries `status` but the NEEDS_INPUT protocol is deferred to Phase 2;
per-session locks went in as planned (lazy, in `_call_tool_locked`).

New `agents/domain_agent.py`:

```python
class DomainAgent:
    name: str                  # "ibn_agent"
    description: str           # the agent card text (vector-indexed)
    domain: str                # "ibn" — claims all servers in that domain
    system_prompt: str         # role + tool-selection guidance + anti-hallucination rules
    async def run(self, task: str, context: AgentContext) -> AgentResult
```

- `run()` = its own ReAct loop (`agents/react.py`) over **only its domain's
  MCP tools**, acquired from the shared `McpPool`.
- `AgentContext` carries: workstream summary, recalled memories,
  preferences block, conversation tail. `AgentResult` carries: answer,
  tool-call audit (for the workstream trail), confidence/`NEEDS_INPUT`
  status so the shell can decide to synthesize, retry, or ask the user.
- Agent definitions live in `agents/catalog/ibn.py` and
  `agents/catalog/dtw.py` — prompt + domain claim, ~50 lines each. The
  five IBN docstrings' "Use this service when…" blocks fold into the IBN
  agent prompt.
- **Agent cards in Atlas**: new collection `agent_registry.agent_cards`
  `{_id, description, domain, tools_claimed, last_seen}` with the same
  autoEmbed vector index pattern as `mcp_services`. This is the demo beat:
  *agent discovery is itself an Atlas vector search* — exactly the A2A
  agent-card idea, stored where the rest of the operational memory lives.

Concurrency prerequisite (do it here): per-agent tool invocation must not
interleave on a shared stdio `ClientSession`. Either give each agent its
own sessions from `McpPool` (keyed `agent×server`) or wrap each session in
an `asyncio.Lock`. Start with the lock — cheaper, sufficient for 2 agents.

Acceptance: a feature flag (`AGENT_MODE=1`) routes IBN queries through
`DomainAgent("ibn")` while everything else uses the legacy path. Outputs
match the legacy path on the scripted demo flows.

## Phase 2 — Promote the shell to coordinator

Rewire `process_query`:

1. Stage 1 (`_classify_domain`) output now resolves to **agents** via
   `agent_cards` (vector search + domain filter), not to server lists.
2. Stage 2 (per-domain `$vectorSearch` over `mcp_services`) moves **inside**
   `DomainAgent.run()` — the agent decides which of its five servers to
   activate for this task. Routing decision analytics gain a
   `dispatched_agents` field.
3. Single-domain query → dispatch one agent, pass its answer through.
   Multi-domain query → `asyncio.gather` both agents with scoped sub-tasks,
   then one synthesis LLM call in the shell.
4. Workstreams/memory stay shell-owned. The agent's tool-call audit flows
   back via `AgentResult` into `_attach_to_workstream` unchanged.
   `agent_memories` documents gain an `agent` field for scoped recall.
5. Broadcast: new `DISPATCH` tag + indent agent-internal `ACTION`/`RESULT`
   lines with the agent name so the live feed shows the hierarchy.

Delete the legacy direct-to-server path once the flag has soaked.

Acceptance: cross-domain query works end-to-end, e.g. *"We're planning the
QoS uplift in NYC — and check whether any IBN intents are currently
violated."* — both agents run concurrently, shell synthesizes one answer.

## Phase 3 — Agent-to-agent consultation

- Each `DomainAgent` gets one extra tool: `consult_agent(agent, question)`.
  The shell mediates the call (it owns the agent instances), enforces
  depth=1 (a consulted agent cannot consult further), and a single-turn
  budget.
- Every exchange is persisted to `agent_registry.agent_conversations`
  `{ts, workstream_id, from_agent, to_agent, question, answer}` — the
  audit trail is itself a demo beat (Change-Stream-able, dashboard panel).
- Demo script: during `simulate_qos_change`, the DTW agent asks the IBN
  agent "any active compliance violations at sites in these markets during
  Saturday peak?" and the simulation narrative cites the answer.

## Phase 4 — Thin the MCP servers

Move the three embedded LLM calls up into the agents:

- `ibn_intent_service.submit_intent(text)` → agent parses NL itself, calls
  new `submit_intent_structured(parsed: dict)`; keep the `text` variant as
  a deprecated wrapper for one release so old flows don't break.
- Same for `dtw_scenario_service.create_scenario` / `update_scenario`
  (the amendment prompt moves into the DTW agent, where it also has the
  conversation context — strictly better than today's blind re-parse).
- Remove `_create_scenario_inline` from `dtw_simulation_service`; the DTW
  agent now guarantees a scenario exists before calling simulate.

After this phase the rule is enforceable: **servers contain zero `openai`
imports** — reasoning lives in agents, execution lives in servers. The
hybrid `$vectorSearch`/`$graphLookup` queries stay in the servers: they are
data operations, not reasoning, and they're the MongoDB story.

---

## Explicitly out of scope / not recommended

- **Ten micro-agents.** Per-service agents multiply LLM hops and shred a
  single user task across loops with no benefit at this scale.
- **A2A protocol wire format.** Adopt the *concepts* (agent cards,
  consultation); skip the protocol plumbing until a second process or a
  third party needs to call our agents. The agent-card collection is
  designed so an A2A endpoint could be bolted on later.
- **Replacing routing analytics, dashboards, seeds.** All Change-Stream
  consumers keep working untouched — same collections, same documents.

## Sequencing & effort

| Phase | Effort | Demo-safe checkpoint |
|---|---|---|
| 0 — decompose | 1–2 days | identical behavior, both demos |
| 1 — DomainAgent + cards | 2–3 days | IBN behind flag matches legacy |
| 2 — coordinator | 2 days | cross-domain query works |
| 3 — consultation | 1 day | DTW→IBN consult in Flow A |
| 4 — thin servers | 1 day | no `openai` import under mcp_servers/ |

Each phase merges independently; the demo stays runnable after every one.

## Risks

- **stdio session concurrency** (Phase 1): mitigated by per-session locks;
  upgrade to per-agent sessions only if lock contention shows up.
- **Latency**: shell→agent adds one LLM hop on single-domain queries.
  Mitigation: shell does *no* ReAct of its own for single-agent dispatch —
  classification + passthrough only, so net cost is ≈ today's Stage 1.
- **Sticky routing regressions**: `last_domain` semantics move from
  "sticky server" to "sticky agent"; the workstream classifier already
  anchors on domain, so this mostly simplifies, but the short-follow-up
  path (`is_short_followup`) needs explicit re-testing.
