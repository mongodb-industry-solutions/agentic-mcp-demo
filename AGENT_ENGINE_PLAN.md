# Atlas Agent Engine Migration — Implementation Plan

Goal: move the Telco OSS half of this demo (IBN + DTW, plus the shared
memory/workstream layers) off our hand-rolled runtime and onto **MongoDB
Atlas Agent Engine**. The Telco OSS story shifts from *"MongoDB is a great
database for your agents"* to *"MongoDB runs your autonomous-network agents
end to end, with execution, memory, governance and audit all in one Atlas
footprint."* We migrate without breaking the live demos at any point.

> Source of truth: the Agent Engine docs (`/docs/agentengine/*.md`, pulled
> 2026-10-14) and the product page. Agent Engine is in **Public Preview**:
> no SLA, breaking changes expected. Every "⚠ verify" below is an
> assumption we have not yet confirmed against a running workspace.

---

## The pitch we are building toward

> **Atlas Agent Engine is the agentic runtime for Telco OSS.** Intent comes in
> (TMF921-style), domain agents reason over live inventory, topology,
> telemetry and a digital twin, all in Atlas. Every tool call runs
> sandboxed under a real identity and is checked against org-level policy.
> Network-changing actions pause for a NOC engineer's approval, and the
> runtime guarantees they are never executed twice. Every model call, tool
> call and memory access is traced. The runtime's own state (checkpoints,
> executions, memory) also lives in Atlas, so the same Change Streams that
> drive the NOC dashboards can watch the agents think.

This maps directly onto the **TM Forum Autonomous Networks** levels. Today
the demo shows L3 (conditional autonomy: agent proposes, scripted flow
executes). On Agent Engine it shows **L4: closed-loop assurance with
policy-bounded autonomy and human approval on blast-radius actions**,
with the guardrails enforced by the platform rather than by our prompts.

---

## What Agent Engine actually gives us (from the docs)

| Capability | Mechanism | Replaces in our code |
|---|---|---|
| Managed execution | `App` + `@app.entrypoint` (LangGraph or Google ADK); Orchestration Engine (OE) + per-session **agent sandbox** + **tool sandbox** | `react.py`, `domain_agent.py` loop plumbing, `mcp_pool.py` subprocess pool |
| Durable checkpoints | `app.checkpointer()` (MongoDB-backed), record/replay of `app.llm()` / `app.tool()` effects | nothing today (a crash mid-turn loses the turn) |
| Human-in-the-loop | LangGraph `interrupt()` → `suspended` → `/resume` with `allowed_decisions`; **replay guarantee** (no duplicate side effects) | nothing today |
| Agent-to-agent | `a2a:` block in `agent.yaml`, `app.a2a_tools()` / `app._runtime.a2a.invoke_agent()`, OE-brokered, nested in traces | `dispatch._consult_agent`, `_dispatch_multi`, `agent_conversations` bookkeeping |
| Remote MCP | `mcp.servers.<name>` (Streamable HTTP only; bearer / OAuth / client-credentials; `allowed_tools`) | stdio `uv run` subprocesses |
| OpenAPI connectors | `connectors:` in `agent.yaml` → compiled `tool.yaml` from OpenAPI 3.0 | n/a (enables a TMF Open API story) |
| Memory | STM turn recording + background extraction into **semantic / episodic / procedural / taxonomic** LTM (+ custom types); `app.memory.build_context()`; Voyage-embedded | `memory.py` extraction/recall, `preferences_service`, `agent_history` |
| Policy Engine | org→project cascading `AUTHORIZED_TOOLS`, `AUTHORIZED_MODELS`, `MAX_TOOL_CALLS/LLM_CALLS/TOKENS_PER_EXECUTION`, `MAX_TOKENS_PER_SESSION`, `MAX_EXECUTION_DURATION_MS` | hard-coded iteration caps, nothing for cost or tool authorization |
| Guardrails | regex `output_validation` on `llm_input`/`llm_output`; `block` / `modify` / `log_only` / `require_review` | nothing today |
| Network egress | per-sandbox `deny_all` / `allow_list` / `allow_all`; linked Atlas cluster is in the base policy | nothing today |
| Observability | Traces page (LLM/tool/policy-check/memory steps, A2A nested), runtime logs, log export | our `broadcast.py` Agent Log (kept, see Phase 6) |
| Invocation | `POST …/invoke`, `…/invokeStream` (SSE: `text`, `step`, `subagent_*`, `done`, `error`, or custom events), `X-Session-ID`, `X-Mdb-Agent-Engine-Custom-*` headers | the WebSocket ↔ `process_query()` coupling in `web/shell.py` |

Hard limits that shape the design (preview): static `scaling.replicas`
(1–512 **per project**, no autoscale, one sandbox pair per session →
`pool full` errors), 0.5 vCPU / 2 GB per agent, OE ≤ 50 req/s, A2A token
lifetime 5 min, A2A invoke timeout ≤ 300 s, **no app-authored LangGraph
`Send` fan-out** in durable workflows, custom headers are **not persisted**
(they must be resent on resume), and **service-account invocations share
one memory identity**.

---

## Ground truth (current state)

- `web/shell.py` creates one `OrchestratorAgent` per browser session with a
  `s_<token>_` collection prefix. The orchestrator composes 8 mixins:
  registry sync, 2-stage routing, workstreams, 3-plane memory, MCP pool,
  ReAct, dispatch, broadcast.
- `AGENT_MODE=all` → `ibn_agent` / `dtw_agent` (`agents/catalog/`), each a
  raw `AsyncOpenAI` ReAct loop (`temperature=0`, `parallel_tool_calls=False`,
  5/8 iterations, VERBATIM short-circuit). Multi-domain turns are split
  by gpt-4o-mini, fanned out with `asyncio.gather`, and synthesized by
  gpt-4o. `consult_agent` runs at depth 1 with a budget of 2.
- 10 Telco MCP servers (`mcp_servers/ibn_*`, `dtw_*`) are FastMCP over
  **stdio**, spawned via `uv run` with `DEMO_PREFIX` in env. They bind
  `_PFX = os.environ["DEMO_PREFIX"]` **at import time**. They contain no
  LLM calls (Phase 4 of MULTI_AGENT_PLAN).
- Mutating tools with real blast radius: `activate_plan`, `apply_runbook`,
  `update_template_version` (fleet-wide), `cancel_intent`,
  `delete_all_scenarios`, plus the chaos tools `inject_event` and
  `reset_telemetry`.
- Dashboards (IBN :8060, DTW :8080) are pure Change-Stream consumers of
  Atlas collections. **They do not care who writes the documents.**
- Deployed as 4 Kanopy containers; only the shell spawns MCP subprocesses.

---

## Concept mapping

| Today | On Agent Engine | Stays in our Atlas collections? |
|---|---|---|
| `OrchestratorAgent` per browser session | Agent Engine **session** (`X-Session-ID` = shell token) with its own sandboxes and checkpoint | — |
| Shell coordinator (Stage 1, card ranking, split, synthesis) | **`noc-coordinator`** agent (LangGraph) | `agent_cards` (vector-ranked), `routing_decisions` |
| `ibn_agent`, `dtw_agent` | **`ibn-agent`**, **`dtw-agent`** workspaces, A2A-enabled with `skills` | — |
| `consult_agent` (depth 1, budget 2) | A2A `invoke_agent` + `x-consult-depth` custom header; budget via policy cap | `agent_conversations` becomes optional (traces cover it) |
| stdio MCP servers | `@app.tool()` in the **tool sandbox** (Phase 2), remote MCP for external OSS (Phase 7) | all domain collections unchanged |
| `DEMO_PREFIX` env per subprocess | `X-Mdb-Agent-Engine-Custom-Demo-Prefix` → `get_current_custom_headers()` per call | prefixed collections unchanged |
| Iteration caps 5/8 | `recursion_limit` + `MAX_TOOL_CALLS_PER_EXECUTION` / `MAX_LLM_CALLS_PER_EXECUTION` | — |
| `agent_workstreams` (working memory + tool trail) | **keep**: domain-shaped, dashboard-visible; closure writes an **episodic** memory | ✅ |
| `agent_memories` plane B (extracted facts, promote/decay) | **procedural** (runbooks, flows) + **semantic** (facts) via platform extraction | promotion/decay → custom memory type or retire |
| `user_preferences` plane C | **semantic** memory (`save_semantic(label, text)`) | retire after migration |
| `agent_history` / conversation tail | **STM** (automatic turn recording) | CLI cursor-up history only |
| — (new) | **taxonomic** memory: Telco glossary (QCI, APN, PCRF, HSS, SLO, CPE, …) shared project-wide | — |
| `broadcast.py` live Agent Log | `invokeStream` **custom events** (`emit_custom_event`) in the same tag format + Traces UI | — |
| `_sync_registry` file-hash scan + hot reload | build-time step in CI (`agentengine build`); registry docs written by a sync script | `mcp_services` (feeds in-domain tool narrowing) |
| Non-telco singletons (restaurant, crypto, portfolio, …) | **out of scope**: they stay on the legacy path | ✅ |

---

## Target architecture

```
 Browser ── web/shell.py (Kanopy, thin client) ── IBN/DTW dashboards (Kanopy)
             │  OAuth client-credentials token (service account)       ▲
             │  POST /invokeStream  X-Session-ID: <token>              │ Change
             │  X-Mdb-Agent-Engine-Custom-Demo-Prefix: s_<token>_      │ Streams
             │  POST /executions/{id}/resume  (approval card)          │
             ▼                                                         │
 ┌──────────── Atlas Agent Engine project "telco-oss" ───────────────┐ │
 │  Policy Engine (org ⊃ project)   Guardrails   Traces   Memory     │ │
 │                                                                   │ │
 │  noc-coordinator ──A2A──► ibn-agent ◄──A2A (consult)──► dtw-agent │ │
 │   rank cards ($vectorSearch)   │ agent sbx: graph, app.llm         │ │
 │   split → parallel A2A         │ tool sbx:  oss_tools.ibn.*        │ │
 │   synthesize                   │   interrupt() before activate/    │ │
 │                                │   apply_runbook/template bump     │ │
 └────────────────────────────────┼──────────────────────────────────┘ │
                                  ▼                                    │
 MongoDB Atlas (linked cluster) ──────────────────────────────────────┘
   domain data (ibn_*, dtw_*, s_<token>_*)  ·  agent_cards  ·  mcp_services
   agent_workstreams  ·  routing_decisions  ·  $vectorSearch / $graphLookup
   MDB_AGENTIC_STORE_DB (checkpoints, executions)  ·  MONGOMEM_DB_NAME (memory)
```

Repository layout (Agent Engine **monorepo manifest**):

```
agent-engine/
  agent.yaml                  # agents: [noc-coordinator, ibn-agent, dtw-agent]
  project-config.yaml         # memory.extraction.enabled, custom memory types
  agents/noc-coordinator/{agent.yaml, pyproject.toml, src/…}
  agents/ibn-agent/{agent.yaml, …}
  agents/dtw-agent/{agent.yaml, …}
oss_tools/                    # NEW shared package: plain async functions
  ibn/{intent,inventory,feasibility,assurance,telemetry}.py
  dtw/{plan,topology,traffic,scenario,simulation}.py
  _ctx.py                     # prefix resolution (header → env fallback)
mcp_servers/ibn_*.py, dtw_*.py  # become ~20-line FastMCP shims over oss_tools
```

---

## Key decisions

**D1: Framework: LangGraph.** It is the documented adapter, with HITL
`interrupt()`, `MongoDBSaver`, Deep Agents and durable record/replay. ADK
exists but has fewer docs. Our raw OpenAI loop has to become a
`StateGraph` either way, because only `app.llm()`/`app.tool()` effects are
audited, policy-checked and replayed.

**D2: Tools: in-process `@app.tool()` first, remote MCP second.**
Agent Engine has no stdio MCP. Hosting our 10 servers as Streamable-HTTP
services would work, but static `mcp.servers.*.headers` cannot carry the
per-session `DEMO_PREFIX`, and remote MCP tools never see
`get_current_custom_headers()`. Instead we extract the tool bodies into
`oss_tools/` (plain functions) and register them twice: as `@app.tool()`
in the Agent Engine tool sandbox, and as thin FastMCP shims so the
CLI/legacy path keeps working. The "bring your existing OSS MCP servers"
beat comes back in Phase 7 with a real external MCP/OpenAPI system, where
it is the honest pattern.

**D3: Routing: Atlas is the switchboard, A2A is the wire.** A2A discovery
filters by `skills` and `capabilities` only and has no semantic ranking. We
keep `$vectorSearch` over `agent_cards` in the coordinator (the "routing
through Atlas" beat from WHY_MONGODB) and then call
`_runtime.a2a.invoke_agent(agent_id, …)` deterministically. Cards get
synced from each agent's `a2a.skills` + description at deploy time.

**D4: Memory: platform memory for user-shaped knowledge, Atlas
collections for domain-shaped state.** Workstreams stay, because they are
operational objects (entities, tool trail, state machine) that the
dashboards render. Preferences, conversation history, extracted facts and
glossary move to Agent Engine memory. **Identity caveat:** the shell
invokes through a service account, so `app.memory` collapses every browser
session into one user. Workaround: the agent uses a standalone `Memory`
client bound to `user_id = <session token>` (read from a custom header),
which the docs recommend for per-end-user isolation. ⚠ verify that the
memory API token can be a sandbox secret.

**D5: Session isolation: two layers, both native.** Compute isolation
comes from Agent Engine sessions (sandboxes reset between sessions). Data
isolation comes from the existing prefix scheme, carried by a custom
header. The `DEMO_MAX_SESSIONS` and `DEMO_SESSION_TTL_SEC` values map
onto `scaling.replicas` and `scaling.agent_idle_ttl_seconds`.

**D6: Scope.** Only the Telco OSS domains move. The legacy runtime stays
for the CLI and the non-telco singletons until they are retired. Both
runtimes write the same collections, so the dashboards cannot tell which
one did the work.

---

## Phase 0: Spike and account setup (½–1 day)

- Install the CLI, `agentengine auth login`, create a dedicated project
  `telco-oss-demo`. **Use a separate project**: agents can read every
  project-level secret.
- `agentengine atlas setup` → **link the existing demo cluster** (do not
  create a Flex cluster). Narrow the auto-created `readWriteAnyDatabase`
  user to `agent_registry` + `MDB_AGENTIC_STORE_DB` + `MONGOMEM_DB_NAME`.
- Workspace secrets (`--workspace-scope`): `OPENAI_API_KEY`,
  `VOYAGE_API_KEY`, `MONGODB_URI`.
- Hello-world LangGraph agent calling one read-only IBN tool
  (`list_sites`) against the real cluster, both via `agentengine dev up`
  (with `dev.yaml: services.mongodb.local: false`) and deployed.
- **Answer the ⚠ questions** (see Open questions) before Phase 1.

Checkpoint: one Telco tool call is visible in the Traces UI.

## Phase 1: Extract `oss_tools/` (1–2 days, no behavior change)

- Move each `@mcp.tool` body into `oss_tools/<domain>/<service>.py` as a
  plain `async def`. The FastMCP files become shims that import and
  register the same functions. Docstrings stay (they are the tool
  descriptions in both runtimes).
- Replace import-time `_PFX` and collection handles with
  `oss_tools/_ctx.py: coll(name)`, which resolves the prefix per call:
  `get_current_custom_headers()["demo-prefix"]` → `DEMO_PREFIX` env →
  `""`. Keep the reference-vs-mutable collection split unchanged.
- Legacy runtime smoke test: both opener flows plus Reset, identical
  results.

Checkpoint: `git diff` on behavior is empty and the demo runs as before.

## Phase 2: Domain agents on Agent Engine (2–3 days)

- `ibn-agent` / `dtw-agent`: `StateGraph(model ⇄ ToolNode)`,
  `app.llm(ChatOpenAI(model="gpt-4o", temperature=0)).bind_tools(…,
  parallel_tool_calls=False)`, system prompt from `agents/catalog/`
  (move it as-is). Add a VERBATIM short-circuit as a conditional edge.
  The iteration cap becomes `recursion_limit` plus a policy cap.
- NL extraction stays inside the agent (`submit_intent` and
  `create_scenario` take structured fields, as today).
- Tool placement: the agent sandbox runs no tools and has no egress except
  the LLM host (`allow_list: api.openai.com:443`; ⚠ verify whether
  `app.llm` needs this). Everything else goes in the tool sandbox. The
  linked Atlas cluster is reachable through the base policy.
- In-domain tool narrowing (`_select_servers`) is kept as a graph node:
  bind only the top-k tools from `$vectorSearch` over `mcp_services`. In
  the story, Atlas shrinks the tool surface per turn, which saves tokens.
  Today's 5-service domains bind all of their tools.
- `features.memory: true`, `features.guardrails: true`,
  `a2a.enabled: true` with skills (`intent-lifecycle`,
  `slo-assurance`, `incident-diagnosis` / `qos-what-if`,
  `policy-change-simulation`, `topology-impact`).

Checkpoint: `agentengine invoke` drives the full IBN onboarding and DTW
Flow A against the per-session prefix, and the dashboards update live.

## Phase 3: HITL for blast-radius actions (1–2 days). The headline beat.

- Wrap `activate_plan`, `apply_runbook`, `update_template_version`,
  `cancel_intent` and `delete_all_scenarios` with an approval node that
  calls `interrupt({…suspend_context…})` **before** the tool node runs
  them. The suspend context carries intent/site, the proposed change,
  blast radius (sister sites inheriting a template bump), the DTW
  simulation verdict, and `allowed_decisions: [approve, reject,
  approve_canary]`.
- `approve_canary` (IBN): apply to the first site only, re-check
  compliance, then ask again for the fleet. This is the L4 story in one
  click.
- Replay guarantee: on resume, the OE serves cached results for the
  `check_feasibility` / `propose_plan` steps. Demo point: "no action is
  ever executed twice, even across a crash."
- Shell: when the stream reports `suspended`, render an **approval
  card** (context, decisions, notes). Approving calls `/resume` and
  **resends the custom headers**. The shell's service account needs
  `PROJECT_OWNER` to resume.
- Dashboards: show a "⏸ awaiting NOC approval" state. The approval
  request is visible in Atlas, either by watching the execution in
  `MDB_AGENTIC_STORE_DB` (⚠ verify the schema) or by having the
  approval node write a small `ibn_approvals` doc.

Checkpoint: violation → diagnose → "apply runbook?" → approve in the
browser → dashboard goes green. The same pending review shows in the
Agent Engine **Pending Reviews** page.

## Phase 4: Coordinator plus A2A (2 days)

- `noc-coordinator` graph: `classify` (Stage 1 domains, gpt-4o-mini via
  `app.llm`) → `rank` ($vectorSearch `agent_cards`) → `delegate` →
  `synthesize` (gpt-4o, only when ≥ 2 agents answered).
- **Parallel delegation without `Send`:** the split step is an
  `app.llm(...)` call with `tool_choice="required"` over a single
  `delegate(agent, subtask)` tool. The model emits N tool calls in one
  message and `ToolNode` runs them concurrently. Ordinals are reserved by
  the secure LLM wrapper, so this is durable. `delegate` is a local
  (agent-sandbox) tool, because A2A is unavailable in the tool sandbox.
  It calls `invoke_agent` and forwards the custom headers explicitly.
- Consultation: ibn/dtw keep a `consult_agent` tool that wraps A2A.
  Depth is enforced with `custom_headers={"x-consult-depth": "1"}` (the
  callee drops the tool), and `allowed_callers` restricts who may call
  whom. Treat a `401` (token older than 5 min) as transient.
- Handle `input-required` from a child (an approval inside a delegated
  sub-task) by bubbling it up as the coordinator's own `interrupt()`.
  ⚠ verify how a nested suspend surfaces to the root caller.
- Retire `_dispatch_multi`, `_consult_agent` and the `agent_conversations`
  writes (A2A calls nest in the session trace automatically). Keep
  `routing_decisions`, because it is the Atlas routing-analytics beat.

Checkpoint: "Flow A, and which IBN retail sites in NYC share those cells?"
fans out to both agents and the trace shows the A2A tree.

## Phase 5: Memory migration (2 days)

- `project-config.yaml`: `memory.extraction.enabled: [semantic,
  episodic, procedural, taxonomic]`.
- **Taxonomic** (new): seed a Telco glossary of about 60 terms (from
  `ibn_knowledge_chunks` / `dtw_knowledge_chunks` vocabulary and 3GPP/TMF
  terms). Shared org-wide, it is the "agent speaks operator" beat.
- **Procedural**: seed the IBN runbooks and DTW mitigation playbooks.
  Extraction learns new procedures (e.g. "onboarding = submit →
  feasibility → plan → activate") from conversations. This replaces
  plane B's extraction prompt.
- **Episodic**: on workstream closure, `save_episode(title, summary +
  tool trail)`. This replaces the `_watch_workstream_closures` →
  `_extract_memories` pipeline.
- **Semantic**: `preferences_service` tools become `save_semantic` /
  `search_semantic`. Retire `user_preferences` after a one-shot
  migration script.
- Context assembly: `build_context(query, enabled_sources={stm,
  episodic, semantic, procedural, taxonomic})` replaces
  `_recall_memories` + `_recall_preferences` + conversation tail. The
  workstream block is still injected from `agent_workstreams`.
- Promotion/decay (`tier: core`): either map it to a custom memory type
  or retire it and let platform consolidation do the job. Decide after
  watching extraction quality in Phase 0.
- Reset button: also delete the session-user's memories (⚠ verify a
  delete-by-user API exists).

Checkpoint: the cross-session "magic moment" from WHY_MONGODB §5b works
on platform memory: a new browser session recalls a preference and a
learned procedure.

## Phase 6: Governance, guardrails, observability (1–2 days)

- **Org policy** (applies to every Telco project; projects can only
  tighten it): `AUTHORIZED_MODELS = {gpt-4o, gpt-4o-mini}`,
  `MAX_TOKENS_PER_SESSION` as a cost ceiling for public demo sessions,
  `MAX_EXECUTION_DURATION_MS`.
- **Two projects, one codebase.** `telco-oss-lab` allows the chaos
  tools (`inject_event`, `reset_telemetry`, `seed_baseline`).
  `telco-oss-prod` has an `AUTHORIZED_TOOLS` allowlist **without** them.
  Demo point: "the same agent build cannot inject faults in production,
  and no prompt can change that." Show the `Policy denied:` error live.
- **Guardrails**: an `llm_output` `modify` guardrail redacting IMSI
  (`\b\d{15}\b`) and MSISDN patterns from `dtw_subscribers` results. Also
  a `require_review` guardrail on outputs that mention fleet-wide template
  changes (a second line of defense behind Phase 3).
- **Live Agent Log**: set `features.use_custom_parser: true` and
  `emit_custom_event({"tag": "ROUTING", …})` at the same points where
  `broadcast.py` fires today, so `shell.html` keeps its colored feed
  unchanged. Keep the optional `NOTIFY_BROADCAST_URL` relay in the shell.
- Engineering mode (`?mode=eng`): a "View trace" deep-link to the Agent
  Engine Traces page per turn (execution id from the stream).

## Phase 7: Telco OSS integration surface (stretch, 2–3 days)

- **TM Forum Open APIs as connectors.** Publish thin OpenAPI façades over
  the Atlas collections: **TMF921 Intent Management** (`ibn_intents`),
  **TMF642 Alarm Management** (`ibn_compliance_events`),
  **TMF639 Resource Inventory** (`ibn_resources`, `dtw_network_elements`),
  **TMF641/640 Service Order/Activation** (`activate_plan`). Bind them via
  `connectors:` → `tool.yaml`. The story: "point Agent Engine at your
  existing TMF-compliant OSS and the tools appear, governed and audited."
  ⚠ The connector core is documented as Python-only and not exposed
  through the CLI. Verify it is usable in preview, and fall back to
  remote MCP if not.
- **Remote MCP for a "third-party" OSS system.** Host one service (e.g. a
  ticketing/ITSM stub, or the IBN inventory) as a Streamable-HTTP MCP
  server on Kanopy with `client_credentials` auth and `allowed_tools`,
  to show the vendor-MCP pattern alongside our own tools.
- **Runbooks as Deep Agent skills** (optional): `SKILL.md` per flow
  (IBN onboarding, assurance loop, DTW Flow A/B) via `app.deep_agent()`.
  This gives progressive disclosure instead of one giant system prompt.

## Phase 8: Shell, CI/CD, cut-over (1–2 days)

- `web/shell.py`: per WebSocket session, hold an OAuth token (refresh it
  before the 1 h expiry) and stream `invokeStream` SSE → WS. The session
  token becomes `X-Session-ID` plus the prefix header. Add a feature flag
  `RUNTIME=agent_engine|legacy` so we can fall back during a live demo.
- Scaling: `scaling.replicas` per agent ≈ expected concurrent demo
  sessions (each user session reserves a sandbox pair in **each** agent it
  touches, ⚠ verify for A2A children). Keep `DEMO_MAX_SESSIONS` ≤
  replicas, `agent_idle_ttl_seconds` = `DEMO_SESSION_TTL_SEC`. Surface
  `pool full` as a friendly "demo at capacity" banner.
- CI: GitHub App is unavailable in preview, so CI runs
  `agentengine build` / `deploy` with a **pinned CLI version** and a
  service account (see `deploy/ci-cd`). A build plus deploy takes 10–20
  min, so iterate with `agentengine dev up`.
- `bin/start.sh` local mode gains `agentengine dev up` for the agent
  stack. The Kanopy shell image drops the `uv` + MCP bootstrap.
- Docs: update CLAUDE.md, README, WHY_MONGODB (new Layer 0: "the
  runtime"), help.html. Add a CHANGES.md entry.

---

## The 5-minute Telco OSS storyline on Agent Engine

1. **Intent in.** "Open Munich Marienplatz with POS priority < 20 ms by
   tomorrow 18:00." The trace shows coordinator → A2A → ibn-agent →
   sandboxed `submit_intent`, and the dashboard lights up.
2. **Plan, then pause.** Feasibility and plan run, then **activation
   suspends for NOC approval**. Approve in the browser (or the Pending
   Reviews page).
3. **Break it.** `inject_event` produces a violation. The agent diagnoses
   it with the four-modality hybrid `$vectorSearch` and proposes a
   runbook, then pauses. Choose **approve_canary**, watch one site go
   green, then approve the fleet-wide template bump.
4. **What-if.** "Before we roll this to NYC: Flow A." The coordinator fans
   out to dtw-agent + ibn-agent in parallel, and `$graphLookup` +
   vector search run in one tool call.
5. **Govern it.** Switch to the `prod` project and try `inject_event`.
   The result is `Policy denied`, enforced by the org, not the prompt.
   Ask for subscriber samples and IMSIs come back redacted.
6. **It remembers.** New browser and new session: "set up another store
   like Munich." The agent recalls the learned onboarding procedure and
   the glossary term ("strict segmentation").
7. **It's all Atlas.** The checkpoints, executions, memories and domain
   data are documents in one cluster, watched by the same Change Streams.

---

## Open questions (resolve in Phase 0)

1. Does `app.llm` need `api.openai.com` in the agent sandbox egress
   allow-list, or does the OE proxy it? (The egress docs list "LLM provider
   APIs" as outside the base policy.)
2. Do `X-Mdb-Agent-Engine-Custom-*` headers reach **tool-sandbox** tool
   bodies via `get_current_custom_headers()`? (The docs show it "inside any
   tool", without naming a sandbox.) If not, pass the prefix as a hidden
   tool argument injected by the graph.
3. Per-end-user memory under service-account invocation: is a standalone
   `Memory` client from inside the agent sandbox supported and billed
   normally? Is there a delete-by-user call for Reset?
4. A2A children: does each child execution reserve its own sandbox pair
   (replica sizing), and how does a child `input-required` surface to the
   root `invokeStream`?
5. Can we read `MDB_AGENTIC_STORE_DB` / `MONGOMEM_DB_NAME` with Change
   Streams for the dashboards, and is that schema stable enough to demo?
6. OpenTelemetry export: the product page claims it, but the docs only
   show the Traces UI and log export. Is there an OTLP endpoint?
7. Connector core availability in preview (Phase 7).

### Phase 0 findings (local dev stack, CLI 0.1.119, SDK 0.11.x, 2026-10-14)

Spike agent: `agent-engine/agents/telco-oss-spike/` (hello-world template,
`gpt-4o` at `temperature=0`, real IBN fixtures read through
`DEMO_MONGODB_URI`). Everything below was measured with
`agentengine dev up`, **not yet on the deployed platform**.

| # | Status | Finding |
|---|---|---|
| 1 | ✅ answered | The LLM host must be on each sandbox's egress allow-list. `agentengine create --llm openai` declares `api.openai.com:443` itself. Local egress is an `HTTP_PROXY`: native sockets (e.g. `pymongo` to Atlas) are **not** blocked locally, so local runs can't prove the deployed egress policy. |
| 2 | ✅ yes (local) | `X-Mdb-Agent-Engine-Custom-Demo-Prefix` arrives as `demo-prefix` in `get_current_custom_headers()` in **both** the agent and the tool sandbox. The prefix-by-header design (D5) works and needs no hidden tool argument. |
| 3 | ◐ partly | `user_id` in the invoke body reaches both sandboxes and scopes memory correctly (user A recalls across sessions, user B sees nothing). With `durable_workflow` + `memory` on, `user_id` is **mandatory** (otherwise `durable Memory identity is incomplete`). The CLI help names a project-level **"end-user delegation"** setting that lets a caller act as another user. That's likely the fix for the service-account limitation; verify when deployed. |
| 4 | ☐ open | Needs two deployed A2A agents. |
| 5 | ✅ yes, internal schema | Runtime state is plain collections in `mdb_store_<project>` (`executions`, `sessions`, `traces`, `workflow_events`, `workflow_activities`, …) and `mdb_memory_<project>` (`memory_semantic` / `_episodic` / `_procedural` / `_taxonomic` / `_short_term`, `tasks`). `executions` carries `status`, `suspend_context`, `interrupts`, `resume_schema`, `parent_execution_id`, `a2a_caller_workspace_id`, `user_id`, `session_id`, so a Change Stream on it can drive the approval card and the dashboards' "awaiting approval" state. The schema is undocumented: treat it as read-only and pin to it per CLI version. |
| 6 | ◐ partly | Traces are **OpenTelemetry spans** (OpenInference conventions, `telemetry.sdk.name: opentelemetry`) stored in `mdb_store_<project>.traces`. OTLP export to an external collector is still unverified. |
| 7 | ✅ available | `agentengine create-tool --spec <openapi>` compiles OpenAPI 3.0 into connector `tool.yaml` / `tool_defs.yaml` (experimental). |

**Deployed run (2026-10-14).** Workspace `ws-6acfac0cea67bce171582ba8` in
the personal Agent Engine org, linked to a dedicated Atlas Flex cluster
(`telco-oss-spike`, AWS us-east-1, project in BenLorenzPersonalOrg) and
seeded with `seed/ibn_seed.py`. The first `deploy --auto` took about 8
min, with the tool sandbox last to become ready.

| # | Deployed result |
|---|---|
| — | `list_sites` returns the 5 seeded sites from the linked cluster. Linked-cluster egress works with no allow-list entry. |
| 2 | ✅ **Confirmed on the platform.** A service-account `POST …/invoke` with `X-Mdb-Agent-Engine-Custom-Demo-Prefix` delivers `demo-prefix` to both the tool and the agent sandbox. The `agentengine invoke` CLI has no custom-header flag, so use the HTTP API. |
| 3 | ✅ **Per-end-user memory works with a service account, contrary to the docs.** With `user_id` in the body, the sandbox sees that `user_id`: user-a saves a fact, user-a recalls it in a new session, user-b recalls nothing. Without `user_id`, identity falls back to the service account's ID. A *human* caller (CLI login) passing another `--user-id` is rejected: "end-user delegation is not enabled for this project". Design D4's standalone-memory workaround is **not needed**: the shell passes `user_id = <session token>`. Re-verify after CLI/SDK upgrades, since the docs say otherwise. |
| — | **Service-account roles:** new service accounts accept only `PROJECT_OWNER` or `PROJECT_READ_ONLY` (`AGENT_DEVELOPER` is rejected), so the shell's invoker is necessarily project owner. That's another reason for a dedicated project per environment. |
| — | **Pool capacity is real:** with the default `scaling.replicas: 4`, a 5th concurrent session returns **HTTP 503** "All agent instances are currently busy… retry after Retry-After". `agentengine workspace sessions stop` frees a sandbox only after a short reset delay. The Phase 8 "demo at capacity" banner should key on 503 + `Retry-After`. |
| 1 | ✅ **Deployed egress is enforced in both sandboxes, native sockets included.** A non-allow-listed host (`example.com`) fails over both `httpx` HTTPS and a raw TCP socket, already at DNS resolution ("Name or service not known"). There is no `HTTPS_PROXY` in the sandboxes. The allow-listed `api.openai.com` is reachable. Unlike the local proxy, the deployed policy is a real boundary: every external OSS system (Phase 7) must be declared in `network.egress`, and only the linked Atlas cluster is implicit. |

Plan corrections from the spike:
- **HITL resume:** the SDK/skill resumes by interrupt ID (`resume_map:
  {<interrupt-id>: {...}}` on the same session), not only the flat
  `{"decision": …}` body from the public docs. The Phase 3 shell code
  must read `suspend_context.interrupts[].id`.
- **Tool placement** is also declared in code (`@app.tool(is_local=False)`
  → tool sandbox, `is_local=True` → agent sandbox), alongside the
  `agent.yaml` lists.
- **Guardrails** need a `GUARDRAILS_SERVER_URL` locally (Phase 6).
- **Memory extraction is fast locally.** An episodic memory appeared
  within about a minute (`snapshot.stale_minutes: 1`). Its default
  extraction LLM is `gpt-5.6-luna` via `LLM_API_KEY` in
  `project-config.yaml`.
- **Reset button (Phase 5):** memories are soft-deleted (`deleted`,
  `deleted_at`, versioned via `memory_lineage_id`), and no delete-by-user
  API has been found yet.

### Phase 2 findings (CLI 0.1.119, 2026-10-14)

Built: `agent-engine/agents/{ibn-agent,dtw-agent}` (about 30 lines each)
on a shared `agent-engine/telco-runtime` package. `graph.py` is the
LangGraph port of `DomainAgent.run`: `gpt-4o` at `temperature=0`,
`parallel_tool_calls=False`, today's date in the system prompt, and a
5-iteration cap followed by a tool-less finalize node. The system prompts
come unchanged from `agents/catalog/` (packaged as `telco_catalog`). The
tools are the `oss_tools` ToolSets from Phase 1, registered with
`app.tool(is_local=False)`: 19 IBN and 25 DTW tools, with no parameter or
`required` differences from the MCP schemas. The repo-root `agent.yaml`
lists both agents, so one build archive carries `oss_tools/`,
`agents/catalog/` and `telco-runtime/` as uv path sources.

Verified locally (`dev up --all`) and **deployed**, as the service
account with `X-Mdb-Agent-Engine-Custom-Demo-Prefix: s_p2deploy01_`
against the Flex cluster:
- **IBN onboarding** goes `submit_intent → check_feasibility →
  propose_plan → activate_plan` across 5 turns on one session. IBN-001
  ends active with all SLOs green. "Show all intents" lists only the lane's
  IBN-001, not the four shared seed intents, so reads and writes both
  stay in the lane.
- **DTW Flow A** goes `create_scenario` (DTW-SCN-001) then
  `simulate_qos_change`: 9 cells over capacity (`cell_NYC_Metro_01_B`
  BLOCK) and no core elements at risk. These are the same numbers as the
  local run and the legacy runtime.

| # | Finding |
|---|---|
| 1 | **`app.llm()` calls run in the tool sandbox**, not the agent sandbox: the SDK's LLM protocol executes in the tool pod. The first local run failed with `LLM_API_KEY` missing there. So the tool sandbox holds the key and the only external egress (`api.openai.com:443`). The agent sandbox needs the key only to construct the client and runs **`deny_all`**. Deployed LLM calls work under that policy, which confirms the split. |
| 2 | **`.agentengineignore` at the archive root:** an anchored `/agents/*` plus negation `!/agents/catalog/` also dropped `agent-engine/agents/*`, and the first cloud build failed. Keep that file to plain, collision-free directory names. |
| 3 | `deploy --auto` can't be combined with `--workspace`/`--all`. Run it from each agent directory (with the monorepo root still the archive root). |
| 4 | The Atlas link is per Agent Engine **project**. New workspaces inherit the Flex cluster with no extra setup. |
| 5 | Deploy refuses a `.env` holding keys that aren't platform secrets. Keep only `LLM_API_KEY` in the agents' `.env`. |
| 6 | Trace attribute keys contain literal dots (`tool.name`, `thread.id`, `execution.id`), so queries on `mdb_store_<project>.traces` need `$getField`. |
| 7 | An expired CLI login (refresh token) kills running `deploy`/`status` watchers mid-rollout. The deployment itself carries on: `agentengine status` after re-login showed it succeeded. A fresh workspace's first rollout also waits several minutes on a cert-manager certificate (`…-aer-pool-int`). |
| 8 | **New workspaces need an Atlas-backed org.** The org Agent Engine auto-created at first login ("Benjamin Lorenz's Organization", not Atlas-backed) now refuses new workspaces; its existing three can still be redeployed. Agent Engine orgs/projects *are* Atlas orgs/projects (the CLI lists BenLorenzPersonalOrg › telco-oss-spike as a project). **Decision:** Phase 3 stays in the existing workspaces; before Phase 4 (new coordinator workspace) agents, Flex cluster, secrets and service account move together into one project of the corporate-billed Industry Solutions TelcoMedia org. |

Deviations from the Phase 2 bullets above:
- **No VERBATIM edge.** No IBN/DTW tool returns `VERBATIM`, so there was
  nothing to short-circuit.
- **No in-domain tool narrowing.** The 5-service domains bind all their
  tools, as the legacy runtime does today.
- **Memory, guardrails and A2A are off.** They come in Phases 5, 6 and 4.
- **Dashboards not yet live.** The legacy dashboards still watch the
  shared dev cluster, while the agents write to the Flex cluster. Point
  them at the Flex cluster in Phase 8, or earlier for a demo.

### Phase 3 findings (CLI 0.1.119, 2026-10-15)

Built: a `review` node between the model and the tool node in
`telco_runtime/graph.py`, plus `oss_tools/approvals.py`. The gated
actions are `activate_plan`, `apply_runbook`, `update_template_version`,
`cancel_intent`, `delete_scenario` and `delete_all_scenarios`
(`delete_scenario` was added to the plan's list because it hard-deletes
too). Per call:
1. The node calls `open_review` in the tool sandbox. It builds the context
   (intent, site, plan, violation, runbook, blast radius), writes a
   pending doc to the session's `agent_approvals` and sets a
   `pending_approval` marker on the target intents/scenarios.
2. It suspends with `interrupt()`, passing the context, `allowed_decisions`
   and a `response_schema`.
3. On resume it calls `close_review`, which records the decision and
   reviewer notes and clears the marker.
4. Reject replaces the call with a ToolMessage ("NOT executed", plus the
   notes), so the tool never runs. Approve passes the call through
   unchanged.
5. `approve_canary` (template bump, more than one site) narrows the call
   to the first site. A `canary` node then re-checks that site with
   `get_compliance` and opens a second review for the remaining sites,
   showing the canary's compliance result. The model sees one tool call
   and one combined result.

Verified deployed (r2 of both workspaces, lane `s_p3deploy01_` on the
Flex cluster, as the service account with the prefix header resent on
every resume): DTW delete-all reject keeps DTW-SCN-001. IBN onboarding
with an approved activation gives IBN-001 `active`. An injected violation
at Marienplatz, after diagnosis, raises an `apply_runbook` review
(observed 30.5 ms vs 20 ms).

Verified locally (`dev up --all`, two seeded lanes):
- **Activation:** reject leaves IBN-005 `planned`, with the decision and
  notes on APR-001 and the marker cleared. A new request plus approve
  activates it exactly once.
- **Template bump across 5 sites:** canary on Hamburg, then fleet approve:
  each intent flagged exactly once. Canary, then fleet reject: only
  Hamburg flagged.
- **Delete all scenarios:** reject keeps DTW-SCN-001.
- **Checkpoint flow:** injected violation at Berlin Mitte → `apply_runbook`
  review (observed 48.5 ms vs 40 ms) → approve → intent `active` again.
  The IBN dashboard's change stream shows `pending_approval` set, then
  cleared, then the new status.

| # | Finding |
|---|---|
| 1 | **Interrupts must be in a graph node or a local tool.** Remote tool-pod interrupts are unsupported in durable workflows, so the review is a node and its database work runs as tool calls from that node. |
| 2 | **Tools invoked from a node need a full ToolCall** (`{"type": "tool_call", "name", "args", "id"}`). Otherwise: "When tool includes an InjectedToolCallId argument, tool must always be invoked with a full model ToolCall". The id is the activity's durable identity, so it is derived from the model's tool-call id plus the step (`…:open`, `…:close`, `…:fleet:check`). |
| 3 | **Replay works as documented.** On resume the node re-runs, but `open_review` returns its recorded result (no second approval doc is created), and the canary call isn't repeated after the second suspension. The interrupt payload is built only from recorded results, so it is replay-stable. |
| 4 | **The platform enforces `response_schema`.** A decision outside the enum is refused with `409 RESUME_INPUT_INVALID` before it reaches the agent. The agent still treats unknown decisions as reject. |
| 5 | **Native `interrupt()` resumes only through `invoke` with `resume_map: {<interrupt id>: {decision, reviewer_notes}}` on the same session**, locally and deployed. The documented `POST /executions/{id}/resume` doesn't work for them: the flat body `{"decision", "reviewer_notes"}` gets `409 RESUME_INPUT_INVALID` "continue input cannot be applied to this session", and a `resume_map` body gets `400` ("Decision required"). The flat body belongs to the legacy `app.suspend` path, which durable sessions don't support. The interrupt id is `suspend_context.interrupts[].id` (the OE activity id). While suspended, other turns on the session get `409 SESSION_BUSY` with `blocking_execution_id`. Locally the UI proxy has no `/executions` routes at all. |
| 6 | **Path-dependency code changes don't reach the dev containers by default.** uv caches a directory dependency by its `pyproject.toml` only. Fixed with `[tool.uv] cache-keys` (`**/*.py`) in the three local packages. After editing them, restart the agent containers. |
| 7 | `update_template_version` was already broken for a second run in any lane: its successor policy doc goes into the *shared* `ibn_knowledge_chunks` with `insert_one`. Now an upsert, plus the optional `intent_ids` for the staged rollout. Its MCP schema gains only that optional field. |
| 8 | Pre-existing bug, found on the deployed checkpoint run: `apply_runbook` restored latency to `max(0.7 × target, 22 ms)`, so the 20 ms Marienplatz intent stayed violated (22 ms) after "Compliance restored". It now restores to 0.7 × target, which is unchanged for targets ≥ ~31 ms. |

Deviations:
- **No shell approval card yet.** The web shell doesn't invoke Agent
  Engine before the cut-over (Phase 8), so reviews happen in Agent Engine
  (Pending Reviews page, interactive CLI, or API). The card and its
  resume call move to Phase 8. That call must be `invoke` + `resume_map`
  on the same session, resending the custom headers (finding 5), not
  `/resume`.
- **`allowed_decisions` sits inside the interrupt value**, because that's
  where the SDK puts `suspend_context.interrupts[].value`. Whether the
  Pending Reviews page reads it there is checked on the deployed run.

### Spike resources to clean up

- Atlas (BenLorenzPersonalOrg → project `telco-oss-spike`): Flex cluster
  `telco-oss-spike` (billed hourly), the database user and Voyage API key
  created by `agentengine atlas setup`, the IP access entries
  `44.214.209.237/32` and `52.44.27.64/32` (Agent Engine) plus the
  laptop IP added for seeding.
- Agent Engine (personal org → Default Project): workspaces
  `ws-6acfac0cea67bce171582ba8` (spike), `ws-6acfc3acea67bce171582e86`
  (ibn-agent), `ws-6acfc3ad6189a50fdc381424` (dtw-agent), service account `spike-shell`
  (`ae_sa_id_6acfb816b2980dbbd0c2e809`), project secrets `LLM_API_KEY`,
  `MONGODB_URI`, `VOYAGE_API_KEY`, and workspace secret `DEMO_MONGODB_URI`.

## Risks

- **Preview churn.** APIs and the CLI change without migration paths. Pin
  the CLI, keep the `RUNTIME=legacy` fallback until GA, and never demo
  without it.
- **Latency.** A2A hops plus sandbox cold start on a fresh session.
  Mitigation: pre-warm with a no-op invoke when the shell session opens,
  and keep single-domain turns on a coordinator fast path (classify →
  one A2A call, no synthesis).
- **Capacity.** A static pool and 0.5 vCPU per agent mean a conference
  booth with 30 laptops needs `replicas ≥ 30` on each agent; the 512 cap
  is project-wide.
- **Fidelity regressions.** Moving from raw OpenAI to LangChain
  `ChatOpenAI` changes tool-call formatting subtly. Re-run both opener
  flows and the follow-up/stickiness cases (`2b45b41`, `2bff3a1`).
- **Memory semantics.** Platform extraction is asynchronous (available on
  the next conversation, not the next turn). The "remember X → recall
  next turn" demo must read STM, not LTM.

## Sequencing & effort

| Phase | Effort | Demo-safe checkpoint |
|---|---|---|
| 0 — spike & setup | ½–1 d | one traced Telco tool call |
| 1 — `oss_tools/` extraction | 1–2 d | legacy demo identical |
| 2 — domain agents | 2–3 d | IBN + DTW flows on Agent Engine via CLI |
| 3 — HITL approvals | 1–2 d | approve-in-browser closes the loop |
| 4 — coordinator + A2A | 2 d | cross-domain fan-out, nested trace |
| 5 — memory | 2 d | cross-session recall on platform memory |
| 6 — governance & feed | 1–2 d | `Policy denied` + IMSI redaction live |
| 7 — TMF / remote MCP | 2–3 d (stretch) | TMF921 connector tool call |
| 8 — shell & cut-over | 1–2 d | browser demo on Agent Engine, legacy flag works |

≈ 13–19 working days. Every phase merges independently, and the legacy
runtime stays runnable throughout.
