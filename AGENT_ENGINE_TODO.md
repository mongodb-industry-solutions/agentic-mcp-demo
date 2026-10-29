# What the demo still needs on Atlas Agent Engine

Status 2026-10-21 (CLI 0.1.119, SDK 0.11.8). Jira: ISTTH-969.

The Telco OSS agents run on Atlas Agent Engine today: `noc-coordinator`,
`ibn-agent` and `dtw-agent` in the shared JeffN project, with their data on
the `telco-oss-agents` cluster. The twin's web shell runs on the presenter's
laptop and drives them with the presenter's CLI login. It does chat, the
Agent Log and NOC approvals, for one presenter at a time. The Kanopy demo
(staging and prod) still runs the legacy runtime and is unaffected.

"Fully fledged" means the Kanopy-hosted shell on Agent Engine: many
visitors at once, each with an isolated demo lane and memory, NOC
approvals in the browser, and the legacy runtime retired (the BIG SWITCH).

This document lists what stands between today and that. Chapter 1 is what
we have to arrange (projects, accounts, users, keys, sign-offs). Chapter 2
is what Agent Engine lacks: missing or broken features we work around or
wait for. Details and evidence for every item are in `AGENT_ENGINE_PLAN.md`
(the findings tables cited as "P3 #5" = Phase 3 finding 5).

**Bottom line.** No missing product feature blocks the hosted demo: each
gap in Chapter 2 has a workaround in the code. What blocks it is
organizational, above all item 1.1: a dedicated Agent Engine project.

---

## 1. Organizational TODOs

### 1.1 A dedicated Agent Engine project (blocker)

- **What:** our own Agent Engine project, linked to our own Atlas project,
  instead of the shared JeffN project. Ideally one per environment
  (staging, prod), or at least one for the hosted demo.
- **Why:**
  - The hosted shell needs a service account. A service account can only
    be `PROJECT_OWNER` (or read-only), and "Atlas service account pairing"
    creates an Atlas service account with Project Owner on the *linked*
    Atlas project. JeffN's linked project holds other teams' production
    clusters (IST-Prod, IST-Shared-Europe). The Agent Engine team (Walter
    Tan) advises against pairing there.
  - JeffN's shared settings already constrain us:
    - the memory configuration and the idle timeout are project-wide;
    - JeffN is at Atlas's 100 database-user cap, so the agents use the
      `istmedia` user, which also reaches IST-Prod (Move to JeffN #4);
    - other teams' project secrets leak into our sandboxes unless
      overridden (Move to JeffN #1, #3).
  - Our memories most likely land in another team's database, through
    the project-scoped `MONGODB_URI` (P5 #10).
- **Decide:**
  - who owns the project;
  - which Agent Engine org it sits in;
  - which Atlas org and project it links to;
  - how billing works (JeffN is on corporate billing).
- **Then:** enable Atlas service account pairing in the new project.

### 1.2 The agents' Atlas cluster in that project

- Move or recreate `telco-oss-agents` (today a Flex cluster in JeffN, AWS
  us-east-1) in the new Atlas project, and reseed it from `seed/`.
- **Size it for the target load.** The platform's run state
  (`mdb_store_<project>`: traces, checkpoints) lands on this cluster too,
  so many concurrent visitors may need more than Flex.
- **Database access:** a cluster-scoped database user for the agents (no
  more `istmedia`).
- **Recreate:**
  - the Atlas Vector Search indexes: `ibn_knowledge_index`,
    `dtw_knowledge_index` and `agent_cards_index`, all on auto-embed
    voyage-4;
  - the change-stream access the dashboards need.

### 1.3 Accounts and keys

- **Service account** for the Kanopy shell:
  - create it in the new project;
  - store its client id and secret in the Kanopy secret
    `agentic-mcp-demo-secrets`;
  - name an owner and a rotation routine.
- **LLM key.** Today it's a Grove gateway key:
  - in each workspace's `LLM_API_KEY`;
  - for memory extraction, in the project secret `CEREBRAS_API_KEY`,
    because JeffN's OpenAI-named secrets belong to other teams (P5 #2).

  In our own project it becomes `LLM_API_KEY` at project scope. Decide
  whose budget pays for it.
- **Voyage key** (`VOYAGE_API_KEY`, required for memory): our own, at
  project scope.
- **End-user delegation** (`--user-id` for someone other than the caller):
  check whether the new project needs it switched on. The shell gives
  each browser session its own memory identity.

### 1.4 Users and roles

- **Who gets which role.** Presenters, developers and NOC reviewers. Today
  every action needs `PROJECT_OWNER`: deploy, invoke, and resuming a paused
  run (approving a review). `AGENT_DEVELOPER` is refused everywhere
  (Move to JeffN #5).
- **Presenters running the twin locally** need an `agentengine` CLI login
  on the project.

### 1.5 Recreate the project's configuration

- **Workspaces:** `ibn-agent`, `dtw-agent`, `noc-coordinator`, with
  workspace secrets `MONGODB_URI`, `LLM_API_KEY`, `LLM_BASE_URL`,
  `LLM_AUTH_HEADER`, `NOC_REVIEW`.
- **Workspace ids:** new ones mean updating:
  - `a2a.allowed_callers` in each `agent.yaml`;
  - `agent-engine/guardrails/subscriber-ids.json`;
  - the defaults in `web/twin_shell.py` and `agent-engine/autopilot.py`.
- **Memory:** configure it (`project-config.yaml`, `agentengine memory
  configure` plus `memory apply`).
- **Guardrail:** recreate the subscriber-id redaction guardrail, in the UI
  or with `POST /guardrails` (there is no CLI command).
- **Scaling:** per agent, `scaling.replicas` of at least the expected
  number of concurrent demo sessions (each session holds a sandbox in
  every agent it touches, for the idle timeout of 600 s; P4 #4). The
  project-wide 512 cap applies.

### 1.6 Kanopy

- **Shell image:** a new image and environment files for the Agent Engine
  shell, first in `staging`. The current image bootstraps `uv` and the MCP
  servers; the new one doesn't need them.
- **Network:** egress from Kanopy to `agentengine.mongodb.com`.
- **Dashboards:** they move unchanged, pointed at the agents' cluster.
- **Fallback:** keep a fallback to the legacy runtime (a `RUNTIME` flag)
  until Agent Engine is GA.
- **The BIG SWITCH:** a date, then the prod cut-over on `main`, then the
  legacy runtime and `mcp_servers/` retired.

### 1.7 Sign-offs

- Agent Engine is a public preview (no SLA, breaking changes expected).
  Get sign-off from management to run a customer-facing demo on it.
- The Policy Engine "policy denied" beat (Phase 6) needs a project of its
  own, because a policy in JeffN would apply to other teams' agents.
- Code reviews: open PRs #45 (approved), #49 and #50, then the twin shell
  and approval card.

---

## 2. Product TODOs: what Agent Engine is missing

Each item: the gap, its effect on the demo, our workaround, and what we'd
ask the Agent Engine team for. Confirmed means we reproduced it; the source
in brackets.

### 2.1 Roles and credentials

- **No least-privilege role** (confirmed: Walter Tan; P0 deployed):
  - *Gap:* invoking an agent and resuming a review both need
    `PROJECT_OWNER`, and a service account can't be anything else
    (besides read-only). Pairing gives it Atlas Project Owner on the
    linked Atlas project, with no narrower option.
  - *Effect:* the shell's credential can deploy, delete and administer.
    This is why we need a project of our own.
  - *Ask:* an invoke-only role, and a reviewer (resume-only) role, for
    service accounts and users. Pairing without Atlas Project Owner.
- **`AGENT_DEVELOPER` is refused** for every account we tried
  (Move to JeffN #5).

### 2.2 Human-in-the-loop

- **The documented `POST /executions/{id}/resume` doesn't resume LangGraph
  `interrupt()` pauses** (confirmed locally and deployed, P3 #5):
  - *Gap:* the flat body gets `409 RESUME_INPUT_INVALID`; a `resume_map`
    body gets `400`.
  - *Workaround:* `invoke` on the same session with
    `resume_map: {<interrupt id>: {decision, reviewer_notes}}`.
  - *Ask:* make `/resume` work for durable sessions, or document
    `invoke` + `resume_map` as the way.
- **A paused A2A child can't be resumed through its parent** (confirmed,
  P4 #3):
  - *Gap:* `invoke_agent` returns `input-required` with only the child's
    execution id, with no interrupt id and no resume over A2A. The pause
    doesn't bubble up to the root caller.
  - *Workaround:* the review document records the child's execution and
    session; the shell resumes the child directly. The coordinator's
    conversation never learns the outcome.
  - *Ask:* propagate a child's suspension to the parent (the root
    execution suspends with the child's interrupts), and resume through
    the root.
- **No Pending Reviews page turned up for a project owner** (the twin):
  - *Effect:* the Agent Engine UI chat has no way to approve a paused run.
    The twin shell's approval card does it instead.
- **No notification when an execution suspends:**
  - *Gap:* in a deployed project, `executions` isn't on our cluster (only
    `traces` and the checkpoints are), so there's nothing to watch.
  - *Workaround:* our own `agent_approvals` documents, watched with a
    change stream.
  - *Ask:* a webhook or an event stream for suspend and resume.
- **Custom headers don't survive a pause, and the CLI's `--resume` can't
  resend them:**
  - *Effect:* a resumed run loses the demo lane (`demo-prefix`) and any
    other header the graph reads. A graph that branches on a header takes
    a different path on replay and fails ("workflow conflict: activity
    step 2 does not match next step 8").
  - *Workaround:* we put the A2A depth into the message itself, which is
    durable. The lane still needs the default lane (one presenter).
  - *Ask:* persist custom headers with the execution, or let the resume
    call carry them.
- **An HTTP `invoke` with `session_id` and `resume_map` didn't resume:**
  - *Gap:* the request was accepted, but it started a new, empty turn on
    the session while the paused execution stayed suspended. The CLI's
    `--resume` uses the same endpoint and works. The HTTP form that
    resumes isn't documented.
  - *Ask:* document the resume request for the HTTP API.
- **The tool sandbox doesn't know its workspace id**
  (`get_current_workspace_id()` is empty there):
  - *Effect:* the review document can't name the paused agent's
    workspace.
  - *Workaround:* the shell maps the action to the agent.

### 2.3 Memory

- **No delete-by-user (or reset) API** (confirmed: Walter Tan; P5):
  - *Effect:* Reset can't clear a visitor's memories.
  - *Workaround:* a fresh `user_id` per Reset.
  - *Ask:* delete memories by user (and by session).
- **Procedural extraction never stores anything** (confirmed, P5 #11;
  reported):
  - *Gap:* the extractor asks for plain JSON mode without the schema, and
    the models name the field differently, so every procedure is dropped.
    The prompt override is ignored.
  - *Workaround:* off; the demo flows are seeded as procedures instead.
- **The memory configuration is project-wide, with no read command**
  (P5 #1):
  - *Effect:* in a shared project one team's config applies to all.
  - *Ask:* per-workspace config, and a `memory get` command.
- **The extraction key must sit under a fixed list of secret names**
  (P5 #2):
  - *Effect:* in JeffN we use `CEREBRAS_API_KEY` for a Grove gateway key.
  - *Ask:* a configurable secret name.
- **Hand-saved memories are superseded by extraction**, losing their
  metadata (P5 #3). Seed writes are create-only and fail silently on
  conflicts (P5 #4).
- **The memory store's location isn't visible** (P5 #10): we can't tell
  which cluster holds our users' memories.

### 2.4 Streaming and observability

- **No live progress stream for a turn:**
  - *Gap:* the invoke stream carries the answer only; there are no step
    events (tool calls, A2A hops). With `use_custom_parser` the stream
    carries *only* custom events, so the answer frames disappear (P6),
    and `emit_custom_event` raises on the synchronous path (CLI, A2A
    children).
  - *Workaround:* the shell's Agent Log reads the platform's OpenTelemetry
    spans from `mdb_store_<project>.traces` with a change stream.
  - *Ask:* standard step events on the invoke stream, next to the text.
- **The traces are batched and undocumented:**
  - *Gap:* spans land about 5 s late, in batches. The schema is internal,
    with dotted attribute keys.
  - *Effect:* the Agent Log trails the agents by up to 5 s and may break
    with a platform update.
  - *Ask:* a documented trace schema, or an OTLP export.
- **No URL scheme for a trace or session in the UI** (P6), so we can't
  link "view trace" from the shell.

### 2.5 CLI

- **No custom-header flag on `invoke`** (P0 #2): the CLI can't select a
  demo lane, so the twin has one lane.
- **No command to read an execution:**
  - *Gap:* the interrupt id of a paused run is only in
    `GET /executions/{id}`.
  - *Workaround:* the twin shell calls the API with the CLI login's token
    from `~/.agentengine/auth.json`.
  - *Ask:* `agentengine executions get`.
- **`--stream` prints no progress frames**, and `--json --stream` buffers
  until the end.
- **Review prompts are interactive only:** HITL in the CLI only works in
  the interactive chat, not with `--json`, stdin or `--file`.
- **No CLI command for guardrails** (P6).
- **No hard delete for service accounts**, only deactivation.

### 2.6 Capacity and sessions

- **Each session holds a sandbox per agent it touches for the idle timeout,
  and every A2A call is a new child session** (P4 #1, #4):
  - *Effect:* with the default 4 replicas, a handful of coordinator turns
    gives `503` "All agent instances are currently busy".
  - *Ask:* autoscaling, or sandbox sharing for A2A children, and a
    per-workspace idle timeout (today it's project-wide: the last deploy
    wins).
- **A secret change interrupts service:**
  - *Gap:* `secret set --sync` rolls the runtime. For a minute or two
    after it, invokes got the same `503` "All agent instances are
    currently busy", with no session holding capacity (seen 2026-10-21,
    `NOC_REVIEW` on two workspaces).
  - *Workaround:* the shell retries a busy answer three times.
  - *Ask:* rolling updates without a capacity gap, or a distinct
    "restarting" error.
- **Not yet verified:** how many concurrent sessions the preview allows
  for our three agents under real load, with service-account invokes, the
  lane header and a `user_id` per visitor.

### 2.7 Platform hygiene in shared projects

- **Project secrets reach every workspace's sandboxes** (Move to JeffN #1,
  #3). Another team's `OPENAI_BASE_URL` reached our tool sandbox, and the
  coordinator once wrote to IST-Shared through the project's `MONGODB_URI`
  (P4 #9).
  - *Ask:* a way to keep a workspace from inheriting project secrets.
- **`agentengine atlas setup` writes project-scoped secrets and creates a
  database user per cluster**, which isn't safe in a shared project
  (Move to JeffN #2).

### 2.8 Preview

- Public preview, with no SLA and breaking changes expected. We pin the
  CLI and SDK versions and keep the legacy runtime as the fallback until
  GA.
- **The build doesn't honor our SDK pin** (2026-10-29):
  - *Gap:* the platform-managed install adds the current
    `agent-engine-runner-shared` (0.11.11, which needs `mcp==2.3.0`) on top
    of our `uv.lock` (0.11.8, `mcp 1.27.0`), so every build of an unchanged
    agent failed with "No solution found when resolving dependencies" until
    we relocked.
  - *Effect:* an SDK release can break the next deploy, and 0.11.11 also
    changed behavior: tool arguments are validated against the
    annotations, so a `None` for `site: str = None` now fails the run.
  - *Ask:* build with the locked versions, or announce the minimum the
    platform requires before it moves.
