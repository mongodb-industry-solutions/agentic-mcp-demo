#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Telemetry Simulator — Push-Button Scenario Injection

Drives synthetic telemetry into ibn_telemetry (a MongoDB time-series
collection) so the dashboard feels live during demos. Push-button: the
operator triggers scenarios on cue rather than running a continuous
simulation. Writes also fire ibn_compliance_events that the dashboard
picks up via Change Streams.

Use this service when users say:
- Inject:   "inject morning rush", "inject morning rush at <site>",
           "trigger violation", "simulate <scenario>", "stress test <site>"
- Baseline: "seed baseline telemetry", "seed telemetry for <intent>",
           "send healthy samples"
- Reset:   "reset telemetry", "clear simulated data", "wipe telemetry"

This service does NOT diagnose violations, apply runbooks, or update intent
status (the assurance service does that). It only writes raw telemetry and
violation events.
"""

import datetime
import logging
import os
import random
from pymongo import MongoClient, DESCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("ibn_telemetry_simulator")
logger = logging.getLogger("ibn_telemetry_simulator")

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db                  = mongo_client["agent_registry"]
intents             = db["ibn_intents"]
sites               = db["ibn_sites"]
telemetry           = db["ibn_telemetry"]
compliance_events   = db["ibn_compliance_events"]


def _resolve_site(site_hint: str) -> dict | None:
    if not site_hint:
        return None
    direct = sites.find_one({"name": {"$regex": site_hint, "$options": "i"}})
    if direct:
        return direct
    tokens = [t for t in site_hint.split() if len(t) >= 3]
    if not tokens:
        return None
    return sites.find_one(
        {"$and": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )


def _resolve_intent(intent_id: str = None, site: str = None) -> dict | None:
    """Resolve intent by ID or by site-name fragment (active intent at that site)."""
    if intent_id:
        return intents.find_one({"_id": intent_id})
    if site:
        site_doc = _resolve_site(site)
        if not site_doc:
            return None
        return intents.find_one({
            "site_id": site_doc["_id"],
            "status":  {"$in": ["active", "violated"]},
        })
    return None


def _write_telemetry_burst(intent: dict, metric: str, values: list[float]):
    """Insert a burst of telemetry samples spaced 1s apart ending now."""
    now = datetime.datetime.now()
    docs = [
        {
            "ts":    now - datetime.timedelta(seconds=len(values) - 1 - i),
            "meta": {"intent_id": intent["_id"],
                     "site_id":   intent.get("site_id"),
                     "metric":    metric},
            "value": v,
        }
        for i, v in enumerate(values)
    ]
    telemetry.insert_many(docs)


SCENARIOS = {
    "morning_rush": {
        "metric": "pos_latency_ms",
        "description": "POS payment terminal latency spike during morning customer rush",
        "trigger": "morning_rush",
        "above_threshold_by": (5, 10),  # observed = threshold + random(5..10)
    },
    "link_saturation": {
        "metric": "pos_latency_ms",
        "description": "Uplink saturation degrading POS terminal response time",
        "trigger": "bandwidth_saturation",
        "above_threshold_by": (15, 25),
    },
    "peak_load": {
        "metric": "pos_latency_ms",
        "description": "Peak transaction load straining QoS scheduling",
        "trigger": "peak_load",
        "above_threshold_by": (8, 14),
    },
}


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def inject_event(scenario: str = "morning_rush",
                 intent_id: str = None, site: str = None) -> str:
    """
    Inject a violation scenario. Writes a burst of synthetic telemetry that
    crosses the intent's SLO threshold, plus a compliance event of kind
    'violation', and flips the intent status to 'violated'. The dashboard
    picks all of this up via Change Streams within a second.

    Args:
        scenario:  One of 'morning_rush', 'link_saturation', 'peak_load'.
                   Default 'morning_rush'.
        intent_id: Specific intent ID to target.
        site:      Site name fragment (e.g. 'Marienplatz'); the simulator
                   will pick the active intent at that site.

    Pass either intent_id or site, not both.
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return (
            f"❌ No active intent found for "
            f"{'intent_id ' + intent_id if intent_id else 'site ' + (site or '<unspecified>')}. "
            f"Activate the intent first."
        )

    spec = SCENARIOS.get(scenario)
    if not spec:
        return (f"❌ Unknown scenario '{scenario}'. "
                f"Available: {', '.join(SCENARIOS.keys())}.")

    targets = intent.get("parsed", {}).get("targets", {})
    threshold = targets.get("pos_latency_ms", 40)

    lo, hi = spec["above_threshold_by"]
    samples = [
        threshold + random.uniform(lo, hi) + random.uniform(-1.5, 1.5)
        for _ in range(8)
    ]
    _write_telemetry_burst(intent, spec["metric"], samples)

    observed_peak = max(samples)
    site_doc = sites.find_one({"_id": intent.get("site_id")})
    site_name = site_doc["name"] if site_doc else "—"

    # Violation event for change-stream pickup
    compliance_events.insert_one({
        "intent_id":   intent["_id"],
        "kind":        "violation",
        "ts":          datetime.datetime.now(),
        "metric":      spec["metric"],
        "observed":    observed_peak,
        "threshold":   threshold,
        "scenario":    scenario,
        "site_name":   site_name,
        "fingerprint": {
            "trigger":             spec["trigger"],
            "segmentation":        targets.get("segmentation", "strict"),
            "link_util_pct":       random.randint(20, 30),
            "latency_ms_observed": round(observed_peak, 1),
            "latency_ms_threshold": threshold,
        },
        "justification": (
            f"{spec['description']}. Observed {observed_peak:.1f}ms vs SLA "
            f"≤{threshold}ms. Symptom signature suggests queue scheduling "
            f"collision rather than capacity issue."
        ),
    })

    intents.update_one(
        {"_id": intent["_id"]},
        {"$set": {"status": "violated"},
         "$push": {"history": {
             "ts": datetime.datetime.now(),
             "event": "violation_detected",
             "note":  f"Scenario '{scenario}' injected; peak {observed_peak:.1f}ms.",
         }}}
    )

    return (
        f"⚡ Injected scenario **{scenario}** at {site_name} ({intent['_id']}).\n"
        f"  Peak POS latency: {observed_peak:.1f}ms (SLA ≤{threshold}ms)\n"
        f"  Status: 🔴 violated\n"
        f"  Run diagnose_violation('{intent['_id']}') to surface root cause."
    )


@mcp.tool()
def seed_baseline(intent_id: str = None, site: str = None,
                  duration_seconds: int = 60) -> str:
    """
    Write a stretch of healthy in-spec telemetry for an active intent. Useful
    to populate the dashboard with a green history before running the demo
    or after a reset.

    Args:
        intent_id:        The intent to seed baseline for.
        site:             Alternative site-name lookup.
        duration_seconds: How many seconds of green samples to seed (default 60).
    """
    intent = _resolve_intent(intent_id, site)
    if not intent:
        return "❌ No matching active intent found."

    targets = intent.get("parsed", {}).get("targets", {})
    threshold = targets.get("pos_latency_ms", 40)
    healthy_band = (max(15, threshold - 18), max(20, threshold - 8))
    values = [random.uniform(*healthy_band) for _ in range(duration_seconds)]
    _write_telemetry_burst(intent, "pos_latency_ms", values)
    return (
        f"✓ Seeded {duration_seconds}s of healthy POS latency telemetry for "
        f"{intent['_id']} (band {healthy_band[0]:.0f}-{healthy_band[1]:.0f}ms vs "
        f"target ≤{threshold}ms)."
    )


@mcp.tool()
def reset_telemetry(intent_id: str = None) -> str:
    """
    Wipe simulated telemetry for a single intent, or all of it if intent_id
    is omitted. Also clears compliance events for the same scope. Use to
    reset state between demo runs.

    Args:
        intent_id: Optional intent ID. If omitted, ALL telemetry is wiped.
    """
    if intent_id:
        t_res = telemetry.delete_many({"meta.intent_id": intent_id})
        c_res = compliance_events.delete_many({"intent_id": intent_id})
        return (
            f"🗑️  Cleared {t_res.deleted_count} telemetry samples and "
            f"{c_res.deleted_count} compliance events for {intent_id}."
        )
    t_res = telemetry.delete_many({})
    c_res = compliance_events.delete_many({})
    return (
        f"🗑️  Wiped all simulated data: {t_res.deleted_count} telemetry "
        f"samples, {c_res.deleted_count} compliance events."
    )


if __name__ == "__main__":
    mcp.run()
