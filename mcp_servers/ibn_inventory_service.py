#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Inventory Service — Sites, Resources, and Topology

Read-only inventory surface for the Intent-Based Networking demo. Owns
queries against ibn_sites and ibn_resources. Heterogeneous resource
documents (access nodes, uplinks, CPEs, segmentation templates) live side
by side under one collection — the document-model selling point. Includes
geospatial lookups via 2dsphere indexes for "find spare resources nearby"
queries.

Use this service when users say:
- Sites:    "list sites", "show all stores", "where are our locations",
           "what sites do we have"
- Resources: "what resources are available at <site>", "show inventory at <site>",
           "list access nodes / uplinks / CPEs at <site>", "topology of <site>"
- Geo:     "any spare resources near <site>", "nearby capacity",
           "failover candidates within Xkm"

This service does NOT submit intents, check feasibility, manage the
lifecycle of services, compute compliance, or run telemetry.
"""

import logging
import os
from pymongo import MongoClient
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("ibn_inventory_service")
logger = logging.getLogger("ibn_inventory_service")

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db           = mongo_client["agent_registry"]
sites        = db["ibn_sites"]
resources    = db["ibn_resources"]


def _resolve_site(site_hint: str) -> dict | None:
    """Resolve a site by fragments, tolerating word-order variations."""
    if not site_hint:
        return None
    direct = sites.find_one({"name": {"$regex": site_hint, "$options": "i"}})
    if direct:
        return direct
    tokens = [t for t in site_hint.split() if len(t) >= 3]
    if not tokens:
        return None
    result = sites.find_one(
        {"$and": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )
    if result:
        return result
    return sites.find_one(
        {"$or": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_sites() -> str:
    """List all sites in the inventory with their status and address."""
    docs = list(sites.find({}).sort("name", 1))
    if not docs:
        return "No sites in inventory."

    lines = [f"**{len(docs)} site{'s' if len(docs) != 1 else ''}:**\n"]
    for s in docs:
        status_emoji = {"active": "🟢", "provisioning": "🛠️ ", "decommissioned": "⊗"}.get(
            s.get("status"), "•"
        )
        lines.append(
            f"- **{s['name']}** {status_emoji} {s.get('status', '—')}\n"
            f"  {s.get('address', '—')}"
        )
    return "\n".join(lines)


@mcp.tool()
def list_resources(site: str) -> str:
    """
    List all resources at a given site, grouped by type. Shows the
    heterogeneous shape of the resource catalog (access nodes, uplinks, CPEs)
    with type-specific fields surfaced.

    Args:
        site: Site name fragment, e.g. 'Marienplatz' or 'Munich Marienplatz'.
    """
    s = _resolve_site(site)
    if not s:
        return f"❌ Site '{site}' not found."

    docs = list(resources.find({"site_id": s["_id"]}))
    if not docs:
        return f"No resources catalogued at {s['name']}."

    by_type: dict[str, list] = {}
    for d in docs:
        by_type.setdefault(d["type"], []).append(d)

    lines = [f"## Resources at {s['name']}\n"]

    for rtype in ("access_node", "uplink", "cpe"):
        if rtype not in by_type:
            continue
        lines.append(f"**{rtype.replace('_', ' ').title()}:**")
        for r in by_type[rtype]:
            if rtype == "access_node":
                caps = ", ".join(r.get("capabilities", [])[:4])
                lines.append(
                    f"- `{r['_id']}` · {r.get('vendor')} {r.get('model')} ({r.get('os', '—')})\n"
                    f"  capacity {r.get('capacity_gbps')} Gbps "
                    f"(free {r.get('free_gbps')} Gbps) · {caps}"
                )
            elif rtype == "uplink":
                lines.append(
                    f"- `{r['_id']}` · {r.get('medium')} "
                    f"{r.get('capacity_mbps')} Mbps · {r.get('provider', '—')} "
                    f"· €{r.get('monthly_cost_eur')}/mo"
                )
            elif rtype == "cpe":
                features = ", ".join(r.get("feature_set", []))
                lines.append(
                    f"- `{r['_id']}` · {r.get('vendor')} {r.get('model')}\n"
                    f"  features: {features}"
                )
        lines.append("")

    return "\n".join(lines).rstrip()


@mcp.tool()
def get_topology(site: str) -> str:
    """
    Summarize topology at a site: access node, current uplink, CPE,
    segmentation template in use. Useful for understanding the actual
    physical/logical layout, vs list_resources which shows the catalog.

    Args:
        site: Site name fragment.
    """
    s = _resolve_site(site)
    if not s:
        return f"❌ Site '{site}' not found."

    rs = list(resources.find({"site_id": s["_id"]}))
    if not rs:
        return f"No resources catalogued at {s['name']}."

    by_type = {r["type"]: [] for r in rs}
    for r in rs:
        by_type[r["type"]].append(r)

    lines = [f"## Topology — {s['name']}", f"Status: **{s.get('status', '—')}**"]

    if "access_node" in by_type:
        an = by_type["access_node"][0]
        lines.append(f"\n**Access Node:** `{an['_id']}` ({an.get('vendor')} "
                     f"{an.get('model')}, {an.get('os', '—')})")
        lines.append(f"  Capabilities: {', '.join(an.get('capabilities', []))}")

    if "uplink" in by_type:
        lines.append(f"\n**Uplink Options:**")
        for u in by_type["uplink"]:
            lines.append(f"  - `{u['_id']}` · {u.get('medium')} "
                         f"{u.get('capacity_mbps')} Mbps · {u.get('provider')}")

    if "cpe" in by_type:
        lines.append(f"\n**CPE Candidates:**")
        for c in by_type["cpe"]:
            lines.append(f"  - `{c['_id']}` · {c.get('vendor')} {c.get('model')} "
                         f"({len(c.get('feature_set', []))} features)")

    return "\n".join(lines)


@mcp.tool()
def find_nearby_spare(site: str, radius_km: float = 20) -> str:
    """
    Find spare access-node capacity within radius_km of the given site,
    using the 2dsphere geospatial index. Returns nearby resources with
    free capacity, ordered by distance. Useful for failover planning.

    Args:
        site:      Site name fragment to use as the anchor location.
        radius_km: Search radius in kilometres (default 20).
    """
    s = _resolve_site(site)
    if not s or not s.get("location"):
        return f"❌ Site '{site}' not found or has no coordinates."

    pipeline = [
        {
            "$geoNear": {
                "near": s["location"],
                "distanceField": "distance_m",
                "maxDistance": radius_km * 1000,
                "spherical": True,
                "query": {
                    "type":    "access_node",
                    "site_id": {"$ne": s["_id"]},  # exclude same site
                },
            }
        },
        {"$limit": 5},
    ]
    candidates = list(resources.aggregate(pipeline))

    if not candidates:
        return (
            f"No spare access-node capacity found within {radius_km}km of "
            f"{s['name']}."
        )

    lines = [f"**{len(candidates)} candidate{'s' if len(candidates) != 1 else ''} "
             f"within {radius_km}km of {s['name']}:**\n"]
    for r in candidates:
        site_doc = sites.find_one({"_id": r["site_id"]})
        site_name = site_doc["name"] if site_doc else r["site_id"]
        dist_km = r["distance_m"] / 1000
        lines.append(
            f"- `{r['_id']}` at **{site_name}** ({dist_km:.1f}km away)\n"
            f"  {r.get('vendor')} {r.get('model')} · "
            f"{r.get('free_gbps')} Gbps free of {r.get('capacity_gbps')} Gbps"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
