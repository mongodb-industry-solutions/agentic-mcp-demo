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
import json
import time
from typing import Dict, List, Optional

from .domain_agent import AgentContext, AgentResult, DomainAgent

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

    async def _rank_agents_via_cards(self, query: str,
                                     domains: List[str]) -> List[tuple]:
        """Rank candidate agents by $vectorSearch over agent_cards —
        the same Atlas primitive Stage 2 uses for services, applied to
        the agents' discovery surface (Phase 2 of MULTI_AGENT_PLAN.md).
        Returns [(domain, score), …] best-first; falls back to the
        Stage 1 order with null scores if the index isn't ready."""
        cards = self.db["agent_cards"]
        pipeline = [
            {"$vectorSearch": {
                "index":         AGENT_CARDS_INDEX_NAME,
                "path":          "description",
                "query":         query,
                "filter":        {"domain": {"$in": domains}},
                "numCandidates": 50,
                "limit":         max(len(domains), 1),
            }},
            {"$project": {
                "_id": 1, "domain": 1,
                "score": {"$meta": "vectorSearchScore"},
            }},
        ]
        try:
            cursor = await cards.aggregate(pipeline)
            hits = await cursor.to_list()
            ranked = [(h["domain"], h.get("score")) for h in hits
                      if h.get("domain") in self.domain_agents]
            # Append any candidate the index missed so Stage 1's verdict
            # is never silently dropped by a stale/partial card index.
            seen = {d for d, _ in ranked}
            ranked += [(d, None) for d in domains if d not in seen]
            if ranked:
                return ranked
        except Exception as e:
            print(f"⚠️ agent card ranking failed (non-fatal): {e}")
        return [(d, None) for d in domains]

    async def _select_agents_for_turn(
            self, user_input: str,
            stage1_domains: Optional[List[str]],
            ws_domain: Optional[str]) -> List[DomainAgent]:
        """Resolve this turn to zero, one, or several DomainAgents.

        Phase 2 rules, in precedence order:
        1. Stage 1 put TWO OR MORE agent-enabled domains in scope →
           multi-agent turn (card-ranked), even inside a workstream —
           cross-domain questions are inherently cross-workstream.
        2. The workstream classifier resolved an agent-enabled domain →
           that single agent (session continuity).
        3. Stage 1 resolved exactly one agent-enabled domain → that agent.
        4. Otherwise → [] (legacy direct-to-server path).

        The AGENT_MODE flag gates which domains are agent-enabled
        (None = all registered)."""
        if not self.domain_agents:
            return []
        enabled = self._agent_domains_enabled
        if enabled is not None and not enabled:
            return []

        def _ok(domain: Optional[str]) -> bool:
            return bool(domain) and domain in self.domain_agents \
                and (enabled is None or domain in enabled)

        candidates: List[str] = []
        for d in stage1_domains or []:
            if _ok(d) and d not in candidates:
                candidates.append(d)

        if len(candidates) >= 2:
            ranked = await self._rank_agents_via_cards(user_input, candidates)
            self._decision_under("agent_cards", ranked=[
                {"agent": self.domain_agents[d].name,
                 "score": s} for d, s in ranked])
            return [self.domain_agents[d] for d, _ in ranked]
        if _ok(ws_domain):
            return [self.domain_agents[ws_domain]]
        if len(candidates) == 1:
            return [self.domain_agents[candidates[0]]]
        return []

    async def _split_subtasks(self, user_input: str,
                              agents: List[DomainAgent]) -> Dict[str, str]:
        """Scope the user's request into one focused sub-task per agent
        (gpt-4o-mini, like the other routing helpers). Returns
        {domain: sub-task}; an agent the splitter rules out gets no
        entry. Any failure degrades to every agent receiving the full
        query — over-asking is safe, dropping an agent is not."""
        prompt = (
            "You are the coordinator of domain-specialist agents. Split "
            "the user's request into one focused sub-task per agent, "
            "phrased as a self-contained instruction. Use null for an "
            "agent that has nothing to contribute to this request.\n\n"
            f"User request: {user_input!r}\n\n"
            "Agents:\n"
            + "\n".join(f"- {a.domain}: {a.description[:220]}"
                        for a in agents)
            + "\n\nReturn ONLY JSON, one key per agent domain: "
              '{"<domain>": "<sub-task>"|null, ...}'
        )
        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(resp.choices[0].message.content)
            out = {}
            for a in agents:
                st = parsed.get(a.domain)
                if isinstance(st, str) and st.strip():
                    out[a.domain] = st.strip()
            if out:
                return out
        except Exception as e:
            print(f"⚠️ sub-task split failed (non-fatal): {e}")
        return {a.domain: user_input for a in agents}

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
            agent_consults=result.consults,
            duration_ms=int((time.monotonic() - turn_t0) * 1000))

        return final_answer

    async def _dispatch_multi(self, agents: List[DomainAgent],
                              user_input: str, replay_recipe: str,
                              turn_t0: float) -> str:
        """Run one turn across several DomainAgents concurrently and
        synthesize a single answer (Phase 2 of MULTI_AGENT_PLAN.md).
        The shared stdio session pool is safe under concurrency via the
        per-session locks in _call_tool_locked; the agents' domains are
        disjoint so their server sets don't overlap anyway."""
        names = [a.name for a in agents]
        self._decision_set(dispatched_agents=names)
        await self._broadcast("DISPATCH",
            "⇉ parallel dispatch: " + ", ".join(
                f"{a.name} [{a.domain}]" for a in agents))

        # Context (workstream block, memories, preferences) is built once
        # and shared — both agents see the same operational memory.
        context = await self._build_agent_context(user_input, replay_recipe)

        subtasks = await self._split_subtasks(user_input, agents)
        active = [a for a in agents if a.domain in subtasks]
        if not active:                      # splitter ruled everyone out
            active = agents
            subtasks = {a.domain: user_input for a in agents}
        for a in active:
            await self._broadcast("DISPATCH",
                f"  {a.name} ← {subtasks[a.domain][:160]}")
        self._decision_under("multi", subtasks={
            a.name: subtasks[a.domain] for a in active})

        # If the splitter narrowed the turn to one agent, it's a plain
        # single dispatch — same post-processing, no synthesis.
        if len(active) == 1:
            return await self._dispatch_to_agent(
                active[0], user_input, replay_recipe, turn_t0)

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

        self.last_domain = active[0].domain  # best-ranked agent's domain

        # Pre-activate ALL registered agents' domain servers from THIS
        # task before fanning out. MCP stdio sessions are anyio-scoped:
        # the context managers entered on the shared AsyncExitStack must
        # be entered in the same task that exits the stack at shutdown.
        # The gather() below runs agents in child tasks — activating
        # there crashes aclose() with "Attempted to exit cancel scope in
        # a different task". All agents (not just the active ones) are
        # covered because any registered agent can be consulted mid-turn
        # from a child task (Phase 3). Inside run(),
        # _activate_domain_servers sees the sessions present and skips.
        for a in self.domain_agents.values():
            names = await a.servers_in_domain()
            await self._activate_servers(self._resolve_server_paths(names))

        results = await asyncio.gather(
            *[a.run(subtasks[a.domain], context, on_tool_call=_on_tool_call)
              for a in active],
            return_exceptions=True)

        sections: List[tuple] = []          # (agent, AgentResult)
        for a, r in zip(active, results):
            if isinstance(r, Exception):
                print(f"⚠️ {a.name} failed: {r}")
                r = AgentResult(
                    answer=f"({a.name} failed: {type(r).__name__}: {r})",
                    status="error")
            sections.append((a, r))

        # Synthesis. VERBATIM content must reach the user untouched, so
        # if any agent short-circuited verbatim we compose labelled
        # sections instead of paraphrasing through an LLM.
        synthesis_t0 = time.monotonic()
        if any(r.verbatim for _, r in sections):
            final_answer = "\n\n".join(
                f"## {a.name} [{a.domain}]\n\n{r.answer}"
                for a, r in sections)
        else:
            synth_messages = [
                {"role": "system", "content": (
                    "You are the coordinator of domain-specialist agents "
                    "in a network operations system. Combine the "
                    "specialist answers below into ONE coherent response "
                    "to the user's request.\n"
                    "- Use ONLY facts from the specialist answers — do "
                    "not invent data.\n"
                    "- Keep concrete details: IDs, metrics, statuses, "
                    "tables, runbook steps.\n"
                    "- If a specialist reported an error or no data, say "
                    "so plainly.\n"
                    "- Operational tone for NOC engineers; speak in "
                    "third person about customers."
                )},
                {"role": "user", "content": (
                    f"User request: {user_input}\n\n"
                    + "\n\n".join(
                        f"### {a.name} [{a.domain}]\n{r.answer}"
                        for a, r in sections))},
            ]
            synth = await self.openai.chat.completions.create(
                model=self.model, messages=synth_messages)
            final_answer = (synth.choices[0].message.content
                            or "\n\n".join(r.answer for _, r in sections))
        synthesis_ms = int((time.monotonic() - synthesis_t0) * 1000)

        # Store conversation turn
        self.conversation_history.append(
            {"role": "user", "content": user_input})
        self.conversation_history.append(
            {"role": "assistant", "content": final_answer})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        if self.current_workstream_id:
            t = asyncio.create_task(self._update_workstream_summary(
                self.current_workstream_id, user_input, final_answer))
            self._ws_summary_tasks.add(t)
            t.add_done_callback(self._ws_summary_tasks.discard)

        await self._persist_decision(
            tool_calls_count=sum(r.tool_calls_count for _, r in sections),
            iterations_used=max((r.iteration for _, r in sections),
                                default=0),
            max_iterations=max((r.max_iterations for _, r in sections),
                               default=0),
            max_iterations_hit=any(
                r.iteration >= r.max_iterations and r.max_iterations
                for _, r in sections),
            had_replay_recipe=bool(replay_recipe),
            agents={a.name: {
                "status":        r.status,
                "subtask":       subtasks[a.domain],
                "tool_calls":    r.tool_calls_count,
                "consults":      r.consults,
                "services_used": r.services_used,
                "verbatim":      r.verbatim,
            } for a, r in sections},
            synthesis_ms=synthesis_ms,
            duration_ms=int((time.monotonic() - turn_t0) * 1000))

        return final_answer

    async def _consult_agent(self, from_agent: DomainAgent,
                             to_name: str, question: str) -> str:
        """Shell-mediated agent-to-agent consultation (Phase 3 of
        MULTI_AGENT_PLAN.md). The consulted agent runs at depth=1 (no
        consult tool — recursion is structurally impossible) with a
        smaller single-turn iteration budget and a bare context: the
        question must be self-contained. Every exchange is persisted to
        agent_registry.agent_conversations as the audit trail."""
        target = next(
            (a for a in self.domain_agents.values()
             if a.name == to_name or a.domain == to_name), None)
        if target is None or target is from_agent:
            available = ", ".join(
                a.name for a in self.domain_agents.values()
                if a is not from_agent)
            return (f"❌ No such agent: {to_name!r}. "
                    f"Available: {available or '(none)'}")

        await self._broadcast("DISPATCH",
            f"↔ {from_agent.name} consults {target.name}: {question[:140]}")
        t0 = time.monotonic()
        try:
            result = await target.run(question, AgentContext(),
                                      depth=1, max_iterations=3)
            answer = result.answer
            status = result.status
        except Exception as e:
            print(f"⚠️ consultation {from_agent.name}→{target.name} "
                  f"failed: {e}")
            answer = (f"❌ Consultation failed: {type(e).__name__}: {e}")
            result = None
            status = "error"

        try:
            await self.db["agent_conversations"].insert_one({
                "ts":            datetime.datetime.now(),
                "workstream_id": self.current_workstream_id,
                "from_agent":    from_agent.name,
                "to_agent":      target.name,
                "question":      question,
                "answer":        answer,
                "status":        status,
                "tool_calls":    result.tool_calls_count if result else 0,
                "services_used": result.services_used if result else [],
                "duration_ms":   int((time.monotonic() - t0) * 1000),
            })
        except Exception as e:
            print(f"⚠️ agent_conversations persist failed (non-fatal): {e}")

        await self._broadcast("DISPATCH",
            f"↩ {target.name} → {from_agent.name}: "
            + self._format_result_preview(answer))
        return answer

    async def _ensure_agent_conversation_indexes(self):
        """agent_conversations is queried by recency (live feed,
        analytics) and by workstream (audit trail per thread)."""
        try:
            conv = self.db["agent_conversations"]
            await conv.create_index([("ts", -1)], name="conv_recency")
            await conv.create_index([("workstream_id", 1)],
                                    name="conv_workstream")
        except Exception as e:
            print(f"⚠️ agent_conversations index ensure failed "
                  f"(non-fatal): {e}")

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
