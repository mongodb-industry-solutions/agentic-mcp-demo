#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
What the self-healing loop learns (SELF_HEALING_PLAN.md, S3), as pure
functions: the SLO monitor (web/ibn_assurance.py) writes the document, the
tools read it back.

When the monitor closes an incident the system fixed on its own, the
incident becomes a new `kind: incident` chunk in ibn_knowledge_chunks,
shaped like the seeded ones (title, site, ts, lng/lat, fingerprint,
runbook_id, text), so the hero query finds it and Atlas embeds its text.
Its text is written from the measurements, not from a template.

On Agent Engine the same lesson also goes into platform memory (lesson()):
await_recovery adds it to its result, and the domain agent's graph saves it
as an org-visible semantic memory, so the coordinator recalls the fix in
plain conversation ("have we seen this before?"). The chunk stays the
system of record for the hero query; the memory is the agents' recollection.

ibn_knowledge_chunks is shared by every lane and by the live Kanopy demo,
and its vector index stays as it is. A learned chunk therefore carries its
lane (the session prefix, '' for the default lane), every reader of
incidents keeps to its own lane plus the seeded chunks (lane_filter), and
Reset deletes the lane's learned chunks.
"""

import datetime

from oss_tools.ibn import slo

LEARNED_BY = "self-healing loop"


def learned_id(incident_id) -> str:
    return f"INC-L-{incident_id}"


def lesson_label(incident_id) -> str:
    return f"learned-incident-{incident_id}"


LESSON_MARK = "Lesson for memory"   # the line await_recovery adds, read by the graph


def lesson(incident: dict, site_name: str, runbook: dict) -> str:
    """One sentence about a closed, autonomously mitigated incident, for
    platform memory."""
    measured = incident.get("measured") or {}
    action = incident["action"]
    p95, util = measured.get("pos_latency_p95_ms"), measured.get("uplink_util_avg_pct")
    symptom = "POS latency"
    if p95 is not None:
        symptom += f" of {p95:.0f} ms p95"
        if incident.get("threshold") is not None:
            symptom += f" against a {incident['threshold']} ms target"
    if util is not None:
        symptom += f", uplink {util:.0f}% busy"
    mttr_s = incident.get("mttr_s") or round(
        (incident["recovered_at"] - incident["opened_at"]).total_seconds())
    return (f"Learned on {incident['recovered_at']:%Y-%m-%d}: in a morning rush at {site_name} "
            f"({incident['intent_id']}), {symptom}, was fixed autonomously with runbook "
            f"{runbook['_id']} ({runbook.get('title', '')}) under "
            f"{action.get('policy', 'the autonomy policy')}; the SLO monitor verified the "
            f"recovery {mttr_s:.0f} s after detection, with no human action. Sister sites on "
            f"the same segmentation template are exposed to the same pattern.")


def lane_filter(prefix: str) -> dict:
    """Seeded chunks (no lane) plus the ones this lane learned."""
    return {"$or": [{"lane": {"$exists": False}}, {"lane": prefix}]}


def chunk(incident: dict, site: dict, customer: str, runbook: dict, prefix: str,
          segmentation: str = "strict") -> dict:
    """The learned incident chunk for a closed, autonomously mitigated
    incident (it has `action` and `recovered_at`)."""
    measured = incident.get("measured") or {}
    action = incident["action"]
    opened, acted, recovered = incident["opened_at"], action["at"], incident["recovered_at"]
    p95 = measured.get("pos_latency_p95_ms")
    util = measured.get("uplink_util_avg_pct")
    target = incident.get("threshold")
    lng, lat = (site.get("location") or {}).get("coordinates", [None, None])
    act_s = round((acted - opened).total_seconds())
    mttr_s = incident.get("mttr_s") or round((recovered - opened).total_seconds(), 1)
    # The symptom part is the measured fingerprint the hero query searches
    # with, so the next rush like this one finds it first (0.93 vs 0.91 for
    # the seeded Hamburg incident on the twin).
    text = (
        slo.fingerprint_text(site["name"], target, segmentation, measured)
        + " Root cause: POS payment traffic shared the best-effort queue with bulk uploads "
        f"(head-of-line blocking). Resolution: runbook {runbook['_id']} "
        f"({runbook.get('title', '')}) applied autonomously {act_s} seconds after detection "
        f"under {action.get('policy', 'the autonomy policy')}; the SLO monitor verified the "
        f"recovery from live telemetry {mttr_s:.0f} seconds after detection, with no human "
        f"action. Sister sites on the same segmentation template face the same pattern."
    )
    return {
        "_id": learned_id(incident["_id"]),
        "kind": "incident",
        "title": f"{site['name']} POS latency in the morning rush, fixed autonomously",
        "site_id": site["_id"],
        "site_name": site["name"],
        "customer": customer,
        "ts": recovered,
        "lng": lng, "lat": lat,
        "runbook_id": runbook["_id"],
        "fingerprint": {
            "trigger": "morning_rush",
            "segmentation": segmentation,
            "link_util_pct": round(util) if util is not None else None,
            "latency_ms_observed": round(p95) if p95 is not None else None,
            "latency_ms_threshold": target,
        },
        "text": text,
        "outcome": "resolved",
        "mttr_s": mttr_s,
        "learned": True,
        "learned_by": LEARNED_BY,
        "learned_at": datetime.datetime.now(),
        "lane": prefix,
        "incident_id": incident["_id"],
        "intent_id": incident["intent_id"],
    }
