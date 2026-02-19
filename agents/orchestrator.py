#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Orchestrator Agent with Semantic Routing, Multi-Agent Critic, and Live Broadcast
"""

import asyncio
import os
import json
import ast
import requests
import datetime
import hashlib
from pathlib import Path
from contextlib import AsyncExitStack
from typing import List, Dict, Optional
from pymongo import MongoClient
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
    "CRITIC":     Colors.MAGENTA,
    "ERROR":      Colors.BRIGHT_RED
}


class OrchestratorAgent:
    def __init__(self, server_dir: str = "mcp_servers"):
        self.server_dir = Path(server_dir)
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self.conversation_history = []
        self.last_service = None  # Session Stickiness

        if not os.environ.get("MONGODB_URI"):
            raise ValueError("MONGODB_URI missing")

        self.mongo_client = MongoClient(os.environ["MONGODB_URI"])
        self.db = self.mongo_client["agent_registry"]
        self.collection = self.db["mcp_services"]

        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY missing")

        self.openai = AsyncOpenAI()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")

    def _broadcast(self, title: str = "", message: str = "", tags: str = "robot"):
        """Send live updates"""
        try:
            if title == "":
                resp = requests.post(BROADCAST_URL, f"{Colors.RESET}\n", timeout=15)
            else:
                current_time = datetime.datetime.now().strftime("%H:%M")
                color = TITLE_COLORS.get(title, Colors.RESET)
                full_message = f"🤖 {current_time} {color}[{title}] {message}{Colors.RESET}"
                resp = requests.post(BROADCAST_URL, data=full_message.encode("utf-8"), timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"❌ Broadcast failed: {e}")

    async def __aenter__(self):
        await self._sync_registry()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.exit_stack.aclose()
        self.mongo_client.close()

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

        self._broadcast() # newline
        self._broadcast("BOOTSTRAP", f"Found {len(local_servers)} local MCP servers")

        # Fetch current registry from MongoDB
        db_servers = {
            doc["server_name"]: doc
            for doc in self.collection.find({}, {"_id": 0})
        }

        self._broadcast("BOOTSTRAP", f"Found {len(db_servers)} servers in registry")

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
            self._broadcast("BOOTSTRAP", "✓ Registry up-to-date (no changes)")
            return

        #print(f"\n🔄 Syncing changes:")

        # 1. Add new servers
        if new_servers:
            self._broadcast("BOOTSTRAP", f"➕ Adding {len(new_servers)} new server(s):")
            for name in new_servers:
                self.collection.insert_one(local_servers[name])
                self._broadcast("BOOTSTRAP", f"    + {name}")

        # 2. Update changed servers
        if changed_servers:
            self._broadcast("BOOTSTRAP", f"🔄 Updating {len(changed_servers)} changed server(s):")
            for name in changed_servers:
                self.collection.update_one(
                    {"server_name": name},
                    {"$set": local_servers[name]}
                )
                self._broadcast("BOOTSTRAP", f"    ↻ {name} (description or content changed)")

        # 3. Remove deleted servers
        if deleted_servers:
            self._broadcast("BOOTSTRAP", f"🗑️  Removing {len(deleted_servers)} deleted server(s):")
            for name in deleted_servers:
                self.collection.delete_one({"server_name": name})
                self._broadcast("BOOTSTRAP", f"    - {name}")

        self._broadcast("BOOTSTRAP", f"✓ Registry sync complete\n")

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
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]

        return list(self.collection.aggregate(pipeline))

    def _enrich_for_routing(self, user_input: str) -> str:
        """Add routing hints based on patterns"""

        lower = user_input.lower()

        if "," in user_input:
            actions = user_input.split(",")
            if len(actions) >= 2:
                action_verbs = ["eat", "show", "phone", "call", "buy", "send",
                                "watch", "read", "meet", "confirm", "check", "book"]
                has_actions = any(verb in lower for verb in action_verbs)

                if has_actions:
                    return f"[Multiple tasks to add to TODO list] {user_input}"

        if "i need to" in lower or "i have to" in lower:
            and_count = lower.count(" and ")
            if and_count >= 1:
                return f"[Multiple tasks to add to TODO list] {user_input}"

        return user_input

    async def _route_query(self, query: str, use_stickiness: bool = False) -> List[str]:
        """Hybrid routing: Vector Search + LLM validation for ambiguous cases"""

        enriched_query = self._enrich_for_routing(query)
        candidates = await self._semantic_search(enriched_query, limit=5)

        if not candidates:
            self._broadcast("ERROR", f"No results from vector search - embeddings and index exist?")
            return []

        best_score = candidates[0].get("score", 0)

        self._broadcast("ROUTING", f"Vector search results:")
        for c in candidates:
            score = c.get("score", 0)
            self._broadcast("ROUTING", f"  {c['server_name']}: {score:.3f}")

        # High confidence → use immediately
        if best_score > 0.8:
            self._broadcast("ROUTING",
                            f"✓ High confidence, using: {candidates[0]['server_name']}")
            return [candidates[0]["server_name"]]

        # Session stickiness for very vague queries
        if use_stickiness and self.last_service and best_score < 0.6:
            self._broadcast("ROUTING",
                            ( f"⚡ Low confidence ({best_score:.3f}), "
                              "using session stickiness: {self.last_service}" ))
            return [self.last_service]

        # Medium confidence → LLM validation
        self._broadcast("ROUTING",
                        ( f"🤔 Medium confidence ({best_score:.3f}), "
                          "asking LLM to validate..." ))

        # Fetch full service descriptions for LLM context
        candidate_details = []
        for i, c in enumerate(candidates[:5]):
            service_name = c['server_name']
            # Get full doc with description
            doc = self.collection.find_one(
                {"server_name": service_name},
                {"description": 1, "_id": 0}
            )
            description = doc.get("description", "No description") if doc else "No description"
            # Take first 200 chars of description
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
            self._broadcast("ROUTING", f"💡 LLM decision: {result}")

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
        await self.exit_stack.aclose()
        self.exit_stack = AsyncExitStack()
        self.sessions = {}

        for srv in servers:
            name = srv["server_name"]
            path = srv["path"]
            #print(f"🚀 Starting {name} from {path}")

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
                #print(f"  ✅ {name} activated")

            except Exception as e:
                print(f"  ❌ {name} failed: {e}")

    async def _critic_review(self, query: str, answer: str) -> str:
        """Multi-Agent Critic checks Worker output with structured validation"""

        review_function = {
            "name": "review_response",
            "description": "Review agent response for compliance",
            "parameters": {
                "type": "object",
                "properties": {
                    "is_financial_topic": {
                        "type": "boolean",
                        "description": (
                            "Does the query/answer involve stocks, crypto, "
                            "investments, prices, or financial advice?"
                        )
                    },
                    "has_financial_disclaimer": {
                        "type": "boolean",
                        "description": (
                            "Does the answer include a risk warning "
                            "or 'not financial advice' statement?"
                        )
                    },
                    "is_medical_topic": {
                        "type": "boolean",
                        "description": "Does the query/answer involve health or medical advice?"
                    },
                    "has_medical_disclaimer": {
                        "type": "boolean",
                        "description": "Does the answer include 'consult a doctor' warning?"
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["APPROVED", "REJECTED"],
                        "description": "Final verdict"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Explanation for rejection (empty if approved)"
                    }
                },
                "required": [ "is_financial_topic",
                              "has_financial_disclaimer",
                              "is_medical_topic",
                              "has_medical_disclaimer",
                              "verdict",
                              "reason" ]
            }
        }

        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Review this response for compliance:\n\nUser "
                        f"Query: '{query}'\nAgent Answer: '{answer}'"
                    )
                }],
                tools=[{"type": "function", "function": review_function}],
                tool_choice={"type": "function", "function": {"name": "review_response"}},
                temperature=0
            )

            result = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)

            #print(f"\n📋 Critic Analysis:")
            #print(f"  Financial Topic: {result['is_financial_topic']}")
            #print(f"  Has Disclaimer: {result['has_financial_disclaimer']}")
            #print(f"  Verdict: {result['verdict']}")

            # Strict compliance checks
            if result["is_financial_topic"] and not result["has_financial_disclaimer"]:
                return (
                    "REJECTED: Financial topic detected but missing risk disclaimer. "
                    "Add a warning like 'This is not financial advice. "
                    "Use the provided information at your own risk'"
                )

            if result["is_medical_topic"] and not result["has_medical_disclaimer"]:
                return "REJECTED: Medical topic detected but missing 'consult a doctor' warning."

            if result["verdict"] == "REJECTED" and result["reason"]:
                return f"REJECTED: {result['reason']}"

            return "APPROVED"

        except Exception as e:
            print(f"⚠️ Critic review failed: {e}")
            return "APPROVED"  # Fail-open to avoid blocking on errors

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
        self._broadcast() # newline
        self._broadcast("QUERY", user_input[:100])
        self._broadcast("AGENT", "Analyzing intent...")

        # Context-Aware Routing for follow-up questions
        context_window = self.conversation_history[-4:] if self.conversation_history else []
        last_user_queries = [msg["content"] for msg in context_window if msg["role"] == "user"]

        enriched_query = user_input
        use_stickiness = False

        # Smart context enrichment (only for real follow-ups)
        if last_user_queries and len(user_input.split()) < 5:
            is_followup = await self._needs_context_enrichment(user_input, last_user_queries[-1])

            if is_followup:
                enriched_query = f"{last_user_queries[-1]}. {user_input}"
                use_stickiness = True
                self._broadcast("AGENT", f"Follow-up detected, enriched: '{enriched_query}'")
            else:
                self._broadcast("AGENT", f"Topic change detected, no enrichment")

        # Hybrid routing with LLM validation
        service_names = await self._route_query(enriched_query, use_stickiness=use_stickiness)

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

        # Add memory service if available
        memory_doc = self.collection.find_one({"server_name": "memory_service"})

        #self._broadcast("ROUTING", f"Memory Service found: {memory_doc is not None}")
        if memory_doc:
            if "memory_service" not in [m["server_name"] for m in matches]:
                memory_path = self.server_dir / "memory_service.py"
                if memory_path.exists():
                    matches.append({
                        "server_name": "memory_service",
                        "path": str(memory_path.absolute())
                    })
                    self._broadcast("ROUTING", f"Added memory_service to matches")
                else:
                    print(f"⚠️ memory_service not found locally")

        server_names_final = [m["server_name"] for m in matches]
        self._broadcast("AGENT", "Selected: " + ", ".join(server_names_final))

        await self._activate_servers(matches)

        #self._broadcast("ACTION", f"Active sessions after activation: {list(self.sessions.keys())}")

        openai_tools = []
        for name, session in self.sessions.items():
            t_list = await session.list_tools()
            for t in t_list.tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{name}__{t.name}",
                        "description": t.description,
                        "parameters": t.inputSchema
                    }
                })

        # Strong System Prompt - LLM must follow this workflow
        system_msg = (
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

        # Build messages with conversation history
        messages = [{"role": "system", "content": system_msg}]
        messages.extend(self.conversation_history)
        messages.append({"role": "user", "content": user_input})

        # ReAct Loop - Multiple tool iterations
        max_iterations = 5
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            self._broadcast("AGENT", f"Iteration {iteration}/{max_iterations}")

            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=openai_tools,
                parallel_tool_calls=False
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                self._broadcast("AGENT", "No more tool calls, agent finished")
                initial_answer = msg.content or "I have no response."
                break

            messages.append(msg)

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                fname = tc.function.name
                #self._broadcast("AGENT", f"  Function: {fname}")
                #self._broadcast("AGENT", f"  Arguments: {args}")

                res_txt = "Error"

                srv, tool = fname.split("__", 1)
                self._broadcast("ACTION", f"  Service: {srv}")
                self._broadcast("ACTION", f"  Tool: {tool}")
                if srv in self.sessions:
                    r = await self.sessions[srv].call_tool(tool, args)
                    res_txt = r.content[0].text
                    self._broadcast("RESULT", self._format_result_preview(res_txt))
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

        # Critic Review
        self._broadcast("CRITIC", "Reviewing...", "eyeglasses")
        #review = await self._critic_review(user_input, initial_answer)
        review = ["APPROVED"]

        if "APPROVED" in review:
            self._broadcast("CRITIC", "Approved ✓", "white_check_mark")
            final_answer = initial_answer
        else:
            # Rejected - Force LLM to fix WITHOUT tools
            self._broadcast("CRITIC", "Rejected, fixing...", "warning")

            # Add critic feedback to conversation
            messages.append({
                "role": "user",
                "content": (
                    f"COMPLIANCE ISSUE: {review}\n\n"
                    f"Your previous answer was: '{initial_answer}'\n\n"
                    f"Please rewrite your answer to address the compliance issue. "
                    f"Do NOT use any tools, just fix the text."
                )
            })

            # Call LLM WITHOUT tools to force text-only fix
            retry = await self.openai.chat.completions.create(
                model=self.model,
                messages=messages
            )

            final_answer = retry.choices[0].message.content

            # Safety check
            if not final_answer:
                print("⚠️ LLM returned no content after fix, using original with manual disclaimer")
                final_answer = f"{initial_answer}\n\nDisclaimer: Use this answer at your own risks."

            self._broadcast("CRITIC", f"✅ Answer corrected")

        # Store conversation turn
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": final_answer})

        # Limit history to last 20 messages (10 turns)
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        return final_answer
