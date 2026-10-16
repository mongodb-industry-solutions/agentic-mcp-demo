#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The network side of the self-healing loop (SELF_HEALING_PLAN.md, S1):
plays morning rushes into a lane's telemetry and detects SLO breaches.
The IBN dashboard runs both per lane while a tab watches it
(web/ibn_dashboard.py), so the legacy demo and the Agent Engine twin get
them without a new service.

- rush_runner: claims `ibn_sim_runs` requests (from the dashboard button
  or the start_morning_rush tool) and plays oss_tools.ibn.rush into
  ibn_telemetry, re-reading the intent every tick so an applied runbook
  takes effect in the data.
- slo_evaluator: every slo.EVAL_EVERY_S, one aggregation over the lane's
  telemetry; per intent, slo.step() opens or closes an incident in
  `ibn_incidents` (at most one open per intent: unique partial index on
  `open`). Opening raises the alarm the dashboard already renders (a
  `violation` compliance event, intent status `violated`); closing
  records the recovery and the time to recover.

Several dashboard processes can watch the same lane: runs are claimed
atomically, and the index and conditional updates make open/close
idempotent.
"""

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable

from oss_tools.ibn import rush, slo
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("ibn_assurance")

Notify = Callable[[dict], Awaitable[None]]

RECENT_EVENT_S = 60   # an alarm/recovery this recent (e.g. from inject_event) isn't repeated


def _now() -> datetime.datetime:
    # Naive local time, like every other ibn_telemetry/ibn_compliance_events writer.
    return datetime.datetime.now()


async def ensure_lane(db, pfx: str) -> None:
    await db[pfx + "ibn_incidents"].create_index(
        [("intent_id", ASCENDING)], unique=True, partialFilterExpression={"open": True},
        name="one_open_incident_per_intent")
    await db[pfx + "ibn_sim_runs"].create_index([("status", ASCENDING)], name="sim_run_status")


# ─── Sense: the morning rush ────────────────────────────────────────────────

async def play_rush(db, pfx: str, run: dict, notify: Notify, rushing: set) -> None:
    intents, runs = db[pfx + "ibn_intents"], db[pfx + "ibn_sim_runs"]
    intent_id = run["intent_id"]
    rushing.add(intent_id)
    status = "done"
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        log.info(f"rush started (prefix={pfx!r}, {intent_id}, {rush.duration_s():.0f}s)")
        while (elapsed := loop.time() - t0) < rush.duration_s():
            intent = await intents.find_one({"_id": intent_id})
            if (not intent or intent.get("status") not in ("active", "violated")
                    or not await runs.find_one({"_id": run["_id"]}, {"_id": 1})):
                # Intent gone or inactive, or the lane was reset.
                status = "stopped"
                break
            now = _now()
            values = rush.sample(intent, elapsed)
            await db[pfx + "ibn_telemetry"].insert_many([
                {"ts": now, "value": v,
                 "meta": {"intent_id": intent_id, "site_id": intent.get("site_id"),
                          "metric": metric, "source": "cpe-probe"}}
                for metric, v in values.items()])
            await notify({"type": "rush", "intent_id": intent_id, "run_id": str(run["_id"]),
                          "clock": rush.sim_clock(elapsed),
                          "load": round(rush.load(rush.sim_minute(elapsed)), 2),
                          "elapsed_s": round(elapsed, 1), "duration_s": rush.duration_s(),
                          "mitigated": rush.mitigated(intent)})
            await asyncio.sleep(rush.TICK_S)
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        rushing.discard(intent_id)
        await runs.update_one({"_id": run["_id"]},
                              {"$set": {"status": status, "finished_at": _now()}})
        await notify({"type": "rush", "intent_id": intent_id, "run_id": str(run["_id"]),
                      "status": status})
        log.info(f"rush {status} (prefix={pfx!r}, {intent_id})")


async def rush_runner(db, pfx: str, notify: Notify, rushing: set) -> None:
    """Claim requested runs (a regular collection, but polling at 1 s also
    picks up requests made while no tab was watching)."""
    runs = db[pfx + "ibn_sim_runs"]
    while True:
        try:
            run = await runs.find_one_and_update(
                {"status": "requested"}, {"$set": {"status": "running", "started_at": _now()}},
                sort=[("requested_at", ASCENDING)], return_document=ReturnDocument.AFTER)
            if run and run["intent_id"] in rushing:
                await runs.update_one({"_id": run["_id"]}, {"$set": {"status": "skipped"}})
            elif run:
                asyncio.create_task(play_rush(db, pfx, run, notify, rushing))
                continue
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep the lane's runner alive
            log.warning(f"rush runner error ({e})")
            await asyncio.sleep(2)


# ─── Detect: SLO evaluation and incidents ───────────────────────────────────

async def _recent_event(db, pfx: str, intent_id: str, kind: str) -> bool:
    since = _now() - datetime.timedelta(seconds=RECENT_EVENT_S)
    return bool(await db[pfx + "ibn_compliance_events"].find_one(
        {"intent_id": intent_id, "kind": kind, "ts": {"$gte": since}}))


async def open_incident(db, pfx: str, intent: dict, metrics: dict, notify: Notify,
                        rushing: set) -> None:
    now = _now()
    target = rush.threshold(intent)
    measured = slo.measured_summary(metrics)
    p95 = measured["pos_latency_p95_ms"]
    site = await db["ibn_sites"].find_one({"_id": intent.get("site_id")}) or {}
    site_name = site.get("name", "—")
    note = (f"POS latency p95 {p95} ms > {target} ms SLO for {slo.BREACH_WINDOWS} consecutive "
            f"{slo.WINDOW_S}s windows")
    doc = {
        "intent_id": intent["_id"], "site_id": intent.get("site_id"), "site_name": site_name,
        "open": True, "status": "open", "opened_at": now, "threshold": target,
        "measured": measured,
        # TMF642-shaped alarm; probable cause is filled in by diagnosis.
        "alarm": {"alarm_type": "QualityOfServiceAlarm", "perceived_severity": "major",
                  "probable_cause": "thresholdCrossed", "specific_problem": note},
        "timeline": [{"ts": now, "stage": "detect", "by": "slo-detector", "note": note}],
    }
    try:
        res = await db[pfx + "ibn_incidents"].insert_one(doc)
    except DuplicateKeyError:
        return   # another watcher of this lane opened it
    doc["_id"] = res.inserted_id
    log.info(f"incident opened (prefix={pfx!r}, {intent['_id']}, p95 {p95} ms)")

    if not await _recent_event(db, pfx, intent["_id"], "violation"):
        await db[pfx + "ibn_compliance_events"].insert_one({
            "intent_id": intent["_id"], "kind": "violation", "ts": now,
            "metric": slo.LATENCY, "observed": p95, "threshold": target,
            "site_name": site_name, "source": "slo-detector", "incident_id": res.inserted_id,
            "scenario": "morning_rush" if intent["_id"] in rushing else None,
            "fingerprint": {
                "trigger": "morning_rush" if intent["_id"] in rushing else "slo_breach",
                "segmentation": ((intent.get("parsed") or {}).get("targets") or {})
                .get("segmentation", "strict"),
                "link_util_pct": measured["uplink_util_avg_pct"],
                "latency_ms_observed": p95,
                "latency_ms_threshold": target,
            },
            "justification": (f"Detected by the SLO monitor: {note}. "
                              f"Symptom signature suggests {slo.diagnosis(measured)}."),
        })
    await db[pfx + "ibn_intents"].update_one(
        {"_id": intent["_id"]},
        {"$set": {"status": "violated"},
         "$push": {"history": {"ts": now, "event": "incident_opened", "by": "slo-detector",
                               "incident_id": res.inserted_id, "note": note}}})
    await notify({"type": "incident", "doc": doc})


async def close_incident(db, pfx: str, intent: dict, incident: dict, metrics: dict,
                         notify: Notify) -> None:
    now = _now()
    target = rush.threshold(intent)
    p95 = metrics[slo.LATENCY]["p95"]
    mttr_s = round((now - incident["opened_at"]).total_seconds(), 1)
    note = (f"POS latency p95 {p95} ms within {target} ms SLO for {slo.CLEAR_WINDOWS} "
            f"consecutive windows; recovered after {mttr_s:.0f} s")
    res = await db[pfx + "ibn_incidents"].find_one_and_update(
        {"_id": incident["_id"], "open": True},
        {"$unset": {"open": ""},
         "$set": {"status": "recovered", "recovered_at": now, "mttr_s": mttr_s},
         "$push": {"timeline": {"ts": now, "stage": "verify", "by": "slo-detector",
                                "note": note}}},
        return_document=ReturnDocument.AFTER)
    if not res:
        return   # already closed by another watcher
    log.info(f"incident recovered (prefix={pfx!r}, {intent['_id']}, {mttr_s} s)")

    if not await _recent_event(db, pfx, intent["_id"], "recovery"):
        await db[pfx + "ibn_compliance_events"].insert_one({
            "intent_id": intent["_id"], "kind": "recovery", "ts": now,
            "metric": slo.LATENCY, "observed": p95, "threshold": target,
            "source": "slo-detector", "incident_id": incident["_id"], "mttr_s": mttr_s,
            "justification": f"Verified by the SLO monitor: {note}.",
        })
    await db[pfx + "ibn_intents"].update_one(
        {"_id": intent["_id"], "status": "violated"},
        {"$set": {"status": "active"},
         "$push": {"history": {"ts": now, "event": "recovered", "by": "slo-detector",
                               "incident_id": incident["_id"], "note": note}}})
    await notify({"type": "incident", "doc": res})


async def slo_evaluator(db, pfx: str, notify: Notify, rushing: set) -> None:
    trackers: dict[str, slo.Tracker] = {}
    log.info(f"SLO evaluator started (prefix={pfx!r}, {slo.WINDOW_S}s window "
             f"every {slo.EVAL_EVERY_S}s)")
    while True:
        try:
            await asyncio.sleep(slo.EVAL_EVERY_S)
            since = _now() - datetime.timedelta(seconds=slo.WINDOW_S)
            cursor = await db[pfx + "ibn_telemetry"].aggregate(slo.window_pipeline(since))
            window = slo.measurements(await cursor.to_list(None))
            if not window:
                continue
            live = await db[pfx + "ibn_intents"].find(
                {"_id": {"$in": list(window)}, "status": {"$in": ["active", "violated"]}}
            ).to_list(None)
            open_ = {i["intent_id"]: i for i in await db[pfx + "ibn_incidents"].find(
                {"open": True}).to_list(None)}
            for intent in live:
                metrics = window[intent["_id"]]
                latency = metrics.get(slo.LATENCY)
                if not latency or latency["n"] < slo.MIN_SAMPLES:
                    continue   # no signal: hold the state
                tracker = trackers.setdefault(intent["_id"], slo.Tracker())
                incident = open_.get(intent["_id"])
                action = slo.step(tracker, latency["p95"], rush.threshold(intent),
                                  incident is not None)
                if action == "open":
                    await open_incident(db, pfx, intent, metrics, notify, rushing)
                elif action == "close" and incident:
                    await close_incident(db, pfx, intent, incident, metrics, notify)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep the lane's evaluator alive
            log.warning(f"SLO evaluator error ({e})")
            await asyncio.sleep(2)
