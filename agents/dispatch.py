#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Agent dispatch — Phase 1 of MULTI_AGENT_PLAN.md.

Selects a DomainAgent for the turn (gated by the AGENT_MODE env flag),
prepares its AgentContext (the same workstream / memory / preferences
blocks the legacy ReAct loop assembles), runs it, and mirrors the legacy
post-processing: workstream attachment per tool call, conversation
history, background summary update, and the routing-decision record.

Also owns the agent-card sync: every catalog agent is published to the
agent_registry.agent_cards collection with an autoEmbed vector index on
`description`, mirroring the mcp_services pattern — agent discovery is
itself an Atlas vector search (used by the Phase 2 coordinator; Phase 1
selects by exact domain match).
"""

import asyncio
import datetime
import time
from typing import List, Optional

from .domain_agent import AgentContext, DomainAgent

AGENT_CARDS_INDEX_NAME = "agent_cards_index"
AGENT_CARDS_INDEX_DEFINITION = {
    "fields": [
        {
            "type":         "autoEmbed",
            "modality":     "text",
            "path":         "description",
            "model":        "voyage-4",
            "quantization": "float",
        },
        {"type": "filter", "path": "domain"},
    ],
}


class AgentDispatchMixin:

    def _select_domain_agent(self, stage1_domains: Optional[List[str]],
                             ws_domain: Optional[str]) -> Optional[DomainAgent]:
        """Pick the DomainAgent for this turn, or None for the legacy
        path. Conservative Phase 1 rule: dispatch only when the domain is
        unambiguous — the workstream classifier resolved a domain, or
        Stage 1 returned exactly one. The AGENT_MODE flag gates which
        domains are agent-enabled (None = all registered)."""
        if not self.domain_agents:
            return None
        enabled = self._agent_domains_enabled
        if enabled is not None and not enabled:
            return None

        def _eligible(domain: Optional[str]) -> Optional[DomainAgent]:
            if not domain:
                return None
            if enabled is not None and domain not in enabled:
                return None
            return self.domain_agents.get(domain)

        if ws_domain:
            return _eligible(ws_domain)
        if stage1_domains and len(stage1_domains) == 1:
            return _eligible(stage1_domains[0])
        return None

    async def _build_agent_context(self, user_input: str,
                                   replay_recipe: str) -> AgentContext:
        """Assemble the per-turn context blocks for a DomainAgent. The
        block texts and broadcasts intentionally mirror the legacy
        assembly in react.py so the agent path behaves and demos the
        same. (The legacy copy is deleted with the legacy path in
        Phase 2.)"""
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
                    bcast_parts.append(
                        f"summary: {summ[:120]}{'…' if len(summ) > 120 else ''}")
                await self._broadcast("WORKSTREAM",
                    f"🗂 Context injected: {self.current_workstream_id} — "
                    + "  |  ".join(bcast_parts))
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
                tier_order = {"core": 0, "extracted": 1, "decayed": 2}
                recalled = sorted(recalled,
                    key=lambda m: tier_order.get(m.get("tier") or "extracted", 1))
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
                tier_counts: dict = {}
                for m in recalled:
                    tier_counts[m.get("tier") or "extracted"] = (
                        tier_counts.get(m.get("tier") or "extracted", 0) + 1)
                tier_summary = ", ".join(
                    f"{n} {t}" for t, n in sorted(tier_counts.items()))
                n_mem = len(recalled)
                await self._broadcast("MEMORY",
                    f"🧠 Recalled {n_mem} relevant "
                    f"{'fact' if n_mem == 1 else 'facts'} ({tier_summary})")
                self._decision_under("memory",
                    recalled_count=len(recalled),
                    tier_breakdown=tier_counts)

            if prefs_recalled:
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
                    f"🧠 Recalled {n_pref} user "
                    f"{'preference' if n_pref == 1 else 'preferences'}")
                self._decision_under("preferences",
                    recalled_count=len(prefs_recalled))
        except Exception as e:
            print(f"⚠️ recall failed (non-fatal): {e}")

        return AgentContext(
            workstream_block=workstream_block,
            memory_block=memory_block,
            preferences_block=preferences_block,
            replay_recipe=replay_recipe,
            conversation_tail=list(self.conversation_history),
        )

    async def _dispatch_to_agent(self, agent: DomainAgent, user_input: str,
                                 replay_recipe: str, turn_t0: float) -> str:
        """Run one turn through a DomainAgent and mirror the legacy
        post-processing (stickiness, workstream attach, history, summary
        task, routing-decision record)."""
        self._decision_set(dispatched_agent=agent.name)
        await self._broadcast("DISPATCH",
            f"→ {agent.name} [{agent.domain}] takes the turn")

        # Sticky domain hint for the next turn's Stage 1 — the agent path
        # equivalent of the legacy last_domain update from Stage 2 matches.
        self.last_domain = agent.domain

        context = await self._build_agent_context(user_input, replay_recipe)

        first_service_seen = False

        async def _on_tool_call(srv: str, tool: str, res_txt: str):
            nonlocal first_service_seen
            if not first_service_seen and srv != "preferences_service":
                self.last_service = srv
                first_service_seen = True
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

        result = await agent.run(user_input, context,
                                 on_tool_call=_on_tool_call)

        if result.verbatim:
            await self._persist_decision(
                tool_calls_count=result.tool_calls_count,
                iterations_used=result.iteration,
                verbatim_short_circuit=True,
                duration_ms=int((time.monotonic() - turn_t0) * 1000))
            return result.answer

        final_answer = result.answer

        # Store conversation turn
        self.conversation_history.append(
            {"role": "user", "content": user_input})
        self.conversation_history.append(
            {"role": "assistant", "content": final_answer})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        # Background workstream summary update — same contract as legacy.
        if self.current_workstream_id:
            t = asyncio.create_task(self._update_workstream_summary(
                self.current_workstream_id, user_input, final_answer))
            self._ws_summary_tasks.add(t)
            t.add_done_callback(self._ws_summary_tasks.discard)

        await self._persist_decision(
            tool_calls_count=result.tool_calls_count,
            iterations_used=result.iteration,
            max_iterations=result.max_iterations,
            max_iterations_hit=(result.iteration >= result.max_iterations),
            had_replay_recipe=bool(replay_recipe),
            agent_services_used=result.services_used,
            agent_status=result.status,
            duration_ms=int((time.monotonic() - turn_t0) * 1000))

        return final_answer

    async def _sync_agent_cards(self):
        """Publish every catalog agent's card to agent_registry.agent_cards
        and ensure the autoEmbed vector index exists. Cards are the
        agents' discovery surface — Phase 2's coordinator selects agents
        via $vectorSearch over `description`, exactly like Stage 2 does
        for services today. Non-fatal on any Atlas error."""
        cards = self.db["agent_cards"]
        now = datetime.datetime.now()
        try:
            names = []
            for agent in self.domain_agents.values():
                tools_claimed = await agent.servers_in_domain()
                await cards.update_one(
                    {"_id": agent.name},
                    {"$set": {
                        "description":   agent.description,
                        "domain":        agent.domain,
                        "tools_claimed": tools_claimed,
                        "last_seen":     now,
                    }},
                    upsert=True)
                names.append(agent.name)
            await cards.delete_many({"_id": {"$nin": names}})
            await self._broadcast("BOOTSTRAP",
                f"🃏 {len(names)} agent cards synced: " + ", ".join(names))
        except Exception as e:
            print(f"⚠️ agent card sync failed (non-fatal): {e}")
            return

        try:
            cursor = await cards.list_search_indexes()
            existing = {i.get("name") async for i in cursor}
            if AGENT_CARDS_INDEX_NAME not in existing:
                from pymongo.operations import SearchIndexModel
                await cards.create_search_index(SearchIndexModel(
                    definition=AGENT_CARDS_INDEX_DEFINITION,
                    name=AGENT_CARDS_INDEX_NAME,
                    type="vectorSearch",
                ))
                await self._broadcast("BOOTSTRAP",
                    f"⚡ {AGENT_CARDS_INDEX_NAME} submitted to Atlas "
                    f"(autoEmbed voyage-4 on description, domain filter)")
        except Exception as e:
            print(f"⚠️ agent_cards_index ensure failed (non-fatal): {e}")
