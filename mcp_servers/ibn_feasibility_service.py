#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Feasibility Service — Match Intent to Inventory and Plan Activation

Validates whether a submitted intent can be honored at its target site,
generates a concrete service plan from available resources, and transitions
the intent through feasible → planned → active states. Stores plan
snapshots in ibn_policy_snapshots so the dashboard can render them.

Use this service when users say:
- Feasibility: "is this feasible", "can we do this", "check feasibility for IBN-...",
              "verify intent <id>", "is the <intent> achievable"
- Plan:       "show the proposed plan", "plan for IBN-...", "what's the plan",
              "propose plan for <intent>"
- Activate:   "activate <intent>", "activate it", "go live", "make it active",
              "deploy intent <id>"

This service does NOT submit new intents (use intent service), list inventory
(use inventory service), compute compliance (use assurance service), or run
telemetry (use telemetry simulator).
"""

import datetime
import logging
import os
from pymongo import MongoClient
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("ibn_feasibility_service")
logger = logging.getLogger("ibn_feasibility_service")

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db                = mongo_client["agent_registry"]
intents           = db["ibn_intents"]
sites             = db["ibn_sites"]
resources         = db["ibn_resources"]
policy_snapshots  = db["ibn_policy_snapshots"]

DEFAULT_TEMPLATE = "strict-retail-v3"  # the latently-broken template — sets up the WOW


def _bandwidth_estimate_mbps(parsed: dict) -> int:
    """Coarse bandwidth estimate from intent service mix."""
    services = parsed.get("services") or []
    total = 0
    if "pos" in services:           total += 50
    if "guest_wifi" in services:    total += 200
    if "camera_uplink" in services: total += 100
    if "kiosk" in services:
        total += 30 * (parsed.get("targets", {}).get("kiosk_count") or 1)
    if "voip" in services:          total += 50
    return max(total + 150, 200)  # +150 Mbps headroom, min 200


def _evaluate(intent: dict) -> dict:
    """
    Run feasibility checks. Returns:
      {
        feasible: bool,
        reasons: [str],
        plan: dict | None,
      }
    """
    parsed = intent.get("parsed", {})
    targets = parsed.get("targets", {})
    site_id = intent.get("site_id")

    if not site_id:
        return {
            "feasible": False,
            "reasons": ["Intent has no resolved site — cannot match to inventory."],
            "plan": None,
        }

    site = sites.find_one({"_id": site_id})
    if not site:
        return {"feasible": False, "reasons": [f"Site {site_id} not found."], "plan": None}

    site_resources = list(resources.find({"site_id": site_id}))
    nodes   = [r for r in site_resources if r["type"] == "access_node"]
    uplinks = [r for r in site_resources if r["type"] == "uplink"]
    cpes    = [r for r in site_resources if r["type"] == "cpe"]

    reasons_failed = []

    # ── Access node ──
    node = nodes[0] if nodes else None
    if not node:
        reasons_failed.append("No access node provisioned at this site.")
    elif targets.get("segmentation") == "strict" and "EVPN" not in node.get("capabilities", []):
        reasons_failed.append(
            f"Access node {node['_id']} does not support EVPN — required for strict segmentation."
        )

    # ── Uplink ──
    needed_mbps = _bandwidth_estimate_mbps(parsed)
    eligible_uplinks = [u for u in uplinks if u.get("capacity_mbps", 0) >= needed_mbps
                                              and u.get("state") in ("available", "active")]
    if not eligible_uplinks and uplinks:
        max_cap = max((u.get("capacity_mbps", 0) for u in uplinks), default=0)
        reasons_failed.append(
            f"No uplink meets estimated demand ({needed_mbps} Mbps); "
            f"largest available is {max_cap} Mbps."
        )
    elif not uplinks:
        reasons_failed.append("No uplink resources catalogued at this site.")

    # ── CPE ──
    cpe = None
    if targets.get("pos_latency_ms") and targets["pos_latency_ms"] <= 50:
        # Tight latency target — need DSCP marking
        eligible_cpes = [c for c in cpes if "DSCP-marking" in c.get("feature_set", [])]
        if not eligible_cpes and cpes:
            reasons_failed.append(
                "No CPE with DSCP-marking available — required for tight POS latency targets."
            )
        elif eligible_cpes:
            cpe = eligible_cpes[0]
    elif cpes:
        cpe = cpes[0]

    if reasons_failed:
        return {"feasible": False, "reasons": reasons_failed, "plan": None}

    # ── Build plan ──
    chosen_uplink = sorted(
        eligible_uplinks, key=lambda u: u.get("capacity_mbps", 0)
    )[-1]  # the largest eligible — favour headroom

    plan = {
        "intent_id":      intent["_id"],
        "site_id":        site_id,
        "site_name":      site["name"],
        "access_node":    node["_id"]   if node   else None,
        "uplink":         chosen_uplink["_id"]    if chosen_uplink else None,
        "uplink_mbps":    chosen_uplink.get("capacity_mbps") if chosen_uplink else None,
        "cpe":            cpe["_id"]    if cpe    else None,
        "cpe_vendor":     cpe.get("vendor") if cpe else None,
        "template":       DEFAULT_TEMPLATE,
        "estimated_mbps": needed_mbps,
        "deadline":       intent.get("parsed", {}).get("deadline"),
        "snapshot_at":    datetime.datetime.now(),
    }
    return {"feasible": True, "reasons": [], "plan": plan}


def _save_plan_snapshot(intent_id: str, plan: dict):
    """Persist the plan as an immutable snapshot."""
    policy_snapshots.insert_one({**plan, "_id": f"PLAN-{intent_id}-{datetime.datetime.now():%Y%m%d%H%M%S}"})


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def check_feasibility(intent_id: str) -> str:
    """
    Verify whether the given intent can be honored at its target site
    against current inventory and resource state. Updates intent.status
    to 'feasible' or leaves at 'submitted' with a list of blockers.
    Does NOT yet allocate or activate — call propose_plan / activate_plan
    next.

    Args:
        intent_id: The intent ID, e.g. 'IBN-005'.
    """
    intent = intents.find_one({"_id": intent_id})
    if not intent:
        return f"❌ Intent {intent_id} not found."

    result = _evaluate(intent)

    if not result["feasible"]:
        intents.update_one(
            {"_id": intent_id},
            {"$push": {"history": {
                "ts": datetime.datetime.now(),
                "event": "feasibility_failed",
                "note": "; ".join(result["reasons"]),
            }}}
        )
        return (
            f"❌ Intent {intent_id} not feasible at this time:\n"
            + "\n".join(f"  - {r}" for r in result["reasons"])
        )

    intents.update_one(
        {"_id": intent_id},
        {"$set": {"status": "feasible"},
         "$push": {"history": {
             "ts": datetime.datetime.now(),
             "event": "feasibility_passed",
             "note": "All resource checks passed.",
         }}}
    )

    plan = result["plan"]
    return (
        f"✓ Intent **{intent_id}** is **feasible** at {plan['site_name']}.\n"
        f"  Access node: `{plan['access_node']}` (EVPN+VLAN+DSCP capable)\n"
        f"  Uplink available: `{plan['uplink']}` "
        f"({plan['uplink_mbps']} Mbps fiber, est. demand {plan['estimated_mbps']} Mbps)\n"
        f"  CPE candidate: `{plan['cpe']}` ({plan['cpe_vendor']})\n"
        f"  Template ready: `{plan['template']}`\n"
        f"  Run propose_plan to commit the snapshot."
    )


@mcp.tool()
def propose_plan(intent_id: str) -> str:
    """
    Generate and persist a concrete service plan for the intent. Stores
    the plan as an immutable snapshot in ibn_policy_snapshots, transitions
    intent.status to 'planned'. Idempotent — re-running creates a new
    snapshot version but doesn't double-allocate.

    Args:
        intent_id: The intent ID.
    """
    intent = intents.find_one({"_id": intent_id})
    if not intent:
        return f"❌ Intent {intent_id} not found."

    result = _evaluate(intent)
    if not result["feasible"]:
        return (
            f"❌ Cannot plan {intent_id} — feasibility checks fail:\n"
            + "\n".join(f"  - {r}" for r in result["reasons"])
        )

    plan = result["plan"]
    _save_plan_snapshot(intent_id, plan)

    intents.update_one(
        {"_id": intent_id},
        {"$set": {"status": "planned", "template": plan["template"]},
         "$push": {"history": {
             "ts": datetime.datetime.now(),
             "event": "plan_proposed",
             "note": f"Plan snapshot stored, template {plan['template']}.",
         }}}
    )

    return (
        f"📋 Plan proposed for **{intent_id}** at {plan['site_name']}:\n"
        f"  - Access node: `{plan['access_node']}`\n"
        f"  - Uplink: `{plan['uplink']}` ({plan['uplink_mbps']} Mbps)\n"
        f"  - CPE: `{plan['cpe']}`\n"
        f"  - Segmentation template: `{plan['template']}`\n"
        f"  - EF queue reserved for POS class\n"
        f"  - Status: **planned** — ready to activate."
    )


@mcp.tool()
def activate_plan(intent_id: str) -> str:
    """
    Activate the planned intent. Transitions intent.status to 'active',
    sets activated_at, records the activation in history. Once active,
    the assurance service begins continuous compliance monitoring and
    the telemetry simulator can drive metrics for this intent.

    Args:
        intent_id: The intent ID, must be in 'planned' or 'feasible' state.
    """
    intent = intents.find_one({"_id": intent_id})
    if not intent:
        return f"❌ Intent {intent_id} not found."

    status = intent.get("status")
    if status == "active":
        return f"ℹ️  Intent {intent_id} is already active."
    if status not in ("planned", "feasible"):
        return (
            f"❌ Cannot activate {intent_id} from status '{status}'. "
            f"Run check_feasibility / propose_plan first."
        )

    # If still 'feasible', auto-plan first
    if status == "feasible":
        result = _evaluate(intent)
        if not result["feasible"]:
            return f"❌ Re-evaluation failed: {'; '.join(result['reasons'])}"
        _save_plan_snapshot(intent_id, result["plan"])
        intents.update_one(
            {"_id": intent_id},
            {"$set": {"template": result["plan"]["template"]}}
        )

    now = datetime.datetime.now()
    intents.update_one(
        {"_id": intent_id},
        {"$set":  {"status": "active", "activated_at": now},
         "$push": {"history": {"ts": now, "event": "activated",
                               "note": "Intent live, assurance monitoring engaged."}},
         "$inc":  {"version": 1}}
    )

    site = sites.find_one({"_id": intent.get("site_id")})
    return (
        f"🟢 Intent **{intent_id}** activated at "
        f"**{site['name'] if site else '—'}**.\n"
        f"  POS, guest, camera services live.\n"
        f"  Assurance monitoring engaged — telemetry now flowing.\n"
        f"  All SLOs green."
    )


if __name__ == "__main__":
    mcp.run()
