# CHANGES.md

## 2026-08-05

### Online help page + pinned demo start queries in the web shell

Two additions to make the shell self-explanatory for a presenter (or a
visitor driving it alone):

- **Pinned cursor-up history.** The two canonical demo openers (the IBN
  Alpenmarkt intent and the DTW ACME M QoS uplift) are hardcoded in
  `web/shell.html` as `PINNED_HISTORY` and pushed to the front of the
  input history — one `↑` for IBN, two for DTW — both at page load and
  after the stored per-session history arrives (de-duped). Clicking
  **Reset demo data** also resets the cursor-up buffer to just those two;
  the server already wipes the prefixed `agent_history` in the same pass
  (`seed_runner.SESSION_STATE_BASES`), the client was only holding a
  stale copy.
- **"❔ Help with this demo"** — a fourth banner control next to reset +
  the two dashboard links, opening the new `web/help.html` (served by
  `GET /help` in `web/shell.py`) in a second tab. It walks both flows
  step by step with click-to-copy prompts — IBN: intent → feasibility
  check → propose and activate → inject morning rush → diagnose
  violation → apply runbook; DTW: scenario → *change to 20 Mbps* → run
  simulation — annotating each step with what happens and which Atlas
  primitive is the point (hybrid `$vectorSearch` for diagnose,
  `$graphLookup` + vector for simulate). Also covers interleaving the two
  demos (independent domains/agents/collections/workstreams, with the
  caveat that short follow-ups resolve against the previous turn, so
  prefix them after a switch) and the header controls. Its footer button
  hands focus back to the opener tab and closes itself (the shell link
  carries `rel="opener"` for that) rather than linking to `/`, which would
  have opened a second shell; if the browser refuses to let the tab close
  itself, it falls back to a ⌘W/Ctrl+W hint.

## 2026-06-26

### Remove etc/nginx.conf

Deleted the checked-in nginx config (it was a full personal server
config, not demo material). The CLAUDE.md deployment note is reworded to
generic reverse-proxy guidance — the app-side sub-path support (WS
`wss://`, per-dashboard prefix derivation, `DEMO_BIND_HOST`) is
unchanged, so any proxy fronting `/`, `/ibn/`, `/dtw/` → 8070/8060/8080
works without app config.

### Remove the author's personal domain (broadcast relay made opt-in)

The demo no longer hardcodes any personal domain:

- **Broadcast relay is now opt-in via env.** `agents/broadcast.py` reads
  `NOTIFY_BROADCAST_URL` / `NOTIFY_RECEIVE_URL` (both empty by default);
  when unset, the external POST is skipped entirely — the in-browser
  Agent Log (local_broadcast) is unaffected. `main.py` only prints the
  live-feed hint when a relay is configured. (Previously hardcoded to a
  personal notify endpoint.)
- Remaining occurrences (the watchOS companion app URL and docs) are
  genericised to `example.com` placeholders; CLAUDE.md and README
  describe the opt-in `NOTIFY_*` relay instead of a fixed URL. The
  checked-in nginx config that also carried the domain was removed
  outright (see above).

Left untouched: author emails and local filesystem paths (neither is a
domain).

### Remove HTTP Basic Auth from the web apps

Dropped the shared-credential gate entirely: deleted `web/auth.py` and
removed the `install_basic_auth(...)` calls + imports from the shell and
both dashboards (now serve unauthenticated). Cleaned up the
`SHELL_AUTH_*` / login handling in `bin/start.sh` + `bin/_common.sh`, the
`web/auth.py` mention in `etc/nginx.conf`, and the auth notes in
CLAUDE.md. Per-browser-session data isolation is unchanged — it never
depended on the gate. If access control is wanted later, put it at the
reverse proxy (nginx `auth_basic`) rather than in the apps.

### Proper start/stop/restart lifecycle scripts in `bin/` (replaces `start_demo.sh`)

`start_demo.sh` (foreground, Ctrl-C only) is replaced by three daemon
scripts that manage all the web-server processes:

- `bin/start.sh` — starts the shell (:8070) + IBN (:8060) + DTW (:8080)
  dashboards DETACHED (`nohup`), writes PIDs to `run/`, logs to `logs/`.
  Idempotent (skips already-running services) and fails fast with the
  tail of the log if one dies during startup.
- `bin/stop.sh` — stops all three via pidfile (falling back to the exact
  script path), SIGTERM-then-SIGKILL with a grace window, AND sweeps any
  orphaned MCP server subprocesses (`uv run <repo>/mcp_servers/*.py`,
  scoped strictly to this repo) left by a crash.
- `bin/restart.sh` — composes stop then start.
- `bin/_common.sh` — shared config (repo root, the service table, pid
  helpers); sourced by the others.

All portable bash (works on NetBSD's pkgsrc bash). `run/` is gitignored.
Env: `PYTHON` (venv python), `DEMO_LOG_DIR`, `DEMO_RUN_DIR`, and the
usual `DEMO_BIND_HOST`/`SHELL_AUTH_*` pass through. Mechanism verified
(pidfile tracking, idempotent skip, graceful + forced kill, stale-pidfile
handling).

### Make ALL user/session data per-session and reset-clearable (`agents/`, `mcp_servers/preferences_service.py`, `mcp_servers/analytics_service.py`, `web/`)

Closes the gaps where session data was still global and survived a
reset. Previously only the 5 demo collections + workstreams + memories
were per-session; `user_preferences`, `agent_conversations`,
`routing_decisions`, and `agent_history` were SHARED and untouched by
the Reset button — so e.g. a "remember I'm vegetarian" preference leaked
across users and outlived a reset.

Now everything user/session-specific is prefixed `s_<token>_` and wiped
on reset:
- `user_preferences` — orchestrator `self.preferences`,
  `preferences_service` (DEMO_PREFIX-aware; legacy-collection migration
  guarded to the default lane), and the shell's preferences view.
- `routing_decisions` — orchestrator + `analytics_service` (reads its
  own session's analytics).
- `agent_conversations` — the agent-to-agent consult log.
- `agent_history` — per-web-session cursor-up recall
  (`history.py` gains an optional `prefix`); the terminal CLI keeps the
  shared/default history (cross-shell recall preserved).

`web/seed_runner.py` gains `SESSION_STATE_BASES` (workstreams, memories,
preferences, conversations, routing_decisions, history) as the single
source of truth; reset `delete_many`-clears them (no drop → live
change-stream watchers undisturbed) and the idle-reaper drops them.
Still shared by design (NOT user data): the service/agent catalogue
(`mcp_services`, `agent_cards`) and the read-only reference fixtures +
their vector indexes.

Verified on live Atlas: preferences/analytics services resolve to the
prefixed collections; a reset wipes a stray intent + every state plane
(preferences, conversations, routing_decisions, workstreams, memories,
history) back to empty/seed; per-session history isolates from the
default lane and clears on reset.

### Fix: web shell pegged at 100% CPU on NetBSD + reset/session not a clean slate (`agents/registry.py`, `agents/workstreams.py`, `web/seed_runner.py`)

Three linked bugs surfaced running the demo on NetBSD 10.1:

1. **`web/shell.py` busy-looped at ~98% CPU.** `_watch_servers` uses
   `watchfiles.awatch`, whose native backend (Rust `notify`) has no
   NetBSD support and spins there. Only the bootstrap orchestrator runs
   it, so only the shell process was affected. Worse, a coroutine
   spinning at 100% **starves the asyncio event loop**, making WS/session
   handling sluggish and erratic — the visible "goes crazy". Fix: force
   `awatch` into polling mode (`force_polling=True, poll_delay_ms=1000`,
   ~0 CPU on every platform), guard the loop against exceptions, and add
   `DEMO_DISABLE_FILE_WATCH=1` to turn hot-reload off entirely.

2. **A new workstream's summary read as "already done", so the agent
   skipped the tool.** It was seeded `"Started: <your request>"`; the
   IBN agent read that as the intent already being submitted and replied
   "this intent has already been submitted…" WITHOUT ever calling
   `submit_intent` (observed: zero servers activated, no write). Reseeded
   as `"New workstream — no actions executed yet. Original request: …"`
   so the agent acts instead of assuming.

3. **Reset wasn't a clean slate — it left `agent_workstreams` /
   `agent_memories` behind.** `reset_session` only wiped the 5 demo
   collections, so the stale workstream (with its "submitted" summary)
   survived and kept interfering with re-runs ("old artefacts from
   earlier runs"). Reset now also clears the session's workstreams +
   memories (via `delete_many`, so the live change-stream watcher isn't
   disturbed). Sticky-resume on reconnect is unaffected — only an
   explicit reset wipes them.

Verified on live Atlas: forced-polling watcher consumes ~0.008s CPU over
2.5s idle (was a full core); a fresh session's first "open a new store"
turn now calls `submit_intent` (creates IBN-005); reset restores the
seed fixtures AND drops the workstream; the redo cleanly reuses IBN-005
with no interference.

## 2026-06-19

### Run behind nginx at agentic.example.com (`etc/nginx.conf`, `web/shell.html`, `web/ibn.html`, `web/dtw.html`, `web/*_dashboard.py`)

One nginx host fronts all three demo apps via path routing: the web
shell at `/`, the IBN dashboard at `/ibn/`, the DTW dashboard at
`/dtw/` (each proxied to its uvicorn port 8070/8060/8080). The
dashboard `proxy_pass` carries a trailing slash to strip the mount
prefix, WebSocket upgrade headers are set on every location, and
read/send timeouts are 86400s so idle live-feed sockets survive.

**Cert prerequisite (verified):** the `example.com` cert is NOT a wildcard
— the live cert covers only `example.com` + `notify.example.com`. It must be
reissued to add `agentic.example.com` as a SAN (e.g. dehydrated
`domains.txt`: `example.com notify.example.com agentic.example.com`, then re-run
dehydrated; the renewed cert stays in the same `example.com/` dir, so the
nginx path is unchanged) before the 443 block validates.

App adaptations for serving under a reverse proxy / sub-path:
- The shell WebSocket now uses `wss://` when the page is `https://`
  (the hardcoded `ws://` would be blocked as mixed content behind TLS).
- The dashboards derive a mount `PREFIX` from `location.pathname` and
  prepend it to their `/ws` and `/snapshot` URLs, so those hit the
  right app under `/ibn` or `/dtw` (empty prefix at the origin root, so
  direct-port dev is unchanged).
- The shell's dashboard links are path-based (`/ibn/`, `/dtw/`) behind
  the proxy and fall back to sibling ports (`:8060`, `:8080`) when the
  shell is hit directly on `:8070` in dev.
- All three apps now share the Basic-Auth realm `Agentic AI Demo`, so
  the single agentic.example.com origin prompts for the login only once.
- `DEMO_BIND_HOST` env (default `0.0.0.0`) lets the deploy bind the
  uvicorn ports to `127.0.0.1` so they're only reachable through nginx
  (and the auth gate can't be bypassed by hitting a port directly).

### Perf: lazy MCP server activation — a turn only spawns the servers it uses (`agents/mcp_pool.py`, `agents/domain_agent.py`)

A domain agent used to activate every server in its domain up front
(all 5 IBN servers for a one-line "submit intent", etc.) just to present
their tool schemas to the LLM — wasteful subprocesses. Now:

- Tool schemas are cached process-wide (`_TOOL_SCHEMA_CACHE`).
  `tool_schemas_for(name)` returns the cache, or harvests it by spawning
  the server TRANSIENTLY (spawn → list_tools → shut down) the first time
  it's seen in the process. So the LLM is still shown the whole domain's
  toolset (it can plan any step), without keeping those subprocesses
  alive.
- `ensure_active(name)` spawns a server into the persistent pool only
  when one of its tools is actually called. The ReAct loop calls it right
  before each tool invocation.
- Net: a one-step turn keeps exactly one server alive instead of the
  whole domain; multi-step flows spawn each server once, on first use,
  and reuse it. Verified — the "open a new store" intent submission
  leaves only `ibn_intent_service` running (was all 5).

`DomainAgent._activate_domain_servers` / `_tools_for` are removed.
Multi-agent (cross-domain) dispatch still pre-activates eagerly in the
parent task — required by anyio (child-task agents must not enter stdio
scopes the shutdown task will close); single-agent turns, the common
case, are now lazy.

### Fix: Basic Auth gate rejected the WebSocket (web shell wouldn't connect) (`web/auth.py`)

The auth gate was gating the websocket scope too, but browsers don't
replay cached Basic-Auth credentials on the WS handshake (Chrome sends
the upgrade with no Authorization header), so every `/ws` connection got
a `403` and the shell sat at "connecting… / Disconnected — reconnecting".
`BasicAuthMiddleware` now gates **only the http scope** and passes the
websocket through — the WS is reachable only from the already-gated page,
so the doorkeeper still holds for normal browser use. Verified: HTTP
401/200 unchanged; WS connects.

### Global HTTP Basic Auth gate + one-shot launcher (`web/auth.py`, `web/shell.py`, `web/ibn_dashboard.py`, `web/dtw_dashboard.py`, `start_demo.sh`)

A single shared credential now protects all three browser apps — the
web shell AND both dashboards — so the public URLs aren't wide open.
`web/auth.py` holds a pure-ASGI `BasicAuthMiddleware` (+ an
`install_basic_auth(app, realm)` helper) covering BOTH the HTML page and
the WebSocket; browsers replay cached Basic-Auth creds on same-origin WS
upgrades, so one prompt covers everything. Default `mdb` /
`mdbagentic2026`, overridable via `SHELL_AUTH_USER` / `SHELL_AUTH_PASS`;
`SHELL_AUTH_DISABLE=1` turns it off for local dev. Constant-time compare
(`hmac.compare_digest`); ASCII-only realm strings (header-safe). A demo
doorkeeper, not per-user auth — orthogonal to the Phase-B per-session
isolation (still keyed off the `localStorage` session token). The
terminal CLI is unaffected. (The dashboards live on separate ports /
origins, so the browser prompts once per dashboard the first time it's
opened, with the same credential.)

`start_demo.sh` launches all three (shell :8070, IBN dashboard :8060,
DTW dashboard :8080) with logs under `./logs/`, fails fast if any
service dies during startup, prints the URLs + login, and stops all
three on Ctrl-C.

Verified: HTTP 401 + `WWW-Authenticate: Basic` with no/bad credentials
and 200 with the right ones on the shell and both dashboards; the
WebSocket upgrade is rejected without credentials and connects with
them.

### Web shell: per-browser-session isolation (Phase B of MULTI_SESSION_PLAN.md) (`web/shell.py`, `web/seed_runner.py`, `agents/`, `mcp_servers/`)

Each browser session now gets its own isolated demo data, so concurrent
users — and their Reset — never touch each other. No login.

- **Per-session collection prefix.** The 5 mutable demo collections
  (`ibn_intents`, `ibn_telemetry`, `ibn_compliance_events`,
  `ibn_policy_snapshots`, `dtw_scenarios`) plus `agent_workstreams` and
  `agent_memories` are namespaced `s_<token>_<base>`. Read-only reference
  data, `mcp_services`/`agent_cards`/`routing_decisions`/
  `user_preferences`, and **both `*_knowledge_chunks` collections with
  their Atlas vector indexes** stay shared — so per-session cost is a
  handful of small collections, not duplicate vector indexes. Chosen over
  per-session *databases* because the Atlas credential can drop
  collections but not databases (Phase-A finding).
- **MCP servers** (the 6 mutating ones) resolve mutable collections via
  `db[os.environ.get("DEMO_PREFIX","") + base]`; reference collections
  stay bare. Empty prefix = byte-identical to before, so the CLI /
  terminal shell are unchanged. `McpPoolMixin._activate_servers` passes
  the orchestrator's `demo_prefix` to each child via `DEMO_PREFIX`.
- **OrchestratorAgent** gains `demo_prefix` + `shared_bootstrap` args.
  `shared_bootstrap=False` skips the one-time global work (registry sync,
  agent-card sync, filesystem watcher); `demo_prefix` namespaces its
  workstream/memory collections and is handed to child servers.
- **Web shell** keeps a `{token: Session}` map (each Session = its own
  orchestrator + lock + connected tabs), capped `DEMO_MAX_SESSIONS`
  (default 6) and reaped after `DEMO_SESSION_TTL_SEC` (default 1800s)
  idle. A shared bootstrap orchestrator does global init. The browser
  stores its token in `localStorage` and sends it in an `init` handshake
  so refresh/reconnect resumes the same lane. Per-session lock replaces
  the global query lock.
- **`seed_runner`** gains `reset_session(prefix)` (drop + re-seed only a
  session's mutable collections) and `ensure_session_seeded(prefix)`
  (seed a lane on first use). Reset is now scoped to the calling session.
- The global workstream change-stream watcher was removed (couldn't be
  session-scoped); the server pings `workstream_update` to the
  originating tab after each turn instead.

Verified on live Atlas (LLM-free isolation test + a live per-session
query): two lanes seed independently (4 intents each, shared `ibn_sites`
untouched); diverging or resetting one leaves the other intact; a server
launched with `DEMO_PREFIX` reads its own lane; a per-session
orchestrator boots with prefixed collections and its data stays
independent of the shared lane.

Notes: fresh deployments still run the CLI seeders once to create the
shared reference data + vector indexes (per-session lanes only copy the
mutable set).

### Dashboards: session-aware (`web/ibn_dashboard.py`, `web/dtw_dashboard.py`, `web/ibn.html`, `web/dtw.html`, `web/shell.html`)

The live dashboards now mirror the exact per-session lane the user is
driving, instead of only the shared default lane. Each dashboard is
refactored to a per-session model keyed by collection prefix: a browser
opens it with `?session=<token>`, the server resolves the prefix, and
lazily starts that session's own watcher set (intents / compliance /
plans change streams + telemetry poller + live telemetry writer for
IBN; scenarios change stream for DTW), broadcasting only to that
session's tabs. Watcher sets are idle-reaped 120s after the last tab
leaves so abandoned sessions don't leak change streams. Reference data
(`ibn_sites`, `dtw_markets`) still resolves from shared collections; no
/ invalid token → the shared default lane (backward compatible).

The session token crosses the port boundary (shell :8070 → dashboards
:8060/:8080, separate origins so localStorage can't) via the URL: the
shell banner now shows **📊 IBN dashboard** / **📊 DTW dashboard** links
pointing at `http://<host>:8060|8080/?session=<token>`. The dashboard
HTML forwards `?session=` on its WebSocket and `/snapshot` requests.

Verified on live Atlas: two dashboard lanes built from prefixed
collections are isolated (session A's cancelled intent shows only in
A's snapshot; B unaffected), while shared reference data resolves for
both.

### Web shell: browser-driven demo reset (Phase A of MULTI_SESSION_PLAN.md) (`web/shell.py`, `web/seed_runner.py`, `web/shell.html`)

A **Reset demo data** button in the web shell banner re-runs the
`seed/ibn_seed.py --reset` + `seed/dtw_seed.py --reset` pipeline from the
browser, with live progress streamed to the Agent Log — so anyone can
get a clean demo without shell access.

- `web/seed_runner.py` — new. Drives the seeders' existing phase
  functions (`reset`/`ensure_indexes`/`insert_all`/… each takes a `db`)
  against a given `db_name`, redirecting their `print()` output
  line-by-line to an async `emit` callback. The blocking work runs in a
  worker thread; lines cross back to the event loop via
  `loop.call_soon_threadsafe` → `asyncio.Queue` and stream to the
  WebSocket as they're produced.
- `web/shell.py` — `reset_demo` WS message, run under the existing
  `_query_lock` so a reset can't interleave with a query mid-tool-call;
  returns a fast "busy" message if the lock is held. On success the
  agent's in-memory turn context (conversation tail, current workstream,
  sticky domain/service) is cleared so the next query starts clean. Each
  connection now also issues a `session_token` (echoed in `hello`), and
  the reset routes through `_demo_db_for(session_token)` → the shared
  `agent_registry` for now. These two are the Phase-B seams.
- `web/shell.html` — banner button, a destructive-action confirm modal,
  a `SEED`-tagged live progress stream, and a completion panel. The
  modal/button lock out while a reset is in flight.

Phase-A is single-user-safe (the process-wide `_query_lock` serialises
everything); concurrent users still share one dataset, which Phase B
fixes.

**Phase-B finding, measured during testing** (recorded in
MULTI_SESSION_PLAN.md): the seed pipeline was validated against a
throwaway database `agent_registry__phaseA_smoketest` (passing a
non-default `db_name` — which also exercises the Phase-B seam). All 15
collections seeded correctly and 38 progress lines streamed. Cleanup
revealed the Atlas credential can CRUD and drop *collections* but cannot
`dropDatabase` on another database — so Phase-B session isolation must
use per-session **collection prefixes within `agent_registry`**, not
per-session databases. The real `agent_registry` demo data was never
touched by the test.

## 2026-06-11

### Market resolution: 'LA' resolved to Dallas_Metro (`dtw_scenario_service`, `dtw_topology_service`)

Live-demo find after Phase 4: "raise ACME M downlink in NYC and LA" was
created with scope `[NYC_Metro, Dallas_Metro]` — the simulation then
faithfully reported Dallas bottlenecks. Root cause: both market
resolvers ran an UNANCHORED case-insensitive name regex before id
matching, and 'LA' substring-matches 'Da-LLA-s-Fort Worth' while
'Los Angeles Metro' doesn't even contain the substring 'la'. The bug
was latent pre-Phase 4 because the old parse LLM was handed the
known-market list and returned canonical ids itself; once the agent
started passing city hints, the resolver became the deciding factor.

Fix in both `_resolve_market_id` (scenario service) and
`_resolve_market` (topology service): id matching (exact, then prefix —
'LA' → 'LA_Metro') runs BEFORE any name matching, and the name regex is
anchored at a word boundary with the hint escaped ('New York' →
NYC_Metro, 'Fort Worth' → Dallas_Metro). The DTW agent prompt now also
suggests passing canonical ids when known. Verified with an 8-case
resolution matrix across both services.

Note for existing data: scenarios created while the bug was live (e.g.
DTW-SCN-002 from the 2026-06-11 demo session) retain Dallas_Metro in
their stored scope — amend conversationally ("change markets to NYC and
LA") and re-run, or delete and recreate.

### Phase 4 of the multi-agent refactor — thin the MCP servers; AGENT_MODE default flipped (`mcp_servers/`, `agents/`)

The three embedded OpenAI calls moved up into the domain agents. The
rule is now enforced for the IBN/DTW domains: **servers contain zero
`openai` imports** — reasoning lives in agents, execution lives in
servers. (The remaining `openai` imports under `mcp_servers/` are
`acc_proof_point_service`, `portfolio_service`, `preferences_service` —
other domains, candidates for when they get agents.)

- **`ibn_intent_service.submit_intent`** now takes structured fields
  (`raw_text`, `site_name`, `services`, `pos_latency_ms`, …, `deadline`
  as ISO). The agent's own LLM performs extraction as part of
  tool-argument generation — no separate parse call exists anywhere.
  Deviation from the plan sketch: no deprecated text wrapper — the only
  callers are LLMs reading live schemas, so the signature changed in
  place.
- **`dtw_scenario_service`** — `create_scenario` and `update_scenario`
  take structured fields; the amendment LLM is gone (the agent computes
  updated values itself, with conversation context — strictly better
  than the old blind re-parse). Hint resolution stays server-side as a
  data operation: new `_resolve_qos_hint` accepts ids, names, and
  numeric rates; a rate with no exact profile maps to the nearest one
  with an explicit substitution note (verified: '47 Mbps' →
  `qos_postpaid_standard` 50 Mbps + warning). update_scenario semantics:
  each provided field REPLACES the stored value wholesale.
- **`dtw_simulation_service`** — `_create_scenario_inline` and the
  `text=` fallback params are gone; simulation never creates scenarios.
- **Agents** — `DomainAgent.run` injects today's date into the system
  prompt (relative-deadline resolution moved agent-side; verified: 'by
  tomorrow 18:00' → 2026-06-12T18:00). IBN/DTW catalog prompts gain
  extraction guidance sections.
- **AGENT_MODE default flipped to `all`** (the deferred Phase 2
  post-soak cleanup): agents are on by default;
  `AGENT_MODE=off|0|legacy` opts out to legacy routing.
- **Workstream-context fix** — testing surfaced that the entity-reuse
  guidance in the workstream block made the agent reuse the existing
  IBN-005 intent (running check_feasibility on it) instead of
  submitting a new one for a "I'm opening a new store at X" request.
  Both copies of the block (react.py legacy + dispatch.py) gain a
  CRITICAL rule: a NEW intent description always goes through
  submit_intent, never reuses an intent ID from context. ⚠ During that
  test the live IBN-005 document was accidentally deleted; it was
  reconstructed from `PLAN-IBN-005-20260602172308` + the workstream
  audit trail (status active, runbook history preserved, an explicit
  'restored' history entry added).

Verified on live Atlas: e2e IBN submission (new IBN-006, site resolved
to site-ham-alt, targets 35ms/99.9%/strict, deadline resolved, test doc
removed afterwards), e2e DTW scenario creation ('7.2 to 19 Mbps in NYC
Saturday evening' → correct change_set/scope), substitution-note unit
test, and `py_compile` across all touched files.

### Phase 3 of the multi-agent refactor — agent-to-agent consultation (`agents/domain_agent.py`, `agents/dispatch.py`)

Domain agents can now ask each other questions mid-turn:

- **`consult_agent` tool** — injected into a DomainAgent's toolset only
  at depth 0 (a user turn) when other agents are registered. It is not
  an MCP tool: the run loop special-cases it before the `srv__tool`
  split and hands it to the shell. The matching prompt guidance
  (`CONSULT_GUIDANCE`) is appended to the system prompt only when the
  tool is actually offered, so consulted agents never see instructions
  for a tool they lack.
- **Shell mediation** (`_consult_agent`) — resolves the target by name
  or domain, runs it at `depth=1` (no consult tool → recursion is
  structurally impossible) with `max_iterations=3` (the single-turn
  budget) and a bare `AgentContext` — the question must be
  self-contained. The asking agent has a `CONSULT_BUDGET` of 2 per
  turn; exhaustion returns an instructive error instead of failing.
- **Audit trail** — every exchange is persisted to
  `agent_registry.agent_conversations` `{ts, workstream_id, from_agent,
  to_agent, question, answer, status, tool_calls, services_used,
  duration_ms}`, indexed by recency and workstream — Change-Stream-able
  for a future dashboard panel. Consultations are deliberately NOT
  attached to the workstream tool-call trail (that would pollute
  service-level stickiness); `agent_conversations` is their home.
  Analytics gain `outcome.agent_consults` (single dispatch) and
  per-agent `consults` (multi dispatch).
- **anyio coverage** — multi-dispatch now pre-activates ALL registered
  agents' servers (not just the active ones) because any agent can be
  consulted from a gather child task.
- **DTW demo beat** — the DTW prompt gains a cross-domain check: after
  simulation results, when the user asked about overall operational
  risk, it may consult `ibn_agent` once for active retail compliance
  violations and cite the answer.

Verified on live Atlas: direct consult (dtw_agent → ibn_agent, depth-1
run used `ibn_assurance_service`, answer attributed "Per the IBN
agent…", conversation doc persisted with timings) and full-pipeline
e2e with `AGENT_MODE=dtw` — the turn routed to dtw_agent alone, which
listed scenarios with its own tool and consulted ibn_agent mid-turn
(`outcome: tool_calls 1, consults 1`).

### Phase 2 of the multi-agent refactor — coordinator shell, card-ranked selection, parallel dispatch + synthesis (`agents/dispatch.py`, `agents/domain_agent.py`)

The shell is now a coordinator over DomainAgents:

- **Agent selection via agent cards** — `_select_agents_for_turn`
  resolves Stage 1's domain verdict to agents. When two or more
  agent-enabled domains are in scope, candidates are ranked by
  `$vectorSearch` over `agent_cards.description` (with `domain` filter) —
  agent discovery is itself an Atlas vector search, recorded in the
  routing-decision under `agent_cards.ranked` with real scores.
  Precedence: multi-domain fan-out beats workstream continuity beats
  single Stage 1 domain (cross-domain questions are inherently
  cross-workstream). `_select_domain_agent` (Phase 1) is removed.
- **Stage 2 moved inside the agent** — `DomainAgent._select_servers`
  narrows large domains per-task via `_semantic_search` pre-filtered to
  the agent's domain; domains at or under `MAX_SERVERS_PER_TASK` (5 —
  today's IBN/DTW) activate everything. The shell no longer runs Stage 2
  for agent turns.
- **Parallel multi-domain dispatch** — `_dispatch_multi`: gpt-4o-mini
  splits the request into per-agent sub-tasks (failure degrades to
  every agent getting the full query), agents run concurrently via
  `asyncio.gather` sharing one context build, and a gpt-4o synthesis
  pass combines the answers (skipped — labelled sections instead — when
  any agent returned VERBATIM content). Analytics gain
  `dispatched_agents`, per-agent `outcome.agents.{status, subtask,
  tool_calls, services_used}`, `multi.subtasks`, and `synthesis_ms`.
- **anyio task-affinity fix** — MCP stdio context managers entered on
  the shared `AsyncExitStack` must be entered in the task that later
  closes the stack. Multi-dispatch therefore pre-activates every
  agent's servers from the dispatcher's task before `gather()`; agents
  find the sessions present and skip activation. Without this,
  shutdown crashed with "Attempted to exit cancel scope in a different
  task than it was entered in".

The legacy direct-to-server path is NOT deleted — it remains the
shell's own tool surface for the 15 domains without an agent, exactly
as the target architecture sketches ("singletons stay direct tools of
the shell"). `AGENT_MODE` default stays off pending soak; flipping the
default is the post-soak step.

Verified on live Atlas: cross-domain turn ("IBN fleet compliance + DTW
scenarios") with Stage 1 [ibn, dtw], card ranking (dtw 0.685 / ibn
0.629), correct per-domain sub-tasks, both agents concurrent (1 tool
call each), coherent synthesis, clean shutdown; single-dispatch and
legacy-fallthrough regressions pass.

### Phase 1 of the multi-agent refactor — DomainAgent + agent cards (`agents/domain_agent.py`, `agents/catalog/`, `agents/dispatch.py`)

First step from *one orchestrator → N MCP servers* toward *shell → domain
agents → thin MCP servers*. New pieces:

- **`DomainAgent`** (`agents/domain_agent.py`) — a domain-scoped
  specialist with its own system prompt and its own ReAct loop over only
  its domain's MCP tools. Loop semantics deliberately mirror the legacy
  loop (5/8 iterations, `parallel_tool_calls=False`, VERBATIM
  short-circuit, forced final answer). Context (workstream block,
  recalled memories, preferences, replay recipe) is prepared by the shell
  and injected via `AgentContext`; the tool-call audit flows back via
  `AgentResult`.
- **Agent catalog** (`agents/catalog/{base,ibn,dtw}.py`) — declarative
  specs `{name, domain, description, system_prompt}`. The per-service
  "use this when…" docstring guidance is folded into each agent's prompt;
  the DTW prompt encodes the scenario-before-simulation discipline.
- **Agent cards in Atlas** (`agents/dispatch.py`) — every catalog agent
  is published to `agent_registry.agent_cards` `{_id, description,
  domain, tools_claimed, last_seen}`; the `agent_cards_index` (autoEmbed
  voyage-4 on `description`, `domain` filter, quantization float) is
  created programmatically. Agent discovery becomes an Atlas vector
  search — used by the Phase 2 coordinator; Phase 1 selects by exact
  domain match.
- **Dispatch** (`AgentDispatchMixin`) — gated by the `AGENT_MODE` env
  flag (unset → legacy only; `1` → IBN agent; `all` → every catalog
  agent; or a domain list `ibn,dtw`). Conservative selection: dispatch
  only when the domain is unambiguous (`ws_domain` from the workstream
  classifier, or a single Stage 1 domain). The dispatcher mirrors all
  legacy post-processing: per-call workstream attachment + meta-tool
  filtering, stickiness, conversation history, background summary task,
  and the routing-decision record (now with `dispatched_agent`,
  `outcome.agent_services_used`, `outcome.agent_status`).
- **Concurrency prerequisite** — `McpPoolMixin._call_tool_locked` adds
  lazy per-session `asyncio.Lock`s so two agents can share the stdio
  session pool; `_resolve_server_paths` extracts the path-resolution
  logic for reuse. New `DISPATCH` broadcast tag (bright magenta) shows
  the hand-off in the live feed; agent-internal lines are prefixed
  `[ibn_agent]`.

Verified: cards + vector index created on live Atlas; `AGENT_MODE`
unset bootstraps with dispatch disabled; `AGENT_MODE=1` end-to-end IBN
turn dispatches to `ibn_agent` (1 tool call via `ibn_assurance_service`,
attached to the open IBN workstream, analytics record correct). A fresh-
session "show all intents" fell through to legacy by design — Stage 1
classified it billing/customer/todo, a pre-existing taxonomy ambiguity
unrelated to dispatch.

### Phase 0 of the multi-agent refactor — decompose the orchestrator monolith (`agents/`)

`agents/orchestrator.py` (3,395 lines, six concerns in one class) is split
into mixin modules with **byte-identical method bodies** — verified by
extracting every member block from `HEAD` and asserting exact-substring
presence in the new files. Zero behavior change; this is the prerequisite
for the multi-agent architecture (see `MULTI_AGENT_PLAN.md`).

New layout: `broadcast.py` (ANSI palette + live-feed POST), `registry.py`
(service discovery / hash sync), `router.py` (two-stage routing + the
routing-decision analytics helpers), `memory.py` (extract / recall /
promote / decay + knobs), `workstreams.py` (short-term working memory),
`mcp_pool.py` (stdio session pool), `react.py` (`_SYSTEM_PROMPT` + the
ReAct loop). `orchestrator.py` remains the composition root —
`OrchestratorAgent` now inherits the seven mixins and keeps only
`__init__`, the context-manager lifecycle, and `process_query`.

The only authored change is the ReAct seam: the tool-collection + context
assembly + tool-iteration section of `process_query` became
`ReactMixin._run_react`, which returns
`{answer, verbatim, iteration, max_iterations, tool_calls_count}`. The
VERBATIM short-circuit persists its routing-decision record inside
`_run_react` (as before) and signals `verbatim=True` so `process_query`
returns immediately, skipping history/summary/persist — identical control
flow to the inline original.

Public surface unchanged: `main.py` and `web/shell.py` keep importing
`OrchestratorAgent` / `BROADCAST_RECEIVE_URL` from `agents.orchestrator`,
which re-exports all former module-level constants for compatibility.

Verified: `py_compile` on all modules; full bootstrap against live Atlas
(26 services synced, 17 domains, open workstream resumed); one end-to-end
query through routing → activation → ReAct → analytics
(`outcome.tool_calls_count=1, iterations_used=2` persisted correctly).

## 2026-05-27

### Asymmetric voyage-4 retrieval — replaces Atlas autoEmbed for Stage 2 routing (`agents/orchestrator.py`, MongoDB `vector_index`)

> ⚠️ **Diagnosis later corrected.** The bypass-autoEmbed approach described
> below was based on the wrong root-cause hypothesis. Atlas `autoEmbed`
> *does* pass voyage-4's `input_type` parameter correctly; the actual
> cause of the score collapse was the default `quantization: scalar`
> (int8). The bypass was later reverted in favor of the one-line fix
> `quantization: "float"` on every autoEmbed field — see the entry
> *"Revert manual voyage-4 embedding pipeline"* below.

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
