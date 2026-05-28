#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Orchestrator Agent with Semantic Routing and Live Broadcast
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
import voyageai


BROADCAST_URL         = "https://notify.bjjl.dev/send"
BROADCAST_RECEIVE_URL = "https://notify.bjjl.dev/receive"

# Pre-compiled in _needs_context_enrichment: queries starting with one of
# these verbs are treated as self-contained imperatives — no enrichment
# from the previous turn (which would dilute their routing signal).
_IMPERATIVE_VERBS = re.compile(
    r"^(run|execute|simulate|launch|start|begin|trigger|fire|"
    r"stop|abort|cancel|reset|clear|delete|wipe|"
    r"show|list|describe|display|find|fetch|get|"
    r"add|remove|update|modify|change|set|"
    r"confirm|approve|proceed|retry|restart|"
    r"inject|apply|diagnose|compare|diff)$", re.I)

# ── Memory promotion / decay knobs ────────────────────────────────────────
# Memories carry a `tier` field that signals their standing:
#   "extracted"  — freshly mined from a closed workstream (default)
#   "core"       — recalled ≥ MEMORY_PROMOTE_THRESHOLD times; agent treats
#                  these as institutional knowledge with floored confidence
#   "decayed"    — old + never recalled; filtered out of LLM context but
#                  still inspectable via workstream_service tools
# Tunables are kept here so the demo can be sped up by lowering the age
# threshold (e.g. minutes instead of days) without code archaeology.
MEMORY_PROMOTE_THRESHOLD          = 3        # recalls → promote to core
MEMORY_CORE_CONFIDENCE_FLOOR      = 0.9      # confidence floor once core
MEMORY_DECAY_AGE_SECONDS          = 14 * 24 * 3600   # 14 days
MEMORY_DECAY_CONFIDENCE_FACTOR    = 0.7      # confidence multiplier on decay
MEMORY_DECAY_SWEEP_INTERVAL_SEC   = 6 * 3600 # background sweep every 6 hours


class Colors:
    RESET     = "\033[0m"
    BOLD      = "\033[1m"

    RED       = "\033[31m"
    GREEN     = "\033[32m"
    YELLOW    = "\033[33m"
    BLUE      = "\033[34m"
    MAGENTA   = "\033[35m"
    CYAN      = "\033[36m"

    BRIGHT_RED     = "\033[91m"
    BRIGHT_GREEN   = "\033[92m"
    BRIGHT_YELLOW  = "\033[93m"
    BRIGHT_BLUE    = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN    = "\033[96m"


TITLE_COLORS = {
    "BOOTSTRAP":  Colors.BLUE,
    "QUERY":      Colors.BRIGHT_YELLOW,
    "REACT":      Colors.RESET,
    "AGENT":      Colors.BRIGHT_CYAN,
    "ROUTING":    Colors.RESET,
    "ACTION":     Colors.RESET,
    "RESULT":     Colors.BRIGHT_GREEN,
    "ERROR":      Colors.BRIGHT_RED
}


_SYSTEM_PROMPT = (
    "You are an AUTONOMOUS AGENT using ReAct.\n\n"
    "🎯 AUDIENCE & TONE:\n"
    "You are assisting NOC engineers and internal operations staff - NOT end customers.\n"
    "Always speak in THIRD PERSON about the customer:\n"
    "  ✅ 'A €10.00 credit has been applied to the customer's account'\n"
    "  ✅ 'The subscriber +49 176 12345678 has been notified'\n"
    "  ❌ 'A credit has been applied to YOUR account'\n"
    "  ❌ 'Thank you for your patience'\n"
    "Use operational, concise language. No customer-facing pleasantries.\n\n"
    "📄 CONTENT PASSTHROUGH RULE:\n"
    "When a tool returns formatted content (proof points, documents, previews, "
    "rendered stories, one-pagers, slide content) — output the tool result VERBATIM "
    "to the user. Do NOT summarize, paraphrase, or condense it. The user asked to "
    "see the content, so show it in full exactly as the tool returned it.\n\n"
    "⚠️ ANTI-HALLUCINATION RULES:\n"
    "1. You can ONLY perform actions using the tools listed below\n"
    "2. NEVER claim to have done something without actually calling the tool\n"
    "3. If you don't have the right tool, say: 'I don't have access to that service right now'\n"
    "4. Always call the appropriate tool BEFORE confirming an action to the user\n"
    "5. If a tool call fails, report the error honestly - don't pretend it succeeded\n\n"
    "⚠️ CRITICAL RULES:\n"
    "1. PERMANENT facts (name, chronic conditions, lasting preferences)\n"
    "   → remember_fact(is_temporary=False)\n"
    "2. TEMPORARY context ('this time', 'today', 'just now')\n"
    "   → remember_fact(is_temporary=True)\n"
    "3. DELETE memories → forget_memory(topic='what to forget')\n"
    "4. LIST ALL memories → list_all_memories()\n\n"
    "⚠️ MANDATORY WORKFLOW for recommendations:\n"
    "   Step 1: ALWAYS call recall_memories(topic='...') FIRST!\n"
    "   Step 2: If user stated NEW preference, call remember_fact() to store it\n"
    "   Step 3: Call domain tool using BOTH recalled AND new preferences\n\n"
    "⚠️ WORKFLOW for listing everything:\n"
    "   User: 'was weißt du über mich?' or 'sage mir alles'\n"
    "   → Step 1: list_all_memories()\n"
    "   → Step 2: Present the complete list to user\n\n"
    "⚠️ WORKFLOW for forgetting:\n"
    "   User: 'vergiss dass ich vegetarier bin'\n"
    "   → Step 1: forget_memory(topic='vegetarian dietary restriction')\n"
    "   → Step 2: Confirm deletion to user\n\n"
    "Examples of recall topics:\n"
    "   - Food: recall_memories(topic='food preferences dietary restrictions allergies')\n"
    "   - Shopping: recall_memories(topic='shopping preferences budget brand')\n"
    "   - Finance: recall_memories(topic='investments portfolio assets')\n\n"
    "5. If recall_memories() returns 'No relevant memories', proceed with defaults.\n"
    "6. ALWAYS use available tools - DO NOT use internal knowledge or pretend to have done something.\n"
    "7. NEVER skip the recall_memories() step before recommendations!\n"
    "8. If you get a tool execution error, report it to the user honestly.\n"
)


class OrchestratorAgent:
    def __init__(self, server_dir: str = "mcp_servers", local_broadcast=None):
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
        self.workstreams = self.db["agent_workstreams"]
        self.current_workstream_id: str | None = None
        self._ws_summary_tasks: set[asyncio.Task] = set()
        # Long-term memory layer. When a workstream closes, the orchestrator
        # extracts 0-5 reusable facts from its summary + tool-call trail and
        # persists them here, vector-indexed for cross-session recall. The
        # ReAct loop pulls top-K relevant memories into the agent's context
        # at the start of each turn so past lessons inform current work.
        self.memories = self.db["agent_memories"]
        # User-stated preferences plane — populated by
        # preferences_service.remember_fact. Auto-recalled into every
        # turn's system prompt alongside agent_memories, so a fact the
        # user told the agent once persists across sessions.
        self.preferences = self.db["user_preferences"]
        self._memory_extract_tasks: set[asyncio.Task] = set()
        self._ws_closure_watcher: asyncio.Task | None = None
        self._memory_decay_task:   asyncio.Task | None = None
        # Routing analytics — every process_query call writes one document
        # capturing what Stage 1, Stage 2, memory, and the ReAct loop did.
        # Powers offline analysis (LLM-tiebreak rate, slow stages, routing
        # misses, service usage) via the analytics_service MCP tools.
        self.routing_decisions = self.db["routing_decisions"]
        self._current_decision: dict | None = None

        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY missing")

        self.openai = AsyncOpenAI()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")

        # Voyage AI client for asymmetric retrieval (input_type='document' at
        # index time, 'query' at search time). Atlas autoEmbed does NOT set
        # input_type — which collapses voyage-4 scores into a tight band and
        # picks wrong winners. We do the embeddings ourselves to get the
        # asymmetric retrieval voyage-4 was designed for.
        if not os.environ.get("VOYAGE_API_KEY"):
            raise ValueError("VOYAGE_API_KEY missing — required for "
                             "asymmetric voyage-4 retrieval in Stage 2")
        self.voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
        self.embed_model = "voyage-4"
        self.embed_dim   = 1024
        self.http_client = httpx.AsyncClient()
        self.tool_cache: Dict[str, List[Dict]] = {}  # server_name → openai tool dicts
        self.temp_dir = Path(tempfile.mkdtemp(prefix="mcp_cloud_"))
        self._watcher_task: asyncio.Task | None = None

    async def _broadcast(self, title: str = "", message: str = "", tags: str = "robot"):
        """Send a live update and wait for delivery (preserves message ordering)."""
        if self.local_broadcast:
            try:
                await self.local_broadcast(title, message)
            except Exception:
                pass
        try:
            if title == "":
                await self.http_client.post(BROADCAST_URL, content=f"{Colors.RESET}\n", timeout=15)
            else:
                current_time = datetime.datetime.now().strftime("%H:%M")
                color = TITLE_COLORS.get(title, Colors.RESET)
                full_message = f"🤖 {current_time} {color}[{title}] {message}{Colors.RESET}"
                await self.http_client.post(BROADCAST_URL, content=full_message.encode("utf-8"), timeout=15)
        except Exception:
            pass  # broadcast failures are non-critical

    async def __aenter__(self):
        await self._sync_registry()
        await self._ensure_workstream_indexes()
        await self._ensure_memory_indexes()
        await self._ensure_routing_decision_indexes()
        await self._resume_open_workstreams()
        # Background tasks:
        #   • filesystem watcher (mcp_servers/ changes)
        #   • workstream-closure watcher (triggers memory extraction)
        #   • memory decay sweep (slow timer, ages out unrecalled facts)
        self._watcher_task        = asyncio.create_task(self._watch_servers())
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

    async def _ensure_workstream_indexes(self):
        """Workstream collection is queried by state, last_activity, and
        (via Atlas Vector Search) by summary. Plain indexes are created
        here; the vector index needs the Atlas UI (see WHY_MONGODB.md)."""
        try:
            await self.workstreams.create_index(
                [("state", 1), ("last_activity", -1)],
                name="ws_state_recency",
            )
            await self.workstreams.create_index([("entities", 1)], name="ws_entities")
        except Exception as e:
            print(f"⚠️ workstream index ensure failed (non-fatal): {e}")

    async def _resume_open_workstreams(self):
        """Survives process restarts: load workstreams in state='open' and
        adopt the most-recent one as the current focus. The next user
        query's classifier will refine — but having last_domain populated
        from a real workstream means we resume context for free."""
        cursor = self.workstreams.find(
            {"state": "open"},
            {"_id": 1, "title": 1, "domain": 1, "last_activity": 1},
        ).sort("last_activity", -1).limit(10)
        open_ws = [d async for d in cursor]
        if not open_ws:
            return
        await self._broadcast("BOOTSTRAP",
            f"Resumed: {len(open_ws)} open workstream(s) — "
            + ", ".join(f"{w['_id']} ({w.get('domain', '?')})" for w in open_ws[:5]))
        # Adopt the most-recent one as the current focus
        focus = open_ws[0]
        self.current_workstream_id = focus["_id"]
        self.last_domain  = focus.get("domain")
        self.last_service = None  # service stickiness doesn't survive a restart

    # ─── Long-term memory layer ───────────────────────────────────────────
    #
    # When a workstream transitions to state=completed, extract reusable
    # facts and store them in agent_memories, vector-indexed for cross-
    # session recall. The ReAct loop pulls top-K relevant memories at the
    # start of each turn so past lessons inform current tool calls.
    #
    # Wired through three surfaces:
    #   • change-stream watcher (background, auto on closure)
    #   • catch-up pass at startup (handles closures while we were down)
    #   • workstream_service MCP tools (explicit user-facing recall)

    # ─── Routing-analytics helpers ────────────────────────────────────────

    def _decision_set(self, **kwargs):
        """Safely merge fields into the in-flight routing decision record.
        No-op when no record is active (e.g. tests, status command)."""
        if self._current_decision is None:
            return
        for k, v in kwargs.items():
            self._current_decision[k] = v

    def _decision_under(self, section: str, **kwargs):
        """Same as _decision_set but for a nested sub-document."""
        if self._current_decision is None:
            return
        slot = self._current_decision.setdefault(section, {})
        for k, v in kwargs.items():
            slot[k] = v

    async def _persist_decision(self, **outcome):
        """Insert the current routing-decision record into MongoDB and
        reset the slot. Called at every process_query exit point. Failures
        are logged and swallowed — analytics shouldn't break the user
        response."""
        if self._current_decision is None:
            return
        try:
            doc = self._current_decision
            if outcome:
                doc.setdefault("outcome", {}).update(outcome)
            await self.routing_decisions.insert_one(doc)
        except Exception as e:
            print(f"⚠️ routing-decision persist failed (non-fatal): {e}")
        finally:
            self._current_decision = None

    async def _ensure_routing_decision_indexes(self):
        try:
            await self.routing_decisions.create_index([("ts", -1)],
                name="rd_recency")
            await self.routing_decisions.create_index([("workstream_id", 1)],
                name="rd_by_ws")
            await self.routing_decisions.create_index(
                [("stage2.winner_services", 1)],
                name="rd_by_winner")
        except Exception as e:
            print(f"⚠️ routing-decision index ensure failed (non-fatal): {e}")

    async def _ensure_memory_indexes(self):
        try:
            await self.memories.create_index([("workstream_id", 1)], name="mem_by_ws")
            await self.memories.create_index([("domain", 1)],        name="mem_by_domain")
            await self.memories.create_index([("entities", 1)],      name="mem_by_entities")
            await self.memories.create_index([("extracted_at", -1)], name="mem_recency")
        except Exception as e:
            print(f"⚠️ memory index ensure failed (non-fatal): {e}")

    async def _watch_workstream_closures(self):
        """Background task: watch agent_workstreams change stream for
        state→completed transitions and trigger memory extraction. Resilient
        to driver/network blips; restarts the stream with backoff."""
        while True:
            try:
                stream = await self.workstreams.watch(full_document="updateLookup")
                async with stream:
                    async for change in stream:
                        if change.get("operationType") not in ("update", "replace"):
                            continue
                        doc = change.get("fullDocument") or {}
                        if doc.get("state") == "completed" \
                                and not doc.get("memories_extracted"):
                            t = asyncio.create_task(self._extract_memories(doc["_id"]))
                            self._memory_extract_tasks.add(t)
                            t.add_done_callback(self._memory_extract_tasks.discard)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"⚠️ workstream-closure watcher: {e}; retrying in 3s")
                await asyncio.sleep(3)

    async def _extract_backlog(self):
        """At boot, find any completed workstreams that didn't have memory
        extraction run on them (e.g. closed while the orchestrator was
        offline) and extract them now. Bounded — extracts the most recent
        few, not the whole archive, so a fresh DB clone doesn't burn LLM
        cost on history."""
        cursor = self.workstreams.find(
            {"state": "completed", "memories_extracted": {"$ne": True}},
            {"_id": 1},
        ).sort("last_activity", -1).limit(10)
        backlog = [d async for d in cursor]
        if not backlog:
            return
        await self._broadcast("MEMORY",
            f"💎 Extracting memories for {len(backlog)} closed workstream(s) "
            "(catch-up after restart)")
        for w in backlog:
            t = asyncio.create_task(self._extract_memories(w["_id"]))
            self._memory_extract_tasks.add(t)
            t.add_done_callback(self._memory_extract_tasks.discard)

    async def _extract_memories(self, ws_id: str):
        """LLM-extract reusable facts from a completed workstream and
        persist them in agent_memories. Marks the workstream as extracted
        so we don't repeat the work (or pay the LLM cost) on next restart.

        Concurrency-safe via an atomic claim: `_extract_backlog` (at boot)
        and `_watch_workstream_closures` (change-stream replay) can both
        queue extraction tasks for the same workstream after a restart
        that interrupted the previous run. `find_one_and_update` ensures
        only one task wins the race. Stale claims (>5min, e.g. when a
        process died mid-LLM) are reclaimable so we never lose a closure
        permanently."""
        now = datetime.datetime.now()
        stale_cutoff = now - datetime.timedelta(minutes=5)
        ws = await self.workstreams.find_one_and_update(
            {
                "_id": ws_id,
                "memories_extracted": {"$ne": True},
                "$or": [
                    {"memories_extraction_started_at": {"$exists": False}},
                    {"memories_extraction_started_at": None},
                    {"memories_extraction_started_at": {"$lt": stale_cutoff}},
                ],
            },
            {"$set": {"memories_extraction_started_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not ws:
            # Already claimed by another task (or already completed).
            return

        # Build a tight context for the LLM — title + summary + the most
        # informative tail of the tool-call audit. The point is *reusable*
        # knowledge, not transcript replay, so we keep the prompt small.
        recent_calls = (ws.get("tool_calls") or [])[-12:]
        call_lines = []
        for c in recent_calls:
            res = (c.get("result") or "").replace("\n", " ")[:180]
            call_lines.append(f"  - {c.get('service', '?')}__{c.get('tool', '?')} → {res}")

        prompt = (
            f"You are reviewing a closed workstream from a multi-agent system. "
            f"Extract REUSABLE facts that would help a future agent run faster "
            f"or more correctly on a similar task involving the same entities.\n\n"
            f"Workstream: {ws_id}\n"
            f"Title: {ws.get('title', '(untitled)')}\n"
            f"Domain: {ws.get('domain', '?')}\n"
            f"Entities: {', '.join(ws.get('entities') or []) or '(none)'}\n"
            f"Summary: {ws.get('summary', '') or '(empty)'}\n\n"
            f"Recent tool calls:\n" + "\n".join(call_lines) + "\n\n"
            f"Return 0 to 5 facts as JSON. Each fact should be a short, "
            f"declarative statement that names the entity/template/value and "
            f"why it matters. Examples of GOOD facts:\n"
            f"  • 'Alpenmarkt's standard retail SLA template is strict-retail-v3.'\n"
            f"  • 'Marienplatz site uses fiber uplink UP-MUC-MAR-F10; copper unavailable.'\n"
            f"  • 'POS latency target for German retail is 40ms; 80ms warning threshold.'\n"
            f"BAD facts (do NOT extract these — return fewer or zero facts "
            f"rather than padding with these):\n"
            f"  • Transient ids (specific intent ids, timestamps, datestamps) — they don't reuse.\n"
            f"  • Generic best practices the LLM already knows.\n"
            f"  • Operational data that's already in another collection.\n"
            f"  • META-FACTS ABOUT THE WORKSTREAM ITSELF — statements about "
            f"this workstream's id, state, last_activity, opened/closed time, "
            f"its title, or the fact that it was completed. These describe "
            f"the audit record, not the work. Examples to REJECT:\n"
            f"      ✗ 'Workstream WS-... was marked as completed.'\n"
            f"      ✗ 'The last activity on WS-... was on YYYY-MM-DD.'\n"
            f"      ✗ 'The state of WS-... was open before it was closed.'\n"
            f"      ✗ 'WS-... had the title \"foo\".'\n"
            f"  • Any fact whose entities array contains a WS-... id and "
            f"nothing else — that's a tell that the fact is about the "
            f"workstream itself rather than something useful.\n"
            f"  • Tool or system limitations — statements about what a tool "
            f"cannot do, data that cannot be retrieved, or actions that are "
            f"not supported (e.g. 'deleted tasks cannot be restored', "
            f"'the system does not support X'). These describe tool behaviour "
            f"the LLM already knows; they add no value when recalled.\n"
            f"  • Facts about transient user-created items that no longer exist "
            f"(deleted tasks, cancelled orders, cleared lists) — recalling "
            f"'User had tasks: Watch TV, Running' after they were all deleted "
            f"is noise, not signal.\n"
            f"  • Facts that would not help a brand-new agent on a brand-new "
            f"problem involving the same external entities.\n\n"
            f"If the workstream's tool calls were trivial (e.g. just listing "
            f"things, or closing itself) and there's nothing substantive to "
            f"distil, return {{\"facts\":[]}} — that's the correct answer.\n\n"
            f"JSON schema:\n"
            f'{{"facts":[{{"text":"...","category":"preference|template|target|config|playbook|lesson",'
            f'"entities":["..."],"confidence":0.0-1.0}}, ...]}}\n'
            f"Return {{\"facts\":[]}} if the workstream has nothing reusable to teach."
        )
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            payload = json.loads(resp.choices[0].message.content)
        except Exception as e:
            print(f"⚠️ memory extraction failed for {ws_id}: {e}")
            # Mark as attempted-but-empty so we don't retry forever
            await self.workstreams.update_one(
                {"_id": ws_id},
                {"$set": {"memories_extracted": True,
                          "memories_extracted_at": datetime.datetime.now(),
                          "memories_extracted_count": 0,
                          "memories_extraction_error": str(e)[:300]}},
            )
            return

        facts = payload.get("facts") or []
        # Sanity-filter the LLM output. Last line of defence against the LLM
        # mining workstream meta-facts despite the prompt's BAD-facts list:
        # any fact whose entities are ONLY workstream ids (WS-…) is the doc
        # talking about itself, not about useful institutional knowledge.
        _ws_id_re = re.compile(r"^WS-\d{4}-\d{2}-\d{2}-\d+$")
        clean = []
        for f in facts:
            text = (f.get("text") or "").strip()
            if not text or len(text) < 10 or len(text) > 600:
                continue
            ents = [e for e in (f.get("entities") or []) if isinstance(e, str)][:8]
            # Reject self-referential facts about this workstream
            ws_only_entities = ents and all(_ws_id_re.match(e) for e in ents)
            text_lower = text.lower()
            mentions_ws_id = _ws_id_re.search(text) is not None or (
                "ws-" in text_lower and ws_id.lower() in text_lower)
            ws_meta_phrases = (
                "marked as completed",
                "last activity",
                "was open before",
                "was 'open'",
                "state was",
                "had the title",
                "the workstream",
                "workstream was",
            )
            looks_like_meta = mentions_ws_id and any(
                p in text_lower for p in ws_meta_phrases)
            if ws_only_entities or looks_like_meta:
                continue
            clean.append({
                "text":       text,
                "category":   (f.get("category") or "fact").strip()[:30],
                "entities":   ents,
                "confidence": max(0.0, min(1.0, float(f.get("confidence", 0.5)))),
            })

        if clean:
            now = datetime.datetime.now()
            ws_seq = ws_id.replace("WS-", "")
            docs = [{
                "_id":              f"MEM-{ws_seq}-{i+1:02d}",
                "workstream_id":    ws_id,
                "text":             f["text"],
                "category":         f["category"],
                "entities":         f["entities"],
                "domain":           ws.get("domain"),
                "confidence":       f["confidence"],
                "extracted_at":     now,
                # Promotion / decay state — facts start in 'extracted' and
                # move to 'core' on enough recalls or 'decayed' on age.
                "tier":             "extracted",
                "recall_count":     0,
                "last_recalled_at": None,
            } for i, f in enumerate(clean)]
            try:
                await self.memories.insert_many(docs, ordered=False)
                await self._broadcast("MEMORY",
                    f"💎 Extracted {len(docs)} fact(s) from {ws_id}")
                for d in docs:
                    await self._broadcast("MEMORY",
                        f"   • [{d['category']}] {d['text'][:120]}")
            except Exception as e:
                # If the atomic claim raced (extremely rare) and a peer
                # task already inserted these exact docs, we get E11000.
                # Treat that as a benign duplicate-detection — the data
                # is already there. Anything else is a real error.
                if "E11000" in str(e) or "duplicate key" in str(e):
                    print(f"ℹ️  memory insert raced (dup keys) for {ws_id} — ignoring")
                else:
                    print(f"⚠️ memory insert failed for {ws_id}: {e}")
        else:
            await self._broadcast("MEMORY",
                f"💎 {ws_id} closed — no reusable facts extracted")

        await self.workstreams.update_one(
            {"_id": ws_id},
            {"$set": {"memories_extracted":       True,
                      "memories_extracted_at":    datetime.datetime.now(),
                      "memories_extracted_count": len(clean)}},
        )

    async def _recall_memories(self, query: str, domain: str | None = None,
                                entities: list[str] | None = None,
                                limit: int = 5,
                                include_decayed: bool = False) -> list[dict]:
        """Recall reusable facts relevant to the current context. Uses
        Atlas Vector Search on agent_memories.text when the index is
        configured; falls back to entity-overlap + recency otherwise so
        the demo always has something to surface.

        Side effects every call:
          • Increments `recall_count` and updates `last_recalled_at` for
            each hit (this drives the promotion lifecycle).
          • Promotes a fact to tier='core' when its recall_count crosses
            MEMORY_PROMOTE_THRESHOLD.
          • Resurrects a decayed fact back to 'extracted' if it gets
            recalled again.

        Decayed facts are filtered OUT by default — they're still in the
        collection (inspectable via list_memories) but the LLM doesn't
        see them in routine recall."""
        hits: list[dict] = []

        # Vector path first
        try:
            vs_spec = {
                "index":         "agent_memories_index",
                "path":          "text",
                "query":         query,
                "numCandidates": 50,
                "limit":         max(1, min(20, limit)),
            }
            flt: dict = {}
            if domain:
                flt["domain"] = {"$eq": domain}
            if not include_decayed:
                # Tier may be missing on older docs — match those as well
                flt["tier"] = {"$ne": "decayed"}
            if flt:
                vs_spec["filter"] = flt
            cursor = await self.memories.aggregate([
                {"$vectorSearch": vs_spec},
                {"$project": {
                    "_id": 1, "text": 1, "category": 1, "entities": 1,
                    "domain": 1, "confidence": 1, "workstream_id": 1,
                    "tier": 1, "recall_count": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }},
            ])
            hits = await cursor.to_list()
        except Exception:
            hits = []

        # Fallback: entity overlap + recency
        if not hits:
            q: dict = {}
            if domain:
                q["domain"] = domain
            if entities:
                q["entities"] = {"$in": entities}
            if not include_decayed:
                q["tier"] = {"$ne": "decayed"}
            cursor = self.memories.find(q).sort("extracted_at", -1).limit(limit)
            hits = [d async for d in cursor]

        # Update lifecycle state for each surfaced fact. Done as a single
        # bulk update so recall is still ~one network round-trip.
        if hits:
            await self._mark_memories_recalled(hits)
        return hits

    async def _recall_preferences(self, query: str,
                                   limit: int = 5) -> list[dict]:
        """
        Recall user-stated preferences from user_preferences that are
        relevant to the current query. Mirror of _recall_memories but
        for the USER plane:

          • agent_memories  → orchestrator's auto-extracted observations.
          • user_preferences → user's explicit self-disclosure
                               ('I love X', 'remember that I…').

        Both contribute to the system-prompt's 'you previously learned'
        block on every turn — that's what makes the demo's cross-session
        preference resolution real: the agent's LLM sees 'User loves to
        play basketball' on EVERY future turn until the user forgets it,
        regardless of how long ago they stated it.

        Atlas Vector Search path first (requires
        `user_preferences_index` on `text`, auto-embed via voyage-4).
        Falls back to recency on permanent preferences when the index
        isn't ready.

        Filters out temporary preferences (TTL-bounded context) by
        default — only stable preferences ride into long-running
        context.
        """
        hits: list[dict] = []

        # Vector path first.
        try:
            vs_spec = {
                "index":         "user_preferences_index",
                "path":          "text",
                "query":         query,
                "numCandidates": 50,
                "limit":         max(1, min(20, limit)),
                "filter":        {"is_temporary": {"$ne": True}},
            }
            cursor = await self.preferences.aggregate([
                {"$vectorSearch": vs_spec},
                {"$project": {
                    "_id":      1, "text": 1, "category": 1,
                    "is_temporary": 1, "createdAt": 1,
                    "score":    {"$meta": "vectorSearchScore"},
                }},
            ])
            hits = await cursor.to_list()
        except Exception:
            hits = []

        # Fallback: most-recent permanent preferences (no vector
        # similarity, but for small collections this still surfaces
        # the right facts — and the demo always renders something
        # even before the vector index is configured).
        if not hits:
            cursor = self.preferences.find(
                {"is_temporary": {"$ne": True}},
            ).sort("createdAt", -1).limit(limit)
            hits = [d async for d in cursor]
        return hits

    async def _mark_memories_recalled(self, hits: list[dict]):
        """Bump recall_count + last_recalled_at on each hit; promote to
        'core' when the threshold is crossed; resurrect decayed facts."""
        now = datetime.datetime.now()
        promoted: list[dict] = []
        resurrected: list[dict] = []
        for h in hits:
            mem_id        = h.get("_id")
            current_tier  = h.get("tier") or "extracted"
            current_count = int(h.get("recall_count") or 0)
            new_count     = current_count + 1

            update_ops: dict = {
                "$inc": {"recall_count": 1},
                "$set": {"last_recalled_at": now},
            }

            # Promotion: crossed the threshold and not already core
            if new_count >= MEMORY_PROMOTE_THRESHOLD and current_tier != "core":
                update_ops["$set"]["tier"] = "core"
                # Floor confidence — core facts are institutional knowledge
                if (h.get("confidence") or 0.0) < MEMORY_CORE_CONFIDENCE_FLOOR:
                    update_ops["$set"]["confidence"] = MEMORY_CORE_CONFIDENCE_FLOOR
                promoted.append(h)

            # Resurrection: a decayed fact got recalled, restore it
            elif current_tier == "decayed":
                update_ops["$set"]["tier"] = "extracted"
                resurrected.append(h)

            try:
                await self.memories.update_one({"_id": mem_id}, update_ops)
                # Reflect the new state on the in-memory hit so callers
                # (broadcast formatter, system-prompt builder) see it.
                h["recall_count"] = new_count
                if "tier" in update_ops["$set"]:
                    h["tier"] = update_ops["$set"]["tier"]
                if "confidence" in update_ops["$set"]:
                    h["confidence"] = update_ops["$set"]["confidence"]
            except Exception as e:
                print(f"⚠️ memory recall-state update failed for {mem_id}: {e}")

        for p in promoted:
            await self._broadcast("MEMORY",
                f"⭐ Promoted to CORE ({p.get('recall_count')} recalls): "
                f"{(p.get('text') or '')[:120]}")
        for r in resurrected:
            await self._broadcast("MEMORY",
                f"🌱 Resurrected (recalled again): {(r.get('text') or '')[:100]}")

    async def _decay_memories_sweep(self) -> int:
        """Mark stale unrecalled extracted memories as 'decayed' and lower
        their confidence. Runs at startup and on a slow background timer.
        Returns the number of facts touched (for broadcast)."""
        cutoff = (datetime.datetime.now()
                  - datetime.timedelta(seconds=MEMORY_DECAY_AGE_SECONDS))
        # Use updateMany so the sweep is one network round-trip regardless
        # of how many memories are due for decay.
        try:
            res = await self.memories.update_many(
                {
                    "tier": {"$in": ["extracted", None]},
                    "recall_count": {"$in": [0, None]},
                    "extracted_at": {"$lt": cutoff},
                },
                [
                    {"$set": {
                        "tier": "decayed",
                        "decayed_at": datetime.datetime.now(),
                        "confidence": {
                            "$multiply": [
                                {"$ifNull": ["$confidence", 0.5]},
                                MEMORY_DECAY_CONFIDENCE_FACTOR,
                            ]
                        },
                    }}
                ],
            )
            n = res.modified_count or 0
        except Exception as e:
            print(f"⚠️ memory decay sweep failed: {e}")
            return 0
        if n:
            await self._broadcast("MEMORY",
                f"🍂 Decayed {n} stale fact(s) (unrecalled for "
                f"{MEMORY_DECAY_AGE_SECONDS // 86400}+ days)")
        return n

    async def _memory_decay_loop(self):
        """Background task that runs the decay sweep on a slow timer.
        Cheap because the sweep is one updateMany; safe to run forever."""
        # Run once shortly after boot so the live feed shows the line
        await asyncio.sleep(5)
        await self._decay_memories_sweep()
        while True:
            try:
                await asyncio.sleep(MEMORY_DECAY_SWEEP_INTERVAL_SEC)
                await self._decay_memories_sweep()
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"⚠️ memory decay loop: {e}; retrying in 60s")
                await asyncio.sleep(60)

    async def _watch_servers(self):
        """Re-sync registry whenever a .py file in mcp_servers/ is added,
        changed, or deleted. watchfiles debounces rapid saves automatically."""
        try:
            async for _ in awatch(self.server_dir, watch_filter=lambda _, p: p.endswith(".py")):
                await self._sync_registry()
        except asyncio.CancelledError:
            pass

    def _extract_docstring(self, file_path: Path) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
                return ast.get_docstring(tree) or f"Service: {file_path.stem}"
        except:
            return f"Service: {file_path.stem}"

    # Stopwords for the text-match tiebreaker — content words only.
    _TM_STOPWORDS = frozenset({
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at",
        "for", "with", "from", "by", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "should", "can", "could", "we", "us", "you", "i", "me", "my", "our",
        "your", "where", "what", "when", "how", "why", "this", "that",
        "these", "those", "it", "if", "as", "so", "than", "then", "into",
        "out", "up", "down", "over", "under", "again", "now", "very",
    })

    def _text_match_score(self, query: str, description: str) -> float:
        """Score a service by how many distinctive query phrases literally
        appear in its embedded description (which contains its trigger
        phrases verbatim). Longer n-gram matches are worth more — a 3-token
        overlap is a much stronger signal than three isolated tokens.

        Used as a deterministic Stage 2 tiebreaker before the LLM call:
        when voyage-4 collapses sibling services into a < 0.001 cosine
        band, literal phrase overlap with the trigger phrases is the
        objective signal that disambiguates them.
        """
        q_lc = query.lower()
        d_lc = description.lower()
        q_tokens = re.findall(r"[a-z0-9.]+", q_lc)
        q_tokens = [t for t in q_tokens
                    if t not in self._TM_STOPWORDS and len(t) >= 2]
        if not q_tokens:
            return 0.0
        # Build a STOPWORD-STRIPPED form of the description so "run simulation"
        # in the query matches "run the simulation" in the description — the
        # article-vs-no-article gap is a common source of false misses.
        d_tokens = re.findall(r"[a-z0-9.]+", d_lc)
        d_tokens = [t for t in d_tokens
                    if t not in self._TM_STOPWORDS and len(t) >= 2]
        d_stripped = " ".join(d_tokens)

        score = 0.0
        # 4-, 3-, 2-gram matches (longer wins more). Same n-gram counted once.
        for n in (4, 3, 2):
            seen: set = set()
            for i in range(len(q_tokens) - n + 1):
                phrase = " ".join(q_tokens[i:i + n])
                if phrase in seen:
                    continue
                if phrase in d_stripped:
                    seen.add(phrase)
                    score += n * n  # 16, 9, 4 per match
        # Plus single-token matches — weakest signal, distinct tokens only.
        d_token_set = set(d_tokens)
        for t in set(q_tokens):
            if t in d_token_set:
                score += 1
        return score

    def _extract_discriminator(self, full_docstring: str, server_name: str) -> str:
        """
        Build the text that gets EMBEDDED for Stage 2 vector routing.

        Architectural beat: voyage-4 (and any bi-encoder) collapses sibling
        services to within 0.001-0.005 of each other when the embedded text
        is dominated by shared exposition — "ACME", "QoS", "plan", "scenario",
        "downlink" all appear in every DTW service description. The tie-break
        LLM then has to disambiguate on every query, which is slow and
        stochastic.

        The fix: stop embedding the exposition. Embed only:
          1. The one-line service tagline (what it IS)
          2. The verbatim trigger phrases from the "Use this service when"
             section (what users SAY)

        This makes each service's embedding a centroid of expected user
        queries. Cosine similarity becomes sharply discriminative: scenario
        descriptions land closer to scenario-service triggers than to
        simulation-service triggers, and the score gap widens enough to skip
        the tie-break entirely.

        Negative-scope guards ("This service does NOT…", "🚫 NOT this
        service") are stripped — they describe sibling services and pollute
        the embedding with the wrong vocabulary.

        Returns a string formatted as:
            <tagline>
            Users invoke this with queries like:
            <quoted trigger phrases, one per line>
        """
        if not full_docstring or not full_docstring.strip():
            return f"Service: {server_name}"

        lines = full_docstring.splitlines()

        # 1. Tagline — first non-empty line, drop "Title — " prefix if any.
        tagline = ""
        for ln in lines:
            s = ln.strip()
            if s:
                tagline = s
                break
        if " — " in tagline:
            tagline = tagline.split(" — ", 1)[1].strip()
        elif tagline.startswith("SERVER:"):
            tagline = tagline[len("SERVER:"):].strip()

        # 2. Trigger phrases — content of the "Use this service when" section,
        # stopping at the first paragraph break (blank line) or negative guard.
        # The trigger section is a bulleted list; trailing prose after it must
        # be excluded or it pollutes the embedding with shared vocabulary.
        trigger_lines: list[str] = []
        in_section = False
        for ln in lines:
            s = ln.strip()
            if re.search(r"use this service when", s, re.I):
                in_section = True
                continue
            if not in_section:
                continue
            # Negative guards / cross-service notes — hard stop.
            if (s.startswith("🚫")
                or re.match(r"this service (does not|is not|operates|only)", s, re.I)
                or re.match(r"both .* tools accept", s, re.I)):
                break
            # Blank line after content → end of bullet block, prose follows.
            if not s and trigger_lines:
                break
            # Skip leading blanks before the first bullet.
            if not s:
                continue
            # Prose break: line is not a bullet and not a quoted continuation.
            if trigger_lines and not (s.startswith("-") or s.startswith('"')):
                break
            trigger_lines.append(s)

        if not trigger_lines:
            # No "Use this service when" section — fall back to the previous
            # behaviour: full text up to the negative guards, capped at 800.
            text = full_docstring
            for marker in ("\n🚫 NOT this service",
                           "\nThis service does NOT",
                           "\nThis service is NOT"):
                idx = text.find(marker)
                if idx > 0:
                    text = text[:idx]
            return text.strip()[:800]

        triggers = "\n".join(trigger_lines)
        result = (
            f"{tagline}\n\n"
            f"Users invoke this with queries like:\n{triggers}"
        )
        # 1500-char cap — trigger sections are typically 400-1000 chars; this
        # leaves headroom while still keeping the embedding focused.
        return result[:1500]

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute hash of file content to detect changes"""
        try:
            with open(file_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except:
            return ""

    def _infer_domain(self, server_name: str) -> str:
        """
        Derive a domain tag from a service name with a zero-friction rule:
        take the first underscore-separated token. Service files following
        a prefix convention (ibn_*, dtw_*, acc_*, portfolio_*) cluster
        naturally; singleton services (preferences_service, restaurant_guide,
        incident_analyzer) become their own one-member domain.

        This means new services join an existing domain just by being named
        with the right prefix — no docstring or registry edit required.
        """
        stem = server_name.strip()
        return stem.split("_", 1)[0] if "_" in stem else stem

    def _embed_for_index(self, text: str) -> list:
        """Compute a voyage-4 embedding with input_type='document'.

        This is the index-time call. voyage-4 is an asymmetric retrieval
        model: it prepends "Represent the document for retrieval: " to the
        text before vectorising. Using this with input_type='query' for
        searches gives ~5-10× larger gap between correct and incorrect
        matches than the symmetric (input_type=None) form that Atlas
        autoEmbed uses under the hood.
        """
        resp = self.voyage.embed([text], model=self.embed_model,
                                 input_type="document")
        return resp.embeddings[0]

    def _embed_for_query(self, text: str) -> list:
        """Compute a voyage-4 embedding with input_type='query'.

        Used at $vectorSearch time. The asymmetric prompt is what gives
        voyage-4 its retrieval quality — without it, queries and short
        documents collapse to nearly the same region of vector space.
        """
        resp = self.voyage.embed([text], model=self.embed_model,
                                 input_type="query")
        return resp.embeddings[0]

    async def _sync_registry(self):
        """Smart sync: Add new, update changed, remove deleted MCP servers"""
        if not self.server_dir.exists():
            print(f"⚠️ MCP server directory not found: {self.server_dir}")
            return

        # Scan local filesystem
        server_files = [f for f in self.server_dir.glob("*.py") if f.name != "__init__.py"]
        local_servers = {}

        for f in server_files:
            server_name = f.stem
            full_doc   = self._extract_docstring(f)
            short_desc = self._extract_discriminator(full_doc, server_name)
            file_hash  = self._compute_file_hash(f)

            local_servers[server_name] = {
                "server_name":     server_name,
                # `description` is the *embedded* field — short, focused,
                # the discriminator content only. Atlas vector_index embeds
                # this on insert/update and on every $vectorSearch.query.
                "description":     short_desc,
                # `full_description` is the LLM tie-break input — full
                # docstring with trigger-phrase lists and scope guards.
                "full_description": full_doc,
                "domain":           self._infer_domain(server_name),
                "file_hash":        file_hash,
                "last_seen":        datetime.datetime.now().isoformat()
            }

        await self._broadcast() # newline
        await self._broadcast("BOOTSTRAP", f"Found {len(local_servers)} local MCP servers")

        # Fetch current registry from MongoDB
        db_servers = {
            doc["server_name"]: doc
            async for doc in self.collection.find({}, {"_id": 0})
        }

        # Restore cloud-sourced servers to temp dir so they can be activated
        cloud_servers = {
            name: doc for name, doc in db_servers.items()
            if doc.get("origin") == "cloud" and doc.get("source_code")
        }
        for name, doc in cloud_servers.items():
            p = self.temp_dir / f"{name}.py"
            p.write_text(doc["source_code"])

        await self._broadcast("BOOTSTRAP", f"Found {len(db_servers)} servers in registry "
                              f"({len(cloud_servers)} cloud-managed)")

        # Compute diff — cloud servers are never auto-deleted by local sync
        local_names = set(local_servers.keys())
        db_names    = set(n for n, d in db_servers.items() if d.get("origin") != "cloud")

        new_servers = local_names - db_names
        deleted_servers = db_names - local_names
        potential_updates = local_names & db_names

        # Check for actual changes (hash comparison) OR missing-field
        # backfill needed (registry doc predates a schema upgrade — missing
        # `domain` after the two-stage routing upgrade, missing
        # `full_description` after the embedded-discriminator split, or
        # missing `description_embedding` after the asymmetric-retrieval
        # upgrade that bypasses Atlas autoEmbed).
        changed_servers = set()
        for name in potential_updates:
            local_hash = local_servers[name]["file_hash"]
            db_hash   = db_servers[name].get("file_hash", "")
            db_doc    = db_servers[name]
            needs_backfill = (
                not db_doc.get("domain")
                or not db_doc.get("full_description")
                or not db_doc.get("description_embedding")
                # Re-embed when the discriminator logic changed even if the
                # file itself didn't — description drift without hash drift.
                or db_doc.get("description") != local_servers[name]["description"]
            )
            if local_hash != db_hash or needs_backfill:
                changed_servers.add(name)

        # Compute voyage-4 document embeddings for every new or changed
        # service. One blocking API call per service (parallelizable later);
        # at demo scale this is a handful of services on startup.
        services_to_embed = list(new_servers | changed_servers)
        if services_to_embed:
            await self._broadcast("BOOTSTRAP",
                f"🧬 Embedding {len(services_to_embed)} service "
                f"description(s) with voyage-4 (input_type='document')…")
            for name in services_to_embed:
                try:
                    vec = await asyncio.to_thread(
                        self._embed_for_index,
                        local_servers[name]["description"])
                    local_servers[name]["description_embedding"] = vec
                except Exception as e:
                    # One bad embedding shouldn't kill the whole sync. Drop
                    # the service from this round; it'll be retried on the
                    # next sync (filewatcher hit, restart, etc.).
                    await self._broadcast("BOOTSTRAP",
                        f"⚠ Embedding failed for {name}: {e!s} — skipping "
                        f"this service this round")
                    new_servers.discard(name)
                    changed_servers.discard(name)

        # Sync operations
        total_changes = len(new_servers) + len(changed_servers) + len(deleted_servers)

        if total_changes == 0:
            await self._broadcast("BOOTSTRAP", "✓ Registry up-to-date (no changes)")
            await self._broadcast_registry_summary()
            return

        #print(f"\n🔄 Syncing changes:")

        # 1. Add new servers
        if new_servers:
            await self._broadcast("BOOTSTRAP", f"➕ Adding {len(new_servers)} new server(s):")
            for name in new_servers:
                await self.collection.insert_one(local_servers[name])
                await self._broadcast("BOOTSTRAP", f"    + {name}")

        # 2. Update changed servers
        if changed_servers:
            await self._broadcast("BOOTSTRAP", f"🔄 Updating {len(changed_servers)} changed server(s):")
            for name in changed_servers:
                await self.collection.update_one(
                    {"server_name": name},
                    {"$set": local_servers[name]}
                )
                if name in self.sessions:
                    del self.sessions[name]
                    self.tool_cache.pop(name, None)
                await self._broadcast("BOOTSTRAP", f"    ↻ {name} (session evicted, will reload on next query)")

        # 3. Remove deleted servers
        if deleted_servers:
            await self._broadcast("BOOTSTRAP", f"🗑️  Removing {len(deleted_servers)} deleted server(s):")
            for name in deleted_servers:
                await self.collection.delete_one({"server_name": name})
                if name in self.sessions:
                    del self.sessions[name]
                    self.tool_cache.pop(name, None)
                await self._broadcast("BOOTSTRAP", f"    - {name}")

        await self._broadcast("BOOTSTRAP", f"✓ Registry sync complete")
        await self._broadcast_registry_summary()
        await self._broadcast()  # newline

    async def _broadcast_registry_summary(self):
        """Emit the canonical 'Registry: N services in M domains — …' line
        from the *actual post-sync state* of agent_registry.mcp_services
        (so adds/updates/deletes that just landed are reflected)."""
        cursor = await self.collection.aggregate([
            {"$group": {"_id": "$domain", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ])
        rows = await cursor.to_list()
        if not rows:
            return
        total = sum(r["n"] for r in rows)
        breakdown = ", ".join(f"{r['_id'] or '(none)'}({r['n']})" for r in rows)
        await self._broadcast("BOOTSTRAP",
            f"Registry: {total} services in {len(rows)} domains — {breakdown}")

    async def _semantic_search(self, query: str, limit: int = 5,
                               domains: List[str] | None = None) -> List[Dict]:
        """Stage 2: vector search, optionally pre-filtered to one or more
        domains. Uses ASYMMETRIC voyage-4 retrieval: query is embedded with
        input_type='query', documents were embedded at index time with
        input_type='document'. This is voyage-4's design and produces
        sharply discriminative scores — Atlas autoEmbed does not do this
        and collapses sibling services into a noise band.

        Falls back to unfiltered search if Atlas rejects the filter
        (index hasn't been re-configured to include `domain` yet) — flips
        a flag so we don't keep trying."""
        # Embed the query off-thread so the event loop stays responsive.
        query_vector = await asyncio.to_thread(self._embed_for_query, query)

        def _build_pipeline(filter_doc: dict | None):
            vs: dict = {
                "index":         "vector_index",
                "path":          "description_embedding",
                "queryVector":   query_vector,
                "numCandidates": 50,
                "limit":         limit,
            }
            if filter_doc:
                vs["filter"] = filter_doc
            return [
                {"$vectorSearch": vs},
                {"$project": {
                    "_id": 0, "server_name": 1, "description": 1,
                    "full_description": 1, "domain": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }},
            ]

        use_filter = domains and self._domain_filter_supported
        pipeline = _build_pipeline({"domain": {"$in": domains}} if use_filter else None)

        try:
            cursor = await self.collection.aggregate(pipeline)
            return await cursor.to_list()
        except Exception as e:
            msg = str(e).lower()
            # Atlas raises an OperationFailure if a filter field isn't declared
            # on the index. Disable the filter for the rest of the session,
            # warn once, and retry unfiltered so the demo keeps working.
            if use_filter and ("filter" in msg or "field" in msg or "path" in msg):
                self._domain_filter_supported = False
                await self._broadcast("ROUTING",
                    "⚠ Atlas vector_index has no `domain` filter — "
                    "running Stage 2 unfiltered. Add `domain` as a filter "
                    "field in the Atlas UI to enable hierarchical scoping.")
                cursor = await self.collection.aggregate(_build_pipeline(None))
                return await cursor.to_list()
            raise

    async def _list_domains(self) -> Dict[str, List[Dict]]:
        """Return {domain: [{server_name, description}, …]} — Stage 1 input."""
        cursor = self.collection.find(
            {}, {"_id": 0, "server_name": 1, "description": 1, "domain": 1},
        )
        by_domain: Dict[str, List[Dict]] = {}
        async for doc in cursor:
            d = doc.get("domain") or self._infer_domain(doc["server_name"])
            by_domain.setdefault(d, []).append(doc)
        return by_domain

    # ─── Workstream layer ─────────────────────────────────────────────────
    #
    # A workstream is a coherent thread of activity — the user opening a
    # store, the user running a what-if simulation, the user shopping.
    # Routing is workstream-anchored: classification picks (or creates) a
    # workstream first; that workstream supplies the sticky domain hint
    # and the in-context entities for the rest of the routing pipeline.
    # Workstreams persist in MongoDB, so killing main.py mid-session and
    # restarting picks up the work exactly where it left off.

    def _next_workstream_id(self) -> str:
        """Allocate WS-YYYY-MM-DD-NNN, monotonic per day."""
        today = datetime.date.today().isoformat()
        # Use a count of today's workstreams + 1
        return None  # placeholder; the real id is allocated via _create_workstream

    async def _open_workstreams_for_classifier(
            self, limit: int = 12,
            domain_filter: List[str] | None = None,
    ) -> List[Dict]:
        """Compact list of workstreams for the classifier prompt — open ones
        first (so 'continue' candidates are obvious) plus a few recently-
        completed ones (so 'replay from X' candidates are reachable). Most-
        recent-first; capped because the prompt has to stay small.

        domain_filter, when provided, scopes the OPEN candidates to that
        domain set. The CLOSED candidates are intentionally NOT filtered,
        because cross-domain replay is legitimate (e.g. replay an IBN
        setup as the recipe for a DTW scenario)."""
        proj = {"_id": 1, "title": 1, "domain": 1, "entities": 1,
                "summary": 1, "last_activity": 1, "state": 1}
        open_query: Dict = {"state": "open"}
        if domain_filter:
            # Include workstreams matching the domain filter AND workstreams
            # with no domain assigned (created before routing was stable) —
            # we can't rule them out without knowing their domain.
            open_query["$or"] = [
                {"domain": {"$in": list(domain_filter)}},
                {"domain": None},
                {"domain": ""},
                {"domain": "—"},
            ]
        open_cur = self.workstreams.find(open_query, proj) \
                                    .sort("last_activity", -1).limit(limit)
        open_ws = [d async for d in open_cur]
        # Pad with recently-completed workstreams so the classifier can
        # nominate them as replay sources without scanning the whole archive.
        # NOTE: closed pool is NOT domain-filtered — replay can cross domains.
        remaining = max(0, limit - len(open_ws))
        if remaining > 0:
            closed_cur = self.workstreams.find(
                {"state": "completed"}, proj,
            ).sort("last_activity", -1).limit(remaining)
            open_ws.extend([d async for d in closed_cur])
        return open_ws

    async def _classify_workstream(
            self, query: str, recent_user_msgs: List[str],
            domain_filter: List[str] | None = None,
    ) -> tuple[str | None, bool, str | None, str | None, bool, List[str]]:
        """
        Classify the query into an open workstream or signal that a new
        one should be created. Returns
            (workstream_id, is_new, domain_hint,
             replay_source_id, was_pure_closure, closed_ids).

        For 'new', the orchestrator allocates the id; the classifier only
        suggests a title + domain.

        domain_filter, when provided (typically the Stage 1 domain
        classification result), scopes the OPEN-workstream candidate
        set to those domains. This prevents the classifier from
        picking, say, an open IBN workstream as the continuation
        target for a TODO query, even if titles overlap.
        """
        open_ws = await self._open_workstreams_for_classifier(
            domain_filter=domain_filter)

        # ── Fast-path: pure-closure heuristic ─────────────────────────────
        # When the query is an unambiguous goodbye ("done with TODOs",
        # "we're finished", "wrap up"), skip the classifier LLM call
        # entirely. Only acts on OPEN workstreams — a closure cue with
        # nothing currently open is a no-op, NOT a reaffirmation of the
        # most-recently-closed workstream.
        #
        # When the cue names a topic ('done with TODOs'), ALL open
        # workstreams matching that topic are closed in a single turn —
        # the LLM classifier can leave multiple open workstreams in the
        # same domain (legacy state, stochastic misclassification, race
        # on concurrent first-turn queries), and a goodbye should clean
        # them all up rather than leaving stragglers.
        if self._is_pure_closure_cue(query):
            open_only = [w for w in open_ws if w.get("state") == "open"]
            if not open_only:
                await self._broadcast("WORKSTREAM",
                    "⏸ Closure-only query — no open workstreams; nothing "
                    "to close (LLM skipped)")
                return (None, False, None, None, True, [])

            topic = self._extract_closure_topic(query)
            if topic:
                targets = [w for w in open_only
                           if self._workstream_matches_topic(w, topic)]
                if not targets:
                    # Topic was specific but no open workstream matches.
                    # Don't randomly close something unrelated.
                    await self._broadcast("WORKSTREAM",
                        f"⏸ Closure cue mentions '{topic}' but no matching "
                        f"open workstream; nothing closed (LLM skipped)")
                    return (None, False, None, None, True, [])
            else:
                # Generic closure ("we're done", "that's it") — be
                # conservative, close only the most-recently-active
                # open workstream rather than nuking unrelated work.
                targets = [open_only[0]]

            closed_ids: List[str] = []
            for t in targets:
                await self._close_workstream(t["_id"],
                    reason="closure cue inferred from query")
                closed_ids.append(t["_id"])

            if len(closed_ids) == 1:
                await self._broadcast("WORKSTREAM",
                    f"⏸ Closure-only query — closed {closed_ids[0]} "
                    f"(LLM skipped)")
            else:
                await self._broadcast("WORKSTREAM",
                    f"⏸ Closure-only query — closed "
                    f"{len(closed_ids)} workstreams: "
                    f"{', '.join(closed_ids)} (LLM skipped)")

            primary = targets[0]
            return (primary["_id"], False, primary.get("domain"),
                    None, True, closed_ids)

        # No open workstreams → trivially a new one (no replay candidate)
        if not open_ws:
            title, domain_hint = await self._propose_new_workstream(query)
            if domain_filter and len(domain_filter) == 1:
                domain_hint = domain_filter[0]
            ws_id = await self._create_workstream(title, domain_hint, query)
            return ws_id, True, domain_hint, None, False, []

        # Build compact context for the LLM, GROUPED BY STATE.
        # Closed workstreams must NEVER be picked for action=continue or
        # closes_workstream — they are reference-only candidates for
        # replay_from_workstream. Mixing them in a single list led the
        # LLM to pick closed workstreams with better-matching titles as
        # continuation targets, diverging the orchestrator's
        # current_workstream_id from the actual DB state.
        open_subset = [w for w in open_ws if w.get("state") == "open"]
        closed_subset = [w for w in open_ws if w.get("state") == "completed"]

        def _fmt_ws(w):
            ents = ", ".join((w.get("entities") or [])[:5])
            summary = (w.get("summary") or "").strip().replace("\n", " ")
            summary = summary[:200] + "…" if len(summary) > 200 else summary
            return (
                f"- {w['_id']} [{w.get('domain', '?')}] "
                f"{w.get('title', '(untitled)')}\n"
                f"    entities: {ents or '(none)'}\n"
                f"    summary: {summary or '(empty)'}"
            )

        open_block = "\n".join(_fmt_ws(w) for w in open_subset) or "(none)"
        closed_block = "\n".join(_fmt_ws(w) for w in closed_subset) or "(none)"

        recent_block = ""
        if recent_user_msgs:
            recent = " | ".join(m[:80] for m in recent_user_msgs[-3:])
            recent_block = f"\n\nRecent user turns: {recent}"

        prompt = (
            f"User query: '{query}'{recent_block}\n\n"
            f"OPEN workstreams (eligible for action='continue' AND "
            f"'closes_workstream'):\n{open_block}\n\n"
            f"RECENTLY-CLOSED workstreams (eligible for "
            f"'replay_from_workstream' ONLY — these are REFERENCE/"
            f"REPLAY sources, NEVER pick them as workstream_id for "
            f"continue or closes_workstream):\n{closed_block}\n\n"
            f"Decide THREE things at once:\n"
            f"  1. Which workstream this query continues (or whether the user "
            f"     is starting a NEW one).\n"
            f"  2. Whether the user is signaling that an open workstream is "
            f"     now DONE — implicitly or explicitly.\n"
            f"  3. Whether the user wants to REPLAY the action sequence from "
            f"     a past workstream onto the current/new one.\n\n"
            f"Reply with valid JSON only, no prose:\n"
            f"{{\n"
            f"  \"action\": \"continue\" | \"new\",\n"
            f"  \"workstream_id\": \"WS-...\",         // when action=continue;\n"
            f"                                          // MUST be an id from the\n"
            f"                                          // OPEN section above.\n"
            f"                                          // Closed ids are FORBIDDEN here.\n"
            f"  \"title\": \"<short title>\",          // when action=new; describe\n"
            f"                                          // the OVERALL GOAL or topic,\n"
            f"                                          // NOT the literal query verb.\n"
            f"                                          // 'what are my TODOs' → 'Manage\n"
            f"                                          // personal TODOs'.  'set up\n"
            f"                                          // Marienplatz network' →\n"
            f"                                          // 'Marienplatz network setup'.\n"
            f"  \"domain_hint\": \"<domain>\",         // when action=new\n"
            f"  \"closes_workstream\": \"WS-...\",     // workstream the user\n"
            f"                                          // just signaled DONE, or null\n"
            f"  \"replay_from_workstream\": \"WS-...\"  // source workstream whose\n"
            f"                                          // tool-call sequence should\n"
            f"                                          // be re-run onto this turn's\n"
            f"                                          // context, or null\n"
            f"}}\n\n"
            f"Rules for action:\n"
            f"- ⛔ HARD RULE: action='continue' requires workstream_id to be "
            f"  an id from the OPEN section above. NEVER pick a closed "
            f"  workstream id for continuation — even if its title matches "
            f"  better. Closed workstreams are HISTORY, not active threads.\n"
            f"- If the query continues an open workstream (mentions its entities, "
            f"  uses its vocabulary, or is a natural follow-up to that thread), "
            f"  prefer 'continue'.\n"
            f"- Brief acknowledgements + follow-ups ('ok thanks', 'now do X') "
            f"  after a recent turn in a workstream are continuations.\n"
            f"- ⚠️ DOMAIN-LEVEL CONTINUITY: a workstream is a CONTAINER of "
            f"  related actions, not a single action. Routine CRUD inside a "
            f"  domain that already has an open workstream IS continuation, "
            f"  even when the query mentions a new item-level entity.\n"
            f"  Examples (all 'continue', not 'new'):\n"
            f"    • open WS [todo] 'Manage TODOs' + 'add watching TV to my tasks'\n"
            f"    • open WS [todo] 'Manage TODOs' + 'delete task #3'\n"
            f"    • open WS [todo] 'Manage TODOs' + 'mark #4 complete'\n"
            f"    • open WS [ibn]  'Marienplatz setup' + 'what's the feasibility status'\n"
            f"  Only return 'new' when the user changes the GOAL or DOMAIN — "
            f"  a clear topic switch ('now let's look at IBN', 'switch to "
            f"  Hamburg setup', 'forget TODOs, let's plan dinner'). A new "
            f"  TODO item, a new task id, a new tag — those are workstream "
            f"  CONTENTS, not new workstreams.\n\n"
            f"Rules for closes_workstream:\n"
            f"- Set to the relevant workstream id when the user signals "
            f"  COMPLETION of work. Examples that close a workstream:\n"
            f"  • 'I'm done with the setup of Marienplatz network'\n"
            f"  • 'we are done', 'wrap up', 'that's everything for X'\n"
            f"  • 'close the Munich workstream'\n"
            f"- The query can BOTH close one workstream AND continue/start "
            f"  another in the same turn: set both fields accordingly.\n"
            f"- Leave null if the user is still in the middle of work.\n"
            f"\n"
            f"⚠️ CRITICAL: when the query is PURELY a closure / "
            f"acknowledgement with no new task to start ('done with X', "
            f"'I'm finished', 'we're wrapping up', 'that's it'), prefer:\n"
            f"    action      = 'continue'\n"
            f"    workstream_id = <the workstream being closed>\n"
            f"    closes_workstream = <same id>\n"
            f"Do NOT set action='new' with a generic recap title like "
            f"'Manage X' or 'Working on X' — that fabricates a workstream "
            f"out of a goodbye. Only set action='new' when the user "
            f"introduces a substantive new task to do (a new entity, a "
            f"new verb that implies new work).\n\n"
            f"Rules for replay_from_workstream:\n"
            f"- Set to a source workstream id when the user wants to apply "
            f"  the SAME ACTION SEQUENCE to a new entity. Examples:\n"
            f"  • 'set up Hamburg the same way as Munich' → replay from WS-…(Munich)\n"
            f"  • 'do the same for the Berlin branch'\n"
            f"  • 'follow the pattern from the Marienplatz workstream'\n"
            f"  • 'repeat what we did for Alpenmarkt'\n"
            f"- The source may be a closed OR open workstream.\n"
            f"- Leave null when the user isn't asking to replicate anything.\n"
        )
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=180,
                response_format={"type": "json_object"},
            )
            decision = json.loads(resp.choices[0].message.content)
        except Exception as e:
            await self._broadcast("ROUTING",
                f"⚠ Workstream classify failed ({e}); using most-recent open WS")
            ws = open_ws[0]
            return ws["_id"], False, ws.get("domain"), None, False, []

        # Honour an implicit close cue BEFORE deciding routing. Closing
        # a workstream triggers the change-stream watcher → memory
        # extraction; the new turn carries on with whatever action follows.
        close_id = decision.get("closes_workstream")
        if close_id and any(w["_id"] == close_id for w in open_ws):
            await self._close_workstream(close_id,
                reason="user-signaled completion in query")

        # Validate the replay source — it has to be a workstream we showed
        # the classifier (open or recently closed) and have a tool_calls trail.
        replay_id = decision.get("replay_from_workstream")
        if replay_id and not any(w["_id"] == replay_id for w in open_ws):
            replay_id = None

        action = (decision.get("action") or "").lower()

        # Helper: was close_id pointing at a genuinely-open workstream?
        # (The block above may have just closed it; check pre-close state.)
        close_target = next((w for w in open_ws if w["_id"] == close_id), None) \
            if close_id else None
        close_was_open = bool(close_target
                              and close_target.get("state") == "open")

        # Post-LLM closure safety net. The upfront _is_pure_closure_cue
        # heuristic catches short, unambiguous goodbyes; this catches
        # the longer closures the LLM identified via its own prompt
        # rules (e.g. "I'm done with the setup of Marienplatz network"
        # — 10 words, over the heuristic's 8-word cap). If the LLM set
        # closes_workstream on an OPEN workstream AND chose action='new',
        # treat as pure closure rather than fabricating a workstream
        # out of a goodbye.
        if action == "new" and close_was_open:
            await self._broadcast("WORKSTREAM",
                f"⏸ Closure intent recognized — using {close_id} for "
                f"context, not opening a new workstream")
            return (close_id, False, close_target.get("domain"),
                    replay_id, True, [close_id])

        if action == "continue":
            ws_id = decision.get("workstream_id")
            ws = next((w for w in open_ws if w["_id"] == ws_id), None)

            # Valid continuation requires: (1) known id, (2) state=='open',
            # (3) not the workstream we just closed this turn. ANY failure
            # in (2) is a serious classifier bug — closed workstreams are
            # forbidden as continuation targets, even when their titles
            # match better.
            if ws and ws_id != close_id and ws.get("state") == "open":
                closed = [close_id] if close_was_open else []
                return ws["_id"], False, ws.get("domain"), replay_id, False, closed

            # Failure mode A: LLM picked a CLOSED workstream as continuation.
            # Try to redirect to an open workstream in the same domain.
            if ws and ws.get("state") == "completed" and ws_id != close_id:
                await self._broadcast("ROUTING",
                    f"⚠ Classifier picked CLOSED {ws_id} for continue; "
                    f"that's forbidden — looking for open redirect in "
                    f"domain '{ws.get('domain')}'")
                domain = ws.get("domain")
                same_domain_open = [w for w in open_subset
                                    if w.get("domain") == domain]
                if len(same_domain_open) == 1:
                    redirect = same_domain_open[0]
                    await self._broadcast("WORKSTREAM",
                        f"↪ Redirected to open {redirect['_id']} "
                        f"(only open workstream in '{domain}')")
                    closed = [close_id] if close_was_open else []
                    return (redirect["_id"], False, redirect.get("domain"),
                            replay_id, False, closed)
                if len(same_domain_open) > 1:
                    # Multiple opens in the same domain — most-recent wins.
                    redirect = same_domain_open[0]
                    await self._broadcast("WORKSTREAM",
                        f"↪ Redirected to open {redirect['_id']} "
                        f"(most-recent of {len(same_domain_open)} open "
                        f"workstreams in '{domain}')")
                    closed = [close_id] if close_was_open else []
                    return (redirect["_id"], False, redirect.get("domain"),
                            replay_id, False, closed)
                # No open workstream in that domain — fall through.

            # Failure mode B: hallucinated id or self-closed. Two sub-cases:
            #   (a) close_id pointed at an OPEN workstream — LLM intended
            #       a closure but gave a bad continuation id. Treat as
            #       pure closure with close_id as the context target.
            #   (b) no close_id (or close_id was already closed) — pure
            #       hallucination. Fall through to new-workstream creation.
            if close_was_open:
                await self._broadcast("WORKSTREAM",
                    f"⏸ Closure intent recognized (continue→bad id) — "
                    f"using {close_id} for context")
                return (close_id, False, close_target.get("domain"),
                        replay_id, True, [close_id])
            if not ws:
                # Hallucinated id with no closure intent. Before
                # fabricating a fresh workstream out of thin air, try
                # to redirect to ANY open workstream already in scope
                # (Stage 1 narrowed the candidate set; the LLM likely
                # meant ONE of those but got the id wrong).
                open_in_scope = [w for w in open_ws
                                 if w.get("state") == "open"]
                if open_in_scope:
                    redirect = open_in_scope[0]
                    await self._broadcast("ROUTING",
                        f"⚠ Classifier hallucinated id {ws_id!r}; "
                        f"redirected to open {redirect['_id']} "
                        f"(most-recent open workstream in Stage 1 scope)")
                    return (redirect["_id"], False,
                            redirect.get("domain"), replay_id, False, [])
                await self._broadcast("ROUTING",
                    f"⚠ Workstream classify returned unknown id {ws_id!r}; opening new WS")

        # "new" (or fell through)
        title = decision.get("title") or query[:60]
        domain_hint = decision.get("domain_hint") or None
        if domain_hint and domain_hint not in [w.get("domain") for w in open_ws]:
            # Validate against the actual domain set
            known_domains = set((await self._list_domains()).keys())
            if domain_hint not in known_domains:
                domain_hint = None
        # When Stage 1 was definitive (single domain), trust it over the
        # classifier LLM's domain_hint — the LLM can be misled by content
        # vocabulary (e.g. "add Hamburg metrics to todos" → 'analytics'
        # instead of 'todo') and cause spurious merges into the wrong WS.
        if domain_filter and len(domain_filter) == 1:
            domain_hint = domain_filter[0]
        ws_id = await self._create_workstream(title, domain_hint, query)
        # If the LLM also asked to close a workstream this turn, report it.
        closed = [close_id] if close_was_open else []
        return ws_id, True, domain_hint, replay_id, False, closed

    async def _propose_new_workstream(self, query: str) -> tuple[str, str | None]:
        """LLM call to derive a title + domain hint when there are no open
        workstreams to compare against. Cheap, called rarely."""
        domains_block = ", ".join((await self._list_domains()).keys())
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": (
                    f"User query: '{query}'\n\n"
                    f"Known domains: {domains_block}\n\n"
                    f"Propose a short descriptive title for this new workstream "
                    f"(max 60 chars) and the best-fit domain.\n\n"
                    f"The title should describe the OVERALL GOAL or topic, "
                    f"NOT the literal query verb. A workstream is a CONTAINER "
                    f"of related actions, so the title should generalize from "
                    f"the first query to the broader thread of work it opens.\n"
                    f"Examples:\n"
                    f"  • 'what are my TODOs'                → 'Manage personal TODOs'\n"
                    f"  • 'add watching TV to my tasks'      → 'Manage personal TODOs'\n"
                    f"  • 'set up the Marienplatz network'   → 'Marienplatz network setup'\n"
                    f"  • 'simulate raising prepaid bandwidth' → 'ACME prepaid QoS what-if'\n"
                    f"  • 'add an Italian restaurant nearby' → 'Dining recommendations'\n\n"
                    f"Reply with JSON: {{\"title\": \"...\", \"domain\": \"...\"}}"
                )}],
                temperature=0,
                max_tokens=80,
                response_format={"type": "json_object"},
            )
            d = json.loads(resp.choices[0].message.content)
            return d.get("title") or query[:60], d.get("domain")
        except Exception:
            return query[:60], None

    async def _create_workstream(self, title: str, domain: str | None,
                                  seed_query: str) -> str:
        """
        Allocate or merge a workstream and return its id.

        Safety-merge invariant: at most ONE open workstream per
        (domain, entity-set) without explicit override. If the
        classifier returned action='new' but a same-domain open
        workstream already exists, this method:

        - Extracts candidate entities from the new title + seed_query
        - Compares against the existing workstream's entities
        - If the new work introduces no NEW entity → MERGE into the
          existing workstream (broadcast ⤴), return its id.
        - If a fully-new entity is present → allow split (broadcast
          🌿), create the new workstream as usual.

        This is the structural fix for the "two workstreams for two
        TODO tasks" failure mode: the LLM classifier is stochastic and
        sometimes returns action='new' on routine CRUD; the data layer
        now refuses to fabricate a duplicate workstream when there's
        nothing materially new to track.
        """
        # ─── Safety-merge ─────────────────────────────────────────────────
        if domain:
            existing = await self.workstreams.find_one(
                {"domain": domain, "state": "open"},
                sort=[("last_activity", -1)],
            )
            if existing:
                existing_entities = set(existing.get("entities") or [])
                candidate_entities = self._extract_potential_entities(
                    f"{title} {seed_query or ''}")
                new_entities = candidate_entities - existing_entities
                if not new_entities:
                    # Routine continuation — merge.
                    await self.workstreams.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {"last_activity": datetime.datetime.now()}},
                    )
                    await self._broadcast("WORKSTREAM",
                        f"⤴ Merged into open {existing['_id']} — "
                        f"same domain '{domain}', no new entity introduced "
                        f"(classifier wanted 'new', data layer refused "
                        f"to duplicate)")
                    return existing["_id"]
                # New entity present — legitimate split. Carry on and
                # create a new workstream, but mark the relationship.
                await self._broadcast("WORKSTREAM",
                    f"🌿 New workstream in domain '{domain}' alongside "
                    f"{existing['_id']} (new entity: "
                    f"{', '.join(sorted(new_entities))})")

        # ─── Allocate id and insert ──────────────────────────────────────
        today = datetime.date.today().isoformat()
        # Count today's workstreams to allocate a per-day sequence number
        count_today = await self.workstreams.count_documents(
            {"_id": {"$regex": f"^WS-{today}-"}}
        )
        ws_id = f"WS-{today}-{count_today + 1:03d}"
        now = datetime.datetime.now()
        # Seed with entities extracted from the seed query so the
        # safety-merge on the NEXT same-domain attempt has something
        # to compare against.
        seed_entities = sorted(self._extract_potential_entities(
            f"{title} {seed_query or ''}"))
        doc = {
            "_id":            ws_id,
            "title":          title[:120],
            "domain":         domain,
            "entities":       seed_entities,
            "state":          "open",
            "opened_at":      now,
            "last_activity":  now,
            "summary":        f"Started: {seed_query[:200]}",
            "tool_calls":     [],
            "turn_count":     0,
        }
        await self.workstreams.insert_one(doc)
        await self._broadcast("WORKSTREAM",
            f"🆕 {ws_id} opened — {title}" + (f" [{domain}]" if domain else ""))
        return ws_id

    async def _close_workstream(self, ws_id: str, reason: str = "completed"):
        """Mark a workstream completed. Triggers the change-stream watcher
        which kicks off long-term memory extraction in the background. Called
        from the workstream classifier when it detects implicit close cues
        like 'I am done with the setup of Marienplatz network'."""
        res = await self.workstreams.update_one(
            {"_id": ws_id, "state": {"$ne": "completed"}},
            {"$set": {"state": "completed",
                      "closed_at": datetime.datetime.now(),
                      "close_note": reason}},
        )
        if res.modified_count:
            await self._broadcast("WORKSTREAM",
                f"✓ {ws_id} closed — {reason}")
            # If the user closed the currently-focused workstream, drop
            # focus and let the next classifier decision adopt a new one.
            if self.current_workstream_id == ws_id:
                self.current_workstream_id = None

    # Tool-name prefixes that should NEVER be replayed onto a new context.
    # These are either read-only intel calls (whose results don't carry
    # over) or destructive verbs (re-running them would undo work).
    _REPLAY_SKIP_PREFIXES = (
        "list_", "get_", "show_", "describe_", "find_", "recall_",
        "peek_", "inspect_", "diff_", "diagnose_", "compare_", "estimate_",
        "cancel_", "delete_", "remove_", "forget_", "drop_",
    )

    @classmethod
    def _is_replayable_tool(cls, tool_name: str) -> bool:
        t = (tool_name or "").lower()
        return not any(t.startswith(p) for p in cls._REPLAY_SKIP_PREFIXES)

    # Multi-word closure phrases. The standalone word "done" is deliberately
    # NOT in this list — it matches benign queries like "delete done todos"
    # or "show me what's done". The phrases here are unambiguous closure
    # cues that cannot be misread as anything else.
    _CLOSURE_PATTERNS = (
        "done with",
        "all done",
        "i'm done", "im done", "i am done",
        "we're done", "were done", "we are done",
        "i'm finished", "im finished", "i am finished",
        "we're finished", "we are finished",
        "let's wrap up", "lets wrap up",
        "wrap up", "wrap-up", "wrapping up",
        "that's it", "thats it",
        "that's all", "thats all",
    )
    _QUESTION_STARTERS = (
        "are ", "is ", "do ", "did ", "does ", "have ", "has ",
        "can ", "could ", "should ", "will ", "would ", "may ", "might ",
        "why ", "when ", "where ", "what ", "what's ", "whats ",
        "who ", "how ",
    )

    # Item-reference patterns. When any of these appears in a query the
    # closure heuristic refuses to fire — the query is about an
    # individual item (a TODO task, an IBN intent, a DTW scenario,
    # etc.), not a workstream as a whole.
    _ITEM_REF_PATTERNS = (
        # '#5', 'task #2', 'TODO #2', '#42' — numeric item ids
        r"#\d+",
        # 'IBN-005', 'DTW-SCN-003', 'MEM-2026-...' — typed entity ids
        r"\b[A-Z]{2,}-\d",
        # 'WS-2026-05-23-001' (just in case the user names a workstream id)
        r"\bWS-\d{4}-\d{2}-\d{2}-\d{3}",
    )

    @classmethod
    def _is_pure_closure_cue(cls, query: str) -> bool:
        """
        Detect short, unambiguous closure cues. Used as an upfront
        fast-path in _classify_workstream to skip the classifier LLM
        call entirely on goodbye turns like "done with TODOs".

        Anti-patterns:
          - questions ('?' suffix or 'are/is/do/...' prefix)
          - long queries (>8 words — likely mixed intent)
          - ITEM REFERENCES: any of '#\\d+', 'XX-NNN', 'WS-...'.
            'done with task #2' is an item-level completion — the
            user wants a complete_todo(2) call on todo_service, not
            a workstream close. Letting the closure short-circuit
            swallow those queries would silently mark the wrong
            workstream done and never touch the item itself.
        """
        q_lower = (query or "").strip().lower()
        if not q_lower or len(q_lower.split()) > 8:
            return False
        if q_lower.endswith("?"):
            return False
        if any(q_lower.startswith(s) for s in cls._QUESTION_STARTERS):
            return False
        # Item-reference guard: match against the ORIGINAL query (case-
        # preserving) for typed ids like 'IBN-005', then against the
        # lowercased query for the numeric form '#N'.
        original = (query or "").strip()
        if any(re.search(p, original) for p in cls._ITEM_REF_PATTERNS):
            return False
        return any(p in q_lower for p in cls._CLOSURE_PATTERNS)

    # Stopwords stripped from closure topic hints before matching.
    # Keep small — over-aggressive removal kills real topic words.
    _CLOSURE_STOPWORDS = frozenset({
        "the", "and", "for", "with", "from", "into", "this", "that",
        "all", "any", "our", "your", "their", "have", "has", "now",
        "today", "tonight", "here", "there",
    })

    @classmethod
    def _extract_closure_topic(cls, query: str) -> str:
        """
        Extract the TOPIC portion from a closure cue. 'done with TODOs'
        → 'todos'. 'we're finished with the Marienplatz setup' →
        'marienplatz setup'. Returns '' for generic closures with no
        topic ('done', 'we're done', 'that's it', 'wrap up').

        The topic is what process_query / the fast-path uses to decide
        WHICH open workstream(s) to close: substring-match against
        each workstream's domain, title, and entities.
        """
        q = (query or "").strip().lower().rstrip("?.!")
        if not q:
            return ""
        # Patterns: "<verb-phrase> [with|on|the] <topic>" or
        #           "<topic> is/are done|finished".
        patterns = (
            # "I'm done with X" / "we are done with X" / "all done with X"
            r"^(?:i'?m|we'?re|we are|i am|all|let'?s|lets)?\s*"
            r"(?:done|finished|complete|completed)\s+"
            r"(?:with|on|about)\s+(.+)$",
            # "wrap up X" / "wrap-up X" / "wrapping up X"
            r"^(?:let'?s|lets)?\s*wrap(?:ping)?[-\s]?up\s+(.+)$",
            # "X is/are done|finished"
            r"^(.+?)\s+(?:is|are)\s+(?:done|finished|complete|completed)$",
            # "no more X"
            r"^no\s+more\s+(.+)$",
        )
        import re as _re
        for pat in patterns:
            m = _re.match(pat, q)
            if m:
                topic = m.group(1).strip()
                # Reject degenerate captures
                if topic and topic not in ("it", "that", "all", "this"):
                    return topic
        return ""

    # ─── Meta / introspection queries ────────────────────────────────────
    # A workstream represents a thread of goal-directed work. Queries that
    # only inspect orchestrator state ("list my workstreams", "what's in
    # memory", "routing analytics") are NOT workstream-worthy: they
    # shouldn't open a new workstream and they shouldn't pollute an
    # existing workstream's tool_calls audit with read-only meta-tool
    # calls. _is_meta_query is the upfront heuristic; _META_TOOL_PREFIXES
    # backs a retro-detach guard for cases the heuristic missed.
    # Workstream-related meta queries are caught by the categorical
    # rule in _is_meta_query (any mention of 'workstream' → meta).
    # This list covers the OTHER meta categories (memory, routing,
    # services), where a simple noun-match would over-trigger.
    _META_QUERY_PATTERNS = (
        # Memory introspection (read-side)
        "what do you remember", "what's in memor", "whats in memor",
        "list memorie", "list memor", "list my memor",
        "show memorie", "show me memor", "show memor", "show my memor",
        "my memorie", "my memories",
        "recall fact", "recall everything", "recall all",
        # Memory bulk management (write-side)
        "forget memor", "forget all memor", "forget everything",
        "clear memor", "clear all memor", "purge memor",
        "reset memor", "wipe memor", "delete all memor",
        # Routing analytics
        "routing analytic", "routing summary", "routing stat",
        "routing performance", "routing metric", "routing miss",
        "any routing miss", "service usage", "slow routing",
        "any slow routing", "how is the routing", "how is routing",
        "show me routing", "show routing",
        # Service introspection
        "list service", "show service", "which service",
        "available service", "what service",
    )

    # Read-only meta tools — if EVERY tool a turn called matches one of
    # these prefixes, retro-detach (don't append to a workstream).
    _META_TOOL_PREFIXES = (
        "list_workstream", "close_workstream", "list_memor",
        "recall_fact", "forget_memor",
        "routing_summary", "routing_misses", "slow_routing",
        "service_usage",
    )

    @classmethod
    def _is_meta_query(cls, query: str) -> bool:
        """
        Detect introspection / observability queries that should NOT
        open or attach to any workstream.

        Rule (categorical, ends whack-a-mole):
          ① Any query containing the literal word "workstream" is meta.
             Workstreams are an INTERNAL concept of the orchestrator;
             if the user names them in a query, they're operating on
             the agent's state machine — not doing domain work. This
             subsumes every workstream-related variant ('list', 'close
             all', 'delete all completed', 'how many', 'what's the
             title of WS-...', etc.) without a pattern enumeration.
          ② Memory and routing-analytics queries are matched via the
             _META_QUERY_PATTERNS list with narrower phrasing rules
             (to avoid false positives like 'remember to buy milk').

        Closure ergonomics: natural-language goodbyes ('done with X',
        "we're finished") DON'T mention 'workstream' — they go through
        the regular classifier's closure short-circuit path.

        Domain queries are unaffected: 'add a TODO', 'set up
        Marienplatz', 'simulate QoS uplift' contain none of the meta
        signals.
        """
        q = (query or "").strip().lower()
        if not q:
            return False
        # ① Categorical: any mention of 'workstream' (singular or plural).
        if "workstream" in q:
            return True
        # ② Narrower phrasing rules for memory + analytics.
        return any(p in q for p in cls._META_QUERY_PATTERNS)

    # Known proper-noun entity names that the demo data uses. Used by
    # _extract_potential_entities for the safety-merge check at
    # workstream creation time AND by _attach_to_workstream's per-call
    # entity capture. Extending this list improves the merge decision —
    # a new entity in the query is a signal that the user genuinely
    # wants a separate workstream (e.g. 'set up Hamburg' vs the open
    # Munich workstream).
    _KNOWN_ENTITY_NAMES = (
        # IBN demo sites / customers
        "Marienplatz", "Schwabing", "Altona", "Mitte", "Königstraße",
        "Alpenmarkt", "ACME",
        # Cities (German)
        "Munich", "München", "Hamburg", "Berlin", "Frankfurt",
        "Stuttgart", "Cologne", "Köln", "Düsseldorf", "Leipzig",
        "Bremen", "Dresden", "Hannover", "Nuremberg", "Nürnberg",
        # Cities (other)
        "London", "Paris", "Madrid", "Rome", "Vienna", "Amsterdam",
        "Brussels", "Warsaw", "Zurich", "Geneva",
    )

    @classmethod
    def _extract_potential_entities(cls, text: str) -> set:
        """
        Cheap entity extractor: ID-shaped tokens (IBN-005, DTW-SCN-003,
        WS-2026-…) plus a hardcoded list of known site/place names.
        Conservative on purpose — false positives here would split
        workstreams that should merge.
        """
        if not text:
            return set()
        cands = set(re.findall(r"\b([A-Z][A-Z0-9]+-[A-Z0-9-]+)\b", text))
        for name in cls._KNOWN_ENTITY_NAMES:
            if name in text:
                cands.add(name)
        return cands

    @classmethod
    def _is_meta_tool(cls, tool_name: str) -> bool:
        """True iff this tool name is a read-only meta tool."""
        t = (tool_name or "").lower()
        return any(t.startswith(p) or p in t for p in cls._META_TOOL_PREFIXES)

    @classmethod
    def _all_tools_are_meta(cls, tool_names: List[str]) -> bool:
        """
        True iff every tool in `tool_names` matches a meta prefix.
        Used by the retro-detach guard at the end of process_query
        to suppress workstream tool_calls appends for turns the
        upfront heuristic missed.
        """
        if not tool_names:
            return False
        return all(cls._is_meta_tool(t) for t in tool_names)

    @classmethod
    def _workstream_matches_topic(cls, ws: dict, topic: str) -> bool:
        """
        True iff the workstream's domain, title, or entities contain
        any significant word from the closure topic. Plural-aware:
        'todos' matches a workstream with 'todo' in its haystack.
        """
        if not topic:
            return False
        import re as _re
        hint_words = [
            w for w in _re.findall(r"\w+", topic.lower())
            if len(w) >= 3 and w not in cls._CLOSURE_STOPWORDS
        ]
        if not hint_words:
            return False
        haystack = " ".join((
            (ws.get("domain") or "").lower(),
            (ws.get("title") or "").lower(),
            " ".join(ws.get("entities") or []).lower(),
        ))
        for w in hint_words:
            if w in haystack:
                return True
            # Plural ↔ singular tolerance
            if w.endswith("s") and w[:-1] in haystack:
                return True
            if not w.endswith("s") and (w + "s") in haystack:
                return True
        return False

    async def _build_replay_recipe(self, source_ws_id: str,
                                    target_workstream_id: str) -> str:
        """Format a successful past tool-call sequence as a 'recipe' the
        ReAct loop can follow on a new target. Filters out read-only and
        destructive verbs — only the *constructive* sequence is replayed.

        Returns a multi-line string suitable for injection into the
        system prompt, or '' if the source has nothing replayable."""
        source = await self.workstreams.find_one(
            {"_id": source_ws_id},
            {"_id": 1, "title": 1, "domain": 1, "entities": 1,
             "tool_calls": 1, "summary": 1, "state": 1})
        if not source:
            await self._broadcast("REPLAY",
                f"⚠ replay source {source_ws_id} not found; ignoring")
            return ""

        calls = source.get("tool_calls") or []
        replayable = [c for c in calls if self._is_replayable_tool(c.get("tool"))]
        if not replayable:
            await self._broadcast("REPLAY",
                f"⚠ {source_ws_id} has no constructive tool calls to replay")
            return ""

        skipped = len(calls) - len(replayable)
        await self._broadcast("REPLAY",
            f"🔁 Replaying {len(replayable)} step(s) from {source_ws_id} "
            + (f"(skipping {skipped} read-only/undo call(s))" if skipped else ""))

        step_lines = []
        for i, c in enumerate(replayable, 1):
            res = (c.get("result") or "").replace("\n", " ")[:140]
            step_lines.append(
                f"  Step {i}: `{c.get('service')}.{c.get('tool')}` — {res}"
            )
            await self._broadcast("REPLAY",
                f"   {i}. {c.get('service')}.{c.get('tool')}")

        return (
            f"\n\nREPLAY RECIPE: The user is asking you to repeat a "
            f"previously-successful sequence of actions onto a new target. "
            f"Source workstream {source_ws_id} ('{source.get('title')}') "
            f"executed these tool calls in order:\n"
            + "\n".join(step_lines) + "\n\n"
            f"Now execute the SAME sequence for the user's current request, "
            f"adapting the arguments to the new entities mentioned in the "
            f"user's query. Follow the exact tool order. If a step's "
            f"argument depends on the output of an earlier step (e.g. an "
            f"intent id), use the id returned by your own previous tool "
            f"call in THIS turn, not the source workstream's old id. "
            f"Skip a step only if it is genuinely not applicable to the "
            f"new context."
        )

    async def _attach_to_workstream(self, ws_id: str, query: str,
                                     service: str | None, tool: str | None,
                                     result_excerpt: str | None):
        """Append the just-executed tool call to the workstream's audit
        trail and bump last_activity. Also extracts simple entity hints
        from the result for future routing context."""
        update: Dict = {
            "$set":  {"last_activity": datetime.datetime.now()},
            "$inc":  {"turn_count": 1},
        }
        if service and tool:
            call_doc = {
                "ts":      datetime.datetime.now(),
                "service": service,
                "tool":    tool,
                "query":   query[:200],
                "result":  (result_excerpt or "")[:300],
            }
            update["$push"] = {"tool_calls": {"$each": [call_doc], "$slice": -50}}
        # Cheap entity extraction — see _extract_potential_entities for
        # the regex + allowlist. Centralised so the safety-merge check
        # in _create_workstream sees the same entity set we attach here.
        entity_candidates = self._extract_potential_entities(
            f"{query} {result_excerpt or ''}")
        if entity_candidates:
            update.setdefault("$addToSet", {})["entities"] = {
                "$each": sorted(entity_candidates)
            }
        await self.workstreams.update_one({"_id": ws_id}, update)

    async def _update_workstream_summary(self, ws_id: str, query: str,
                                          response: str):
        """Lazily rewrite the workstream summary after each turn. Runs in
        the background so it doesn't block the user's response. Caps the
        running summary at a sensible length so the classifier prompt stays
        small. Persisted so killing the process mid-stream keeps it intact."""
        ws = await self.workstreams.find_one(
            {"_id": ws_id}, {"summary": 1, "title": 1, "domain": 1})
        if not ws:
            return
        prev = ws.get("summary") or ""
        prompt = (
            f"Workstream title: {ws.get('title')}\n"
            f"Previous summary: {prev}\n\n"
            f"Latest turn:\n"
            f"  User: {query[:400]}\n"
            f"  Assistant: {response[:400]}\n\n"
            f"Rewrite a concise running summary (max 300 chars) that captures "
            f"what's been done, what entities are involved, and what's left. "
            f"No prose preamble — just the summary text."
        )
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
            )
            new_summary = resp.choices[0].message.content.strip()[:600]
            await self.workstreams.update_one(
                {"_id": ws_id}, {"$set": {"summary": new_summary}})
        except Exception as e:
            # Summary update is non-critical — don't break the chat
            print(f"⚠️ workstream summary update failed for {ws_id}: {e}")

    async def _classify_domain(self, query: str,
                               sticky_hint: str | None = None) -> List[str]:
        """
        Stage 1: classify the query into one or more domain tags. Cheap
        gpt-4o-mini call against a *small* taxonomy (domains, not services),
        which is what lets the routing pipeline scale by tree depth rather
        than by leaf count. If only one domain exists in the registry, skip.
        """
        stage1_t0 = time.monotonic() if hasattr(self, "_current_decision") and self._current_decision else None
        by_domain = await self._list_domains()
        if not by_domain:
            self._decision_under("stage1", method="no_domains", duration_ms=0,
                                 domains_selected=[])
            return []
        if len(by_domain) == 1:
            only = next(iter(by_domain))
            n = len(by_domain[only])
            label = "service" if n == 1 else "services"
            await self._broadcast("ROUTING", f"Stage 1 → {only} ({n} {label})")
            self._decision_under("stage1",
                method="singleton",
                domains_available=list(by_domain.keys()),
                domains_selected=[only],
                duration_ms=int((time.monotonic() - stage1_t0) * 1000) if stage1_t0 else None)
            return [only]

        # Deterministic pre-check: if the user typed a literal domain name
        # as a word in the query, trust that — it's an explicit selection
        # signal that overrides sticky bias and skips the LLM call entirely.
        # Catches cases like 'ibn feasibility check!' after a topic switch
        # to todo, where the LLM would otherwise stay in todo because the
        # add_todo tool can plausibly accept any text.
        ql = query.lower()
        # Plural-aware: 'workstream' domain matches both 'workstream'
        # and 'workstreams' in the query. Without this, 'delete all
        # workstreams' fails to fire the explicit-mention shortcut
        # because '\bworkstream\b' has a word-boundary after the
        # 'm', not after the 's'.
        explicit = [d for d in by_domain
                    if re.search(rf"\b{re.escape(d.lower())}s?\b", ql)]
        if explicit:
            total = sum(len(by_domain[d]) for d in explicit)
            label = "service" if total == 1 else "services"
            scope = ', '.join(f"{d}({len(by_domain[d])})" for d in explicit) \
                    if len(explicit) > 1 else f"{explicit[0]} ({total} {label})"
            await self._broadcast("ROUTING",
                f"Stage 1 → {scope}  (explicit domain mention)")
            self._decision_under("stage1",
                method="explicit_mention",
                domains_available=list(by_domain.keys()),
                domains_selected=explicit,
                sticky_hint=sticky_hint,
                duration_ms=int((time.monotonic() - stage1_t0) * 1000) if stage1_t0 else None)
            return explicit

        # Build a compact taxonomy for the LLM (sent in the prompt only, NOT
        # broadcast — the BOOTSTRAP line already enumerates the taxonomy once
        # for the audience).
        #
        # Each domain's blurb is derived entirely from the service docstrings
        # stored in MongoDB — no hardcoded per-domain knowledge here.
        # Structure per domain:
        #   taglines  — first-line tagline of every member service
        #   triggers  — quoted example phrases extracted from each service's
        #               "Use this service when" section (up to MAX_TRIGGERS
        #               per service, MAX_TRIGGERS*5 per domain)
        # The trigger phrases are the single source of truth for routing
        # vocabulary; keep them maintained in the service docstrings.

        def _extract_triggers(desc: str, max_per_service: int = 6) -> list[str]:
            """Return quoted trigger phrases from 'Use this service when' section."""
            in_section = False
            found: list[str] = []
            for line in desc.splitlines():
                s = line.strip()
                if re.search(r"use this service when", s, re.I):
                    in_section = True
                    continue
                if in_section:
                    if re.match(r"this service (does not|is not|operates)", s, re.I):
                        break
                    for phrase in re.findall(r'"([^"]{3,50})"', s):
                        kw = phrase.split(",")[0].strip()
                        if kw and kw not in found:
                            found.append(kw)
                            if len(found) >= max_per_service:
                                return found
            return found

        lines = []
        for d, members in sorted(by_domain.items()):
            members_str = ", ".join(m["server_name"] for m in members[:5])
            taglines: list[str] = []
            all_triggers: list[str] = []
            for m in members[:5]:
                desc = (m.get("description") or "").strip()
                if not desc:
                    continue
                first_line = next((ln for ln in desc.splitlines() if ln.strip()), "")
                if " — " in first_line:
                    first_line = first_line.split(" — ", 1)[1].strip()
                elif first_line.startswith("SERVER:"):
                    first_line = first_line[len("SERVER:"):].strip()
                if first_line:
                    taglines.append(first_line[:80])
                for t in _extract_triggers(desc):
                    if t not in all_triggers:
                        all_triggers.append(t)
            blurb = " · ".join(taglines) if taglines else "(no description)"
            if all_triggers:
                blurb += "  |  e.g. " + ", ".join(f'"{t}"' for t in all_triggers[:20])
            lines.append(f"- {d}: {blurb}  [services: {members_str}]")
        taxonomy = "\n".join(lines)

        # Single prompt regime — "soft sticky": session context is an
        # *inclusion bias*, not a lock. The classifier should:
        #   • Include the session domain in the candidate set when the
        #     query could plausibly continue the session.
        #   • Also include any other domain whose content matches the
        #     query strongly (cross-vocabulary queries, topic switches).
        #   • Up to 3 domains total; Stage 2 vector search picks the
        #     right service from the union — overmatching is cheap,
        #     undermatching is a routing miss.
        if sticky_hint:
            hint = (
                f"\n\nSESSION CONTEXT: The user's recent activity has been "
                f"in the '{sticky_hint}' domain. Include '{sticky_hint}' "
                f"in your candidate set whenever the query could plausibly "
                f"continue that work — even if the query's words also fit "
                f"another domain. Do NOT exclude '{sticky_hint}' on grounds "
                f"of vocabulary alone; the user's intent is more informative "
                f"than surface keywords."
            )
            directive = (
                f"Return 1-3 domains.\n"
                f" • Always include '{sticky_hint}' when the query could "
                f"continue the session (continuation cues like 'plan', "
                f"'check', 'activate', 'list', 'show', 'next', 'and also X' "
                f"are extensions, not topic changes).\n"
                f" • Also include any other domain whose tagline strongly "
                f"matches the query content.\n"
                f" • Omit '{sticky_hint}' only when the query is a clear "
                f"topic switch — names an entity / domain identifier from "
                f"elsewhere, or uses vocabulary that has NO plausible "
                f"reading in any '{sticky_hint}' service."
            )
        else:
            hint = ""
            directive = (
                "If the query plausibly fits multiple domains (mixed "
                "vocabulary, ambiguous scope), return up to 3 domains. "
                "Stage 2 vector search will pick the right service from "
                "the union — better to overmatch slightly than miss the "
                "right domain. If the query clearly belongs to one domain, "
                "return just that one."
            )

        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"User query: '{query}'\n\n"
                        f"Available domains:\n{taxonomy}{hint}\n\n"
                        f"{directive}\n"
                        f"Reply with domain name(s) only, comma-separated. "
                        f"Never return 'NONE'."
                    ),
                }],
                temperature=0,
                max_tokens=30,
            )
            raw = resp.choices[0].message.content.strip()
        except Exception as e:
            await self._broadcast("ROUTING",
                f"⚠ Stage 1 LLM call failed ({e}); using sticky/fallback")
            fallback = [sticky_hint] if sticky_hint and sticky_hint in by_domain \
                                      else [next(iter(by_domain))]
            self._decision_under("stage1",
                method="llm_failed_fallback",
                domains_available=list(by_domain.keys()),
                domains_selected=fallback,
                sticky_hint=sticky_hint,
                duration_ms=int((time.monotonic() - stage1_t0) * 1000) if stage1_t0 else None)
            return fallback

        candidates = [d.strip() for d in raw.split(",") if d.strip()]
        valid = [d for d in candidates if d in by_domain]
        if not valid:
            await self._broadcast("ROUTING",
                f"⚠ Stage 1: unknown domain(s) {candidates!r}; using all")
            self._decision_under("stage1",
                method="llm_unknown_domain",
                domains_available=list(by_domain.keys()),
                domains_selected=list(by_domain.keys()),
                sticky_hint=sticky_hint,
                duration_ms=int((time.monotonic() - stage1_t0) * 1000) if stage1_t0 else None)
            return list(by_domain.keys())

        total_svcs = sum(len(by_domain.get(d, [])) for d in valid)
        label = "service" if total_svcs == 1 else "services"
        if len(valid) == 1:
            msg = f"Stage 1 → {valid[0]} ({total_svcs} {label})"
        else:
            per_domain = ", ".join(f"{d}({len(by_domain.get(d, []))})" for d in valid)
            msg = f"Stage 1 → {per_domain} — {total_svcs} {label} total"
        await self._broadcast("ROUTING", msg)
        self._decision_under("stage1",
            method="llm",
            domains_available=list(by_domain.keys()),
            domains_selected=valid,
            sticky_hint=sticky_hint,
            services_in_scope=total_svcs,
            duration_ms=int((time.monotonic() - stage1_t0) * 1000) if stage1_t0 else None)
        return valid

    async def _is_session_continuation(self, query: str, service: str,
                                        service_description: str) -> bool:
        """
        Ask gpt-4o-mini whether the current query continues the active
        conversational session or is a new, unrelated request.
        Returns True = stay locked, False = release lock and re-route.
        """
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"The user is in an active session with service: '{service}'\n"
                        f"Service purpose: {service_description[:300]}\n\n"
                        f"New user message: '{query}'\n\n"
                        f"Is this message continuing the current session, "
                        f"or is it a completely different/unrelated request?\n"
                        f"Reply with exactly one word: CONTINUE or NEW_TOPIC"
                    )
                }],
                temperature=0,
                max_tokens=5,
            )
            decision = resp.choices[0].message.content.strip().upper()
            return "CONTINUE" in decision
        except Exception:
            return True  # safe fallback: stay in session

    async def _route_query(self, query: str, use_stickiness: bool = False,
                           _disable_sticky: bool = False,
                           precomputed_domains: List[str] | None = None) -> List[str]:
        """
        Two-stage hybrid routing:
          Stage 1 (breadth) — classify the query into one or more domain tags.
                              Small, stable taxonomy; scales by tree depth.
          Stage 2 (depth)   — vector search within the chosen domain(s),
                              clear-winner shortcut + LLM tie-break as before.

        use_stickiness    — enables the LLM-NONE retry fallback (set by
                            callers that detected a short follow-up).
        _disable_sticky   — set on recursive retry calls (internal). When
                            Stage 2's LLM tie-break returns NONE while Stage 1
                            was sticky-biased, retry the routing fresh.
        precomputed_domains — Stage 1 result computed by the caller (used
                            by process_query when it runs Stage 1 BEFORE
                            workstream classification to scope candidates).
                            When provided, Stage 1 is skipped here.

        Note on sticky_hint: it is ALWAYS passed to Stage 1 when
        last_domain exists (regardless of use_stickiness), unless we're
        on a topic-switch retry. The prompt treats it as a soft inclusion
        bias — "include the session domain in your candidate set if the
        query could continue the session" — not a hard lock. This catches
        mid-length continuations like 'propose plan and execute it' that
        don't match the short-follow-up heuristic but are clearly part of
        an ongoing workflow. Multi-domain output is fine; Stage 2 vector
        search picks the right service from the union.
        """
        # ── Stage 1 — domain classification (skip if precomputed) ─────────
        if precomputed_domains is not None:
            domains = precomputed_domains
        else:
            sticky = None if _disable_sticky else self.last_domain
            domains = await self._classify_domain(query, sticky_hint=sticky)

        # ── Stage 2 — vector search within selected domain(s) ─────────────
        candidates = await self._semantic_search(query, limit=5, domains=domains)

        if not candidates:
            scope = ', '.join(domains) if domains else "(unscoped)"
            await self._broadcast("ERROR",
                f"Stage 2 in '{scope}' returned no vector hits — index built?")
            self._decision_under("stage2",
                method="no_vector_hits",
                domains_scope=list(domains) if domains else [],
                candidates=[], winner_services=[])
            return []

        best_score = candidates[0].get("score", 0)
        second_score = candidates[1].get("score", 0) if len(candidates) > 1 else 0
        gap = best_score - second_score
        third_score = candidates[2].get("score", 0) if len(candidates) > 2 else 0
        gap_23 = max(second_score - third_score, 1e-9)
        # Stash compact candidate snapshot for analytics; trim to the
        # five fields we'd actually query on later.
        self._decision_under("stage2",
            domains_scope=list(domains) if domains else [],
            candidates=[{
                "name":   c["server_name"],
                "domain": c.get("domain"),
                "score":  float(c.get("score", 0)),
            } for c in candidates],
            best_score=float(best_score),
            gap_12=float(gap),
            gap_23=float(gap_23) if len(candidates) >= 3 else None)

        # Compact Stage 2 broadcast: highlight the winner with ▶ and show
        # gap-to-winner rather than absolute scores alone. Modern embedding
        # models (voyage-4, text-embedding-3, embed-v3) output unit-norm
        # vectors that compress all semantically-related docs into a narrow
        # absolute-score band; the *relative* gap is what carries the signal.
        multi_domain = domains and len(domains) > 1
        scope_label = ', '.join(domains) if domains else "(unscoped)"
        await self._broadcast("ROUTING", f"Stage 2 in '{scope_label}':")
        winner_score = best_score
        for i, c in enumerate(candidates):
            tag = f" [{c.get('domain', '?')}]" if multi_domain else ""
            score = c.get("score", 0)
            mark  = "▶" if i == 0 else " "
            delta = "" if i == 0 else f"  (-{(winner_score - score):.4f})"
            await self._broadcast("ROUTING",
                f"  {mark} {c['server_name']}{tag}: {score:.4f}{delta}")

        # Sole candidate — Stage 1 already chose the domain; whatever vector
        # search returned is the only option. No LLM tie-break needed.
        if len(candidates) == 1:
            self._decision_under("stage2",
                method="sole_candidate",
                winner_services=[candidates[0]["server_name"]])
            return [candidates[0]["server_name"]]

        # Clear winner — either of two criteria fires the fast-path so the
        # logic stays correct regardless of which embedding model the index
        # uses.
        #
        #   (a) Absolute: best_score > 0.65 AND gap > 0.03.
        #       Kept as a belt-and-braces shortcut for embedding models
        #       that spread scores widely. Rarely fires on voyage-4
        #       (unit-norm vectors compress everything into 0.45-0.55) —
        #       in that regime the relative criterion below carries the
        #       fast-path.
        #
        #   (b) Relative: gap_1→2 ≥ 1.5 × gap_2→3 AND gap_1→2 ≥ 0.0005.
        #       The winner clearly leads — its gap to runner-up is at least
        #       50% larger than the next gap below. Empirical floor: data
        #       collected so far shows the LLM tie-break only earns its keep
        #       when the ratio is below ~1.3× (winner and runner-up are
        #       genuinely co-strong matches). Anything above ~1.5× the LLM
        #       just re-confirms the vector top-1.
        absolute_winner = best_score > 0.65 and gap > 0.03
        relative_winner = (len(candidates) >= 3
                           and gap >= 0.0005
                           and gap / gap_23 >= 1.5)

        if absolute_winner or relative_winner:
            if absolute_winner:
                why = f"score {best_score:.3f}, gap {gap:.3f}"
                method = "absolute_winner"
            else:
                ratio = gap / gap_23
                ratio_str = f"{ratio:.1f}×" if ratio < 100 else "decisive"
                why = f"standalone winner, gap ratio {ratio_str}"
                method = "relative_winner"
            await self._broadcast("ROUTING",
                f"✓ Clear winner ({why}): {candidates[0]['server_name']}")
            winner = candidates[0]["server_name"]
            self._decision_under("stage2",
                method=method,
                winner_services=[winner])
            return [winner]

        # ── Deterministic text-match tiebreaker ─────────────────────────────
        # Voyage-4 (and most bi-encoders) compress scores into a tight band
        # for short focused service descriptions, so the cosine gap is often
        # < 0.001 even when one service is the obviously-correct match. The
        # LLM tie-break is slow (1-2s) and stochastic. Before falling through
        # to it, run a cheap deterministic check: count literal phrase
        # matches between the query and each candidate's description (which
        # contains its trigger phrases verbatim). If one candidate clearly
        # leads on phrase overlap, the LLM is unnecessary.
        text_scores = [
            (self._text_match_score(query, c.get("description") or ""), c)
            for c in candidates[:5]
        ]
        text_scores.sort(key=lambda x: x[0], reverse=True)
        top_t = text_scores[0]
        second_t = text_scores[1] if len(text_scores) >= 2 else (0, None)
        # Fire when top has a real match AND a clear lead over runner-up.
        # "Clear lead" = at least 2× the runner-up score, or runner-up is 0.
        if top_t[0] >= 3 and (second_t[1] is None
                              or top_t[0] >= 2 * second_t[0] + 1):
            winner = top_t[1]["server_name"]
            await self._broadcast("ROUTING",
                f"✓ Text-match tiebreaker (phrase overlap "
                f"{int(top_t[0])} vs {int(second_t[0])}): {winner}")
            self._decision_under("stage2",
                method="text_match_tiebreaker",
                winner_services=[winner])
            return [winner]

        # Stickiness is intentionally NOT applied here — it runs as a last-
        # resort fallback AFTER the LLM tie-break, not as a shortcut around
        # it. The previous behaviour ("if best_score < 0.6 and use_stickiness
        # → reuse last_service") short-circuited the LLM exactly in the
        # cases where the LLM was needed most. With model upgrades (voyage-4)
        # absolute scores compress, so any absolute threshold misfires.

        # Conversational lock: services like acc_proof_point_service hold a
        # session lock once selected — but check whether the user has switched
        # topics before applying it.
        if (self.last_service in self.CONVERSATIONAL_SERVICES and
                any(c["server_name"] == self.last_service for c in candidates)):
            service_desc = next(
                (c.get("description", "") for c in candidates
                 if c["server_name"] == self.last_service), ""
            )
            is_continuation = await self._is_session_continuation(
                query, self.last_service, service_desc
            )
            if is_continuation:
                await self._broadcast("ROUTING",
                                f"🔒 Conversational lock → {self.last_service}")
                return [self.last_service]
            else:
                await self._broadcast("ROUTING",
                                f"🔓 Topic switch detected, releasing lock from {self.last_service}")
                self.last_service = None
                self.last_domain  = None

        # Explicit-mention shortcut — BEFORE the LLM tie-break.
        # If exactly one candidate's domain appears as a literal word
        # in the query, that candidate wins regardless of vector
        # score. The query 'delete all workstreams' MUST resolve to
        # workstream_service even when vector ranking puts another
        # candidate within 0.001 of it — otherwise we get destructive
        # misfires like "delete all workstreams → delete_all_todos".
        # Plural-aware (workstream/workstreams), case-insensitive.
        ql_lc = query.lower()
        explicit_candidates = []
        for c in candidates:
            domain = (c.get("domain") or "").lower()
            if not domain:
                continue
            if re.search(rf"\b{re.escape(domain)}s?\b", ql_lc):
                explicit_candidates.append(c)
        if explicit_candidates:
            # Multiple matches → pick highest-scored among them.
            winner = max(explicit_candidates,
                         key=lambda c: c.get("score", 0))
            await self._broadcast("ROUTING",
                f"✓ Explicit domain mention ({winner.get('domain')}): "
                f"{winner['server_name']} (tie-break skipped)")
            self._decision_under("stage2",
                method="explicit_domain_mention",
                winner_services=[winner["server_name"]])
            return [winner["server_name"]]

        # Medium confidence → LLM validation (silent until the result line).
        # The LLM gets the FULL docstring (trigger phrases + scope guards),
        # which carries far more disambiguation signal than the discriminator
        # paragraph we use for embedding.
        candidate_details = []
        for i, c in enumerate(candidates[:5]):
            service_name = c['server_name']
            description = c.get("full_description") or c.get("description") or "No description"
            short_desc = description[:1000] + "..." if len(description) > 1000 else description
            candidate_details.append(
                f"{i+1}. {service_name} (score: {c.get('score', 0):.2f})\n"
                f"   Purpose: {short_desc}"
            )
        candidate_list = "\n\n".join(candidate_details)

        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"User query: '{query}'\n\n"
                        f"Top service matches:\n{candidate_list}\n\n"
                        f"Pick the SINGLE service that PERFORMS the user's "
                        f"action.\n\n"
                        f"ACTION vs OBJECT — disambiguation rules:\n"
                        f"  • The verb of the query identifies the ACTION "
                        f"(run, simulate, create, list, show, get, apply, "
                        f"inject, diagnose, compare, update, cancel, …).\n"
                        f"  • Entity IDs / proper nouns (DTW-SCN-003, IBN-005, "
                        f"plan_ACME_M, runbook DTW-RB-007) are the OBJECT of "
                        f"the action — they do NOT identify the service.\n"
                        f"  • Pick the service that owns the ACTION on that "
                        f"object, not the service that owns the object's "
                        f"lifecycle. Example: 'run simulation for scenario "
                        f"DTW-SCN-003' — the verb 'run simulation' identifies "
                        f"the simulation service; 'scenario DTW-SCN-003' is "
                        f"just the input. Pick the simulation service.\n"
                        f"  • Only the scenario service is correct when the "
                        f"verb itself is 'create/list/show/cancel/update' a "
                        f"scenario (lifecycle), not when the verb operates "
                        f"ON a scenario via another tool.\n\n"
                        f"Only return more than one service if the query "
                        f"EXPLICITLY asks for multiple distinct actions "
                        f"(e.g. 'submit and then check feasibility').\n"
                        f"Reply with service name(s) only, comma-separated.\n"
                        f"If truly none apply, reply 'NONE'."
                    )
                }],
                temperature=0,
                max_tokens=50
            )

            result = resp.choices[0].message.content.strip()
            await self._broadcast("ROUTING",
                f"🤔 Tie-break ({best_score:.3f}) → LLM: {result}")

            if result == "NONE":
                # LLM refused. Two possible meanings:
                #
                #   (a) Genuine topic switch — Stage 1 was biased by a
                #       sticky hint into the wrong domain, and now Stage 2
                #       (LLM tie-break) reports that no service in that
                #       domain handles the query. Retry the WHOLE routing
                #       without sticky to let Stage 1 re-classify.
                #
                #   (b) No service can handle the query at all — even a
                #       sticky-free Stage 1 would land on the same dead
                #       end. In that case the retry will return NONE again
                #       and we fall through to either stickiness (if the
                #       user is in a session) or an empty result.
                if use_stickiness and not _disable_sticky and self.last_domain:
                    await self._broadcast("ROUTING",
                        "⚡ LLM returned NONE — looks like a topic switch, "
                        "retrying without sticky hint…")
                    self._decision_under("stage2",
                        method="llm_none_retry_unsticky",
                        winner_services=[])
                    return await self._route_query(query, use_stickiness,
                                                    _disable_sticky=True)
                if use_stickiness and self.last_service:
                    await self._broadcast("ROUTING",
                        f"⚡ LLM returned NONE, stickiness → {self.last_service}")
                    self._decision_under("stage2",
                        method="llm_none_stickiness_fallback",
                        winner_services=[self.last_service])
                    return [self.last_service]
                self._decision_under("stage2",
                    method="llm_none_no_fallback",
                    winner_services=[])
                return []

            # Parse comma-separated service names, filter to valid candidates
            services = [s.strip() for s in result.split(",") if s.strip()]
            valid_services = [s for s in services if s in [c["server_name"] for c in candidates]]

            winner = valid_services if valid_services else [candidates[0]["server_name"]]
            self._decision_under("stage2",
                method="llm_tiebreak",
                llm_response=result[:200],
                winner_services=winner)
            return winner

        except Exception as e:
            print(f"  ⚠️ LLM validation failed: {e}, falling back")
            # LLM call failed — stickiness is again the safer fallback than
            # blindly taking the top vector hit (which can be noise with
            # voyage-4-tight clusters).
            if use_stickiness and self.last_service:
                self._decision_under("stage2",
                    method="llm_error_stickiness_fallback",
                    winner_services=[self.last_service])
                return [self.last_service]
            self._decision_under("stage2",
                method="llm_error_top_fallback",
                winner_services=[candidates[0]["server_name"]])
            return [candidates[0]["server_name"]]

    async def _activate_servers(self, servers: List[Dict]):
        for srv in servers:
            name = srv["server_name"]
            if name in self.sessions:
                continue  # already running, reuse

            path = srv["path"]

            try:
                params = StdioServerParameters(
                    command="uv",
                    args=["run", path],
                    env=os.environ.copy()
                )
                read, write = await self.exit_stack.enter_async_context(stdio_client(params))
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                self.sessions[name] = session
                self.tool_cache.pop(name, None)  # invalidate stale cache on (re)start
                await self._broadcast("AGENT", f"✓ Activated: {name}")

            except Exception as e:
                print(f"  ❌ {name} failed: {e}")
                await self._broadcast("ERROR",
                    f"❌ Failed to activate {name}: {type(e).__name__}: {e}")

    async def _needs_context_enrichment(self, current_query: str, last_query: str) -> bool:
        """Use LLM to detect if current query is a follow-up or new topic"""

        # Skip for long queries (already have context)
        if len(current_query.split()) > 5:
            return False

        # Skip if no previous query
        if not last_query:
            return False

        # Skip for self-contained imperative commands. "run simulation",
        # "execute scenario", "show fleet status" are complete commands and
        # carry their own routing signal — enriching them with the previous
        # query DILUTES that signal (e.g. "run simulation" appended to a
        # scenario description routes to scenario_service, not
        # simulation_service, because the description vocab dominates).
        # Pattern: starts with an imperative verb AND has at least one more
        # token. Single-word inputs ("yes", "now") still go through LLM.
        words = current_query.strip().split()
        if len(words) >= 2:
            if _IMPERATIVE_VERBS.match(words[0]):
                return False

        # Ask LLM: Is this a follow-up?
        resp = await self.openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Previous query: '{last_query}'\n"
                    f"Current query: '{current_query}'\n\n"
                    f"Is the current query a FOLLOW-UP to the previous one? "
                    f"Answer only 'YES' or 'NO'.\n\n"
                    f"Examples:\n"
                    f"- Previous: 'solana price', Current: 'and now?' → YES\n"
                    f"- Previous: 'solana price', Current: 'update' → YES\n"
                    f"- Previous: 'hungry', Current: 'crypto price' → NO (topic change)\n"
                    f"- Previous: 'restaurant', Current: 'crypto' → NO (topic change)"
                )
            }],
            temperature=0,
            max_tokens=5
        )

        result = resp.choices[0].message.content.strip().upper()
        return result == "YES"

    def _format_result_preview(self, text: str, max_lines: int = 3, max_chars: int = 250) -> str:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        preview = lines[:max_lines]
        truncated = len(lines) > max_lines
        result = " │ ".join(preview)
        if len(result) > max_chars:
            result = result[:max_chars - 1] + "…"
        elif truncated:
            result += " …"
        return result

    def list_servers_info(self) -> List[Dict]:
        """Return all registered servers (local filesystem + cloud), with session status."""
        result = []
        seen = set()

        # Local filesystem servers
        if self.server_dir.exists():
            for f in sorted(self.server_dir.glob("*.py")):
                if f.name == "__init__.py":
                    continue
                name = f.stem
                seen.add(name)
                result.append({
                    "name":   name,
                    "origin": "local",
                    "active": name in self.sessions,
                })

        # Cloud servers (temp dir)
        for f in sorted(self.temp_dir.glob("*.py")):
            name = f.stem
            if name not in seen:
                seen.add(name)
                result.append({
                    "name":   name,
                    "origin": "cloud",
                    "active": name in self.sessions,
                })

        return result

    async def add_server(self, name: str, description: str, source_code: str) -> str:
        """Write source to temp dir, register in MongoDB, activate session."""
        if not name.isidentifier():
            return f"❌ Invalid server name '{name}' — must be a valid Python identifier."

        path = self.temp_dir / f"{name}.py"
        path.write_text(source_code)

        await self.collection.update_one(
            {"server_name": name},
            {"$set": {
                "server_name":  name,
                "description":  description,
                "origin":       "cloud",
                "source_code":  source_code,
                "file_hash":    hashlib.sha256(source_code.encode()).hexdigest(),
                "last_seen":    datetime.datetime.now().isoformat(),
            }},
            upsert=True,
        )

        # Close existing session if any so it restarts fresh
        if name in self.sessions:
            del self.sessions[name]
            self.tool_cache.pop(name, None)

        await self._activate_servers([{"server_name": name, "path": str(path)}])

        if name in self.sessions:
            await self._broadcast("BOOTSTRAP", f"✓ Cloud server '{name}' added and active")
            return f"✓ Server '{name}' added and active."
        else:
            return f"❌ Server '{name}' registered but failed to start — check the source code."

    async def remove_server(self, name: str) -> str:
        """Deactivate session and remove from registry."""
        if name in self.sessions:
            del self.sessions[name]
            self.tool_cache.pop(name, None)

        cloud_path = self.temp_dir / f"{name}.py"
        if cloud_path.exists():
            cloud_path.unlink()
            await self.collection.delete_one({"server_name": name, "origin": "cloud"})
            await self._broadcast("BOOTSTRAP", f"🗑️  Cloud server '{name}' removed")
            return f"✓ Cloud server '{name}' removed."

        # Local server — just evict the session; file stays on disk
        local_path = self.server_dir / f"{name}.py"
        if local_path.exists():
            await self._broadcast("BOOTSTRAP", f"⏏  Local server '{name}' session evicted (file kept)")
            return f"✓ Session for '{name}' evicted. File is local — it will reload on next query."

        return f"❌ Server '{name}' not found."

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

        # Compute enrichment posture upfront — used by both Stage 1 and
        # _route_query downstream.
        _SELF_CONTAINED = {
            "list", "show", "add", "update", "delete", "remove", "change",
            "set", "refresh", "display", "what", "how", "get", "find",
            "create", "book", "confirm", "cancel", "check", "search", "buy",
        }
        first_word = user_input.split()[0].lower() if user_input.split() else ""
        is_self_contained = first_word in _SELF_CONTAINED
        needs_enrichment_check = (
            last_user_queries
            and len(user_input.split()) < 5
            and not is_self_contained
        )

        stage1_domains: List[str] | None = None
        query_for_routing: str = user_input

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
                # Stage 1 FIRST — possibly in parallel with follow-up
                # detection on short queries.
                if needs_enrichment_check:
                    is_followup, stage1_domains = await asyncio.gather(
                        self._needs_context_enrichment(
                            user_input, last_user_queries[-1]),
                        self._classify_domain(
                            user_input, sticky_hint=self.last_domain),
                    )
                    if is_followup:
                        enriched_query = f"{last_user_queries[-1]}. {user_input}"
                        await self._broadcast("AGENT",
                            f"Follow-up detected, enriched: '{enriched_query}'")
                        # Re-run Stage 1 on the enriched text — it may
                        # surface a domain the bare short query missed.
                        stage1_domains = await self._classify_domain(
                            enriched_query, sticky_hint=self.last_domain)
                        query_for_routing = enriched_query
                    else:
                        await self._broadcast("AGENT",
                            "Topic change detected, no enrichment")
                else:
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

        # ── Stage 2 — vector search within precomputed Stage 1 domains ────
        # Follow-up detection and Stage 1 already ran upfront (in
        # parallel where applicable). For meta queries stage1_domains
        # is None, so _route_query runs its own Stage 1.
        service_names = await self._route_query(
            query_for_routing,
            use_stickiness=needs_enrichment_check and not is_meta_query,
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

        async def _fetch_tools(name: str) -> List[Dict]:
            if name not in self.tool_cache:
                t_list = await self.sessions[name].list_tools()
                self.tool_cache[name] = [
                    {"type": "function", "function": {
                        "name": f"{name}__{t.name}",
                        "description": t.description,
                        "parameters": t.inputSchema,
                    }}
                    for t in t_list.tools
                ]
            return self.tool_cache[name]

        active = [m["server_name"] for m in matches if m["server_name"] in self.sessions]
        tool_lists = await asyncio.gather(*[_fetch_tools(n) for n in active])
        openai_tools = [tool for tools in tool_lists for tool in tools]

        # Pull top-K reusable facts from agent_memories that match the
        # current query in the active workstream's domain. These ride into
        # the system prompt as a "you previously learned" block so the
        # agent's tool decisions reflect lessons from prior workstreams.
        memory_block = ""
        preferences_block = ""
        workstream_block = ""
        try:
            ws_doc = await self.workstreams.find_one(
                {"_id": self.current_workstream_id},
                {"domain": 1, "entities": 1, "title": 1, "summary": 1,
                 "state": 1}) if self.current_workstream_id else None
            if ws_doc:
                ents = ws_doc.get("entities") or []
                summ = (ws_doc.get("summary") or "").strip()
                workstream_block = (
                    f"\n\n🗂 ACTIVE WORKSTREAM: {self.current_workstream_id}"
                    f" — {ws_doc.get('title', '(untitled)')}"
                    f" [{ws_doc.get('domain', '?')}]\n"
                    + (f"Entities: {', '.join(ents)}\n" if ents else "")
                    + (f"Summary: {summ}\n" if summ else "")
                    + "Use the entities above (IDs, names) directly as tool "
                    "arguments when the query targets a specific item — do not "
                    "invent or guess IDs.\n"
                    "When the query says 'all', 'every', 'fleet', or 'across "
                    "all stores/sites', call the tool with NO scope arguments "
                    "to get the full result set — do not narrow to workstream "
                    "entities.\n"
                    "CRITICAL: The workstream summary describes PAST actions "
                    "and results — it is history, not live data. Never answer "
                    "a user query from the summary alone. Always call the "
                    "appropriate tool to get current results, even if the "
                    "summary appears to contain the answer.\n"
                    "CRITICAL: When the user describes a new what-if scenario "
                    "('raise X to Y in Z', 'what if we change plan X'), always "
                    "call create_scenario to parse and record it as a new "
                    "scenario — do not reuse a scenario ID from the workstream "
                    "context for a new simulation request."
                )
                bcast_parts = [ws_doc.get("title", "(untitled)")]
                if ents:
                    bcast_parts.append(f"entities: {', '.join(ents)}")
                if summ:
                    bcast_parts.append(f"summary: {summ[:120]}{'…' if len(summ) > 120 else ''}")
                await self._broadcast("WORKSTREAM",
                    f"🗂 Context injected: {self.current_workstream_id} — "
                    + "  |  ".join(bcast_parts))
            # Both planes recalled in parallel — different collections,
            # different shapes, independent failure modes.
            recalled, prefs_recalled = await asyncio.gather(
                self._recall_memories(
                    user_input,
                    domain   = (ws_doc or {}).get("domain") or self.last_domain,
                    entities = (ws_doc or {}).get("entities"),
                    limit    = 5),
                self._recall_preferences(user_input, limit=5),
                return_exceptions=False,
            )
            if recalled:
                # Sort core facts first so the LLM weights them more — vector
                # ranking is preserved within each tier.
                tier_order = {"core": 0, "extracted": 1, "decayed": 2}
                recalled = sorted(recalled,
                    key=lambda m: tier_order.get(m.get("tier") or "extracted", 1))
                # Each line carries tier + category labels so the model can
                # treat 'core' facts as institutional knowledge.
                lines = [
                    f"  • [{(m.get('tier') or 'extracted').upper()}/"
                    f"{m.get('category','fact')}] {m.get('text','')}"
                    for m in recalled
                ]
                memory_block = (
                    "\n\nYou previously learned the following from past "
                    "workstreams (CORE facts are institutional knowledge "
                    "with many recalls; use them when relevant):\n"
                    + "\n".join(lines)
                )
                # Tier breakdown in the broadcast so the demo audience sees
                # whether the agent is pulling fresh facts or settled ones.
                tier_counts: dict = {}
                for m in recalled:
                    tier_counts[m.get("tier") or "extracted"] = (
                        tier_counts.get(m.get("tier") or "extracted", 0) + 1)
                tier_summary = ", ".join(
                    f"{n} {t}" for t, n in sorted(tier_counts.items()))
                n_mem = len(recalled)
                await self._broadcast("MEMORY",
                    f"🧠 Recalled {n_mem} relevant {'fact' if n_mem == 1 else 'facts'} ({tier_summary})")
                self._decision_under("memory",
                    recalled_count=len(recalled),
                    tier_breakdown=tier_counts)

            if prefs_recalled:
                # User-stated preferences — separate block, labelled
                # distinctly so the LLM treats them as authoritative
                # self-disclosure rather than 'something the agent
                # learned about its own work'.
                pref_lines = [
                    f"  • [{(p.get('category') or 'preference').upper()}] "
                    f"{p.get('text','')}"
                    for p in prefs_recalled
                ]
                preferences_block = (
                    "\n\nThe user has explicitly told you the following "
                    "about themselves (preferences, identity, restrictions). "
                    "Treat these as authoritative when resolving "
                    "first-person references ('the sport I love', "
                    "'my favourite X', 'my usual') in the query:\n"
                    + "\n".join(pref_lines)
                )
                n_pref = len(prefs_recalled)
                await self._broadcast("PREFERENCES",
                    f"🧠 Recalled {n_pref} user {'preference' if n_pref == 1 else 'preferences'}")
                self._decision_under("preferences",
                    recalled_count=len(prefs_recalled))
        except Exception as e:
            print(f"⚠️ recall failed (non-fatal): {e}")

        # Build messages with conversation history. The system prompt is
        # augmented with three optional sections:
        #   • memory_block       — top-K facts from past workstreams
        #                          (agent_memories plane, auto-extracted)
        #   • preferences_block  — top-K user-stated preferences
        #                          (user_preferences plane, explicit)
        #   • replay_recipe      — the constructive tool-call sequence
        #                          from a source workstream the user
        #                          asked to repeat
        messages = [{"role": "system",
                     "content": _SYSTEM_PROMPT
                                + workstream_block
                                + memory_block
                                + preferences_block
                                + replay_recipe}]
        messages.extend(self.conversation_history)
        messages.append({"role": "user", "content": user_input})

        # ReAct Loop - Multiple tool iterations
        # Replay turns can chain submit → check → propose → activate plus a
        # final summary turn, which needs at least 5–6 tool iterations
        # before the agent gives its narrated response. Be generous.
        max_iterations = 8 if replay_recipe else 5
        iteration = 0
        tool_calls_count = 0  # analytics

        while iteration < max_iterations:
            iteration += 1
            await self._broadcast("AGENT", f"Iteration {iteration}/{max_iterations}")

            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=openai_tools,
                parallel_tool_calls=False
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                await self._broadcast("AGENT", "No more tool calls, agent finished")
                initial_answer = msg.content or "I have no response."
                break

            messages.append(msg)

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                fname = tc.function.name

                res_txt = "Error"

                srv, tool = fname.split("__", 1)
                await self._broadcast("ACTION", f"  Service: {srv}")
                await self._broadcast("ACTION", f"  Tool: {tool}")
                if srv in self.sessions:
                    r = await self.sessions[srv].call_tool(tool, args)
                    res_txt = r.content[0].text
                    tool_calls_count += 1
                    await self._broadcast("RESULT", self._format_result_preview(res_txt))
                    # Append every successful tool call to the workstream's
                    # audit trail — UNLESS the call is a read-only meta
                    # tool (list_workstreams, recall_facts, routing_summary
                    # etc.) inside a non-meta turn. The upfront meta-query
                    # heuristic catches most introspection queries; this
                    # is a belt-and-braces guard for cases where the
                    # heuristic missed but the agent ended up calling
                    # only meta tools anyway.
                    if self.current_workstream_id:
                        if self._is_meta_tool(tool):
                            self._current_decision["meta_tool_calls_filtered"] = (
                                self._current_decision.get(
                                    "meta_tool_calls_filtered", 0) + 1
                            )
                        else:
                            try:
                                await self._attach_to_workstream(
                                    self.current_workstream_id,
                                    user_input, srv, tool, res_txt)
                            except Exception as e:
                                print(f"⚠️ workstream attach failed: {e}")
                    if res_txt.startswith("VERBATIM:"):
                        await self._persist_decision(
                            tool_calls_count=tool_calls_count,
                            iterations_used=iteration,
                            verbatim_short_circuit=True,
                            duration_ms=int((time.monotonic() - turn_t0) * 1000))
                        return res_txt[len("VERBATIM:\n"):]
                else:
                    print(f"  ❌ Service '{srv}' NOT in active sessions!")
                    print(f"  Available: {list(self.sessions.keys())}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(res_txt)
                })

        # If max iterations reached, force final answer
        if iteration >= max_iterations:
            print("⚠️ Max iterations reached, forcing final answer")
            messages.append({"role": "user", "content": "Provide your final answer now."})
            final = await self.openai.chat.completions.create(
                model=self.model, messages=messages
            )
            initial_answer = final.choices[0].message.content or "Max iterations reached."

        final_answer = initial_answer

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
            tool_calls_count=tool_calls_count,
            iterations_used=iteration,
            max_iterations=max_iterations,
            max_iterations_hit=(iteration >= max_iterations),
            had_replay_recipe=bool(replay_recipe),
            duration_ms=int((time.monotonic() - turn_t0) * 1000))

        return final_answer
