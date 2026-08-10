#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Orchestrator Agent with Semantic Routing and Live Broadcast

Composition root. The six concerns that used to live here as one
3,400-line class are decomposed into mixins (Phase 0 of
MULTI_AGENT_PLAN.md):

    broadcast.py    — live-feed broadcast + ANSI palette
    registry.py     — MCP service discovery / registry sync
    router.py       — two-stage semantic routing + routing analytics
    memory.py       — long-term memory extract / recall / promote / decay
    workstreams.py  — short-term working memory (workstream layer)
    mcp_pool.py     — MCP server activation + stdio session pool
    react.py        — the ReAct tool-calling loop

The public surface (OrchestratorAgent, BROADCAST_RECEIVE_URL, constants)
is unchanged — main.py and web/shell.py import from here as before.
"""

import asyncio
import os
import json
import ast
import re
import httpx
import datetime
import hashlib
import tempfile
import time
from pathlib import Path
from contextlib import AsyncExitStack
from typing import List, Dict
from watchfiles import awatch
from pymongo import AsyncMongoClient, ReturnDocument
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

# Re-exported names (BROADCAST_*, Colors, TITLE_COLORS, MEMORY_*,
# _SYSTEM_PROMPT) keep backwards compatibility for existing importers.
from .broadcast import (BROADCAST_URL, BROADCAST_RECEIVE_URL, Colors,
                        TITLE_COLORS, BroadcastMixin)
from .registry import RegistryMixin
from .router import RouterMixin
from .memory import (MemoryMixin, MEMORY_PROMOTE_THRESHOLD,
                     MEMORY_CORE_CONFIDENCE_FLOOR, MEMORY_DECAY_AGE_SECONDS,
                     MEMORY_DECAY_CONFIDENCE_FACTOR,
                     MEMORY_DECAY_SWEEP_INTERVAL_SEC)
from .workstreams import WorkstreamMixin
from .mcp_pool import McpPoolMixin
from .react import ReactMixin, _SYSTEM_PROMPT
from .dispatch import AgentDispatchMixin
from .catalog import build_agents


class OrchestratorAgent(BroadcastMixin, RegistryMixin, RouterMixin,
                        MemoryMixin, WorkstreamMixin, McpPoolMixin,
                        ReactMixin, AgentDispatchMixin):

    def __init__(self, server_dir: str = "mcp_servers", local_broadcast=None,
                 demo_prefix: str = "", shared_bootstrap: bool = True):
        # Phase B (MULTI_SESSION_PLAN.md): demo_prefix namespaces this
        # orchestrator's mutable demo collections AND its workstream /
        # memory collections, and is passed to child MCP servers via the
        # DEMO_PREFIX env var so a browser session's data is isolated.
        # Empty prefix = the shared/default lane (CLI, terminal shell) —
        # byte-identical to before. shared_bootstrap=False skips the
        # one-time global work (registry sync, agent-card sync, the
        # filesystem watcher): a per-session orchestrator reuses the
        # shared mcp_services / agent_cards a bootstrap orchestrator
        # already populated.
        self.demo_prefix = demo_prefix
        self.shared_bootstrap = shared_bootstrap
        self.server_dir = Path(server_dir)
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self.conversation_history = []
        self.last_service = None  # Session Stickiness (service-level)
        self.last_domain  = None  # Session Stickiness (domain-level — Stage 1)
        # Set to False once we see the Atlas vector_index reject `domain` as
        # a filter, so we stop trying to filter on subsequent queries.
        self._domain_filter_supported = True
        self.local_broadcast = local_broadcast  # optional async callback(tag, msg)

        # Services that hold a session lock once selected — follow-up messages
        # are always routed here regardless of vector score, because the user
        # is in a multi-turn conversation with them.
        self.CONVERSATIONAL_SERVICES = {"acc_proof_point_service", "acc_export_service"}

        if not os.environ.get("MONGODB_URI"):
            raise ValueError("MONGODB_URI missing")

        self.mongo_client = AsyncMongoClient(os.environ["MONGODB_URI"])
        self.db = self.mongo_client["agent_registry"]
        self.collection = self.db["mcp_services"]
        # Workstream layer — the agent's short-term working memory. Each
        # workstream is a coherent thread of activity (one or more turns,
        # one or more services involved). Routing is workstream-anchored:
        # which workstream a query belongs to determines its sticky domain
        # and the entities the agent has in context. State is persisted so
        # killing main.py mid-workstream and restarting resumes correctly.
        # Workstreams + memories are per-session (prefixed) so one
        # browser session's conversation context can't bleed into
        # another's; the service registry, agent cards, routing
        # analytics, and user preferences stay shared (bare).
        self.workstreams = self.db[demo_prefix + "agent_workstreams"]
        self.current_workstream_id: str | None = None
        self._ws_summary_tasks: set[asyncio.Task] = set()
        # Long-term memory layer. When a workstream closes, the orchestrator
        # extracts 0-5 reusable facts from its summary + tool-call trail and
        # persists them here, vector-indexed for cross-session recall. The
        # ReAct loop pulls top-K relevant memories into the agent's context
        # at the start of each turn so past lessons inform current work.
        self.memories = self.db[demo_prefix + "agent_memories"]
        # User-stated preferences plane — populated by
        # preferences_service.remember_fact. Auto-recalled into every
        # turn's system prompt alongside agent_memories. Per-session
        # (Phase B): one user's facts must not leak into another's, and a
        # reset must clear them — so this is prefixed like the rest of the
        # session state.
        self.preferences = self.db[demo_prefix + "user_preferences"]
        self._memory_extract_tasks: set[asyncio.Task] = set()
        self._ws_closure_watcher: asyncio.Task | None = None
        self._memory_decay_task:   asyncio.Task | None = None
        # Routing analytics — every process_query call writes one document
        # capturing what Stage 1, Stage 2, memory, and the ReAct loop did.
        # Per-session so the analytics view + a reset are scoped to the
        # session that produced them.
        self.routing_decisions = self.db[demo_prefix + "routing_decisions"]
        self._current_decision: dict | None = None

        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY missing")

        self.openai = AsyncOpenAI()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        self.http_client = httpx.AsyncClient()
        self.tool_cache: Dict[str, List[Dict]] = {}  # server_name → openai tool dicts
        self.temp_dir = Path(tempfile.mkdtemp(prefix="mcp_cloud_"))
        self._watcher_task: asyncio.Task | None = None

        # ── Phase 1 (MULTI_AGENT_PLAN.md): domain agents ──────────────
        # Per-server stdio session locks — required once domain agents
        # can run concurrently against the shared session pool. Created
        # lazily by _call_tool_locked.
        self.session_locks: Dict[str, asyncio.Lock] = {}
        # DomainAgent instances, built from agents/catalog at __aenter__.
        self.domain_agents: Dict[str, object] = {}
        # AGENT_MODE flag gates which domains dispatch to a DomainAgent
        # instead of the legacy routing path. Default flipped to 'all'
        # after the Phase 2 soak (post-soak cleanup):
        #   unset/'all'        → every catalog agent (default)
        #   '0'/'off'/'legacy' → legacy routing only (opt-out)
        #   '1'/'ibn'          → the IBN agent only
        #   'ibn,dtw'          → explicit domain list
        mode = os.environ.get("AGENT_MODE", "all").strip().lower()
        if mode in ("0", "false", "off", "legacy"):
            self._agent_domains_enabled: set | None = set()
        elif mode in ("1", "true", "on", "ibn"):
            self._agent_domains_enabled = {"ibn"}
        elif mode in ("", "all"):
            self._agent_domains_enabled = None  # None = all registered
        else:
            self._agent_domains_enabled = {
                d.strip() for d in mode.split(",") if d.strip()}

    async def __aenter__(self):
        # Shared, one-time global work — only the bootstrap orchestrator
        # (prefix="") does it. Per-session orchestrators reuse the
        # mcp_services / agent_cards it populated.
        if self.shared_bootstrap:
            await self._sync_registry()
        # Build domain agents from the catalog (instantiation only — reads
        # the shared mcp_services at dispatch time). Cards are published
        # once by the bootstrap orchestrator.
        self.domain_agents = build_agents(self)
        if self.shared_bootstrap:
            await self._sync_agent_cards()
        await self._ensure_agent_conversation_indexes()
        # Per-session (or shared) index ensure + workstream resume on this
        # orchestrator's own (possibly prefixed) collections.
        await self._ensure_workstream_indexes()
        await self._ensure_memory_indexes()
        await self._ensure_routing_decision_indexes()
        await self._resume_open_workstreams()
        # Background tasks. The filesystem watcher (mcp_servers/ hot
        # reload) is a global concern — bootstrap only. The
        # workstream-closure watcher (drives memory extraction) and the
        # decay sweep operate on this orchestrator's prefixed collections,
        # so every session runs its own.
        if self.shared_bootstrap:
            self._watcher_task = asyncio.create_task(self._watch_servers())
        self._ws_closure_watcher  = asyncio.create_task(self._watch_workstream_closures())
        self._memory_decay_task   = asyncio.create_task(self._memory_decay_loop())
        # Catch-up: if any workstream was closed while the orchestrator
        # wasn't running, extract its memories now.
        await self._extract_backlog()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        for t in (self._watcher_task, self._ws_closure_watcher,
                  self._memory_decay_task):
            if t:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
        # Wait for pending background tasks so we don't lose summaries
        # or partially-written memory extractions.
        pending = list(self._ws_summary_tasks) + list(self._memory_extract_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.exit_stack.aclose()
        await self.http_client.aclose()
        await self.mongo_client.close()

    async def process_query(self, user_input: str) -> str:
        await self._broadcast() # newline
        await self._broadcast("QUERY", user_input[:300])
        await self._broadcast("AGENT", "Analyzing intent...")

        # Start a fresh routing-decision record for this turn. _decision_set
        # and _decision_under helpers populate it as routing progresses; we
        # insert it into routing_decisions in a finally block at the end so
        # even a partial / failed turn produces an analytics row.
        turn_t0 = time.monotonic()
        self._current_decision = {
            "ts":                 datetime.datetime.now(),
            "query":              user_input[:400],
            "query_length_words": len(user_input.split()),
        }

        # Context-Aware Routing for follow-up questions
        context_window = self.conversation_history[-4:] if self.conversation_history else []
        last_user_queries = [msg["content"] for msg in context_window if msg["role"] == "user"]

        # ── Pipeline order (Option B refactor) ───────────────────────────
        # Run Stage 1 (domain classification) BEFORE workstream
        # classification, then pass the Stage 1 domain set into the
        # workstream classifier so its OPEN-workstream candidate pool
        # is pre-filtered. This prevents the classic failure mode where
        # the classifier picks WS-IBN-Munich as the continuation target
        # for 'add play golf to my TODOs' just because titles overlap —
        # Stage 1 has already established the query is in the 'todo'
        # domain, so the classifier only sees TODO workstreams.
        #
        # Two shortcuts skip the upfront Stage 1 because they don't
        # need it:
        #   - Meta queries: routing happens later; _route_query runs
        #     its own Stage 1 then.
        #   - Closure cues: handled by the classifier's pure-Python
        #     fast-path which uses topic substring matching against
        #     each workstream's domain/title/entities — domain
        #     scoping is irrelevant.
        is_meta_query = self._is_meta_query(user_input)

        # Sticky-bias posture: short non-imperative follow-ups (e.g. "yes",
        # "do it", "and now?") have weak routing signal of their own, so
        # _route_query should bias toward last_service. Longer or
        # imperative-led queries carry their own signal and shouldn't get
        # the sticky boost.
        _SELF_CONTAINED = {
            "list", "show", "add", "update", "delete", "remove", "change",
            "set", "refresh", "display", "what", "how", "get", "find",
            "create", "book", "confirm", "cancel", "check", "search", "buy",
        }
        first_word = user_input.split()[0].lower() if user_input.split() else ""
        is_self_contained = first_word in _SELF_CONTAINED
        is_short_followup = (
            last_user_queries
            and len(user_input.split()) < 5
            and not is_self_contained
        )

        stage1_domains: List[str] | None = None

        if is_meta_query:
            await self._broadcast("WORKSTREAM",
                "⚙ Meta / introspection query — skipping workstream "
                "classification; not attaching tool calls to any workstream")
            self.current_workstream_id = None
            self._decision_set(meta_query=True)
            ws_id = None
            ws_is_new = False
            ws_domain = None
            replay_source_id = None
            was_pure_closure = False
            closed_ids: List[str] = []
        else:
            # Closure cue → classifier's fast-path handles everything
            # without needing Stage 1's domain set. Saves an LLM call
            # on goodbye turns.
            if self._is_pure_closure_cue(user_input):
                ws_id, ws_is_new, ws_domain, replay_source_id, \
                    was_pure_closure, closed_ids = \
                    await self._classify_workstream(user_input, last_user_queries)
            else:
                # Stage 1 always runs on the bare user_input. We do NOT
                # concatenate previous-turn text into the routing input —
                # that biases the vector search toward the prior domain's
                # vocabulary (e.g. "feasibility check!" after intent
                # creation would route to intent_service because the
                # enriched form is 95% intent vocabulary). Cross-turn
                # continuity is carried by:
                #   (a) last_domain sticky hint passed to Stage 1, and
                #   (b) the workstream context block injected into the
                #       ReAct system prompt at execution time.
                stage1_domains = await self._classify_domain(
                    user_input, sticky_hint=self.last_domain)

                # Workstream classifier — scoped to Stage 1's domains.
                ws_id, ws_is_new, ws_domain, replay_source_id, \
                    was_pure_closure, closed_ids = \
                    await self._classify_workstream(
                        user_input, last_user_queries,
                        domain_filter=stage1_domains)

            self.current_workstream_id = ws_id
            if not ws_is_new and not was_pure_closure:
                await self._broadcast("WORKSTREAM", f"↪ {ws_id} continued")
            self._decision_set(
                workstream_id=ws_id,
                workstream_is_new=ws_is_new,
                workstream_domain=ws_domain,
                replay_source_id=replay_source_id,
                was_pure_closure=was_pure_closure,
                closed_workstreams=closed_ids,
                meta_query=False)
            if ws_domain:
                self.last_domain = ws_domain

        # ── Closure-only short-circuit ────────────────────────────────────
        # The user just said goodbye to one or more workstreams ('done with
        # TODOs', 'we're finished'). The workstream(s) are already closed
        # by the classifier; memory extraction kicks off via the change-
        # stream watcher. There is nothing for the agent to *do* — running
        # the ReAct loop would just have the LLM speculate a tool call
        # ('let me list_todos to confirm'). Short-circuit with a canned
        # acknowledgement instead, listing every workstream that was
        # closed so the user sees the full effect of the safeguard.
        if was_pure_closure:
            await self._broadcast("AGENT", "Closure acknowledged — no tool call needed")
            if not closed_ids:
                # Closure cue but nothing open to close. Be explicit so
                # the user can see we deliberately did nothing rather
                # than fabricating a workstream just to "close" it.
                answer = ("You have no active workstream — nothing to "
                          "close. (No tool call, no LLM call, no new "
                          "workstream created.)")
            elif len(closed_ids) == 1:
                wid = closed_ids[0]
                ws_doc = await self.workstreams.find_one(
                    {"_id": wid}, {"title": 1})
                title = (ws_doc or {}).get("title") or wid
                answer = (f"Got it — closed the **{title}** workstream "
                          f"(`{wid}`). Long-term memory extraction will "
                          f"run in the background.")
            else:
                docs = self.workstreams.find(
                    {"_id": {"$in": closed_ids}}, {"title": 1})
                title_map = {
                    d["_id"]: d.get("title") or d["_id"]
                    async for d in docs
                }
                bullets = "\n".join(
                    f"  • **{title_map.get(i, i)}** (`{i}`)"
                    for i in closed_ids
                )
                answer = (f"Got it — closed **{len(closed_ids)} "
                          f"workstreams** in one go. Long-term memory "
                          f"extraction will run for each in the "
                          f"background:\n{bullets}")
            # Still record the conversation turn so the next classifier
            # has continuity, but skip ReAct entirely.
            self.conversation_history.append({"role": "user", "content": user_input})
            self.conversation_history.append({"role": "assistant", "content": answer})
            if len(self.conversation_history) > 20:
                self.conversation_history = self.conversation_history[-20:]
            await self._persist_decision(
                tool_calls_count=0,
                iterations_used=0,
                closure_short_circuit=True,
                duration_ms=int((time.monotonic() - turn_t0) * 1000))
            return answer

        # ── Replay-recipe prep ────────────────────────────────────────────
        # If the user asked to "do the same thing for X", build a recipe
        # from the source workstream's tool-call audit and stash it; it
        # gets injected into the ReAct loop's system prompt below.
        replay_recipe = ""
        if replay_source_id:
            replay_recipe = await self._build_replay_recipe(
                replay_source_id, target_workstream_id=ws_id)

        # ── Phase 2 agent dispatch (MULTI_AGENT_PLAN.md) ──────────────────
        # Resolve the turn to DomainAgents via the card-ranked selector.
        # One agent → passthrough dispatch (the shell does no ReAct of
        # its own). Two or more → parallel dispatch with scoped
        # sub-tasks and a synthesis pass. Domains without an agent (or
        # with AGENT_MODE off) fall through to the direct-to-server
        # path below — that path remains the shell's own tool surface
        # for singleton services.
        if not is_meta_query:
            _agents = await self._select_agents_for_turn(
                user_input, stage1_domains, ws_domain,
                is_short_followup=is_short_followup)
            if len(_agents) == 1:
                return await self._dispatch_to_agent(
                    _agents[0], user_input, replay_recipe, turn_t0)
            if len(_agents) >= 2:
                return await self._dispatch_multi(
                    _agents, user_input, replay_recipe, turn_t0)

        # ── Stage 2 — vector search within precomputed Stage 1 domains ────
        # Follow-up detection and Stage 1 already ran upfront (in
        # parallel where applicable). For meta queries stage1_domains
        # is None, so _route_query runs its own Stage 1.
        service_names = await self._route_query(
            user_input,
            use_stickiness=is_short_followup and not is_meta_query,
            precomputed_domains=stage1_domains,
        )

        if not service_names:
            await self._persist_decision(
                no_services_found=True,
                duration_ms=int((time.monotonic() - turn_t0) * 1000))
            return "I couldn't find relevant services for this request."

        # Resolve paths — local filesystem first, then cloud temp dir
        matches = []
        for service_name in service_names:
            local_path = self.server_dir / f"{service_name}.py"
            cloud_path = self.temp_dir   / f"{service_name}.py"

            if local_path.exists():
                matches.append({"server_name": service_name, "path": str(local_path.absolute())})
            elif cloud_path.exists():
                matches.append({"server_name": service_name, "path": str(cloud_path.absolute())})
            else:
                print(f"⚠️ {service_name} not found locally or in cloud temp dir, skipping")

        if not matches:
            await self._persist_decision(
                services_not_resolvable=True,
                duration_ms=int((time.monotonic() - turn_t0) * 1000))
            return (
                "Services found in registry but not available locally. "
                "Please ensure MCP servers are installed in the mcp_servers directory."
            )

        # Store last non-preferences service AND its domain for stickiness.
        # last_domain is consulted by Stage 1 on the next short/ambiguous
        # turn. Preferences statements ('I love X') are isolated events
        # that shouldn't drag subsequent unrelated turns into the
        # preferences domain.
        for match in matches:
            name = match["server_name"]
            if name != "preferences_service":
                self.last_service = name
                self.last_domain  = self._infer_domain(name)
                break

        # preferences_service is routed normally — no forced injection.
        # It will be selected by the vector search when the query is
        # about preferences, personal facts, or 'remember that I…'
        # operations.

        server_names_final = [m["server_name"] for m in matches]
        await self._broadcast("AGENT", "Selected: " + ", ".join(server_names_final))

        await self._activate_servers(matches)

        #self._broadcast("ACTION", f"Active sessions after activation: {list(self.sessions.keys())}")

        react = await self._run_react(user_input, matches,
                                      replay_recipe, turn_t0)
        if react["verbatim"]:
            return react["answer"]
        final_answer = react["answer"]

        # Store conversation turn
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": final_answer})

        # Limit history to last 20 messages (10 turns)
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        # Update the workstream summary in the background — it shouldn't
        # block the user response. We track the task so __aexit__ can wait
        # on pending ones at shutdown (no lost summaries on Ctrl-C).
        if self.current_workstream_id:
            t = asyncio.create_task(self._update_workstream_summary(
                self.current_workstream_id, user_input, final_answer))
            self._ws_summary_tasks.add(t)
            t.add_done_callback(self._ws_summary_tasks.discard)

        # Persist the routing-decision record (analytics).
        await self._persist_decision(
            tool_calls_count=react["tool_calls_count"],
            iterations_used=react["iteration"],
            max_iterations=react["max_iterations"],
            max_iterations_hit=(react["iteration"] >= react["max_iterations"]),
            had_replay_recipe=bool(replay_recipe),
            duration_ms=int((time.monotonic() - turn_t0) * 1000))

        return final_answer
