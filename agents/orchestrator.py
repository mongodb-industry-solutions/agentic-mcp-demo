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
from pathlib import Path
from contextlib import AsyncExitStack
from typing import List, Dict
from watchfiles import awatch
from pymongo import AsyncMongoClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI


BROADCAST_URL         = "https://notify.bjjl.dev/send"
BROADCAST_RECEIVE_URL = "https://notify.bjjl.dev/receive"


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

        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY missing")

        self.openai = AsyncOpenAI()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")
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
        await self._resume_open_workstreams()
        self._watcher_task = asyncio.create_task(self._watch_servers())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._watcher_task:
            self._watcher_task.cancel()
            await asyncio.gather(self._watcher_task, return_exceptions=True)
        # Wait for any pending summary updates so we don't lose them.
        if self._ws_summary_tasks:
            await asyncio.gather(*self._ws_summary_tasks, return_exceptions=True)
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

    def _extract_discriminator(self, full_docstring: str, server_name: str) -> str:
        """
        Pull the unique semantic content out of a docstring for the embedded
        `description` field. The goal is to make the vector representation
        reflect what makes this service *different* from its siblings, not
        the shared scaffolding that all our services repeat.

        Convention used by our docstrings:
          Line 1:  "Service Title — short tagline"      ← unique
          Blank line
          Paragraph: focused purpose statement          ← unique
          Blank line
          "Use this service when users say:" block      ← noise (overlaps siblings)
          "This service does NOT …" guard              ← noise (overlaps siblings)

        We take title + first body paragraph. If the docstring is short or
        unstructured, fall back to the whole thing. Capped to keep the
        embedder focused — voyage-4 produces tighter clusters when fed long
        boilerplate-heavy text.
        """
        if not full_docstring or not full_docstring.strip():
            return f"Service: {server_name}"
        # Drop "Use this service" / "NOT this service" sections deterministically
        cutoffs = [
            "\nUse this service when users say",
            "\nUse this service when",
            "\n🚫 NOT this service",
            "\nThis service does NOT",
            "\nThis service is NOT",
        ]
        text = full_docstring
        for marker in cutoffs:
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx]
        # Take title + first body paragraph (paragraphs separated by blank line)
        paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
        keep = paragraphs[:2] if len(paragraphs) >= 2 else paragraphs[:1]
        result = "\n\n".join(keep).strip() or full_docstring.strip()
        return result[:600]

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
        naturally; singleton services (memory_service, restaurant_guide,
        incident_analyzer) become their own one-member domain.

        This means new services join an existing domain just by being named
        with the right prefix — no docstring or registry edit required.
        """
        stem = server_name.strip()
        return stem.split("_", 1)[0] if "_" in stem else stem

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
        # `domain` after the two-stage routing upgrade, or missing
        # `full_description` after the embedded-discriminator split).
        changed_servers = set()
        for name in potential_updates:
            local_hash = local_servers[name]["file_hash"]
            db_hash   = db_servers[name].get("file_hash", "")
            db_doc    = db_servers[name]
            needs_backfill = (
                not db_doc.get("domain")
                or not db_doc.get("full_description")
            )
            if local_hash != db_hash or needs_backfill:
                changed_servers.add(name)

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
        domains. Falls back to unfiltered search if Atlas rejects the filter
        (index hasn't been re-configured to include `domain` yet) — flips
        a flag so we don't keep trying."""
        def _build_pipeline(filter_doc: dict | None):
            vs: dict = {
                "index": "vector_index",
                "path":  "description",
                "query": query,
                "numCandidates": 50,
                "limit": limit,
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

    async def _open_workstreams_for_classifier(self, limit: int = 10) -> List[Dict]:
        """Compact list of open workstreams for the classifier prompt.
        Most-recent-first; capped because the prompt has to stay small."""
        cursor = self.workstreams.find(
            {"state": "open"},
            {"_id": 1, "title": 1, "domain": 1, "entities": 1,
             "summary": 1, "last_activity": 1},
        ).sort("last_activity", -1).limit(limit)
        return [d async for d in cursor]

    async def _classify_workstream(self, query: str, recent_user_msgs: List[str]) \
            -> tuple[str, bool, str | None]:
        """
        Classify the query into an open workstream or signal that a new
        one should be created. Returns (workstream_id, is_new, domain_hint).

        For 'new', the orchestrator allocates the id; the classifier only
        suggests a title + domain.
        """
        open_ws = await self._open_workstreams_for_classifier()

        # No open workstreams → trivially a new one
        if not open_ws:
            title, domain_hint = await self._propose_new_workstream(query)
            ws_id = await self._create_workstream(title, domain_hint, query)
            return ws_id, True, domain_hint

        # Build compact context for the LLM
        ws_lines = []
        for w in open_ws:
            ents = ", ".join((w.get("entities") or [])[:5])
            summary = (w.get("summary") or "").strip().replace("\n", " ")
            summary = summary[:200] + "…" if len(summary) > 200 else summary
            ws_lines.append(
                f"- {w['_id']} [{w.get('domain', '?')}] {w.get('title', '(untitled)')}\n"
                f"    entities: {ents or '(none)'}\n"
                f"    summary: {summary or '(empty)'}"
            )
        ws_block = "\n".join(ws_lines)

        recent_block = ""
        if recent_user_msgs:
            recent = " | ".join(m[:80] for m in recent_user_msgs[-3:])
            recent_block = f"\n\nRecent user turns: {recent}"

        prompt = (
            f"User query: '{query}'{recent_block}\n\n"
            f"Open workstreams:\n{ws_block}\n\n"
            f"Decide which workstream this query continues, or whether "
            f"the user is starting a new one.\n\n"
            f"Reply with valid JSON, no prose:\n"
            f"{{\"action\": \"continue\", \"workstream_id\": \"WS-...\"}}\n"
            f"  OR\n"
            f"{{\"action\": \"new\", \"title\": \"<short descriptive title, max 60 chars>\", "
            f"\"domain_hint\": \"<one of the known domains or empty>\"}}\n\n"
            f"Rules:\n"
            f"- If the query continues an open workstream (mentions its entities, "
            f"  uses its vocabulary, or is a natural follow-up to that thread), "
            f"  prefer 'continue'.\n"
            f"- Brief acknowledgements + follow-ups ('ok thanks', 'now do X') after "
            f"  a recent turn in a workstream are continuations.\n"
            f"- Brand-new entities or a clear topic switch → 'new'.\n"
        )
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=120,
                response_format={"type": "json_object"},
            )
            decision = json.loads(resp.choices[0].message.content)
        except Exception as e:
            await self._broadcast("ROUTING",
                f"⚠ Workstream classify failed ({e}); using most-recent open WS")
            ws = open_ws[0]
            return ws["_id"], False, ws.get("domain")

        action = (decision.get("action") or "").lower()
        if action == "continue":
            ws_id = decision.get("workstream_id")
            ws = next((w for w in open_ws if w["_id"] == ws_id), None)
            if ws:
                return ws["_id"], False, ws.get("domain")
            # Hallucinated id — fall through to "new"
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
        ws_id = await self._create_workstream(title, domain_hint, query)
        return ws_id, True, domain_hint

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
                    f"(max 60 chars) and the best-fit domain.\n"
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
        """Insert a new workstream document and return its id."""
        today = datetime.date.today().isoformat()
        # Count today's workstreams to allocate a per-day sequence number
        count_today = await self.workstreams.count_documents(
            {"_id": {"$regex": f"^WS-{today}-"}}
        )
        ws_id = f"WS-{today}-{count_today + 1:03d}"
        now = datetime.datetime.now()
        doc = {
            "_id":            ws_id,
            "title":          title[:120],
            "domain":         domain,
            "entities":       [],
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
        # Cheap entity extraction: any ALL-CAPS-or-dash id-shape token
        # (IBN-005, DTW-SCN-003, WS-2026-…) that appears in query+result
        text = f"{query} {result_excerpt or ''}"
        entity_candidates = set(re.findall(
            r"\b([A-Z][A-Z0-9]+-[A-Z0-9-]+)\b", text))
        # Plus the well-known site names (cheap dictionary; could be extended)
        for name in ("Marienplatz", "Schwabing", "Altona", "Mitte",
                     "Königstraße", "Alpenmarkt", "ACME"):
            if name in text:
                entity_candidates.add(name)
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
        by_domain = await self._list_domains()
        if not by_domain:
            return []
        if len(by_domain) == 1:
            only = next(iter(by_domain))
            n = len(by_domain[only])
            label = "service" if n == 1 else "services"
            await self._broadcast("ROUTING", f"Stage 1 → {only} ({n} {label})")
            return [only]

        # Deterministic pre-check: if the user typed a literal domain name
        # as a word in the query, trust that — it's an explicit selection
        # signal that overrides sticky bias and skips the LLM call entirely.
        # Catches cases like 'ibn feasibility check!' after a topic switch
        # to todo, where the LLM would otherwise stay in todo because the
        # add_todo tool can plausibly accept any text.
        ql = query.lower()
        explicit = [d for d in by_domain
                    if re.search(rf"\b{re.escape(d.lower())}\b", ql)]
        if explicit:
            total = sum(len(by_domain[d]) for d in explicit)
            label = "service" if total == 1 else "services"
            scope = ', '.join(f"{d}({len(by_domain[d])})" for d in explicit) \
                    if len(explicit) > 1 else f"{explicit[0]} ({total} {label})"
            await self._broadcast("ROUTING",
                f"Stage 1 → {scope}  (explicit domain mention)")
            return explicit

        # Build a compact taxonomy for the LLM (sent in the prompt only, NOT
        # broadcast — the BOOTSTRAP line already enumerates the taxonomy once
        # for the audience).
        #
        # Each domain's blurb concatenates the tagline of EVERY member
        # service so the classifier sees what the whole domain covers, not
        # just the alphabetically-first service. Critical for multi-service
        # domains (ibn, dtw) where a query may match the tagline of service
        # #3, not service #1 — e.g. 'propose and activate the plan' maps to
        # ibn_feasibility_service's tagline ('Match Intent to Inventory and
        # Plan Activation'), but the old blurb only showed
        # ibn_intent_service's tagline and the LLM missed the connection.
        lines = []
        for d, members in sorted(by_domain.items()):
            members_str = ", ".join(m["server_name"] for m in members[:5])
            taglines = []
            for m in members[:5]:
                desc = (m.get("description") or "").strip()
                if not desc:
                    continue
                first_line = next((ln for ln in desc.splitlines() if ln.strip()), "")
                # Strip the "Service Title —" prefix to keep just the unique
                # tagline. Handles both "X Service — Y" and "SERVER: Y" forms.
                if " — " in first_line:
                    first_line = first_line.split(" — ", 1)[1].strip()
                elif first_line.startswith("SERVER:"):
                    first_line = first_line[len("SERVER:"):].strip()
                if first_line:
                    taglines.append(first_line[:80])
            blurb = " · ".join(taglines) if taglines else "(no description)"
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
            return [sticky_hint] if sticky_hint and sticky_hint in by_domain \
                                  else [next(iter(by_domain))]

        candidates = [d.strip() for d in raw.split(",") if d.strip()]
        valid = [d for d in candidates if d in by_domain]
        if not valid:
            await self._broadcast("ROUTING",
                f"⚠ Stage 1: unknown domain(s) {candidates!r}; using all")
            return list(by_domain.keys())

        total_svcs = sum(len(by_domain.get(d, [])) for d in valid)
        label = "service" if total_svcs == 1 else "services"
        if len(valid) == 1:
            msg = f"Stage 1 → {valid[0]} ({total_svcs} {label})"
        else:
            per_domain = ", ".join(f"{d}({len(by_domain.get(d, []))})" for d in valid)
            msg = f"Stage 1 → {per_domain} — {total_svcs} {label} total"
        await self._broadcast("ROUTING", msg)
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
                           _disable_sticky: bool = False) -> List[str]:
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
        # ── Stage 1 — domain classification ───────────────────────────────
        sticky = None if _disable_sticky else self.last_domain
        domains = await self._classify_domain(query, sticky_hint=sticky)

        # ── Stage 2 — vector search within selected domain(s) ─────────────
        candidates = await self._semantic_search(query, limit=5, domains=domains)

        if not candidates:
            scope = ', '.join(domains) if domains else "(unscoped)"
            await self._broadcast("ERROR",
                f"Stage 2 in '{scope}' returned no vector hits — index built?")
            return []

        best_score = candidates[0].get("score", 0)
        second_score = candidates[1].get("score", 0) if len(candidates) > 1 else 0
        gap = best_score - second_score

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
            return [candidates[0]["server_name"]]

        # Clear winner — either of two criteria fires the fast-path so the
        # logic stays correct regardless of which embedding model the index
        # uses.
        #
        #   (a) Absolute: best_score > 0.65 AND gap > 0.03.
        #       Tuned for older models (voyage-3-large) that spread scores
        #       wide. Almost never fires on voyage-4 because unit-norm
        #       vectors compress all related docs into 0.45-0.55.
        #
        #   (b) Relative: gap_1→2 ≥ 1.5 × gap_2→3 AND gap_1→2 ≥ 0.0005.
        #       The winner clearly leads — its gap to runner-up is at least
        #       50% larger than the next gap below. Empirical floor: data
        #       collected so far shows the LLM tie-break only earns its keep
        #       when the ratio is below ~1.3× (winner and runner-up are
        #       genuinely co-strong matches). Anything above ~1.5× the LLM
        #       just re-confirms the vector top-1.
        absolute_winner = best_score > 0.65 and gap > 0.03
        relative_winner = False
        if len(candidates) >= 3:
            third_score = candidates[2].get("score", 0)
            gap_23 = max(second_score - third_score, 1e-9)
            if gap >= 0.0005 and gap / gap_23 >= 1.5:
                relative_winner = True

        if absolute_winner or relative_winner:
            if absolute_winner:
                why = f"score {best_score:.3f}, gap {gap:.3f}"
            else:
                ratio = gap / gap_23
                ratio_str = f"{ratio:.1f}×" if ratio < 100 else "decisive"
                why = f"standalone winner, gap ratio {ratio_str}"
            await self._broadcast("ROUTING",
                f"✓ Clear winner ({why}): {candidates[0]['server_name']}")
            return [candidates[0]["server_name"]]

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
                        f"Pick the SINGLE best service for this query.\n"
                        f"Only return more than one if the query EXPLICITLY asks "
                        f"for multiple distinct actions (e.g. 'submit and then "
                        f"check feasibility'). For vague or ambiguous queries, "
                        f"pick the one most likely meant.\n"
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
                    return await self._route_query(query, use_stickiness,
                                                    _disable_sticky=True)
                if use_stickiness and self.last_service:
                    await self._broadcast("ROUTING",
                        f"⚡ LLM returned NONE, stickiness → {self.last_service}")
                    return [self.last_service]
                return []

            # Parse comma-separated service names, filter to valid candidates
            services = [s.strip() for s in result.split(",") if s.strip()]
            valid_services = [s for s in services if s in [c["server_name"] for c in candidates]]

            return valid_services if valid_services else [candidates[0]["server_name"]]

        except Exception as e:
            print(f"  ⚠️ LLM validation failed: {e}, falling back")
            # LLM call failed — stickiness is again the safer fallback than
            # blindly taking the top vector hit (which can be noise with
            # voyage-4-tight clusters).
            if use_stickiness and self.last_service:
                return [self.last_service]
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

            except Exception as e:
                print(f"  ❌ {name} failed: {e}")

    async def _needs_context_enrichment(self, current_query: str, last_query: str) -> bool:
        """Use LLM to detect if current query is a follow-up or new topic"""

        # Skip for long queries (already have context)
        if len(current_query.split()) > 5:
            return False

        # Skip if no previous query
        if not last_query:
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

        # Context-Aware Routing for follow-up questions
        context_window = self.conversation_history[-4:] if self.conversation_history else []
        last_user_queries = [msg["content"] for msg in context_window if msg["role"] == "user"]

        # ── Workstream classification ─────────────────────────────────────
        # Before routing, decide which workstream this query continues (or
        # whether it opens a new one). The chosen workstream's domain
        # becomes the sticky bias for Stage 1 below, which means routing
        # respects multi-turn intent rather than just the last turn.
        ws_id, ws_is_new, ws_domain = await self._classify_workstream(
            user_input, last_user_queries)
        self.current_workstream_id = ws_id
        if not ws_is_new:
            await self._broadcast("WORKSTREAM", f"↪ {ws_id} continued")
        # Workstream domain takes precedence over last_domain for stickiness
        if ws_domain:
            self.last_domain = ws_domain

        # Smart context enrichment — run in parallel with routing when possible
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

        if needs_enrichment_check:
            # Run follow-up detection and optimistic routing concurrently.
            # The optimistic pass passes use_stickiness=True because reaching
            # this branch already means the query is short enough to be a
            # follow-up candidate — session context is the right tiebreaker
            # for the routing decision.
            enrichment_task, routing_task = await asyncio.gather(
                self._needs_context_enrichment(user_input, last_user_queries[-1]),
                self._route_query(user_input, use_stickiness=True),
            )
            is_followup       = enrichment_task
            optimistic_result = routing_task

            # If the optimistic pass already produced a single confident
            # service, trust it — re-routing the enriched query would just
            # introduce contradictions when the enriched text is dominated
            # by the prior turn's vocabulary.
            if len(optimistic_result) == 1:
                service_names = optimistic_result
            elif is_followup:
                enriched_query = f"{last_user_queries[-1]}. {user_input}"
                await self._broadcast("AGENT",
                    f"Follow-up detected, enriched: '{enriched_query}'")
                service_names = await self._route_query(enriched_query, use_stickiness=True)
            else:
                await self._broadcast("AGENT", "Topic change detected, no enrichment")
                service_names = optimistic_result
        else:
            service_names = await self._route_query(user_input, use_stickiness=False)

        if not service_names:
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
            return (
                "Services found in registry but not available locally. "
                "Please ensure MCP servers are installed in the mcp_servers directory."
            )

        # Store last non-memory service AND its domain for stickiness.
        # last_domain is consulted by Stage 1 on the next short/ambiguous turn.
        for match in matches:
            name = match["server_name"]
            if name != "memory_service":
                self.last_service = name
                self.last_domain  = self._infer_domain(name)
                break

        # Memory service is routed normally — no forced injection.
        # It will be selected by the vector search when the query is about
        # preferences, personal facts, or memory operations.

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

        # Build messages with conversation history
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        messages.extend(self.conversation_history)
        messages.append({"role": "user", "content": user_input})

        # ReAct Loop - Multiple tool iterations
        max_iterations = 5
        iteration = 0

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
                    await self._broadcast("RESULT", self._format_result_preview(res_txt))
                    # Append every successful tool call to the workstream's
                    # audit trail. This is what survives across process
                    # restarts and powers the dashboard's history panel.
                    if self.current_workstream_id:
                        try:
                            await self._attach_to_workstream(
                                self.current_workstream_id, user_input, srv, tool, res_txt)
                        except Exception as e:
                            print(f"⚠️ workstream attach failed: {e}")
                    if res_txt.startswith("VERBATIM:"):
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

        return final_answer
