#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW Topology Service — Digital twin RAN + core inventory and dependency graph.

The network-inventory surface of the Digital Twin demo. Owns dtw_network_elements
(polymorphic: HSS, HLR, MME, SGW, PGW, eNodeB, Cell — one collection, one
schema-per-type) and dtw_topology_edges (the dependency graph). Use this
service to inspect network elements, find what cells exist in a market, and
walk the dependency graph from any starting node via $graphLookup.

Use this service when users say:
- Element:   "show network element <id>", "describe <network-element-id>",
             "look up <ne_*>", "get the HSS / SGW / PGW record"
- Cells:     "list cells in <market>", "show cells in a market",
             "what cells does an eNB host"
- Traverse:  "what depends on a network element", "trace dependencies",
             "show the dependency graph from <id>",
             "graph walk from <id>", "topology traversal"
- Markets:   "list markets", "what markets do we cover",
             "geographic footprint"

The traversal tools wrap MongoDB `$graphLookup` over dtw_topology_edges so
the LLM never has to construct the aggregation itself.

This service does NOT own plans, QoS profiles, traffic models, scenarios, or
simulations — those are the plan, traffic, scenario, and simulation DTW
services respectively. It does NOT serve the IBN retail demo's site/resource
inventory either — that lives in ibn_inventory_service.

This service operates exclusively on dependency-graph identifiers of the
shape `ne_HSS_*`, `ne_HLR_*`, `ne_MME_*`, `ne_SGW_*`, `ne_PGW_*`,
`ne_eNB_*`, `cell_*`, or `plan_ACME_*`. If the user's request does not
reference one of those id shapes, this service is the wrong tool.
"""

import logging
import os
from pymongo import MongoClient, ASCENDING
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp           = FastMCP("dtw_topology_service")
logger        = logging.getLogger("dtw_topology_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
elements      = db["dtw_network_elements"]
edges         = db["dtw_topology_edges"]
markets_coll  = db["dtw_markets"]


def _resolve_market(hint: str) -> str | None:
    """Map loose market hints to canonical market ids."""
    if not hint:
        return None
    h = hint.lower().replace("-", "_").replace(" ", "_")
    direct = markets_coll.find_one({"_id": {"$regex": f"^{hint}$", "$options": "i"}})
    if direct:
        return direct["_id"]
    # Fuzzy match against names
    by_name = markets_coll.find_one({"name": {"$regex": hint, "$options": "i"}})
    if by_name:
        return by_name["_id"]
    # Match prefix
    candidates = [m["_id"] for m in markets_coll.find({})]
    for c in candidates:
        if c.lower().startswith(h):
            return c
    return None


def _format_element_brief(el: dict) -> str:
    bits = [f"`{el['_id']}` · {el.get('type')}"]
    if el.get("market"):
        bits.append(el["market"])
    if el.get("vendor"):
        bits.append(el["vendor"])
    if el.get("model"):
        bits.append(el["model"])
    cap = el.get("capacity") or {}
    if cap.get("downlink_mbps"):
        bits.append(f"{cap['downlink_mbps']} Mbps DL")
    if cap.get("throughput_gbps"):
        bits.append(f"{cap['throughput_gbps']} Gbps")
    if cap.get("current_utilization") is not None:
        bits.append(f"util {int(100*cap['current_utilization'])}%")
    return " · ".join(bits)


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_markets() -> str:
    """
    List all markets covered by the digital twin, with population estimates
    and counts of network elements per market.
    """
    docs = list(markets_coll.find({}).sort("_id", ASCENDING))
    if not docs:
        return "No markets seeded."
    counts = {d["_id"]: 0 for d in docs}
    for row in elements.aggregate([{"$group": {"_id": "$market", "n": {"$sum": 1}}}]):
        if row["_id"] in counts:
            counts[row["_id"]] = row["n"]
    lines = [f"**{len(docs)} markets:**"]
    for d in docs:
        lines.append(f"- **{d['_id']}** ({d.get('name')}) · pop {d.get('population_m')}M "
                     f"· {counts.get(d['_id'], 0)} network elements")
    return "\n".join(lines)


@mcp.tool()
def get_network_element(element_id: str) -> str:
    """
    Show full details of one network element (HSS, HLR, MME, SGW, PGW, eNB, Cell).
    Args:
        element_id: Element identifier, e.g. 'ne_PGW_NYC_Metro' or 'cell_NYC_Metro_01_A'.
    """
    el = elements.find_one({"_id": element_id})
    if not el:
        return f"❌ Network element {element_id!r} not found."

    lines = [
        f"## {el['_id']} · {el.get('type')}",
        f"- Market: {el.get('market', '—')}",
        f"- Vendor: {el.get('vendor', '—')} {el.get('model', '')}".rstrip(),
    ]
    cap = el.get("capacity") or {}
    if cap:
        lines.append("")
        lines.append("**Capacity:**")
        for k, v in cap.items():
            lines.append(f"- {k}: {v}")
    if el.get("location"):
        coords = el["location"].get("coordinates", [None, None])
        lines.append(f"\n**Location:** {coords[1]:.4f}, {coords[0]:.4f}")
    if el.get("pci") is not None:
        lines.append(f"\n**PCI:** {el['pci']} · EARFCN-DL: {el.get('earfcn_dl')} · "
                     f"sector: {el.get('sector')} · tech: {el.get('tech')}")
    return "\n".join(lines)


@mcp.tool()
def find_cells_in_market(market: str) -> str:
    """
    List all cells in a given market with their parent eNodeB.

    Args:
        market: Market id or name fragment (e.g. 'NYC_Metro', 'New York', 'NYC').
    """
    mid = _resolve_market(market)
    if not mid:
        return f"❌ Unknown market {market!r}."

    cells = list(elements.find({"market": mid, "type": "Cell"}).sort("_id", ASCENDING))
    if not cells:
        return f"No cells in {mid}."

    # Lookup parent eNB for each via topology_edges
    parent_map = {}
    for e in edges.find({"from_id": {"$in": [c["_id"] for c in cells]},
                          "relation": "hosted_on"}):
        parent_map[e["from_id"]] = e["to_id"]

    lines = [f"**{len(cells)} cell(s) in {mid}:**"]
    for c in cells:
        cap = c.get("capacity") or {}
        parent = parent_map.get(c["_id"], "—")
        lines.append(f"- `{c['_id']}` · {c.get('tech')} · sector {c.get('sector')} "
                     f"· {cap.get('downlink_mbps')} Mbps DL · util "
                     f"{int(100*cap.get('current_utilization', 0))}% · hosted on `{parent}`")
    return "\n".join(lines)


@mcp.tool()
def traverse_dependencies(from_id: str, direction: str = "downstream", max_depth: int = 3) -> str:
    """
    Walk the dependency graph from a starting node. Uses MongoDB `$graphLookup`
    over dtw_topology_edges. Use this to answer "what does element X depend on"
    (downstream) or "what depends on element X" (upstream).

    Args:
        from_id:   The starting node id (any element id, also accepts plan_*).
        direction: 'downstream' (default) follows from→to. 'upstream' follows to→from.
        max_depth: Maximum traversal depth (default 3, max 6).
    """
    direction = direction.lower().strip() if direction else "downstream"
    if direction not in ("downstream", "upstream"):
        return f"❌ direction must be 'downstream' or 'upstream', got {direction!r}"
    max_depth = max(1, min(6, int(max_depth or 3)))

    # Verify the start node exists in either elements or edges (it could be
    # a plan_* / qos_* id which is not in dtw_network_elements but appears
    # as a from_id on edges).
    if not elements.find_one({"_id": from_id}) and \
       not edges.find_one({"$or": [{"from_id": from_id}, {"to_id": from_id}]}):
        return f"❌ {from_id!r} not found as an element or as a graph node."

    connect_from = "from_id" if direction == "downstream" else "to_id"
    connect_to   = "to_id"   if direction == "downstream" else "from_id"
    start_field  = connect_to  # we want to traverse outward; seed with `from_id`

    pipeline = [
        # Bootstrap a single doc with the starting node
        {"$match": {start_field: from_id}},
        {"$limit": 1},
        {
            "$graphLookup": {
                "from":             "dtw_topology_edges",
                "startWith":        f"${start_field}",
                "connectFromField": connect_from,
                "connectToField":   connect_to,
                "as":               "walk",
                "maxDepth":         max_depth - 1,
                "depthField":       "depth",
            }
        },
        {"$project": {"_id": 0, "walk": 1}},
    ]

    docs = list(edges.aggregate(pipeline))
    if not docs:
        # Maybe the node only appears on the OTHER side; reverse the seed.
        pipeline[0] = {"$match": {connect_from: from_id}}
        docs = list(edges.aggregate(pipeline))
    if not docs:
        return f"No edges found from {from_id!r}."

    walk = docs[0].get("walk", [])
    # Group by depth for readable output
    by_depth: dict[int, list[dict]] = {}
    for e in walk:
        d = e.get("depth", 0)
        by_depth.setdefault(d, []).append(e)

    lines = [f"## Dependency walk from `{from_id}` ({direction}, depth ≤ {max_depth})",
             f"_{len(walk)} edge(s) discovered across {len(by_depth)} hop(s)._", ""]
    for d in sorted(by_depth.keys()):
        lines.append(f"**Hop {d + 1}:**")
        for e in by_depth[d][:15]:  # cap to 15 per hop in the rendered output
            lines.append(f"  - `{e['from_id']}` --[{e.get('relation', '?')}]→ `{e['to_id']}`")
        if len(by_depth[d]) > 15:
            lines.append(f"  … and {len(by_depth[d]) - 15} more.")
        lines.append("")

    # Summarize impacted element types (downstream only — meaningful)
    if direction == "downstream":
        impacted_ids = {e["to_id"] for e in walk}
        impacted_ids.discard(from_id)
        type_counts: dict = {}
        for el in elements.find({"_id": {"$in": list(impacted_ids)}}, {"type": 1}):
            type_counts[el["type"]] = type_counts.get(el["type"], 0) + 1
        if type_counts:
            lines.append("**Impacted element types:** "
                         + ", ".join(f"{v} {k}" for k, v in sorted(type_counts.items())))
    return "\n".join(lines)


@mcp.tool()
def find_path_between(from_id: str, to_id: str, max_depth: int = 5) -> str:
    """
    Try to find a directed path in the dependency graph from `from_id` to
    `to_id`. Useful to confirm whether one element actually depends on
    another (e.g. does plan_ACME_M reach PGW_NYC_Metro?).

    Args:
        from_id:   Starting node id.
        to_id:     Target node id.
        max_depth: Max traversal depth (default 5, max 8).
    """
    max_depth = max(1, min(8, int(max_depth or 5)))
    pipeline = [
        {"$match": {"from_id": from_id}},
        {
            "$graphLookup": {
                "from":             "dtw_topology_edges",
                "startWith":        "$to_id",
                "connectFromField": "from_id",
                "connectToField":   "to_id",
                "as":               "walk",
                "maxDepth":         max_depth - 1,
                "depthField":       "depth",
            }
        },
    ]
    docs = list(edges.aggregate(pipeline))
    if not docs:
        return f"No outgoing edges from {from_id!r}."

    # Search the walk for an edge whose to_id == target
    seen_paths = []
    for d in docs:
        if d.get("to_id") == to_id:
            seen_paths.append([(d["from_id"], d["to_id"], d.get("relation"), 0)])
        for w in d.get("walk", []):
            if w.get("to_id") == to_id:
                seen_paths.append([(w["from_id"], w["to_id"], w.get("relation"),
                                   w.get("depth", 0))])

    if not seen_paths:
        return f"❌ No directed path from `{from_id}` to `{to_id}` within depth {max_depth}."

    lines = [f"## ✓ Path exists: `{from_id}` → `{to_id}`",
             f"_{len(seen_paths)} edge(s) terminate at the target._", ""]
    for p in seen_paths[:5]:
        for src, dst, rel, depth in p:
            lines.append(f"  `{src}` --[{rel}]→ `{dst}`   (hop {depth + 1})")
    if len(seen_paths) > 5:
        lines.append(f"  … and {len(seen_paths) - 5} more.")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
