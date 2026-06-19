# Browser-Session-Safe Demo — Phase A / Phase B Plan

Goal: let anyone drive the demo from the browser, including the
destructive **Reset demo data** action, without trampling other people's
sessions — and without a login.

## Phase A — single-user reset (DONE)

Adds a **Reset demo data** button to the web shell that re-runs the
`seed/ibn_seed.py --reset` + `seed/dtw_seed.py --reset` pipeline against
the shared `agent_registry` database, streaming progress to the browser.

- `web/seed_runner.py` — drives the seeders' phase functions against a
  given `db_name`, redirecting their `print()` output line-by-line to an
  async `emit` callback (worker thread → `loop.call_soon_threadsafe` →
  `asyncio.Queue` → WebSocket).
- `web/shell.py` — `reset_demo` WS message, run under the existing
  `_query_lock` so a reset can never interleave with a query
  mid-tool-call. On completion the agent's in-memory turn context
  (conversation tail, current workstream, sticky domain/service) is
  cleared so the next query starts clean.
- Each WS connection already gets a `session_token` (echoed in `hello`),
  and the reset path routes through `_demo_db_for(session_token)` →
  currently the constant `agent_registry`. **These two are the Phase-B
  seams**: nothing else needs to move.

Assumes one user at a time — `_query_lock` serialises everything in the
single server process, so a lone user is fully safe. With two users, a
reset by one wipes the other's data: that is what Phase B fixes.

## The constraint that shapes Phase B (measured, not assumed)

During Phase-A testing the seed pipeline was run against a throwaway
database `agent_registry__phaseA_smoketest`. Result:

- **Writes to another database succeeded** (collections created and
  populated).
- **`dropDatabase` on it was DENIED** (`not authorized`), and so was
  dropping `system.views`.
- **Dropping ordinary collections within it succeeded.**

So the Atlas credential behind `MONGODB_URI` is effectively scoped: it
can CRUD and drop *collections*, but cannot drop *databases*. Therefore:

> **Per-session separate databases are out** — we could create them but
> never cleanly reap them. Phase B must isolate sessions by
> **collection name within `agent_registry`**, which we *can* drop.

## Phase B — session isolation by collection prefix

### Session identity
- The server already issues `session_token` per WS connection. Make it
  sticky: the browser stores it in `localStorage` and sends it back in
  the first WS message; the server reuses it if present, else mints one.
  A refresh keeps your demo; a new browser/incognito gets a fresh one.
- Sanitise hard before it ever touches a collection name: accept only
  `^[a-z0-9]{8,32}$` (the minted tokens already are), reject otherwise.
  Collection names are built as `s_<token>_<base>`.

### What actually needs isolating (the cost-saver)
Most demo collections are **read-only reference data** at demo time and
can stay shared. Only a handful are mutated by tool calls:

| Mutated per session | Written by |
|---|---|
| `ibn_intents` | intent / feasibility / assurance |
| `ibn_policy_snapshots` | feasibility |
| `ibn_compliance_events` | assurance |
| `ibn_telemetry` (timeseries) | feasibility / assurance / simulator |
| `dtw_scenarios` | scenario / simulation |

Shared read-only reference (NOT namespaced): `ibn_customers`,
`ibn_sites`, `ibn_resources`, `dtw_markets`, `dtw_plans`,
`dtw_qos_profiles`, `dtw_network_elements`, `dtw_topology_edges`,
`dtw_subscribers`, `dtw_traffic_models`, and both `*_knowledge_chunks`
collections — which is what carries the Atlas Vector Search indexes.

**Consequence: the expensive Atlas Search indexes stay shared and
global.** Per-session isolation only duplicates a handful of small
mutable collections. No per-session vector indexes → no Atlas Search
index-count ceiling on concurrent sessions.

One wrinkle: `ibn_assurance_service.update_template_version` *inserts*
into `ibn_knowledge_chunks`. Options: (a) accept it as a rare, additive,
shared write; (b) namespace `ibn_knowledge_chunks` too and pay for
per-session vector indexes only for sessions that use that action. Ship
(a); revisit if it bites.

### Threading the prefix through the MCP servers
The servers hardcode `db["ibn_intents"]`. They are launched as stdio
subprocesses by `McpPoolMixin._activate_servers` with
`env=os.environ.copy()`. So:

1. Add a tiny helper each server uses for the *mutable* collections:
   `coll(base) = db[os.environ.get("DEMO_PREFIX", "") + base]`. Reference
   collections keep their bare names.
2. The web shell keeps a `{session_token: OrchestratorAgent}` map. Each
   session's orchestrator launches its own MCP server set with
   `DEMO_PREFIX=s_<token>_` in the child env. (Memers: the orchestrator's
   own infra collections — `agent_workstreams`, `agent_memories`,
   `mcp_services`, `agent_cards` — can also be prefixed or kept shared;
   recommend prefixing `agent_workstreams`/`agent_memories` so one user's
   conversation context can't leak into another's, while keeping the
   registry/cards shared.)
3. `_demo_db_for` is replaced by `_demo_prefix_for(session_token)` and
   `seed_runner` gains a `prefix` arg: it seeds `s_<token>_ibn_intents`
   etc. for the mutable set and **skips** the shared reference collections
   if they already exist (seed them once, globally, at startup).

### Lifecycle / reaping
- A background sweeper drops `s_<token>_*` collections whose session has
  been idle past a TTL (e.g. 2h) — collection drops are authorised, so
  this works with the current credential.
- On `reset_demo`, only this session's mutable collections are dropped
  and re-seeded from fixtures; shared reference data is untouched.
- Per-session `asyncio.Lock` replaces the global `_query_lock` (keep a
  global only around shared-resource bootstrap).

### Resource ceiling
Bounded by MongoDB collection count, not Atlas Search indexes — far
cheaper. Still cap concurrent sessions (e.g. 50) and reap aggressively;
optionally pre-create a small pool of seeded "lanes" and hand them out
to avoid seed latency on first interaction.

### Migration path (small, ordered diffs)
1. Sticky `session_token` (localStorage + first-message echo). No
   behaviour change yet.
2. `coll()` helper in the five mutating MCP servers, default prefix `""`
   → byte-identical behaviour when unset.
3. Per-session orchestrator map + `DEMO_PREFIX` child env in the pool.
4. `seed_runner` prefix arg; one-time global seed of shared reference
   collections at startup; reset scoped to the session prefix.
5. Idle-session sweeper; swap global lock for per-session lock.

Each step ships independently and leaves the single-user path working.
