#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The coordinator's Atlas side (Phase 4 of AGENT_ENGINE_PLAN.md): agent
ranking by $vectorSearch over agent_cards and the routing-decision record.
Both are internal tools in the tool sandbox (the agent sandbox has no
database access), called by the coordinator's nodes, never by the LLM.

A2A discovery filters only by skill and capability tags, so the semantic
"routing through Atlas" step stays here: the same autoEmbed index and card
documents as the legacy shell (agents/dispatch.py), with `a2a_name` added
so the coordinator can call the agent.
"""

import datetime
import json

from oss_tools._ctx import ToolSet, collection
from pymongo.errors import PyMongoError
from telco_catalog import SPECS

tools = ToolSet()

cards = collection("agent_cards")
decisions = collection("routing_decisions", mutable=True)
approvals = collection("agent_approvals", mutable=True)

CARDS_INDEX = "agent_cards_index"
CARDS_INDEX_DEFINITION = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": "description",
         "model": "voyage-4", "quantization": "float"},
        {"type": "filter", "path": "domain"},
    ],
}

_cards_synced = False


def _sync_cards() -> None:
    """Publish the catalog agents' cards once per process, like the legacy
    shell does at startup, and submit the index if it's missing. Ranking
    falls back to the classifier's order until the index is queryable."""
    global _cards_synced
    if _cards_synced:
        return
    now = datetime.datetime.now()
    for spec in SPECS:
        cards.update_one({"_id": spec["name"]}, {"$set": {
            "description": spec["description"], "domain": spec["domain"],
            "a2a_name": spec["name"].replace("_", "-"), "last_seen": now,
        }}, upsert=True)
    if CARDS_INDEX not in {i.get("name") for i in cards.list_search_indexes()}:
        from pymongo.operations import SearchIndexModel

        cards.create_search_index(SearchIndexModel(
            definition=CARDS_INDEX_DEFINITION, name=CARDS_INDEX, type="vectorSearch"))
    _cards_synced = True


@tools.tool
def rank_agents(query: str, domains: list[str]) -> str:
    """Internal (not offered to the LLM). Rank the candidate domains' agents
    for this query by $vectorSearch over agent_cards. Returns JSON
    [{"domain", "agent", "score"}] best first; score is null for a
    candidate the index didn't return (or when it isn't ready yet)."""
    ranked: list[dict] = []
    try:
        _sync_cards()
        hits = cards.aggregate([
            {"$vectorSearch": {"index": CARDS_INDEX, "path": "description", "query": query,
                               "filter": {"domain": {"$in": domains}},
                               "numCandidates": 50, "limit": max(len(domains), 1)}},
            {"$project": {"domain": 1, "a2a_name": 1, "score": {"$meta": "vectorSearchScore"}}},
        ])
        ranked = [{"domain": h["domain"], "agent": h.get("a2a_name"),
                   "score": round(h["score"], 4)} for h in hits if h.get("domain") in domains]
    except PyMongoError as e:
        print(f"⚠️ agent card ranking failed (non-fatal): {e}")
    seen = {r["domain"] for r in ranked}
    ranked += [{"domain": d, "agent": f"{d}-agent", "score": None} for d in domains if d not in seen]
    return json.dumps(ranked)


@tools.tool
def record_routing(decision: dict) -> str:
    """Internal (not offered to the LLM). Persist one coordinator turn to the
    session's routing_decisions (the routing-analytics collection the
    legacy shell also writes)."""
    decisions.insert_one({"ts": datetime.datetime.now(), "runtime": "agent-engine", **decision})
    return json.dumps({"recorded": True})


@tools.tool
def pending_reviews(execution_ids: list[str]) -> str:
    """Internal (not offered to the LLM). The pending NOC reviews that paused
    the given delegated executions (oss_tools.approvals records each
    review's execution). Returns JSON [{approval_id, action, title, summary,
    execution_id, session_id, workspace_id}]."""
    found = approvals.find(
        {"status": "pending", "execution_id": {"$in": execution_ids}},
        {"_id": 1, "action": 1, "title": 1, "summary": 1, "execution_id": 1,
         "session_id": 1, "workspace_id": 1},
    ).sort("seq", 1)
    return json.dumps([{"approval_id": a.pop("_id"), **a} for a in found])


@tools.tool
def runbooks_for_memory() -> str:
    """Internal (not offered to the LLM). The IBN runbooks and DTW mitigation
    playbooks from the shared knowledge chunks, for seeding procedural
    memory (telco_runtime.memory). Returns JSON [{_id, title, text, domain}]."""
    out = []
    for domain in ("ibn", "dtw"):
        for rb in collection(f"{domain}_knowledge_chunks").find(
                {"kind": "runbook"}, {"_id": 1, "title": 1, "text": 1}).sort("_id", 1):
            out.append({**rb, "domain": domain})
    return json.dumps(out)
