#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SLO detection for the self-healing network (SELF_HEALING_PLAN.md, S1
"Detect"): deterministic arithmetic, no LLM.

Every EVAL_EVERY_S the evaluator (web/ibn_assurance.py) runs
window_pipeline() over the lane's ibn_telemetry: p95 and mean per intent
and metric over the last WINDOW_S. An intent breaches when its POS latency
p95 is above the intent's target. BREACH_WINDOWS breached evaluations in a
row open an incident; CLEAR_WINDOWS evaluations below CLEAR_RATIO × target
close it (hysteresis, so a p95 hovering at the line doesn't flap).

Time-series collections can't be watched with change streams, so this is
polled, like the dashboard's telemetry feed.

Also builds the measured symptom fingerprint that diagnose_violation sends
to the hero query, instead of a fixed description.
"""

import datetime
from dataclasses import dataclass

WINDOW_S = 20
EVAL_EVERY_S = 2
BREACH_WINDOWS = 3
CLEAR_WINDOWS = 3
CLEAR_RATIO = 0.95
MIN_SAMPLES = 3

LATENCY = "pos_latency_ms"


def window_pipeline(since: datetime.datetime, intent_id: str | None = None) -> list[dict]:
    """p95, mean and count per (intent, metric) for samples since `since`."""
    match: dict = {"ts": {"$gte": since}}
    if intent_id:
        match["meta.intent_id"] = intent_id
    return [
        {"$match": match},
        {"$group": {
            "_id": {"intent": "$meta.intent_id", "metric": "$meta.metric"},
            "p95": {"$percentile": {"input": "$value", "p": [0.95], "method": "approximate"}},
            "avg": {"$avg": "$value"},
            "n": {"$sum": 1},
        }},
    ]


def measurements(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """{intent_id: {metric: {p95, avg, n}}} from window_pipeline rows."""
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        intent, metric = r["_id"].get("intent"), r["_id"].get("metric")
        if intent and metric:
            out.setdefault(intent, {})[metric] = {
                "p95": round(r["p95"][0], 1), "avg": round(r["avg"], 1), "n": r["n"]}
    return out


def measured_summary(metrics: dict[str, dict]) -> dict:
    """The incident's `measured` block from one intent's window."""
    def get(metric, stat):
        m = metrics.get(metric)
        return m[stat] if m else None
    return {
        "window_s": WINDOW_S,
        "samples": get(LATENCY, "n"),
        "pos_latency_p95_ms": get(LATENCY, "p95"),
        "pos_latency_avg_ms": get(LATENCY, "avg"),
        "uplink_util_avg_pct": get("uplink_util_pct", "avg"),
        "be_queue_depth_avg_pkts": get("be_queue_depth_pkts", "avg"),
        "guest_throughput_avg_mbps": get("guest_throughput_mbps", "avg"),
    }


@dataclass
class Tracker:
    """Consecutive breached / clear evaluations of one intent."""
    breach: int = 0
    clear: int = 0


def step(tracker: Tracker, p95: float, target: float, is_open: bool) -> str | None:
    """Advance one evaluation. Returns 'open', 'close' or None."""
    if p95 > target:
        tracker.breach += 1
        tracker.clear = 0
        if not is_open and tracker.breach >= BREACH_WINDOWS:
            return "open"
    elif p95 <= target * CLEAR_RATIO:
        tracker.clear += 1
        tracker.breach = 0
        if is_open and tracker.clear >= CLEAR_WINDOWS:
            return "close"
    else:
        # In the hysteresis band: neither streak advances.
        tracker.breach = 0
    return None


def diagnosis(measured: dict) -> str:
    """The symptom signature, read from the measurements."""
    util = measured.get("uplink_util_avg_pct")
    if util is None:
        return "unknown (no uplink measurement)"
    if util >= 80:
        return "uplink saturation (capacity issue)"
    return "queue scheduling collision rather than physical-layer or capacity issue"


def fingerprint_text(site_name: str, target: float, segmentation: str, measured: dict) -> str:
    """Natural-language fingerprint for the hero query, from measurements.
    Keeps the shape of the seeded incident texts so similarity stays
    meaningful."""
    p95 = measured.get("pos_latency_p95_ms") or target
    util = measured.get("uplink_util_avg_pct")
    queue = measured.get("be_queue_depth_avg_pkts")
    parts = [
        (f"{site_name} retail branch reports POS payment terminal latency reaching "
         f"{p95:.0f} milliseconds (p95 over {measured.get('window_s', WINDOW_S)} seconds) "
         f"during the morning customer rush, exceeding the {target:.0f} millisecond intent SLA."),
        (f"{segmentation.capitalize()} guest segmentation policy is active with isolated "
         f"VLANs for guest WiFi, camera uplink and POS."),
    ]
    if util is not None:
        parts.append(f"Uplink utilization averages {util:.0f}%"
                     + (" — bandwidth is not saturated." if util < 80 else " — the uplink is saturated."))
    if queue is not None:
        parts.append(f"Best-effort queue depth averages {queue:.0f} packets.")
    parts.append(f"Symptom signature suggests {diagnosis(measured)}.")
    return " ".join(parts)
