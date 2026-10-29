#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The autonomy envelope of the self-healing loop (SELF_HEALING_PLAN.md, S2):
when the system may apply a runbook on its own, and when it has to hand
the incident to a NOC engineer.

The policy is data: one document per lane in `ibn_autonomy_policy`
(`_id: "policy"`), with POLICY_V1 as the default for any field it doesn't
set. `evaluate` is the only place that decides, and it reads everything
from the database (the incident's recorded diagnosis, the knowledge base,
the lane's past actions), never from what the model passes in, so no
prompt can widen the envelope. check_autonomy and
apply_runbook(autonomous=True) in assurance.py both call it.

Level L3 means detect, diagnose and recommend, then wait for a human.
Level L4 means act inside the envelope. The default is L3.
"""

import datetime
import os

from oss_tools._ctx import collection, demo_prefix
from oss_tools.ibn import learning

POLICY_ID = "policy"
POLICY_V1 = {
    "version": "v1",
    "level": "L3",
    "similarity_min": 0.80,
    "max_actions_per_hour": 3,
    "deny_runbooks": [],
    "change_freeze": False,
}
LEVELS = ("L3", "L4")
L4_ENV = "SELF_HEALING_L4"
AUTONOMOUS_BY = "ibn-agent (autonomous)"

policies = collection("ibn_autonomy_policy", mutable=True)    # mutable (per lane)
incidents = collection("ibn_incidents", mutable=True)         # mutable (self-healing)
knowledge_chunks = collection("ibn_knowledge_chunks")         # shared


def merge(doc: dict | None) -> dict:
    """A stored policy document with POLICY_V1 filling the fields it doesn't set."""
    return {**POLICY_V1, **{k: v for k, v in (doc or {}).items() if k != "_id"}}


def policy() -> dict:
    return merge(policies.find_one({"_id": POLICY_ID}))


def policy_name(pol: dict) -> str:
    return f"autonomy-policy {pol['version']}"


# The responder's task for one incident, the same in both runtimes: the twin
# autopilot sends it to the deployed ibn-agent (agent-engine/autopilot.py),
# the browser shell to its own IBN agent (web/self_healing.py).
TASK = """AUTONOMOUS MODE ({policy}, level {level}). No human is in the loop: do not ask questions.
The SLO monitor opened incident {incident_id} for intent {intent_id} at {site}: {note}.
Work through these steps, one tool call at a time:
1. diagnose_violation(intent_id="{intent_id}")
2. check_autonomy(intent_id="{intent_id}", runbook_id=<the runbook of the top match>)
3. If the decision is "allow": apply_runbook(runbook_id=<that runbook>, intent_id="{intent_id}", autonomous=true), then await_recovery(intent_id="{intent_id}").
   If it is "escalate": apply nothing.
4. If await_recovery reported a recovery: propose_prevention(intent_id="{intent_id}"). It only proposes; never apply the proposal yourself.
5. Answer in at most four lines: what was detected, what you did or why it went to the NOC, the outcome, and what you proposed for the sister sites."""


def task(incident: dict, pol: dict) -> str:
    return TASK.format(policy=policy_name(pol), level=pol["level"],
                       incident_id=incident["_id"], intent_id=incident["intent_id"],
                       site=incident.get("site_name", "the site"),
                       note=(incident.get("alarm") or {}).get("specific_problem", "SLO breach"))


def l4_allowed() -> bool:
    """L4 is only switched on where SELF_HEALING_L4=1: bin/twin.sh sets it
    for the twin cluster. The Kanopy demo shares its database with the live
    production code, which has no lane filter for learned incidents
    (learning.py), so it must never act, and therefore never learn, on its
    own. Only setting the level is gated; the tools read it as stored."""
    return os.environ.get(L4_ENV) == "1"


def set_level(level: str) -> dict:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, got {level!r}")
    if level == "L4" and not l4_allowed():
        raise PermissionError(f"L4 needs {L4_ENV}=1 (the Agent Engine twin only)")
    policies.update_one({"_id": POLICY_ID}, {"$set": {"level": level}}, upsert=True)
    return policy()


def evaluate(intent: dict, runbook_id: str, incident: dict | None) -> dict:
    """{decision: allow|escalate, policy, level, checks: [{check, ok, detail}],
    reasons: [failed details]}. Every check runs, so an escalation lists
    all of its reasons."""
    pol = policy()
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("incident", incident is not None,
          f"open incident {incident['_id']} from the SLO monitor" if incident
          else "no open incident from the SLO monitor for this intent")
    check("level", pol["level"] == "L4",
          "autonomy level L4: act inside the envelope" if pol["level"] == "L4"
          else f"autonomy level {pol['level']}: recommend only, a NOC engineer decides")
    check("change_freeze", not pol["change_freeze"],
          "change freeze in force" if pol["change_freeze"] else "no change freeze")
    check("deny_list", runbook_id not in pol["deny_runbooks"],
          f"{runbook_id} is on the deny list" if runbook_id in pol["deny_runbooks"]
          else f"{runbook_id} is not on the deny list")
    check("single_site", True, f"one site ({intent.get('site_id')}), one intent ({intent['_id']})")

    diagnosis = (incident or {}).get("diagnosis") or {}
    top_runbook, similarity = diagnosis.get("runbook_id"), diagnosis.get("similarity") or 0.0
    if not diagnosis:
        check("precedent", False, "no diagnosis recorded on the incident: run diagnose_violation first")
    else:
        check("precedent", top_runbook == runbook_id,
              f"top match ({diagnosis.get('top_match')}) was fixed with {runbook_id}"
              if top_runbook == runbook_id
              else f"top match ({diagnosis.get('top_match')}) recommends "
                   f"{top_runbook or 'no runbook'}, not {runbook_id}")
        ok = similarity >= pol["similarity_min"]
        check("similarity", ok, f"similarity {similarity:.2f} {'≥' if ok else '<'} "
                                f"{pol['similarity_min']:.2f}")

    successes = knowledge_chunks.count_documents(
        {"kind": "incident", "runbook_id": runbook_id, "outcome": {"$ne": "failed"},
         **learning.lane_filter(demo_prefix())})
    check("track_record", successes > 0,
          f"{runbook_id} resolved {successes} past incident(s)" if successes
          else f"{runbook_id} has no recorded success on a past incident")

    hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
    recent = incidents.count_documents({"autonomous_act_at": {"$gte": hour_ago}})
    check("rate", recent < pol["max_actions_per_hour"],
          f"{recent} autonomous action(s) in the last hour (limit {pol['max_actions_per_hour']})")

    reasons = [c["detail"] for c in checks if not c["ok"]]
    return {"decision": "escalate" if reasons else "allow", "policy": policy_name(pol),
            "level": pol["level"], "runbook_id": runbook_id, "intent_id": intent["_id"],
            "incident_id": (incident or {}).get("_id"), "checks": checks, "reasons": reasons}


def record(verdict: dict) -> None:
    """Note the decision on the incident's timeline once per change of
    decision (check_autonomy and the agents' review step may both ask)."""
    incident_id = verdict.get("incident_id")
    if not incident_id:
        return
    now = datetime.datetime.now()
    update = {"$set": {"decision": {k: verdict[k] for k in
                                    ("decision", "policy", "level", "runbook_id", "reasons")}}}
    if verdict["decision"] == "escalate":
        update["$set"].update({"status": "escalated", "escalated_at": now})
    note = (f"allow: {verdict['runbook_id']} inside the envelope"
            if verdict["decision"] == "allow"
            else "escalate to the NOC: " + "; ".join(verdict["reasons"]))
    update["$push"] = {"timeline": {"ts": now, "stage": "decide", "by": verdict["policy"],
                                    "note": note}}
    incidents.update_one({"_id": incident_id, "open": True,
                          "decision.decision": {"$ne": verdict["decision"]}}, update)
