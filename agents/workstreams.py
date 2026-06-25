#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Workstream layer — the agent's short-term working memory.
Classification of queries into open workstreams, lifecycle
(create/attach/close), closure cues, meta-query detection, replay
recipes, and background summary updates.

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


class WorkstreamMixin:

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
            # NB: a brand-new workstream has executed NOTHING yet. Phrase
            # the seed summary so the agent doesn't misread it as "the
            # action already happened" and skip the tool call — it used to
            # be "Started: <request>", which made the agent reply "already
            # submitted" without ever calling submit_intent.
            "summary":        (f"New workstream — no actions executed yet. "
                               f"Original request: {seed_query[:200]}"),
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

