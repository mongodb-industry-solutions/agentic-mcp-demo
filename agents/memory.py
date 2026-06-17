#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Long-term memory — extraction of reusable facts from closed
workstreams, vector recall into the ReAct context, user-preference
recall, and the promotion/decay lifecycle.

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


class MemoryMixin:

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

