#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
NOC approval for blast-radius actions (Phase 3 of AGENT_ENGINE_PLAN.md).

Agent Engine only: the agents' review node calls open_review in the tool
sandbox before it suspends the run with interrupt(), and close_review once
the reviewer has decided. Both are durable tool activities, so on resume
the platform replays their recorded results and the interrupt payload is
rebuilt identically. The legacy MCP runtime has no review step and no shim
for this module.

Each review is one document in the session's agent_approvals collection
(the audit trail). While it is pending, the documents it would change carry
a `pending_approval` marker, which the dashboards' existing change streams
pick up to show "awaiting NOC approval".
"""

import datetime
import json

from pymongo import DESCENDING

from oss_tools._ctx import ToolSet, collection

tools = ToolSet()

approvals = collection("agent_approvals", mutable=True)
intents = collection("ibn_intents", mutable=True)
policy_snapshots = collection("ibn_policy_snapshots", mutable=True)
compliance_events = collection("ibn_compliance_events", mutable=True)
sites = collection("ibn_sites")
knowledge_chunks = collection("ibn_knowledge_chunks")
scenarios = collection("dtw_scenarios", mutable=True)

APPROVE, REJECT, CANARY = "approve", "reject", "approve_canary"


def _site_name(site_id):
    site = sites.find_one({"_id": site_id}) if site_id else None
    return site["name"] if site else (site_id or "unknown site")


def _intent(intent_id=None, site=None):
    """The intent an IBN action targets, resolved the way the tools do."""
    from oss_tools.ibn.assurance import _resolve_intent

    return _resolve_intent(intent_id, site)


def _activate_plan(args):
    intent = _intent(args.get("intent_id"))
    if not intent:
        return None
    plan = policy_snapshots.find_one({"intent_id": intent["_id"]}, sort=[("snapshot_at", DESCENDING)])
    site = _site_name(intent.get("site_id"))
    targets = (intent.get("parsed") or {}).get("targets") or {}
    details = {
        "intent": intent["_id"],
        "customer": intent.get("customer_id"),
        "site": site,
        "status": intent.get("status"),
        "targets": targets,
    }
    if plan:
        details["plan"] = {
            k: plan.get(k)
            for k in ("access_node", "uplink", "uplink_mbps", "cpe", "template", "deadline")
        }
    return {
        "title": f"Activate {intent['_id']} at {site}",
        "summary": (
            f"Go live at {site}: services become customer-facing and assurance "
            f"monitoring starts. Template {(plan or {}).get('template') or 'from feasibility'}."
        ),
        "details": details,
        "targets": {"ibn_intents": [intent["_id"]]},
    }


def _apply_runbook(args):
    intent = _intent(args.get("intent_id"), args.get("site"))
    if not intent:
        return None
    runbook_id = args.get("runbook_id")
    runbook = knowledge_chunks.find_one({"_id": runbook_id, "kind": "runbook"}) or {}
    violation = compliance_events.find_one(
        {"intent_id": intent["_id"], "kind": "violation"}, sort=[("ts", DESCENDING)]
    )
    site = _site_name(intent.get("site_id"))
    details = {
        "intent": intent["_id"],
        "site": site,
        "status": intent.get("status"),
        "runbook": runbook_id,
        "runbook_title": runbook.get("title"),
        "runbook_text": (runbook.get("text") or "")[:400],
    }
    if violation:
        details["violation"] = {
            "metric": violation.get("metric"),
            "observed": violation.get("observed"),
            "threshold": violation.get("threshold"),
        }
    return {
        "title": f"Apply runbook {runbook_id} to {intent['_id']} at {site}",
        "summary": f"Change the live network at {site}: {runbook.get('title') or runbook_id}.",
        "details": details,
        "targets": {"ibn_intents": [intent["_id"]]},
    }


def _update_template_version(args):
    template_id = args.get("template_id") or "strict-retail-v3"
    new_version = args.get("new_version") or "strict-retail-v4"
    query = {"template": template_id, "status": "active"}
    if args.get("intent_ids"):
        query["_id"] = {"$in": list(args["intent_ids"])}
    affected = list(intents.find(query, {"_id": 1, "site_id": 1}).sort("_id", 1))
    sites_affected = [{"intent": it["_id"], "site": _site_name(it.get("site_id"))} for it in affected]
    staged = bool(args.get("intent_ids"))
    review = {
        "title": f"Template {template_id} → {new_version}"
        + (f" for {len(affected)} remaining site(s)" if staged else ""),
        "summary": (
            f"Blast radius: {len(affected)} active intent(s) on {template_id} are flagged "
            f"for migration in the next change window."
        ),
        "details": {"template": template_id, "new_version": new_version, "sites": sites_affected},
        "blast_radius": len(affected),
        "targets": {"ibn_intents": [it["_id"] for it in affected]},
    }
    if len(affected) > 1 and not staged:
        # L4 story: migrate one site, re-check it, then ask again for the rest.
        review["allowed_decisions"] = [APPROVE, CANARY, REJECT]
        review["canary"] = {
            "intent": affected[0]["_id"],
            "site": sites_affected[0]["site"],
            "rest": [it["_id"] for it in affected[1:]],
        }
    return review


def _cancel_intent(args):
    intent = _intent(args.get("intent_id"))
    if not intent:
        return None
    site = _site_name(intent.get("site_id"))
    return {
        "title": f"Cancel {intent['_id']} at {site}",
        "summary": f"Cancel the {intent.get('status')} intent at {site}. Reason: "
        f"{args.get('reason') or 'customer request'}.",
        "details": {"intent": intent["_id"], "site": site, "status": intent.get("status")},
        "targets": {"ibn_intents": [intent["_id"]]},
    }


def _scenario_line(s):
    results = s.get("results") or {}
    verdict = results.get("narrative_summary") or "not simulated"
    return {"scenario": s["_id"], "status": s.get("status"), "description": s.get("description"),
            "simulation": verdict[:240]}


def _delete_scenario(args):
    s = scenarios.find_one({"_id": args.get("scenario_id")})
    if not s:
        return None
    return {
        "title": f"Delete scenario {s['_id']}",
        "summary": "Hard delete: the document and its simulation results are removed, "
        "not just cancelled.",
        "details": _scenario_line(s),
        "targets": {"dtw_scenarios": [s["_id"]]},
    }


def _delete_all_scenarios(args):
    docs = list(scenarios.find({}, {"results.narrative_summary": 1, "status": 1, "description": 1}))
    if not docs:
        return None
    return {
        "title": f"Delete all {len(docs)} scenario(s)",
        "summary": f"Hard delete of every scenario in this lane ({len(docs)}), including "
        "simulation results.",
        "details": {"scenarios": [_scenario_line(s) for s in docs]},
        "blast_radius": len(docs),
        "targets": {"dtw_scenarios": [s["_id"] for s in docs]},
    }


REVIEWED_ACTIONS = {
    "activate_plan": _activate_plan,
    "apply_runbook": _apply_runbook,
    "update_template_version": _update_template_version,
    "cancel_intent": _cancel_intent,
    "delete_scenario": _delete_scenario,
    "delete_all_scenarios": _delete_all_scenarios,
}

_TARGET_COLLECTIONS = {"ibn_intents": intents, "dtw_scenarios": scenarios}


def _mark(targets, update):
    for name, ids in (targets or {}).items():
        if ids:
            _TARGET_COLLECTIONS[name].update_many({"_id": {"$in": ids}}, update)


@tools.tool
def open_review(action: str, arguments: dict) -> str:
    """Internal (not offered to the LLM). Build the NOC review for a gated
    action, record it as pending in agent_approvals and mark its target
    documents. Returns JSON; {"skip": true} when the target doesn't exist
    (the tool itself then reports the error and nothing needs approving)."""
    build = REVIEWED_ACTIONS.get(action)
    review = build(arguments or {}) if build else None
    if not review:
        return json.dumps({"skip": True})
    review.setdefault("allowed_decisions", [APPROVE, REJECT])
    last = approvals.find_one({}, {"seq": 1}, sort=[("seq", DESCENDING)])
    seq = (last["seq"] if last else 0) + 1
    approval_id = f"APR-{seq:03d}"
    now = datetime.datetime.now()
    approvals.insert_one({
        "_id": approval_id, "seq": seq, "action": action, "arguments": arguments,
        "status": "pending", "requested_at": now, **review,
    })
    _mark(review["targets"], {"$set": {"pending_approval": {
        "approval_id": approval_id, "action": action, "title": review["title"],
        "requested_at": now,
    }}})
    review["approval_id"] = approval_id
    return json.dumps(review, default=str)


@tools.tool
def close_review(approval_id: str, decision: str, reviewer_notes: str = "") -> str:
    """Internal (not offered to the LLM). Record the reviewer's decision on
    agent_approvals and clear the pending marker from the target documents."""
    doc = approvals.find_one({"_id": approval_id})
    if not doc:
        return json.dumps({"error": f"approval {approval_id} not found"})
    approvals.update_one({"_id": approval_id}, {"$set": {
        "status": "rejected" if decision == REJECT else "approved",
        "decision": decision, "reviewer_notes": reviewer_notes,
        "decided_at": datetime.datetime.now(),
    }})
    _mark(doc.get("targets"), {"$unset": {"pending_approval": ""}})
    return json.dumps({"approval_id": approval_id, "decision": decision})
