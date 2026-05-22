#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Diagnostic for the agentic-MCP routing index.

Answers three questions, in order:
  1. Did the orchestrator write the short discriminator into `description`,
     or is the long boilerplate-heavy docstring still in there?
  2. Is the Atlas `vector_index` on agent_registry.mcp_services Active,
     and does it embed `description` with the model we expect?
  3. Does a direct $vectorSearch produce a visible score spread for a clear
     query, or are scores still clustered (indicating the index hasn't
     re-embedded since the last description update)?

Run:
    python seed/check_routing_index.py
"""

import os
import sys
from pymongo import MongoClient


MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

client = MongoClient(MONGO_URI)
db     = client[DB_NAME]
coll   = db["mcp_services"]


def section(title: str):
    print("\n" + "━" * 72)
    print(f"  {title}")
    print("━" * 72)


# ─── 1. What's actually stored in `description` and `full_description` ────

section("1. Stored description fields (sample: IBN services)")
docs = list(coll.find(
    {"server_name": {"$regex": "^ibn_"}},
    {"_id": 0, "server_name": 1, "description": 1,
     "full_description": 1, "domain": 1},
).sort("server_name", 1))

if not docs:
    print("⚠️  No ibn_* services found in agent_registry.mcp_services.")
    print("    Either the orchestrator never synced, or you have a different demo loaded.")
else:
    for d in docs:
        desc      = d.get("description") or ""
        full      = d.get("full_description") or ""
        domain    = d.get("domain") or "(none)"
        print(f"\n  ▸ {d['server_name']}  [domain={domain}]")
        print(f"      description     : {len(desc):>4} chars")
        print(f"      full_description: {len(full):>4} chars {'(MISSING)' if not full else ''}")
        first_line = next((ln for ln in desc.splitlines() if ln.strip()), "")
        print(f"      first line      : {first_line[:100]}")

if docs and not docs[0].get("full_description"):
    print("\n⚠️  full_description is missing on at least one doc.")
    print("    This means the orchestrator was NOT restarted (or the sync didn't")
    print("    run) after the discriminator-split commit. Restart main.py and the")
    print("    sync will backfill on the next startup.")

# Detect: did the orchestrator already write the SHORT discriminator, or is
# the long docstring still there? Heuristic: a real discriminator is < 600
# chars (we cap there) and does NOT contain 'Use this service when'.
if docs:
    likely_long = sum(
        1 for d in docs
        if len(d.get("description") or "") > 700
        or "Use this service when" in (d.get("description") or "")
    )
    if likely_long:
        print(f"\n⚠️  {likely_long}/{len(docs)} ibn_* services still have the LONG docstring")
        print("    in `description`. The discriminator split hasn't landed in MongoDB yet.")
        print("    Restart main.py and watch for the BOOTSTRAP '↻ ibn_*' lines.")
    else:
        print(f"\n✓ All {len(docs)} ibn_* services have the focused discriminator in `description`.")


# ─── 2. Atlas search index configuration ──────────────────────────────────

section("2. Atlas search indexes on mcp_services")
try:
    idxs = list(coll.list_search_indexes())
    if not idxs:
        print("⚠️  No search indexes found on mcp_services!")
        print("    The orchestrator's $vectorSearch will fail. Create vector_index")
        print("    in the Atlas UI with auto-embed on `description`.")
    for idx in idxs:
        print(f"\n  ▸ {idx.get('name')}  [type={idx.get('type')}, "
              f"status={idx.get('status')}, queryable={idx.get('queryable')}]")
        latest = idx.get("latestDefinition") or idx.get("definition") or {}
        fields = latest.get("fields") or []
        for fld in fields:
            ftype = fld.get("type")
            path  = fld.get("path")
            extras = []
            if ftype == "text":
                extras.append(f"model={fld.get('model')}")
                extras.append(f"similarity={fld.get('similarity')}")
            elif ftype == "vector":
                extras.append(f"dims={fld.get('numDimensions')}")
                extras.append(f"similarity={fld.get('similarity')}")
            extras_str = ", ".join(e for e in extras if e and "None" not in e)
            print(f"      {ftype:<8} on `{path}`  {('— ' + extras_str) if extras_str else ''}")
except Exception as e:
    print(f"⚠️  Could not list search indexes: {e}")


# ─── 3. Live vector search — does the spread look healthy? ────────────────

section("3. Live $vectorSearch against vector_index")
queries = [
    "I'm opening a new Alpenmarkt store at Marienplatz Munich. POS priority, "
    "guest WiFi strict, camera uplink, online by 18:00, max 40ms POS latency",
    "feasibility check on intent IBN-005",
    "diagnose this violation, find similar past incident",
    "inject morning rush at Marienplatz",
    "list spare access nodes nearby",
]

for q in queries:
    print(f"\n  ▸ query: {q[:80]}{'…' if len(q) > 80 else ''}")
    try:
        cursor = coll.aggregate([
            {
                "$vectorSearch": {
                    "index": "vector_index",
                    "path":  "description",
                    "query": q,
                    "filter": {"domain": {"$in": ["ibn"]}},
                    "numCandidates": 50,
                    "limit": 5,
                }
            },
            {
                "$project": {
                    "_id": 0, "server_name": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ])
        rows = list(cursor)
        if not rows:
            print("      (no results — index may not be ready yet)")
            continue
        for r in rows:
            print(f"      {r['server_name']:<32s} {r.get('score', 0):.4f}")
        if len(rows) >= 2:
            spread = rows[0]["score"] - rows[-1]["score"]
            verdict = ("👍 healthy" if spread > 0.05 else
                       "⚠ tight cluster — descriptions still too similar" if spread > 0.005 else
                       "🚨 nearly flat — index probably not re-embedded yet")
            print(f"      [spread top→bottom = {spread:.4f} — {verdict}]")
    except Exception as e:
        print(f"      ⚠ query failed: {e}")

print()
client.close()
