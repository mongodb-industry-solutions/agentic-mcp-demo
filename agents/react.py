#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The ReAct tool-calling loop — system prompt, per-turn context
assembly (workstream block, recalled memories, preferences, replay
recipe), and the OpenAI tool-iteration loop.

Phase 0 extraction from the former monolithic OrchestratorAgent
(see MULTI_AGENT_PLAN.md). Method bodies are unchanged; they share
state with the composition root in orchestrator.py via mixin `self`.
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


class ReactMixin:

    async def _run_react(self, user_input: str, matches: List[Dict],
                         replay_recipe: str, turn_t0: float) -> Dict:
        """Collect tools from the active sessions, build the
        context-augmented system prompt (workstream block, recalled
        memories, user preferences, replay recipe), and run the ReAct
        tool-calling loop against them.

        Returns a dict consumed by process_query:
          answer            — the final assistant answer text
          verbatim          — True when a VERBATIM tool short-circuit
                              fired (routing-decision record already
                              persisted; caller must return answer
                              as-is and skip post-processing)
          iteration / max_iterations / tool_calls_count — loop stats
                              for the routing-decision record

        Extracted verbatim from process_query in the Phase 0
        decomposition (see MULTI_AGENT_PLAN.md)."""
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
                    "context for a new simulation request.\n"
                    "CRITICAL: When the user describes a NEW customer intent "
                    "('I'm opening a new store at X', 'new branch', 'we need "
                    "connectivity at Y'), always call submit_intent to capture "
                    "it as a NEW intent — do not reuse an existing intent ID "
                    "from the workstream context, even when the site or "
                    "wording looks similar."
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

            # temperature=0 — see the matching note in DomainAgent.run; the
            # legacy direct-to-server ReAct loop had the same unpinned
            # sampling on its tool-selection call.
            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=openai_tools,
                parallel_tool_calls=False,
                temperature=0,
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
                        return {"answer": res_txt[len("VERBATIM:\n"):],
                                "verbatim": True,
                                "iteration": iteration,
                                "max_iterations": max_iterations,
                                "tool_calls_count": tool_calls_count}
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

        return {"answer": initial_answer, "verbatim": False,
                "iteration": iteration,
                "max_iterations": max_iterations,
                "tool_calls_count": tool_calls_count}
