#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
MCP service registry — filesystem discovery of mcp_servers/*.py,
hash-based change detection, domain tagging, and sync into the
vector-indexed agent_registry.mcp_services collection.

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


class RegistryMixin:

    async def _watch_servers(self):
        """Re-sync registry whenever a .py file in mcp_servers/ is added,
        changed, or deleted. watchfiles debounces rapid saves automatically.

        Hot-reload is a dev convenience; disable it with
        DEMO_DISABLE_FILE_WATCH=1. We force POLLING because watchfiles'
        native backend (the Rust `notify` crate) has no NetBSD support and
        busy-loops there — pegging the event loop at ~100% CPU and starving
        every other coroutine (WS handling, change streams, sessions).
        Polling a handful of files once a second costs ~nothing and behaves
        identically on every platform."""
        if os.environ.get("DEMO_DISABLE_FILE_WATCH"):
            return
        try:
            async for _ in awatch(self.server_dir,
                                  watch_filter=lambda _, p: p.endswith(".py"),
                                  force_polling=True, poll_delay_ms=1000):
                await self._sync_registry()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Never let a watcher failure spin or crash the app.
            print(f"⚠️ file watcher disabled ({e})")

    def _extract_docstring(self, file_path: Path) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
                return ast.get_docstring(tree) or f"Service: {file_path.stem}"
        except:
            return f"Service: {file_path.stem}"

    # Stopwords for the text-match tiebreaker — content words only.
    _TM_STOPWORDS = frozenset({
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at",
        "for", "with", "from", "by", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "should", "can", "could", "we", "us", "you", "i", "me", "my", "our",
        "your", "where", "what", "when", "how", "why", "this", "that",
        "these", "those", "it", "if", "as", "so", "than", "then", "into",
        "out", "up", "down", "over", "under", "again", "now", "very",
    })

    def _extract_discriminator(self, full_docstring: str, server_name: str) -> str:
        """
        Build the text that gets EMBEDDED for Stage 2 vector routing.

        Architectural beat: voyage-4 (and any bi-encoder) collapses sibling
        services to within 0.001-0.005 of each other when the embedded text
        is dominated by shared exposition — "ACME", "QoS", "plan", "scenario",
        "downlink" all appear in every DTW service description. The tie-break
        LLM then has to disambiguate on every query, which is slow and
        stochastic.

        The fix: stop embedding the exposition. Embed only:
          1. The one-line service tagline (what it IS)
          2. The verbatim trigger phrases from the "Use this service when"
             section (what users SAY)

        This makes each service's embedding a centroid of expected user
        queries. Cosine similarity becomes sharply discriminative: scenario
        descriptions land closer to scenario-service triggers than to
        simulation-service triggers, and the score gap widens enough to skip
        the tie-break entirely.

        Negative-scope guards ("This service does NOT…", "🚫 NOT this
        service") are stripped — they describe sibling services and pollute
        the embedding with the wrong vocabulary.

        Returns a string formatted as:
            <tagline>
            Users invoke this with queries like:
            <quoted trigger phrases, one per line>
        """
        if not full_docstring or not full_docstring.strip():
            return f"Service: {server_name}"

        lines = full_docstring.splitlines()

        # 1. Tagline — first non-empty line, drop "Title — " prefix if any.
        tagline = ""
        for ln in lines:
            s = ln.strip()
            if s:
                tagline = s
                break
        if " — " in tagline:
            tagline = tagline.split(" — ", 1)[1].strip()
        elif tagline.startswith("SERVER:"):
            tagline = tagline[len("SERVER:"):].strip()

        # 2. Trigger phrases — content of the "Use this service when" section,
        # stopping at the first paragraph break (blank line) or negative guard.
        # The trigger section is a bulleted list; trailing prose after it must
        # be excluded or it pollutes the embedding with shared vocabulary.
        trigger_lines: list[str] = []
        in_section = False
        for ln in lines:
            s = ln.strip()
            if re.search(r"use this service when", s, re.I):
                in_section = True
                continue
            if not in_section:
                continue
            # Negative guards / cross-service notes — hard stop.
            if (s.startswith("🚫")
                or re.match(r"this service (does not|is not|operates|only)", s, re.I)
                or re.match(r"both .* tools accept", s, re.I)):
                break
            # Blank line after content → end of bullet block, prose follows.
            if not s and trigger_lines:
                break
            # Skip leading blanks before the first bullet.
            if not s:
                continue
            # Prose break: line is not a bullet and not a quoted continuation.
            if trigger_lines and not (s.startswith("-") or s.startswith('"')):
                break
            trigger_lines.append(s)

        if not trigger_lines:
            # No "Use this service when" section — fall back to the previous
            # behaviour: full text up to the negative guards, capped at 800.
            text = full_docstring
            for marker in ("\n🚫 NOT this service",
                           "\nThis service does NOT",
                           "\nThis service is NOT"):
                idx = text.find(marker)
                if idx > 0:
                    text = text[:idx]
            return text.strip()[:800]

        triggers = "\n".join(trigger_lines)
        result = (
            f"{tagline}\n\n"
            f"Users invoke this with queries like:\n{triggers}"
        )
        # 1500-char cap — trigger sections are typically 400-1000 chars; this
        # leaves headroom while still keeping the embedding focused.
        return result[:1500]

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
        naturally; singleton services (preferences_service, restaurant_guide,
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
        # `domain` after the two-stage routing upgrade or missing
        # `full_description` after the embedded-discriminator split).
        changed_servers = set()
        for name in potential_updates:
            local_hash = local_servers[name]["file_hash"]
            db_hash   = db_servers[name].get("file_hash", "")
            db_doc    = db_servers[name]
            needs_backfill = (
                not db_doc.get("domain")
                or not db_doc.get("full_description")
                # Re-sync when the discriminator logic changed even if the
                # file itself didn't — description drift without hash drift.
                # Atlas autoEmbed will re-embed the changed `description`
                # field on the update automatically.
                or db_doc.get("description") != local_servers[name]["description"]
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

