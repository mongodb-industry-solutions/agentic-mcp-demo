#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Two-stage semantic routing — Stage 1 domain classification
(gpt-4o-mini over the domain taxonomy) and Stage 2 $vectorSearch
within the selected domains, plus the per-turn routing-decision
analytics record.

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


class RouterMixin:

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

    def _text_match_score(self, query: str, description: str) -> float:
        """Score a service by how many distinctive query phrases literally
        appear in its embedded description (which contains its trigger
        phrases verbatim). Longer n-gram matches are worth more — a 3-token
        overlap is a much stronger signal than three isolated tokens.

        Used as a deterministic Stage 2 tiebreaker before the LLM call.
        Even with full-precision (`quantization: float`) voyage-4 autoEmbed,
        sibling services that share vocabulary occasionally land within
        ~0.005 cosine of each other; literal phrase overlap with the
        trigger phrases is the objective signal that disambiguates them
        without an LLM round-trip.
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

    async def _semantic_search(self, query: str, limit: int = 5,
                               domains: List[str] | None = None) -> List[Dict]:
        """Stage 2: vector search, optionally pre-filtered to one or more
        domains. Uses Atlas autoEmbed against voyage-4 — Atlas embeds the
        `description` field at insert/update time AND embeds the raw query
        text on every search, applying voyage-4's asymmetric input_type
        prompts ('document' vs 'query') internally. The autoEmbed index is
        configured with `quantization: float` so scores keep full float32
        precision and stay discriminative (the default `scalar` int8
        quantization compressed sibling-service scores into a noise band).

        Falls back to unfiltered search if Atlas rejects the filter
        (index hasn't been re-configured to include `domain` yet) — flips
        a flag so we don't keep trying."""
        def _build_pipeline(filter_doc: dict | None):
            vs: dict = {
                "index":         "vector_index",
                "path":          "description",
                "query":         query,
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

