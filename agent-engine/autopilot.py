#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The Agent Engine twin's autonomous responder (SELF_HEALING_PLAN.md, S2),
run by `bin/twin.sh autopilot [L3|L4]`.

Agent Engine has no triggers yet, and service accounts are blocked in the
shared JeffN project, so this local process stands in for them until
Phase 8: it watches the twin cluster's `ibn_incidents` (default lane, like
the rest of the twin) and, for every incident the SLO monitor opens, calls
the deployed ibn-agent through `agentengine invoke` with the presenter's
CLI login. Each run is its own session in Agent Engine Traces; its ids are
recorded on the incident.

The agent gets a short, fixed task (autonomy.TASK, which the browser shell
sends its own IBN agent too). What it may do on its own is decided by
the lane's autonomy policy, checked inside the tools (oss_tools/ibn/
autonomy.py): at L3 it diagnoses and recommends, at L4 it also applies a
runbook inside the envelope and waits for the monitor to verify recovery.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import threading
import time

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from oss_tools.ibn import autonomy

# The deployed ibn-agent in JeffN (an explicit target, so the CLI's current
# context doesn't matter).
TARGET = {
    "--base-url": os.environ.get("TWIN_AE_BASE_URL", "https://agentengine.mongodb.com"),
    "--org-id": os.environ.get("TWIN_AE_ORG_ID", "6a99bbc5fa6869c45dee52b6"),
    "--project-id": os.environ.get("TWIN_AE_PROJECT_ID", "6aa4558bb19c96d52970c253"),
    "--workspace-id": os.environ.get("TWIN_IBN_WORKSPACE_ID", "ws-6ad0efe66189a50fdc461f8d"),
}
INVOKE_TIMEOUT = "5m"
# Two invokes started together may both refresh the CLI login, and the
# slower one then holds a refresh token the other just used up. A retry
# picks up the token the other process stored.
AUTH_RACE = ("session expired", "refresh token")
# An agent scaled down (or still starting) answers "no capacity is
# available ... retry after" (10 s) instead of queueing the request.
COLD_START = ("no capacity is available",)
RETRIES = 6
RETRY_S = {AUTH_RACE: 3, COLD_START: 10}
CATCH_UP = datetime.timedelta(minutes=5)   # open incidents this young are picked up at start


def _log(msg: str) -> None:
    print(f"{datetime.datetime.now():%H:%M:%S}  {msg}", flush=True)


def respond(incidents, incident: dict) -> None:
    pol = autonomy.policy()
    task = autonomy.task(incident, pol)
    _log(f"🤖 {incident['intent_id']} at {incident.get('site_name')}: invoking ibn-agent "
         f"({pol['level']})")
    cmd = ["agentengine", "invoke", "--json", "--timeout", INVOKE_TIMEOUT, "--quiet"]
    for flag, value in TARGET.items():
        cmd += [flag, value]
    for attempt in range(RETRIES + 1):
        proc = subprocess.run(cmd + [task], capture_output=True, text=True, check=False)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {"success": False, "error": (proc.stderr or proc.stdout).strip()[-500:]}
        error = out.get("error") or ""
        kind = next((k for k in RETRY_S if any(s in error for s in k)), None)
        if attempt < RETRIES and kind:
            if kind is COLD_START:
                _log(f"   ibn-agent has no free instance yet; retrying in {RETRY_S[kind]}s")
            time.sleep(RETRY_S[kind])
            continue
        break
    ok = proc.returncode == 0 and str(out.get("success")).lower() == "true"
    answer = (out.get("response") or out.get("error") or "").strip()
    now = datetime.datetime.now()
    incidents.update_one({"_id": incident["_id"]}, {
        "$set": {"autopilot.status": "done" if ok else "failed", "autopilot.finished_at": now,
                 "autopilot.session_id": out.get("session_id"),
                 "autopilot.execution_id": out.get("execution_id"),
                 "autopilot.answer": answer[:2000]},
        "$push": {"timeline": {"ts": now, "stage": "report", "by": "ibn-agent (autopilot)",
                               "note": answer.splitlines()[0][:300] if answer else "(no answer)"}},
    })
    _log(("✓" if ok else "❌") + f" {incident['intent_id']}: session {out.get('session_id')}")
    for line in answer.splitlines():
        _log(f"   {line}")


def claim(incidents, incident_id) -> dict | None:
    """One responder per incident, even with two autopilots running."""
    return incidents.find_one_and_update(
        {"_id": incident_id, "autopilot": {"$exists": False}},
        {"$set": {"autopilot": {"status": "running", "started_at": datetime.datetime.now()}}})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("level", nargs="?", default="L4", choices=autonomy.LEVELS)
    args = parser.parse_args()

    pol = autonomy.set_level(args.level)
    incidents = MongoClient(os.environ["MONGODB_URI"])["agent_registry"]["ibn_incidents"]
    _log(f"autopilot on ({autonomy.policy_name(pol)}, level {pol['level']}): "
         f"watching ibn_incidents. Ctrl-C to stop.")

    def start(incident_id):
        if doc := claim(incidents, incident_id):
            threading.Thread(target=respond, args=(incidents, doc), daemon=True).start()

    for doc in incidents.find({"open": True, "opened_at": {"$gte": datetime.datetime.now() - CATCH_UP},
                               "autopilot": {"$exists": False}}, {"_id": 1}):
        start(doc["_id"])
    pipeline = [{"$match": {"operationType": "insert", "fullDocument.open": True}}]
    try:
        with incidents.watch(pipeline) as stream:
            for change in stream:
                start(change["documentKey"]["_id"])
    except KeyboardInterrupt:
        _log("autopilot off (the autonomy level stays as set; Reset returns it to L3)")


if __name__ == "__main__":
    main()
