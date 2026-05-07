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
from pathlib import Path
from contextlib import AsyncExitStack
from typing import List, Dict
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
    def __init__(self, server_dir: str = "mcp_servers"):
        self.server_dir = Path(server_dir)
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self.conversation_history = []
        self.last_service = None  # Session Stickiness

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

    async def _broadcast(self, title: str = "", message: str = "", tags: str = "robot"):
        """Send a live update and wait for delivery (preserves message ordering)."""
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
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.exit_stack.aclose()
        await self.http_client.aclose()
        await self.mongo_client.close()

    def _extract_docstring(self, file_path: Path) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
                return ast.get_docstring(tree) or f"Service: {file_path.stem}"
        except:
            return f"Service: {file_path.stem}"

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute hash of file content to detect changes"""
        try:
            with open(file_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except:
            return ""

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
            docstring = self._extract_docstring(f)
            file_hash = self._compute_file_hash(f)

            local_servers[server_name] = {
                "server_name": server_name,
                "description": docstring,
                "file_hash": file_hash,
                "last_seen": datetime.datetime.now().isoformat()
            }

        await self._broadcast() # newline
        await self._broadcast("BOOTSTRAP", f"Found {len(local_servers)} local MCP servers")

        # Fetch current registry from MongoDB
        db_servers = {
            doc["server_name"]: doc
            async for doc in self.collection.find({}, {"_id": 0})
        }

        await self._broadcast("BOOTSTRAP", f"Found {len(db_servers)} servers in registry")

        # Compute diff
        local_names = set(local_servers.keys())
        db_names = set(db_servers.keys())

        new_servers = local_names - db_names
        deleted_servers = db_names - local_names
        potential_updates = local_names & db_names

        # Check for actual changes (hash comparison)
        changed_servers = set()
        for name in potential_updates:
            local_hash = local_servers[name]["file_hash"]
            db_hash = db_servers[name].get("file_hash", "")

            if local_hash != db_hash:
                changed_servers.add(name)

        # Sync operations
        total_changes = len(new_servers) + len(changed_servers) + len(deleted_servers)

        if total_changes == 0:
            await self._broadcast("BOOTSTRAP", "✓ Registry up-to-date (no changes)")
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
                await self._broadcast("BOOTSTRAP", f"    ↻ {name} (description or content changed)")

        # 3. Remove deleted servers
        if deleted_servers:
            await self._broadcast("BOOTSTRAP", f"🗑️  Removing {len(deleted_servers)} deleted server(s):")
            for name in deleted_servers:
                await self.collection.delete_one({"server_name": name})
                await self._broadcast("BOOTSTRAP", f"    - {name}")

        await self._broadcast("BOOTSTRAP", f"✓ Registry sync complete\n")

    async def _semantic_search(self, query: str, limit: int = 5) -> List[Dict]:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "vector_index",
                    "path": "description",
                    "query": query,
                    "numCandidates": 50,
                    "limit": limit
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "server_name": 1,
                    "description": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]

        cursor = await self.collection.aggregate(pipeline)
        return await cursor.to_list()

    async def _route_query(self, query: str, use_stickiness: bool = False) -> List[str]:
        """Hybrid routing: Vector Search + LLM validation for ambiguous cases"""

        candidates = await self._semantic_search(query, limit=5)

        if not candidates:
            await self._broadcast("ERROR", f"No results from vector search - embeddings and index exist?")
            return []

        best_score = candidates[0].get("score", 0)
        second_score = candidates[1].get("score", 0) if len(candidates) > 1 else 0
        gap = best_score - second_score

        await self._broadcast("ROUTING", f"Vector search results:")
        for c in candidates:
            score = c.get("score", 0)
            await self._broadcast("ROUTING", f"  {c['server_name']}: {score:.3f}")

        # Clear winner: decent score + decisive lead over runner-up
        if best_score > 0.65 and gap > 0.03:
            await self._broadcast("ROUTING",
                            f"✓ Clear winner (gap {gap:.3f}), using: {candidates[0]['server_name']}")
            return [candidates[0]["server_name"]]

        # Session stickiness for very vague queries
        if use_stickiness and self.last_service and best_score < 0.6:
            await self._broadcast("ROUTING",
                            ( f"⚡ Low confidence ({best_score:.3f}), "
                              "using session stickiness: {self.last_service}" ))
            return [self.last_service]

        # Medium confidence → LLM validation
        await self._broadcast("ROUTING",
                        ( f"🤔 Medium confidence ({best_score:.3f}), "
                          "asking LLM to validate..." ))

        # Use descriptions already returned by _semantic_search
        candidate_details = []
        for i, c in enumerate(candidates[:5]):
            service_name = c['server_name']
            description = c.get("description", "No description")
            short_desc = description[:200] + "..." if len(description) > 200 else description
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
                        f"Which service(s) can handle this query?\n"
                        f"Consider the service PURPOSE and user intent.\n"
                        f"Reply with service name(s) only, comma-separated.\n"
                        f"If NONE are relevant, reply 'NONE'."
                    )
                }],
                temperature=0,
                max_tokens=50
            )

            result = resp.choices[0].message.content.strip()
            await self._broadcast("ROUTING", f"💡 LLM decision: {result}")

            if result == "NONE":
                return []

            # Parse comma-separated service names
            services = [s.strip() for s in result.split(",") if s.strip()]
            # Filter to only valid service names from candidates
            valid_services = [s for s in services if s in [c["server_name"] for c in candidates]]

            return valid_services if valid_services else [candidates[0]["server_name"]]

        except Exception as e:
            print(f"  ⚠️ LLM validation failed: {e}, falling back to top match")
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

    def _format_result_preview(self, text: str, max_lines: int = 3, max_chars: int = 120) -> str:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        preview = lines[:max_lines]
        truncated = len(lines) > max_lines
        result = " │ ".join(preview)
        if len(result) > max_chars:
            result = result[:max_chars - 1] + "…"
        elif truncated:
            result += " …"
        return result

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
            is_followup = enrichment_task

            if is_followup:
                enriched_query = f"{last_user_queries[-1]}. {user_input}"
                await self._broadcast("AGENT", f"Follow-up detected, enriched: '{enriched_query}'")
                # Re-route with enriched query + stickiness
                service_names = await self._route_query(enriched_query, use_stickiness=True)
            else:
                await self._broadcast("AGENT", f"Topic change detected, no enrichment")
                service_names = routing_task  # use the optimistic result
        else:
            service_names = await self._route_query(user_input, use_stickiness=False)

        if not service_names:
            return "I couldn't find relevant services for this request."

        # Resolve paths from local filesystem
        matches = []
        for service_name in service_names:
            local_path = self.server_dir / f"{service_name}.py"

            if local_path.exists():
                matches.append({
                    "server_name": service_name,
                    "path": str(local_path.absolute())
                })
                #print(f"✓ Resolved {service_name} → {local_path}")
            else:
                print(f"⚠️ {service_name} not found locally at {local_path}, skipping")

        if not matches:
            return (
                "Services found in registry but not available locally. "
                "Please ensure MCP servers are installed in the mcp_servers directory."
            )

        # Store last non-memory service for stickiness
        for match in matches:
            name = match["server_name"]
            if name != "memory_service":
                self.last_service = name
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
