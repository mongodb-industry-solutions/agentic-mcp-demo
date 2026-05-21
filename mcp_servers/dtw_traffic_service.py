#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Traffic Service — Digital-twin traffic patterns and load estimation.

The behavioural-model surface of the Digital Twin demo. Owns dtw_traffic_models,
which captures, per plan × market × time-window, the active subscriber
estimate and per-user throughput characteristics on each cell. The simulation
service consumes these models to project load changes; this service is the
read/inspect API for them.

Use this service when users say:
- Lookup:    "show traffic model for ACME M in NYC on Saturday night",
             "what is the load model for plan X in market Y",
             "traffic model details", "get traffic model X"
- Estimate:  "estimate load on cell X during Saturday night",
             "what's the projected utilization on cell X",
             "cell load for time window Y"
- Windows:   "list time windows", "what time windows do we have",
             "show traffic windows"
- Peak:      "what are the peak hours for NYC", "peak window for market X"

The traffic models are static fixtures — they describe the *expected* load
pattern. The simulation service applies them under a proposed change to
produce *projected* load.

This service does NOT model commercial plans (that is the plan service),
walk the dependency graph (the topology service), own scenarios, or run
simulations (the scenario and simulation services respectively).
"""

import logging
import os
from pymongo import MongoClient, ASCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("dtw_traffic_service")
logger        = logging.getLogger("dtw_traffic_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
traffic       = db["dtw_traffic_models"]
elements      = db["dtw_network_elements"]


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_time_windows() -> str:
    """List all distinct time windows present in the traffic models."""
    rows = list(traffic.aggregate([
        {"$group": {
            "_id":   "$time_window._id",
            "label": {"$first": "$time_window.label"},
            "day":   {"$first": "$time_window.day_of_week"},
            "from":  {"$first": "$time_window.from"},
            "to":    {"$first": "$time_window.to"},
            "n":     {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]))
    if not rows:
        return "No time windows present in traffic models."
    lines = [f"**{len(rows)} time window(s):**"]
    for r in rows:
        lines.append(f"- `{r['_id']}` · {r.get('day')} {r.get('from')}–{r.get('to')} "
                     f"· {r.get('label')} · {r['n']} model(s)")
    return "\n".join(lines)


@mcp.tool()
def get_traffic_model(plan_id: str, market: str = None, time_window: str = None) -> str:
    """
    Look up traffic models for a plan, optionally narrowed by market and/or
    time window. Returns the per-cell load estimates.

    Args:
        plan_id:     Plan id (e.g. 'plan_ACME_M').
        market:      Optional market id (e.g. 'NYC_Metro').
        time_window: Optional time-window id (e.g. 'Saturday_20_23').
    """
    q: dict = {"plan_id": plan_id}
    if market:
        q["market"] = market
    if time_window:
        q["time_window._id"] = time_window
    docs = list(traffic.find(q).sort("_id", ASCENDING))
    if not docs:
        return f"No traffic models match plan_id={plan_id!r}, market={market!r}, " \
               f"time_window={time_window!r}."

    lines = [f"**{len(docs)} traffic model(s) for {plan_id}:**", ""]
    for tm in docs[:8]:
        cells = tm.get("cells", [])
        total_subs = sum(c.get("active_subscribers_estimate", 0) for c in cells)
        avg_dl = (sum(c.get("avg_per_user_mbps", 0) for c in cells) / len(cells)) if cells else 0
        peak_dl = max((c.get("peak_per_user_mbps", 0) for c in cells), default=0)
        tw = tm.get("time_window", {})
        lines.append(f"### `{tm['_id']}`")
        lines.append(f"- market: {tm.get('market')} · segment: {tm.get('segment')}")
        lines.append(f"- window: {tw.get('day_of_week')} {tw.get('from')}–{tw.get('to')} "
                     f"({tw.get('label')})")
        lines.append(f"- cells modeled: {len(cells)} · "
                     f"total active estimate: {total_subs} subscribers")
        lines.append(f"- per-user DL: avg {avg_dl:.2f} Mbps · peak {peak_dl:.2f} Mbps")
        # Show top-3 most-loaded cells
        top = sorted(cells, key=lambda c: c.get("active_subscribers_estimate", 0),
                     reverse=True)[:3]
        if top:
            lines.append("- top cells:")
            for c in top:
                lines.append(f"    • `{c['cell_id']}` · "
                             f"{c.get('active_subscribers_estimate')} subs · "
                             f"avg {c.get('avg_per_user_mbps')} Mbps · "
                             f"peak {c.get('peak_per_user_mbps')} Mbps")
        lines.append("")
    if len(docs) > 8:
        lines.append(f"_… and {len(docs) - 8} more models not shown._")
    return "\n".join(lines)


@mcp.tool()
def estimate_cell_load(cell_id: str, time_window: str = None) -> str:
    """
    Estimate aggregate load on a single cell across all plans, for a given
    time window (or all known windows). Sums across traffic models from
    every plan that touches the cell.

    Args:
        cell_id:     Cell id (e.g. 'cell_NYC_Metro_01_A').
        time_window: Optional time window id; if omitted, all windows.
    """
    cell = elements.find_one({"_id": cell_id, "type": "Cell"})
    if not cell:
        return f"❌ Cell {cell_id!r} not found."
    cap = cell.get("capacity") or {}
    cap_dl = cap.get("downlink_mbps") or 0

    q: dict = {"cells.cell_id": cell_id}
    if time_window:
        q["time_window._id"] = time_window
    models = list(traffic.find(q))

    if not models:
        return f"No traffic models touch `{cell_id}`" + \
               (f" in window {time_window}" if time_window else "") + "."

    # Sum per-window
    by_window: dict[str, dict] = {}
    for tm in models:
        tw_id = tm["time_window"]["_id"]
        slot = by_window.setdefault(tw_id, {
            "label": tm["time_window"].get("label"),
            "total_subs": 0,
            "weighted_avg": 0.0,
            "peak_per_user": 0.0,
            "plans": [],
        })
        for c in tm.get("cells", []):
            if c.get("cell_id") != cell_id:
                continue
            slot["total_subs"]     += c.get("active_subscribers_estimate", 0)
            slot["weighted_avg"]   += (c.get("active_subscribers_estimate", 0)
                                       * c.get("avg_per_user_mbps", 0))
            slot["peak_per_user"]  = max(slot["peak_per_user"],
                                          c.get("peak_per_user_mbps", 0))
            slot["plans"].append(tm.get("plan_id"))

    lines = [f"## Estimated load on `{cell_id}` (capacity {cap_dl} Mbps DL)", ""]
    for tw_id, slot in sorted(by_window.items()):
        avg = slot["weighted_avg"]  # already-summed DL Mbps across users
        # Note: per-user avg × subs already gives expected aggregate Mbps
        agg_mbps = avg
        util_pct = 100.0 * agg_mbps / cap_dl if cap_dl else 0
        peak_mbps = slot["peak_per_user"] * (slot["total_subs"] / 4)  # rough peak concurrent estimate
        lines.append(f"**{tw_id}** ({slot['label']})")
        lines.append(f"- contributing plans: {', '.join(set(slot['plans']))}")
        lines.append(f"- total active estimate: {slot['total_subs']} subscribers")
        lines.append(f"- expected aggregate DL: {agg_mbps:.1f} Mbps "
                     f"({util_pct:.1f}% of cell capacity)")
        lines.append(f"- worst-case peak DL: ~{peak_mbps:.0f} Mbps")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def peak_hours_for_market(market: str) -> str:
    """
    For a given market, identify the time windows with the highest aggregate
    estimated subscribers across all plans. Useful for picking the scope of
    a what-if simulation.

    Args:
        market: Market id (e.g. 'NYC_Metro').
    """
    rows = list(traffic.aggregate([
        {"$match": {"market": market}},
        {"$unwind": "$cells"},
        {"$group": {
            "_id":          "$time_window._id",
            "label":        {"$first": "$time_window.label"},
            "from":         {"$first": "$time_window.from"},
            "to":           {"$first": "$time_window.to"},
            "total_subs":   {"$sum": "$cells.active_subscribers_estimate"},
            "plans":        {"$addToSet": "$plan_id"},
            "cells":        {"$addToSet": "$cells.cell_id"},
        }},
        {"$sort": {"total_subs": -1}},
    ]))
    if not rows:
        return f"No traffic data for market {market!r}."
    lines = [f"## Peak windows for {market}",
             f"_{len(rows)} window(s) modeled, ranked by aggregate active subscribers._", ""]
    for r in rows:
        lines.append(f"**{r['_id']}** ({r.get('label')}) {r.get('from')}–{r.get('to')}")
        lines.append(f"  - total active estimate: {r['total_subs']} subs")
        lines.append(f"  - {len(r['plans'])} contributing plan(s) · {len(r['cells'])} cell(s)")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
