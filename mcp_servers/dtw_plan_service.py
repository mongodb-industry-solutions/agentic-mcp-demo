#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Plan Service — ACME Mobile digital-twin plans, QoS profiles, subscribers.

The catalog surface of the Digital Twin demo. Owns dtw_plans, dtw_qos_profiles,
and read access to dtw_subscribers. Use this service to look up the commercial
products (ACME M, Premium, Plus5G, Connect, …), their bound QoS profiles
(max downlink/uplink Mbps, QCI, ARP, APN overrides, PCRF template refs), and
to sample subscribers for a given plan.

Use this service when users say:
- Describe:  "describe plan ACME M", "show plan ACME-M", "what is in plan X"
- QoS:       "show QoS profile qos_prepaid_7_2", "details on the prepaid QoS",
             "what does qos_prepaid_20 look like", "QoS profile params"
- List:      "list plans", "show all plans", "what plans do we have",
             "list prepaid plans", "postpaid plans"
- Compare:   "compare qos_prepaid_7_2 to qos_prepaid_20",
             "diff QoS profiles", "what changes from 7.2 to 20 Mbps"
- Sample:    "show some subscribers on plan ACME M",
             "subscriber sample", "who is on plan X"

This service does NOT traverse the topology graph, model traffic, run
simulations, or own scenarios — those belong to the topology, traffic,
scenario, and simulation DTW services respectively.

This service is NOT about retail intents, retail sites, or compliance — that
is the IBN demo. Don't confuse `plan` (mobile data plan) with `policy
template` (provisioning template) — those are different demos.
"""

import logging
import os
from pymongo import MongoClient, ASCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("dtw_plan_service")
logger        = logging.getLogger("dtw_plan_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
plans         = db["dtw_plans"]
qos_profiles  = db["dtw_qos_profiles"]
subscribers   = db["dtw_subscribers"]


def _resolve_plan(plan_hint: str) -> dict | None:
    """Resolve a plan by id or name fragment. Tolerates 'M', 'ACME M',
    'plan_ACME_M', 'plan-ACME-M', 'ACME-M'."""
    if not plan_hint:
        return None
    # Direct id
    doc = plans.find_one({"_id": plan_hint})
    if doc:
        return doc
    # Name match (case-insensitive)
    doc = plans.find_one({"name": {"$regex": plan_hint, "$options": "i"}})
    if doc:
        return doc
    # Loose id match: try prefix variations
    suffix = plan_hint.replace(" ", "_").replace("-", "_")
    bare   = suffix[len("ACME_"):] if suffix.startswith("ACME_") else suffix
    variants = [
        suffix,
        f"plan_ACME_{suffix}",
        f"plan_ACME_{bare}",
    ]
    for v in variants:
        doc = plans.find_one({"_id": v})
        if doc:
            return doc
    return None


def _resolve_qos(qos_hint: str) -> dict | None:
    """Resolve a QoS profile by id or name fragment."""
    if not qos_hint:
        return None
    doc = qos_profiles.find_one({"_id": qos_hint})
    if doc:
        return doc
    return qos_profiles.find_one({"name": {"$regex": qos_hint, "$options": "i"}})


def _format_plan_card(plan: dict) -> str:
    qos = qos_profiles.find_one({"_id": plan.get("current_qos_profile_id")})
    flags = plan.get("hlr_service_flags", {})
    flag_bits = []
    if flags.get("data_allowed"):    flag_bits.append("data")
    if flags.get("roaming_allowed"): flag_bits.append("roaming")
    if flags.get("volte_allowed"):   flag_bits.append("VoLTE")
    lines = [
        f"**{plan['_id']}** · {plan.get('name')} · {plan.get('segment')}",
        f"  QoS: {qos.get('name') if qos else plan.get('current_qos_profile_id')}"
        f" ({qos.get('max_downlink_mbps') if qos else '?'} Mbps DL)",
        f"  Flags: {', '.join(flag_bits) or '—'}",
        f"  Price: ${plan.get('monthly_price_usd', '?')}/mo",
    ]
    if plan.get("target_qos_profile_id"):
        tqos = qos_profiles.find_one({"_id": plan["target_qos_profile_id"]})
        lines.append(f"  ⓘ Earmarked target QoS: {tqos.get('name') if tqos else plan['target_qos_profile_id']}")
    return "\n".join(lines)


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def describe_plan(plan_id: str) -> str:
    """
    Show a full plan with its current QoS profile and HSS service flags.
    Accepts loose identifiers ('M', 'ACME M', 'plan_ACME_M').

    Args:
        plan_id: Plan identifier or name fragment.
    """
    plan = _resolve_plan(plan_id)
    if not plan:
        return f"❌ No plan found for {plan_id!r}."

    qos = qos_profiles.find_one({"_id": plan.get("current_qos_profile_id")})
    flags = plan.get("hlr_service_flags", {})
    hss   = plan.get("hss_service_profile", {})

    lines = [
        f"## Plan {plan['_id']} — {plan.get('name')}",
        f"**Segment:** {plan.get('segment')}",
        f"**Price:** ${plan.get('monthly_price_usd', '?')}/month",
        "",
        "**HLR service flags:**",
        f"- data_allowed: {flags.get('data_allowed')}",
        f"- roaming_allowed: {flags.get('roaming_allowed')}",
        f"- volte_allowed: {flags.get('volte_allowed')}",
        "",
        "**HSS service profile:**",
        f"- APN: {hss.get('apn', '—')}",
        f"- IMS services: {', '.join(hss.get('ims_services', [])) or '—'}",
        "",
        "**Current QoS profile:**",
    ]
    if qos:
        lines.extend([
            f"- {qos['_id']} ({qos.get('name')})",
            f"- max DL: {qos.get('max_downlink_mbps')} Mbps · max UL: {qos.get('max_uplink_mbps')} Mbps",
            f"- QCI: {qos.get('qci')} · ARP: {qos.get('arp')}",
            f"- PCRF template: {qos.get('hss_policy_template_ref')}",
        ])
    else:
        lines.append(f"- {plan.get('current_qos_profile_id')} (profile doc not found)")

    if plan.get("target_qos_profile_id"):
        tqos = qos_profiles.find_one({"_id": plan["target_qos_profile_id"]})
        lines.append("")
        lines.append("**Earmarked target QoS (for what-if change):**")
        if tqos:
            lines.append(f"- {tqos['_id']} ({tqos.get('name')}) — "
                         f"{tqos.get('max_downlink_mbps')} Mbps DL")
        else:
            lines.append(f"- {plan['target_qos_profile_id']}")
    return "\n".join(lines)


@mcp.tool()
def get_qos_profile(qos_profile_id: str) -> str:
    """
    Show full QoS profile parameters: throughput caps, QCI, ARP, per-APN
    overrides, PCRF template reference.

    Args:
        qos_profile_id: QoS profile id (e.g. 'qos_prepaid_20') or name fragment.
    """
    qos = _resolve_qos(qos_profile_id)
    if not qos:
        return f"❌ No QoS profile found for {qos_profile_id!r}."

    lines = [
        f"## QoS profile {qos['_id']} — {qos.get('name')}",
        f"- max downlink: {qos.get('max_downlink_mbps')} Mbps",
        f"- max uplink:   {qos.get('max_uplink_mbps')} Mbps",
        f"- QCI: {qos.get('qci')}  ·  ARP: {qos.get('arp')}",
        f"- PCRF template: `{qos.get('hss_policy_template_ref')}`",
    ]
    overrides = qos.get("per_apn_overrides") or []
    if overrides:
        lines.append("")
        lines.append("**Per-APN overrides:**")
        for o in overrides:
            lines.append(f"- {o.get('apn')}: {o.get('max_downlink_mbps')} Mbps DL")
    return "\n".join(lines)


@mcp.tool()
def list_plans(segment: str = None) -> str:
    """
    List all plans, optionally filtered by segment ('prepaid' or 'postpaid').

    Args:
        segment: Optional filter. Common values: 'prepaid', 'postpaid'.
    """
    q = {"segment": segment} if segment else {}
    docs = list(plans.find(q).sort("_id", ASCENDING))
    if not docs:
        scope = f" with segment '{segment}'" if segment else ""
        return f"No plans found{scope}."
    header = f"**{len(docs)} plan{'s' if len(docs) != 1 else ''}" + \
             (f" ({segment})" if segment else "") + ":**"
    return header + "\n\n" + "\n\n".join(_format_plan_card(p) for p in docs)


@mcp.tool()
def compare_qos_profiles(before: str, after: str) -> str:
    """
    Side-by-side diff of two QoS profiles. Useful before running a what-if
    QoS uplift scenario.

    Args:
        before: Current QoS profile id (or name fragment).
        after:  Proposed QoS profile id (or name fragment).
    """
    a = _resolve_qos(before)
    b = _resolve_qos(after)
    if not a or not b:
        return f"❌ Could not resolve both QoS profiles ({before!r}, {after!r})."

    def _delta(x, y):
        if x is None or y is None: return ""
        if x == y: return ""
        try:
            d = float(y) - float(x)
            return f" ({'+' if d >= 0 else ''}{d:g})"
        except Exception:
            return ""

    lines = [
        f"## QoS diff: `{a['_id']}` → `{b['_id']}`",
        "",
        f"| Field | {a['_id']} | {b['_id']} |",
        "|---|---|---|",
        f"| name | {a.get('name')} | {b.get('name')} |",
        f"| max DL Mbps | {a.get('max_downlink_mbps')} | {b.get('max_downlink_mbps')}"
        f"{_delta(a.get('max_downlink_mbps'), b.get('max_downlink_mbps'))} |",
        f"| max UL Mbps | {a.get('max_uplink_mbps')} | {b.get('max_uplink_mbps')}"
        f"{_delta(a.get('max_uplink_mbps'), b.get('max_uplink_mbps'))} |",
        f"| QCI | {a.get('qci')} | {b.get('qci')} |",
        f"| ARP | {a.get('arp')} | {b.get('arp')} |",
        f"| PCRF | `{a.get('hss_policy_template_ref')}` | `{b.get('hss_policy_template_ref')}` |",
    ]
    return "\n".join(lines)


@mcp.tool()
def subscribers_for_plan(plan_id: str, limit: int = 5) -> str:
    """
    Return a small sample of subscribers on a given plan, plus the total
    count and the market-level distribution. The full subscriber base is
    typically too large to list — use this to spot-check.

    Args:
        plan_id: Plan identifier (e.g. 'plan_ACME_M').
        limit:   How many sample subscribers to return (default 5, max 20).
    """
    plan = _resolve_plan(plan_id)
    if not plan:
        return f"❌ No plan found for {plan_id!r}."
    pid = plan["_id"]
    limit = max(1, min(20, int(limit or 5)))

    total = subscribers.count_documents({"plan_id": pid})
    if total == 0:
        return f"No subscribers on plan {pid}."

    sample = list(subscribers.find({"plan_id": pid}).limit(limit))
    # Market distribution
    dist = list(subscribers.aggregate([
        {"$match": {"plan_id": pid}},
        {"$group": {"_id": "$home_market", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]))

    lines = [f"## Sample subscribers on **{plan.get('name')}** (`{pid}`)",
             f"Total subscribers on plan: **{total}**", ""]
    if dist:
        lines.append("**Market distribution:**")
        for d in dist:
            pct = 100.0 * d["n"] / total
            lines.append(f"- {d['_id']}: {d['n']} ({pct:.1f}%)")
        lines.append("")
    lines.append(f"**{len(sample)} sample(s):**")
    for s in sample:
        cells = ", ".join(c["cell_id"] for c in s.get("approx_active_cells", [])[:2])
        lines.append(f"- `{s['imsi']}` · {s.get('msisdn')} · {s.get('home_market')} "
                     f"· {s.get('sim_type')} · cells: {cells or '—'}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
