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
        self._watcher_task = asyncio.create_task(self._watch_servers())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._watcher_task:
            self._watcher_task.cancel()
            await asyncio.gather(self._watcher_task, return_exceptions=True)
        await self.exit_stack.aclose()
        await self.http_client.aclose()
        await self.mongo_client.close()

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

        # Build a compact taxonomy for the LLM (sent in the prompt only, NOT
        # broadcast — the BOOTSTRAP line already enumerates the taxonomy once
        # for the audience).
        lines = []
        for d, members in sorted(by_domain.items()):
            members_str = ", ".join(m["server_name"] for m in members[:5])
            blurb = ""
            for m in members:
                desc = (m.get("description") or "").strip()
                if desc:
                    blurb = next((ln for ln in desc.splitlines() if ln.strip()), "")[:140]
                    break
            lines.append(f"- {d}: {blurb}  [services: {members_str}]")
        taxonomy = "\n".join(lines)

        hint = ""
        if sticky_hint:
            hint = (f"\n\nPrevious turn used domain '{sticky_hint}'. For short or "
                    f"ambiguous follow-up queries, prefer the same domain unless "
                    f"the new query clearly belongs elsewhere.")

        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"User query: '{query}'\n\n"
                        f"Available domains:\n{taxonomy}{hint}\n\n"
                        f"Which domain(s) handle this query? Reply with domain "
                        f"name(s) only, comma-separated. If multiple are plausible, "
                        f"list up to 2. If unsure, return your single best guess. "
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

    async def _route_query(self, query: str, use_stickiness: bool = False) -> List[str]:
        """
        Two-stage hybrid routing:
          Stage 1 (breadth) — classify the query into one or more domain tags.
                              Small, stable taxonomy; scales by tree depth.
          Stage 2 (depth)   — vector search within the chosen domain(s),
                              clear-winner shortcut + LLM tie-break as before.
        """
        # ── Stage 1 — domain classification ───────────────────────────────
        sticky_hint = self.last_domain if use_stickiness else None
        domains = await self._classify_domain(query, sticky_hint=sticky_hint)

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

        # Clear winner: decent score + decisive lead over runner-up
        if best_score > 0.65 and gap > 0.03:
            await self._broadcast("ROUTING",
                            f"✓ Clear winner (gap {gap:.3f}), using: {candidates[0]['server_name']}")
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
                # LLM refused. Stickiness is the right last-resort here —
                # the user is in a session, we'd rather stay than give up.
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
            # Run follow-up detection and optimistic routing concurrently
            enrichment_task, routing_task = await asyncio.gather(
                self._needs_context_enrichment(user_input, last_user_queries[-1]),
                self._route_query(user_input, use_stickiness=False),
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

        return final_answer
