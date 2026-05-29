#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DTW (Digital Twin) demo seed loader — ACME Mobile HLR/HSS what-if demo.

Run once to populate MongoDB with the Digital Twin demo fixtures:
    python seed/dtw_seed.py [--reset]

With --reset, all dtw_* collections are dropped before re-seeding.

After running, create the Atlas Search vector index for dtw_knowledge_chunks
in the Atlas UI using the JSON config printed at the end. The simulation
MCP service will fall back to graph-only mode if the index isn't ready,
but the hybrid query that distinguishes Atlas is gated on the index.
"""

import argparse
import datetime
import os
import random

from pymongo import MongoClient, ASCENDING, GEOSPHERE


MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

# Deterministic seed for reproducible demo data
random.seed(1234)

NOW = datetime.datetime(2026, 5, 21, 9, 0, 0)


def days_ago(n: int) -> datetime.datetime:
    return NOW - datetime.timedelta(days=n)


# ─── Markets ──────────────────────────────────────────────────────────────
#
# Six US metros. Each has a geographic center used as the seed point for
# distributing cell sites within a small radius.

MARKETS = [
    {"_id": "NYC_Metro",     "name": "New York City Metro", "center": (-73.9851,  40.7589), "population_m": 8.3},
    {"_id": "LA_Metro",      "name": "Los Angeles Metro",   "center": (-118.2437, 34.0522), "population_m": 13.2},
    {"_id": "Chicago_Metro", "name": "Chicago Metro",       "center": (-87.6298,  41.8781), "population_m": 9.6},
    {"_id": "Dallas_Metro",  "name": "Dallas-Fort Worth",   "center": (-96.7970,  32.7767), "population_m": 7.6},
    {"_id": "Miami_Metro",   "name": "Miami Metro",         "center": (-80.1918,  25.7617), "population_m": 6.3},
    {"_id": "Seattle_Metro", "name": "Seattle Metro",       "center": (-122.3321, 47.6062), "population_m": 4.0},
]
MARKET_IDS = [m["_id"] for m in MARKETS]


# ─── QoS Profiles (15) ────────────────────────────────────────────────────

QOS_PROFILES = [
    {
        "_id": "qos_prepaid_7_2",
        "name": "Prepaid Basic 7.2 Mbps",
        "max_downlink_mbps": 7.2,
        "max_uplink_mbps":   2.0,
        "qci": 9,
        "arp": 7,
        "per_apn_overrides": [
            {"apn": "fast.acme-mobile.net", "max_downlink_mbps": 7.2}
        ],
        "hss_policy_template_ref": "pcrf_template_prepaid_basic",
    },
    {
        "_id": "qos_prepaid_20",
        "name": "Prepaid Plus 20 Mbps",
        "max_downlink_mbps": 20.0,
        "max_uplink_mbps":   5.0,
        "qci": 8,
        "arp": 6,
        "per_apn_overrides": [
            {"apn": "fast.acme-mobile.net", "max_downlink_mbps": 20.0}
        ],
        "hss_policy_template_ref": "pcrf_template_prepaid_plus",
    },
    # Intermediate prepaid tiers (5-30 Mbps integer values) so what-if
    # amendments like "change to 19 Mbps" resolve to an exact profile rather
    # than being silently rounded to the nearest available. Generated from
    # the 20 Mbps template with proportional uplink. Skips 20 (already
    # defined above as `qos_prepaid_20`).
    *[
        {
            "_id": f"qos_prepaid_{mbps}",
            "name": f"Prepaid {mbps} Mbps",
            "max_downlink_mbps": float(mbps),
            "max_uplink_mbps":   round(mbps * 5.0 / 20.0, 1),
            "qci": 8,
            "arp": 6,
            "per_apn_overrides": [
                {"apn": "fast.acme-mobile.net", "max_downlink_mbps": float(mbps)}
            ],
            "hss_policy_template_ref": "pcrf_template_prepaid_plus",
        }
        for mbps in range(5, 31) if mbps != 20  # 5-30 minus the explicit 20
    ],
    {
        "_id": "qos_prepaid_basic",
        "name": "Prepaid Entry 3 Mbps",
        "max_downlink_mbps": 3.0,
        "max_uplink_mbps":   1.0,
        "qci": 9,
        "arp": 8,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_prepaid_basic",
    },
    {
        "_id": "qos_postpaid_standard",
        "name": "Postpaid Standard 50 Mbps",
        "max_downlink_mbps": 50.0,
        "max_uplink_mbps":   10.0,
        "qci": 8,
        "arp": 5,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_postpaid_standard",
    },
    {
        "_id": "qos_postpaid_premium",
        "name": "Postpaid Premium 150 Mbps",
        "max_downlink_mbps": 150.0,
        "max_uplink_mbps":   30.0,
        "qci": 7,
        "arp": 4,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_postpaid_premium",
    },
    {
        "_id": "qos_postpaid_unlimited",
        "name": "Postpaid Unlimited",
        "max_downlink_mbps": 300.0,
        "max_uplink_mbps":   60.0,
        "qci": 7,
        "arp": 3,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_postpaid_unlimited",
    },
    {
        "_id": "qos_5g_standard",
        "name": "5G Standard 200 Mbps",
        "max_downlink_mbps": 200.0,
        "max_uplink_mbps":   40.0,
        "qci": 8,
        "arp": 5,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_5g_standard",
    },
    {
        "_id": "qos_5g_uc",
        "name": "5G Ultra Capacity",
        "max_downlink_mbps": 1000.0,
        "max_uplink_mbps":   100.0,
        "qci": 6,
        "arp": 3,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_5g_uc",
    },
    {
        "_id": "qos_video_priority",
        "name": "Video Priority",
        "max_downlink_mbps": 25.0,
        "max_uplink_mbps":   5.0,
        "qci": 4,
        "arp": 4,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_video",
    },
    {
        "_id": "qos_voice_priority",
        "name": "VoLTE / Voice Priority",
        "max_downlink_mbps": 1.0,
        "max_uplink_mbps":   0.5,
        "qci": 1,
        "arp": 2,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_voice",
    },
    {
        "_id": "qos_business",
        "name": "Business Enterprise",
        "max_downlink_mbps": 500.0,
        "max_uplink_mbps":   100.0,
        "qci": 5,
        "arp": 2,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_business",
    },
    {
        "_id": "qos_iot_basic",
        "name": "IoT Basic Cat-M",
        "max_downlink_mbps": 0.3,
        "max_uplink_mbps":   0.1,
        "qci": 9,
        "arp": 9,
        "per_apn_overrides": [
            {"apn": "iot.acme-mobile.net", "max_downlink_mbps": 0.3}
        ],
        "hss_policy_template_ref": "pcrf_template_iot",
    },
    {
        "_id": "qos_roaming",
        "name": "International Roaming",
        "max_downlink_mbps": 2.0,
        "max_uplink_mbps":   1.0,
        "qci": 9,
        "arp": 7,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_roaming",
    },
    {
        "_id": "qos_connect_5g",
        "name": "Connect 5G",
        "max_downlink_mbps": 35.0,
        "max_uplink_mbps":   8.0,
        "qci": 8,
        "arp": 6,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_connect_5g",
    },
    {
        "_id": "qos_plus5g_pro",
        "name": "Plus5G Pro Premium",
        "max_downlink_mbps": 400.0,
        "max_uplink_mbps":   80.0,
        "qci": 7,
        "arp": 3,
        "per_apn_overrides": [],
        "hss_policy_template_ref": "pcrf_template_plus5g_pro",
    },
]


# ─── Plans (10) ───────────────────────────────────────────────────────────

PLANS = [
    {
        "_id": "plan_ACME_M",
        "name": "ACME M",
        "segment": "prepaid",
        "current_qos_profile_id": "qos_prepaid_7_2",
        "target_qos_profile_id":  "qos_prepaid_20",
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": False, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE"]},
        "monthly_price_usd": 40,
    },
    {
        "_id": "plan_ACME_S",
        "name": "ACME S",
        "segment": "prepaid",
        "current_qos_profile_id": "qos_prepaid_basic",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": False, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE"]},
        "monthly_price_usd": 25,
    },
    {
        "_id": "plan_ACME_L",
        "name": "ACME L",
        "segment": "prepaid",
        "current_qos_profile_id": "qos_postpaid_standard",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": True, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE"]},
        "monthly_price_usd": 60,
    },
    {
        "_id": "plan_ACME_Premium",
        "name": "ACME Premium",
        "segment": "postpaid",
        "current_qos_profile_id": "qos_postpaid_standard",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": True, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE", "VoNR"]},
        "monthly_price_usd": 70,
    },
    {
        "_id": "plan_ACME_PremiumMax",
        "name": "ACME Premium MAX",
        "segment": "postpaid",
        "current_qos_profile_id": "qos_postpaid_premium",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": True, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE", "VoNR"]},
        "monthly_price_usd": 85,
    },
    {
        "_id": "plan_ACME_Essentials",
        "name": "ACME Essentials",
        "segment": "postpaid",
        "current_qos_profile_id": "qos_postpaid_standard",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": False, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE"]},
        "monthly_price_usd": 50,
    },
    {
        "_id": "plan_ACME_Plus5G",
        "name": "ACME Plus5G",
        "segment": "postpaid",
        "current_qos_profile_id": "qos_5g_standard",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": True, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE", "VoNR"]},
        "monthly_price_usd": 75,
    },
    {
        "_id": "plan_ACME_Plus5GPro",
        "name": "ACME Plus5G Pro",
        "segment": "postpaid",
        "current_qos_profile_id": "qos_plus5g_pro",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": True, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE", "VoNR"]},
        "monthly_price_usd": 90,
    },
    {
        "_id": "plan_ACME_Lite",
        "name": "ACME Lite",
        "segment": "prepaid",
        "current_qos_profile_id": "qos_prepaid_basic",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": False, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE"]},
        "monthly_price_usd": 15,
    },
    {
        "_id": "plan_ACME_Lite5G",
        "name": "ACME Lite 5G",
        "segment": "prepaid",
        "current_qos_profile_id": "qos_connect_5g",
        "target_qos_profile_id":  None,
        "hlr_service_flags":  {"data_allowed": True, "roaming_allowed": False, "volte_allowed": True},
        "hss_service_profile": {"apn": "fast.acme-mobile.net", "ims_services": ["VoLTE", "VoNR"]},
        "monthly_price_usd": 30,
    },
]
PLAN_IDS = [p["_id"] for p in PLANS]


# ─── Network elements: core, RAN, cells ───────────────────────────────────
#
# All network elements are stored in one polymorphic collection with `type`
# distinguishing them. This is the document-model selling point — one
# collection, many shapes — that is otherwise spread across separate tables
# in a relational design.

def _offset_coord(center: tuple, dx_deg: float, dy_deg: float) -> list:
    lng, lat = center
    return [lng + dx_deg, lat + dy_deg]


def _build_market_elements(market: dict) -> tuple[list, list]:
    """For one market: build HSS/HLR/MME/SGW/PGW + eNBs + cells, and the
    edges connecting them. Returns (elements, edges)."""
    elements: list = []
    edges: list = []
    mkt = market["_id"]
    center = market["center"]

    # Core elements per market — one of each (real deployments have more,
    # but for the demo this keeps the graph readable).
    hss = {
        "_id": f"ne_HSS_{mkt}",
        "type": "HSS",
        "vendor": random.choice(["Ericsson", "Nokia", "Oracle"]),
        "model": "HSS-9-2",
        "market": mkt,
        "capacity": {"active_sessions": 5_000_000, "current_utilization": round(random.uniform(0.45, 0.70), 2)},
    }
    hlr = {
        "_id": f"ne_HLR_{mkt}",
        "type": "HLR",
        "vendor": "Ericsson",
        "model": "HLR-Legacy-7",
        "market": mkt,
        "capacity": {"active_subscribers": 2_000_000, "current_utilization": round(random.uniform(0.30, 0.55), 2)},
    }
    mme = {
        "_id": f"ne_MME_{mkt}",
        "type": "MME",
        "vendor": "Cisco",
        "model": "MME-4.5",
        "market": mkt,
        "capacity": {"attach_rate_per_sec": 50_000, "current_utilization": round(random.uniform(0.40, 0.65), 2)},
    }
    sgw = {
        "_id": f"ne_SGW_{mkt}",
        "type": "SGW",
        "vendor": "Cisco",
        "model": "ASR-5500",
        "market": mkt,
        "capacity": {"throughput_gbps": 80, "current_utilization": round(random.uniform(0.50, 0.72), 2)},
    }
    pgw = {
        "_id": f"ne_PGW_{mkt}",
        "type": "PGW",
        "vendor": "Cisco",
        "model": "ASR-5500",
        "market": mkt,
        "capacity": {"throughput_gbps": 100, "current_utilization": round(random.uniform(0.55, 0.75), 2)},
    }
    elements.extend([hss, hlr, mme, sgw, pgw])

    # Core-to-core edges
    edges.append({"_id": f"edge_{sgw['_id']}_to_{pgw['_id']}", "from_id": sgw["_id"], "to_id": pgw["_id"], "from_type": "SGW", "to_type": "PGW", "relation": "s5_to"})
    edges.append({"_id": f"edge_{mme['_id']}_to_{hss['_id']}", "from_id": mme["_id"], "to_id": hss["_id"], "from_type": "MME", "to_type": "HSS", "relation": "queries"})

    # 4-5 eNBs per market, each hosting 3 cells.
    n_enbs = random.randint(4, 5)
    for e in range(n_enbs):
        # Distribute eNBs within ~5km of market center
        enb_lng_off = random.uniform(-0.05, 0.05)
        enb_lat_off = random.uniform(-0.05, 0.05)
        enb_id = f"ne_eNB_{mkt}_{e+1:02d}"
        enb = {
            "_id": enb_id,
            "type": "eNodeB",
            "vendor": random.choice(["Ericsson", "Nokia", "Samsung"]),
            "model": random.choice(["AIR-6488", "AirScale-32T", "MMU-5G-32"]),
            "market": mkt,
            "capacity": {
                "downlink_mbps":   random.choice([400, 500, 600, 800]),
                "uplink_mbps":     random.choice([120, 150, 180, 240]),
                "max_active_users": random.choice([600, 800, 1000, 1200]),
                "current_utilization": round(random.uniform(0.40, 0.70), 2),
            },
            "location": {"type": "Point", "coordinates": _offset_coord(center, enb_lng_off, enb_lat_off)},
        }
        elements.append(enb)
        edges.append({"_id": f"edge_{enb_id}_to_{sgw['_id']}", "from_id": enb_id, "to_id": sgw["_id"], "from_type": "eNodeB", "to_type": "SGW", "relation": "serves_via_s1u"})
        edges.append({"_id": f"edge_{enb_id}_to_{mme['_id']}", "from_id": enb_id, "to_id": mme["_id"], "from_type": "eNodeB", "to_type": "MME", "relation": "s1_mme_to"})

        # 3 cells per eNB
        for c, sector in enumerate(["A", "B", "C"]):
            # Small offset around the eNB
            cell_lng_off = enb_lng_off + random.uniform(-0.005, 0.005)
            cell_lat_off = enb_lat_off + random.uniform(-0.005, 0.005)
            cell_id = f"cell_{mkt}_{e+1:02d}_{sector}"
            cell = {
                "_id": cell_id,
                "type": "Cell",
                "market": mkt,
                "sector": sector,
                "pci": random.randint(100, 500),
                "earfcn_dl": random.choice([2100, 1900, 700, 600]),
                "tech": random.choice(["LTE", "LTE", "LTE", "5G-NSA", "5G-SA"]),  # 60% LTE, 40% 5G
                "capacity": {
                    "downlink_mbps":  random.choice([100, 150, 200, 250]),
                    "uplink_mbps":    random.choice([30, 40, 60, 80]),
                    "max_active_users": random.choice([150, 200, 250, 300]),
                    "current_utilization": round(random.uniform(0.55, 0.78), 2),
                },
                "location": {"type": "Point", "coordinates": _offset_coord(center, cell_lng_off, cell_lat_off)},
            }
            elements.append(cell)
            edges.append({"_id": f"edge_{cell_id}_to_{enb_id}", "from_id": cell_id, "to_id": enb_id, "from_type": "Cell", "to_type": "eNodeB", "relation": "hosted_on"})

    return elements, edges


def build_all_network_elements() -> tuple[list, list]:
    all_elements: list = []
    all_edges: list = []
    for m in MARKETS:
        e, ed = _build_market_elements(m)
        all_elements.extend(e)
        all_edges.extend(ed)
    return all_elements, all_edges


# ─── Plan-to-QoS topology edges ───────────────────────────────────────────

def build_plan_qos_edges() -> list:
    out = []
    for p in PLANS:
        qid = p["current_qos_profile_id"]
        out.append({
            "_id": f"edge_{p['_id']}_to_{qid}",
            "from_id":   p["_id"],
            "to_id":     qid,
            "from_type": "plan",
            "to_type":   "qos_profile",
            "relation":  "uses_qos",
        })
    return out


def build_qos_to_cell_edges(network_elements: list) -> list:
    """Edges from each QoS profile to every cell it can apply to.

    Without these edges, $graphLookup starting at a plan dead-ends at the
    QoS profile — the WOW graph walk (plan → qos → cells → eNBs → SGW →
    PGW) needs this 'applies_to' link to descend into the RAN. We connect
    every QoS profile to every Cell because QoS is configured at the
    bearer level and is not market-scoped; the simulation narrows by
    scope at query time.
    """
    cells = [e for e in network_elements if e.get("type") == "Cell"]
    out = []
    for qos in QOS_PROFILES:
        for cell in cells:
            cid = cell["_id"]
            out.append({
                "_id":       f"edge_{qos['_id']}_to_{cid}",
                "from_id":   qos["_id"],
                "to_id":     cid,
                "from_type": "qos_profile",
                "to_type":   "Cell",
                "relation":  "applies_to",
                "market":    cell.get("market"),
            })
    return out


# ─── Subscribers (~1000) ──────────────────────────────────────────────────
#
# Procedurally generated. Distribution roughly mirrors plan popularity for
# ACME Mobile (Premium family ~40%, Connect prepaid ~15%, Plus5G ~15%, M/S/L
# prepaid ~25%, Essentials ~5%). Each subscriber is homed in one market
# and lists 2-4 cells as probable serving cells.

PLAN_DISTRIBUTION = [
    ("plan_ACME_Premium",     0.20),
    ("plan_ACME_PremiumMax",  0.10),
    ("plan_ACME_Plus5G",        0.10),
    ("plan_ACME_Plus5GPro",    0.05),
    ("plan_ACME_M",           0.15),  # the demo target
    ("plan_ACME_S",           0.08),
    ("plan_ACME_L",           0.05),
    ("plan_ACME_Essentials",  0.07),
    ("plan_ACME_Lite",     0.12),
    ("plan_ACME_Lite5G",  0.08),
]


def _pick_plan() -> str:
    r = random.random()
    cum = 0.0
    for pid, weight in PLAN_DISTRIBUTION:
        cum += weight
        if r <= cum:
            return pid
    return PLAN_DISTRIBUTION[-1][0]


def build_subscribers(elements: list, count: int = 1000) -> list:
    cells_by_market: dict[str, list] = {}
    for el in elements:
        if el["type"] == "Cell":
            cells_by_market.setdefault(el["market"], []).append(el["_id"])

    plan_by_id = {p["_id"]: p for p in PLANS}
    subs: list = []
    for i in range(count):
        plan_id = _pick_plan()
        plan    = plan_by_id[plan_id]
        market  = random.choice(MARKET_IDS)
        cells   = cells_by_market.get(market, [])
        sample  = random.sample(cells, k=min(3, len(cells))) if cells else []
        # Probability weights summing to 1.0 (descending)
        probs = [0.5, 0.3, 0.2][:len(sample)]
        if probs:
            total = sum(probs)
            probs = [p / total for p in probs]

        imsi    = f"310999{i+1:09d}"
        msisdn  = f"+1555{i+1:07d}"
        is_5g_capable = "5G" in plan.get("hss_service_profile", {}).get("ims_services", []) or \
                        plan_id in {"plan_ACME_Plus5G", "plan_ACME_Plus5GPro", "plan_ACME_Lite5G",
                                    "plan_ACME_Premium", "plan_ACME_PremiumMax"}
        subs.append({
            "_id":     f"imsi_{imsi}",
            "imsi":    imsi,
            "msisdn":  msisdn,
            "plan_id": plan_id,
            "segment": plan["segment"],
            "home_mcc_mnc":            "310999",
            "home_market":             market,
            "home_location_area_id":   f"LAC_{random.randint(100, 999)}",
            "current_qos_profile_id":  plan["current_qos_profile_id"],
            "sim_type":   "5G" if is_5g_capable else "4G",
            "access_tech": ["LTE", "5G-NSA"] if is_5g_capable else ["LTE"],
            "approx_active_cells": [
                {"cell_id": c, "probability": round(p, 3)}
                for c, p in zip(sample, probs)
            ],
        })
    return subs


# ─── Traffic models (~40) ─────────────────────────────────────────────────
#
# Per market × time window. Captures expected demand on cells from a
# segment/plan/time slice. The simulation service reads these to project
# load when QoS or subscriber distribution changes.

TIME_WINDOWS = [
    {"_id": "Saturday_20_23",  "day_of_week": "Saturday",  "from": "20:00", "to": "23:00", "label": "Sat night peak"},
    {"_id": "Friday_18_22",    "day_of_week": "Friday",    "from": "18:00", "to": "22:00", "label": "Fri commute+evening"},
    {"_id": "Weekday_08_10",   "day_of_week": "Weekday",   "from": "08:00", "to": "10:00", "label": "Morning rush"},
    {"_id": "Weekday_17_19",   "day_of_week": "Weekday",   "from": "17:00", "to": "19:00", "label": "Evening rush"},
    {"_id": "Sunday_14_17",    "day_of_week": "Sunday",    "from": "14:00", "to": "17:00", "label": "Sunday afternoon"},
    {"_id": "Weekday_00_06",   "day_of_week": "Weekday",   "from": "00:00", "to": "06:00", "label": "Overnight idle"},
]


def build_traffic_models(elements: list) -> list:
    cells_by_market: dict[str, list] = {}
    for el in elements:
        if el["type"] == "Cell":
            cells_by_market.setdefault(el["market"], []).append(el)

    out: list = []
    # Focused traffic models: target plan × top 4 markets × all time windows
    focus_plans = ["plan_ACME_M", "plan_ACME_Premium", "plan_ACME_Plus5G"]
    focus_markets = ["NYC_Metro", "LA_Metro", "Chicago_Metro", "Dallas_Metro"]

    for plan_id in focus_plans:
        plan = next(p for p in PLANS if p["_id"] == plan_id)
        for market in focus_markets:
            for tw in TIME_WINDOWS:
                if "00_06" in tw["_id"]:
                    continue  # skip overnight for focus plans (almost no load)
                cells_here = cells_by_market.get(market, [])
                if not cells_here:
                    continue
                cell_load = []
                for cell in cells_here[:8]:  # top 8 cells per market
                    # Estimate subscribers: peak windows pack more.
                    is_peak = tw["_id"] in ("Saturday_20_23", "Friday_18_22", "Weekday_17_19")
                    base = random.randint(80, 180)
                    estimate = int(base * (1.5 if is_peak else 1.0))
                    cell_load.append({
                        "cell_id": cell["_id"],
                        "active_subscribers_estimate": estimate,
                        "avg_per_user_mbps":  round(random.uniform(0.8, 2.4), 2),
                        "peak_per_user_mbps": round(random.uniform(3.0, 6.0), 2),
                        "correlation_to_qos": round(random.uniform(0.55, 0.85), 2),
                    })
                out.append({
                    "_id": f"traffic_{plan_id}_{market}_{tw['_id']}",
                    "plan_id":  plan_id,
                    "segment":  plan["segment"],
                    "market":   market,
                    "time_window": tw,
                    "cells":    cell_load,
                })
    # Add a few general overnight/idle models for the other plans to keep
    # the collection diverse
    for plan_id in ("plan_ACME_Lite", "plan_ACME_Essentials"):
        plan = next(p for p in PLANS if p["_id"] == plan_id)
        for market in ("NYC_Metro", "LA_Metro"):
            cells_here = cells_by_market.get(market, [])[:5]
            tw = TIME_WINDOWS[0]  # Sat night
            cell_load = [{
                "cell_id": c["_id"],
                "active_subscribers_estimate": random.randint(30, 80),
                "avg_per_user_mbps":  round(random.uniform(0.4, 1.0), 2),
                "peak_per_user_mbps": round(random.uniform(1.5, 3.0), 2),
                "correlation_to_qos": round(random.uniform(0.40, 0.65), 2),
            } for c in cells_here]
            out.append({
                "_id": f"traffic_{plan_id}_{market}_{tw['_id']}",
                "plan_id":  plan_id,
                "segment":  plan["segment"],
                "market":   market,
                "time_window": tw,
                "cells":    cell_load,
            })
    return out


# ─── Knowledge chunks (~25) — for hybrid vector search ────────────────────
#
# Past simulation outcomes, mitigation playbooks, vendor notes. Vector
# index on `text`; structured filters on kind, segment, market, ts, lng,
# lat. The simulate_qos_change tool runs a hybrid query against this
# collection to surface analogous past situations.

KNOWLEDGE_CHUNKS = [
    # ── Past QoS uplift incidents (8) ────────────────────────────────────
    {
        "_id": "DTW-KCH-001",
        "kind": "incident",
        "title": "LA Metro prepaid DL uplift drove PGW headroom from 28% to 9%",
        "segment": "prepaid",
        "market":  "LA_Metro",
        "plan_id": "plan_ACME_L",
        "ts":      days_ago(420),
        "lng":     -118.2437,
        "lat":     34.0522,
        "tags":    ["qos_uplift", "pgw_saturation", "weekend_peak"],
        "linked_runbook": "DTW-RB-001",
        "text": (
            "In 2024-Q2 we raised the prepaid L downlink cap from 12 Mbps to 30 Mbps in LA Metro. "
            "Saturday evening peak headroom on PGW_LA_1 dropped from 28% to 9% within three weeks. "
            "The driver was concentrated in 14 cells in Hollywood and Downtown LA where prepaid L "
            "subscribers were heavily represented in the 20:00-23:00 window. Mitigation involved "
            "adding a second 20 MHz carrier in three sectors plus migrating some subscribers to "
            "a lower ARP class so the strict-priority queue did not starve the rest of the traffic."
        ),
    },
    {
        "_id": "DTW-KCH-002",
        "kind": "incident",
        "title": "NYC Manhattan eNB sectors exceeded 92% projected utilization after Premium video uplift",
        "segment": "postpaid",
        "market":  "NYC_Metro",
        "plan_id": "plan_ACME_Premium",
        "ts":      days_ago(310),
        "lng":     -73.9851,
        "lat":     40.7589,
        "tags":    ["qos_uplift", "enb_sector_overload", "video_priority"],
        "linked_runbook": "DTW-RB-002",
        "text": (
            "Postpaid Premium video-priority class was widened in NYC in late 2024. The projection "
            "model flagged three Manhattan sectors crossing 92% DL utilization at Friday evening "
            "peak. The real outcome was within 4 percentage points of the projection. Lesson: when "
            "the segment is dense and video-priority class is involved, cell-level capacity should "
            "be confirmed before rolling out market-wide. Mitigation was to delay rollout in the "
            "three sectors until carrier aggregation was added."
        ),
    },
    {
        "_id": "DTW-KCH-003",
        "kind": "incident",
        "title": "Chicago Loop weekend prepaid uplift had no measurable PGW impact",
        "segment": "prepaid",
        "market":  "Chicago_Metro",
        "plan_id": "plan_ACME_M",
        "ts":      days_ago(180),
        "lng":     -87.6298,
        "lat":     41.8781,
        "tags":    ["qos_uplift", "no_impact", "headroom_ok"],
        "linked_runbook": None,
        "text": (
            "Prepaid M doubled the DL cap in Chicago Loop in 2025. Headroom on PGW_Chicago_1 was "
            "already 41% pre-change. Post-change Saturday peak measured 36% — barely any change. "
            "The takeaway is that prepaid M in Chicago does not dominate peak traffic; postpaid "
            "video flows do. QoS uplift on prepaid in this market is low risk and high satisfaction "
            "win. Recommend rolling out unconditionally."
        ),
    },
    {
        "_id": "DTW-KCH-004",
        "kind": "incident",
        "title": "Dallas Friday peak PGW saturation after Plus5G plus aggressive QoS",
        "segment": "postpaid",
        "market":  "Dallas_Metro",
        "plan_id": "plan_ACME_Plus5GPro",
        "ts":      days_ago(150),
        "lng":     -96.7970,
        "lat":     32.7767,
        "tags":    ["pgw_saturation", "5g_uc", "friday_peak"],
        "linked_runbook": "DTW-RB-003",
        "text": (
            "Plus5G Pro was activated for premium subs in Dallas-Fort Worth in Q1 2025. Friday "
            "18:00-22:00 PGW_Dallas_1 saturated at 94% within ten days. The aggregator pattern "
            "is that 5G Ultra Capacity profile subscribers concentrate in business districts. "
            "Mitigation was adding a redundant PGW and re-homing 30% of the subscriber base. "
            "Without that, sustained peak would have crossed 100% and triggered packet drops."
        ),
    },
    {
        "_id": "DTW-KCH-005",
        "kind": "incident",
        "title": "Miami beach corridor prepaid QoS hike triggered handover failures",
        "segment": "prepaid",
        "market":  "Miami_Metro",
        "plan_id": "plan_ACME_M",
        "ts":      days_ago(95),
        "lng":     -80.1918,
        "lat":     25.7617,
        "tags":    ["qos_uplift", "handover_failure", "coastal"],
        "linked_runbook": "DTW-RB-004",
        "text": (
            "Raising prepaid M to 20 Mbps in the Miami Beach corridor caused unexpected handover "
            "failures on the beachfront cells. The fast-moving population (joggers, beachgoers) "
            "combined with higher per-user throughput pushed cells into rapid quality degradation "
            "that confused the handover algorithm. Mitigation: tighter handover hysteresis "
            "parameters and reduced max DL during weekend daytime windows."
        ),
    },
    {
        "_id": "DTW-KCH-006",
        "kind": "incident",
        "title": "Seattle downtown Postpaid premium uplift smooth with no projection drift",
        "segment": "postpaid",
        "market":  "Seattle_Metro",
        "plan_id": "plan_ACME_PremiumMax",
        "ts":      days_ago(230),
        "lng":     -122.3321,
        "lat":     47.6062,
        "tags":    ["qos_uplift", "no_impact", "smooth_rollout"],
        "linked_runbook": None,
        "text": (
            "Premium MAX postpaid uplift in Seattle downtown went smoothly. Projection model "
            "predicted 7% additional load on PGW_Seattle_1 across the change window; actual was "
            "8%. The lesson here is that Seattle traffic is well distributed across many cells "
            "and the postpaid premium subscriber base is small relative to the prepaid M base. "
            "Confidence is high for similar uplifts in this market."
        ),
    },
    {
        "_id": "DTW-KCH-007",
        "kind": "incident",
        "title": "NYC Brooklyn HSS hit attach-rate ceiling after prepaid uplift",
        "segment": "prepaid",
        "market":  "NYC_Metro",
        "plan_id": "plan_ACME_M",
        "ts":      days_ago(60),
        "lng":     -73.9442,
        "lat":     40.6782,
        "tags":    ["hss_pressure", "attach_rate", "prepaid_uplift"],
        "linked_runbook": "DTW-RB-005",
        "text": (
            "After the QoS uplift on prepaid M in Brooklyn, HSS_NYC was queried 35% more often "
            "due to per-session policy reapplication driven by the new PCRF template. The HSS hit "
            "attach-rate ceiling and started rejecting bearer activations during the Saturday "
            "evening peak. Mitigation: rolled back to the old PCRF template until HSS capacity "
            "was added. Lesson: QoS changes are not just data-plane — control-plane pressure on "
            "HSS/HLR must be modeled."
        ),
    },
    {
        "_id": "DTW-KCH-008",
        "kind": "incident",
        "title": "Dallas suburban prepaid uplift hit Cat-M IoT secondary effect",
        "segment": "prepaid",
        "market":  "Dallas_Metro",
        "plan_id": "plan_ACME_M",
        "ts":      days_ago(40),
        "lng":     -96.7970,
        "lat":     32.7767,
        "tags":    ["qos_uplift", "iot_collision", "secondary_effect"],
        "linked_runbook": "DTW-RB-006",
        "text": (
            "Prepaid M uplift in Dallas suburbs unexpectedly degraded Cat-M IoT performance. "
            "The shared QCI-9 default queue was now competing with much higher prepaid throughput, "
            "starving IoT devices that needed only a few hundred kbps but were sensitive to "
            "scheduling delay. Fix: move IoT to a dedicated bearer with QCI-9 isolation. Worth "
            "checking IoT KPIs alongside any prepaid QoS uplift planning."
        ),
    },
    # ── Roaming/APN change incidents (5) ────────────────────────────────
    {
        "_id": "DTW-KCH-009",
        "kind": "incident",
        "title": "APN migration on prepaid M caused split-routing in two NYC cells",
        "segment": "prepaid",
        "market":  "NYC_Metro",
        "plan_id": "plan_ACME_M",
        "ts":      days_ago(280),
        "lng":     -73.9851,
        "lat":     40.7589,
        "tags":    ["apn_migration", "split_routing", "control_plane"],
        "linked_runbook": "DTW-RB-007",
        "text": (
            "Migrating prepaid M from fast.acme-mobile.net to fast2.acme-mobile.net APN created a "
            "transient split-routing condition in two NYC cells: half the subscribers attached "
            "via the old PGW, half via the new. Worked but added latency. The fix is to drain "
            "the old APN entries from HSS before adding the new ones — strict ordering matters."
        ),
    },
    {
        "_id": "DTW-KCH-010",
        "kind": "incident",
        "title": "Canada roaming enable on prepaid M added 32% HSS query load",
        "segment": "prepaid",
        "market":  "NYC_Metro",
        "plan_id": "plan_ACME_M",
        "ts":      days_ago(120),
        "lng":     -73.9851,
        "lat":     40.7589,
        "tags":    ["roaming_enable", "hss_load", "canada"],
        "linked_runbook": "DTW-RB-008",
        "text": (
            "Enabling Canada roaming on prepaid M added 32% query load on HSS_NYC during the "
            "first weekend post-enable. The cause was clients near the border re-registering "
            "more aggressively. Mitigation: precompute roaming policy snapshots and push to HSS "
            "ahead of the change window. Reduces real-time load."
        ),
    },
    {
        "_id": "DTW-KCH-011",
        "kind": "incident",
        "title": "Roaming policy enable on Connect plan revealed HLR-only legacy subscribers",
        "segment": "prepaid",
        "market":  "Chicago_Metro",
        "plan_id": "plan_ACME_Lite",
        "ts":      days_ago(75),
        "lng":     -87.6298,
        "lat":     41.8781,
        "tags":    ["roaming_enable", "hlr_legacy", "4g_only"],
        "linked_runbook": "DTW-RB-009",
        "text": (
            "Enabling roaming on prepaid Connect surfaced ~8000 legacy subscribers still only "
            "in HLR (not migrated to HSS). They missed the policy update entirely. The lesson "
            "is to scan for HLR-only legacy entries before any policy change and migrate them "
            "first. Otherwise an unknown fraction silently lacks the new policy."
        ),
    },
    {
        "_id": "DTW-KCH-012",
        "kind": "incident",
        "title": "APN swap with concurrent PCRF template change on Premium caused outage",
        "segment": "postpaid",
        "market":  "LA_Metro",
        "plan_id": "plan_ACME_Premium",
        "ts":      days_ago(195),
        "lng":     -118.2437,
        "lat":     34.0522,
        "tags":    ["apn_migration", "pcrf_change", "outage", "concurrent_changes"],
        "linked_runbook": "DTW-RB-010",
        "text": (
            "Combining an APN migration with a PCRF template change in the same change window "
            "on Premium postpaid in LA caused a 23-minute outage for affected subscribers. The "
            "PCRF couldn't find policy for sessions in the transitional APN. Lesson: never bundle "
            "APN migration with PCRF template change. Serialize them, with verification in between."
        ),
    },
    {
        "_id": "DTW-KCH-013",
        "kind": "incident",
        "title": "Mexico roaming enable on Connect 5G saw negligible impact",
        "segment": "prepaid",
        "market":  "Dallas_Metro",
        "plan_id": "plan_ACME_Lite5G",
        "ts":      days_ago(50),
        "lng":     -96.7970,
        "lat":     32.7767,
        "tags":    ["roaming_enable", "no_impact", "mexico"],
        "linked_runbook": None,
        "text": (
            "Enabling Mexico roaming on Connect 5G in the Dallas market had essentially no "
            "measurable impact. The subscriber base is small and concentrated, HSS query load "
            "didn't move meaningfully. Confidence to roll out similar roaming enables on small "
            "5G prepaid plans is high."
        ),
    },
    # ── Mitigation playbooks (5) ─────────────────────────────────────────
    {
        "_id": "DTW-RB-001",
        "kind": "runbook",
        "title": "Add 20 MHz secondary carrier in over-loaded sectors",
        "ts":   days_ago(500),
        "lng":  None,
        "lat":  None,
        "tags": ["mitigation", "capacity_expansion"],
        "text": (
            "Mitigation runbook for cell-sector overload after QoS uplift. Add a 20 MHz secondary "
            "carrier in the affected sectors and enable carrier aggregation. Steps: identify the "
            "three sectors with highest projected utilization, file change request for additional "
            "20 MHz, configure CA in eNodeB, verify with KPI dashboards over 7 days. Expected "
            "headroom recovery: 25-35 percentage points."
        ),
        "actions": [
            {"step": 1, "command": "identify top-3 sectors by projected_utilization desc"},
            {"step": 2, "command": "file CR for 20MHz secondary carrier in those sectors"},
            {"step": 3, "command": "configure carrier aggregation on eNodeB"},
            {"step": 4, "command": "verify uplift over 7-day KPI window"},
        ],
    },
    {
        "_id": "DTW-RB-002",
        "kind": "runbook",
        "title": "Move dominant segment to lower ARP class to free strict-priority queue",
        "ts":   days_ago(420),
        "lng":  None,
        "lat":  None,
        "tags": ["mitigation", "qos_tuning"],
        "text": (
            "When a single segment dominates the strict-priority queue, others starve. Mitigation "
            "is to move the dominant segment to a lower ARP (numerically higher) so it falls back "
            "to a weighted-fair queue. Steps: identify dominant segment from traffic model, "
            "update PCRF template to assign lower ARP, push to HSS, monitor QoE proxies."
        ),
        "actions": [
            {"step": 1, "command": "identify dominant segment from traffic_model.correlation_to_qos"},
            {"step": 2, "command": "update PCRF template: ARP +1 (lower priority)"},
            {"step": 3, "command": "push PCRF template via HSS sync"},
            {"step": 4, "command": "monitor QoE proxies for 24h"},
        ],
    },
    {
        "_id": "DTW-RB-003",
        "kind": "runbook",
        "title": "Add redundant PGW and re-home affected subscriber population",
        "ts":   days_ago(350),
        "lng":  None,
        "lat":  None,
        "tags": ["mitigation", "core_expansion"],
        "text": (
            "When a PGW saturates and there's no headroom path via QoS tuning, add a redundant "
            "PGW in the same market and re-home a portion of the subscriber base. Steps: deploy "
            "redundant PGW, configure S5 from existing SGWs, gradually re-home subscribers (10% "
            "per change window), watch latency and session-setup metrics."
        ),
        "actions": [
            {"step": 1, "command": "deploy redundant PGW in same market"},
            {"step": 2, "command": "configure S5 from existing SGWs to new PGW"},
            {"step": 3, "command": "re-home subscribers 10%/window"},
            {"step": 4, "command": "monitor latency and session-setup KPIs"},
        ],
    },
    {
        "_id": "DTW-RB-004",
        "kind": "runbook",
        "title": "Tighten handover hysteresis on fast-mobility cells",
        "ts":   days_ago(260),
        "lng":  None,
        "lat":  None,
        "tags": ["mitigation", "handover_tuning"],
        "text": (
            "On fast-mobility cells (coastal, transit corridors), handover failures spike when "
            "per-user throughput rises. Mitigation: tighten hysteresis margin and add time-to-trigger "
            "delay so brief signal dips don't cause unnecessary handovers. Steps: identify cells "
            "with >5% handover failures, update handover parameters, validate over 72h."
        ),
        "actions": [
            {"step": 1, "command": "identify cells with handover failure rate >5%"},
            {"step": 2, "command": "update hysteresis +2dB, time-to-trigger +160ms"},
            {"step": 3, "command": "validate over 72h"},
        ],
    },
    {
        "_id": "DTW-RB-005",
        "kind": "runbook",
        "title": "Stage PCRF template rollout to avoid HSS attach-rate ceiling",
        "ts":   days_ago(180),
        "lng":  None,
        "lat":  None,
        "tags": ["mitigation", "control_plane"],
        "text": (
            "When a PCRF template change causes mass policy reapplication, HSS attach-rate "
            "ceilings can be hit. Stage the rollout: instead of a flag-day switch, ramp the new "
            "template at 5% of the population every hour, watching HSS attach-rate. Stop and "
            "roll back if attach-rate exceeds 85% of ceiling."
        ),
        "actions": [
            {"step": 1, "command": "ramp PCRF template change 5%/hour"},
            {"step": 2, "command": "watch HSS attach-rate continuously"},
            {"step": 3, "command": "abort if attach-rate > 0.85 * ceiling"},
        ],
    },
    # ── Vendor + policy notes (4) ────────────────────────────────────────
    {
        "_id": "DTW-POL-001",
        "kind": "policy",
        "title": "Change-window policy for QoS modifications",
        "ts":   days_ago(700),
        "lng":  None,
        "lat":  None,
        "tags": ["policy", "change_management"],
        "text": (
            "QoS changes affecting more than 1% of the subscriber base require change advisory "
            "board approval. Mandatory rollback plan. Implementation must be scheduled outside "
            "Friday and Saturday evening peak windows. Verification window of 7 days after change "
            "with daily KPI review. Anything affecting more than 10% requires regulatory disclosure."
        ),
    },
    {
        "_id": "DTW-POL-002",
        "kind": "policy",
        "title": "Capacity threshold for projected utilization risk classification",
        "ts":   days_ago(650),
        "lng":  None,
        "lat":  None,
        "tags": ["policy", "thresholds"],
        "text": (
            "Risk classification thresholds for projected utilization in what-if simulations. "
            "GREEN: < 70% projected. YELLOW: 70-85% projected, requires mitigation plan filed "
            "before rollout. RED: > 85% projected, requires explicit signoff from network "
            "operations leadership. > 95%: rollout blocked pending capacity expansion."
        ),
    },
    {
        "_id": "DTW-VEN-001",
        "kind": "vendor_note",
        "title": "Ericsson AIR-6488 carrier aggregation behavior with mixed-vendor PCRF",
        "ts":   days_ago(400),
        "lng":  None,
        "lat":  None,
        "tags": ["vendor_note", "ericsson", "carrier_aggregation"],
        "text": (
            "Ericsson AIR-6488 carrier aggregation behaves well with Ericsson HSS but has a known "
            "quirk with Nokia HSS where the secondary cell can be unused for the first 5 minutes "
            "after a PCRF policy push. Workaround is to soft-bounce the eNodeB after the policy "
            "push. Confirmed on releases earlier than 22.Q4."
        ),
    },
    {
        "_id": "DTW-VEN-002",
        "kind": "vendor_note",
        "title": "Cisco ASR-5500 PGW per-bearer counter caveat",
        "ts":   days_ago(350),
        "lng":  None,
        "lat":  None,
        "tags": ["vendor_note", "cisco", "pgw"],
        "text": (
            "Cisco ASR-5500 PGW per-bearer counters undercount by approximately 3-5% under "
            "heavy session-establishment load. For capacity planning purposes, add a 5% safety "
            "margin to the reported utilization before comparing against thresholds. The vendor "
            "has acknowledged this in release notes for 21.27."
        ),
    },
]


# ─── Loader ───────────────────────────────────────────────────────────────

DTW_COLLECTIONS = [
    "dtw_markets",
    "dtw_plans",
    "dtw_qos_profiles",
    "dtw_subscribers",
    "dtw_network_elements",
    "dtw_topology_edges",
    "dtw_traffic_models",
    "dtw_scenarios",
    "dtw_knowledge_chunks",
]


def reset(db):
    print("⚠️  Resetting DTW collections...")
    for c in DTW_COLLECTIONS:
        db[c].drop()
    print("    done.")


def ensure_indexes(db):
    print("🔧 Ensuring indexes...")
    db["dtw_network_elements"].create_index([("market", ASCENDING)], name="ne_by_market")
    db["dtw_network_elements"].create_index([("type", ASCENDING)],   name="ne_by_type")
    db["dtw_network_elements"].create_index([("location", GEOSPHERE)],
                                            name="ne_geo_2dsphere", sparse=True)
    db["dtw_topology_edges"].create_index([("from_id", ASCENDING)],  name="edge_from")
    db["dtw_topology_edges"].create_index([("to_id", ASCENDING)],    name="edge_to")
    db["dtw_topology_edges"].create_index([("relation", ASCENDING)], name="edge_relation")
    db["dtw_subscribers"].create_index([("plan_id", ASCENDING)],     name="sub_by_plan")
    db["dtw_subscribers"].create_index([("home_market", ASCENDING)], name="sub_by_market")
    db["dtw_subscribers"].create_index([("segment", ASCENDING)],     name="sub_by_segment")
    db["dtw_traffic_models"].create_index(
        [("plan_id", ASCENDING), ("market", ASCENDING), ("time_window._id", ASCENDING)],
        name="traffic_lookup",
    )
    db["dtw_scenarios"].create_index([("status", ASCENDING)],        name="scenario_status")


def insert_all(db):
    print("📥 Inserting fixtures...")
    elements, ne_edges = build_all_network_elements()
    plan_edges = build_plan_qos_edges()
    qos_cell_edges = build_qos_to_cell_edges(elements)
    edges = ne_edges + plan_edges + qos_cell_edges
    subscribers = build_subscribers(elements, count=1000)
    traffic_models = build_traffic_models(elements)

    db["dtw_markets"].insert_many(MARKETS)
    db["dtw_plans"].insert_many(PLANS)
    db["dtw_qos_profiles"].insert_many(QOS_PROFILES)
    db["dtw_network_elements"].insert_many(elements)
    db["dtw_topology_edges"].insert_many(edges)
    db["dtw_subscribers"].insert_many(subscribers)
    db["dtw_traffic_models"].insert_many(traffic_models)
    db["dtw_knowledge_chunks"].insert_many(KNOWLEDGE_CHUNKS)
    # dtw_scenarios is intentionally empty after seed — scenarios are
    # created at runtime by dtw_scenario_service.create_scenario.

    type_counts: dict = {}
    for el in elements:
        type_counts[el["type"]] = type_counts.get(el["type"], 0) + 1

    print(f"    {len(MARKETS)} markets")
    print(f"    {len(PLANS)} plans")
    print(f"    {len(QOS_PROFILES)} QoS profiles")
    print(f"    {len(elements)} network elements"
          f" ({', '.join(f'{v} {k}' for k, v in sorted(type_counts.items()))})")
    print(f"    {len(edges)} topology edges")
    print(f"    {len(subscribers)} subscribers")
    print(f"    {len(traffic_models)} traffic models")
    print(f"    {len(KNOWLEDGE_CHUNKS)} knowledge chunks")
    print(f"    dtw_scenarios: empty (populated at runtime)")


VECTOR_INDEX_CONFIG = {
    "name": "dtw_knowledge_index",
    "type": "vectorSearch",
    "definition": {
        "fields": [
            {
                "type":         "autoEmbed",
                "modality":     "text",
                "path":         "text",
                "model":        "voyage-4",
                "quantization": "float",   # full precision; default 'scalar' (int8) compresses scores
            },
            {"type": "filter", "path": "kind"},
            {"type": "filter", "path": "segment"},
            {"type": "filter", "path": "market"},
            {"type": "filter", "path": "plan_id"},
            {"type": "filter", "path": "ts"},
            {"type": "filter", "path": "lng"},
            {"type": "filter", "path": "lat"},
        ],
    },
}


def create_vector_index(db):
    """Try to create the vector index programmatically. No-op if it exists."""
    from pymongo.operations import SearchIndexModel
    coll = db["dtw_knowledge_chunks"]
    existing = [i for i in coll.list_search_indexes() if i.get("name") == "dtw_knowledge_index"]
    if existing:
        print(f"⚡ Vector index already exists (status={existing[0].get('status')})")
        return
    try:
        coll.create_search_index(SearchIndexModel(
            definition=VECTOR_INDEX_CONFIG["definition"],
            name="dtw_knowledge_index",
            type="vectorSearch",
        ))
        print("⚡ Vector index 'dtw_knowledge_index' submitted to Atlas.")
        print("   It will become queryable in ~30-90 seconds. Check Atlas UI for status.")
    except Exception as e:
        print(f"⚠️  Could not create vector index automatically: {e}")
        print_index_instructions()


def print_index_instructions():
    import json as _json
    print()
    print("━" * 72)
    print("⚡  Atlas Vector Search index — create manually in the Atlas UI")
    print("━" * 72)
    print()
    print("Database:    agent_registry")
    print("Collection:  dtw_knowledge_chunks")
    print("Index name:  dtw_knowledge_index")
    print()
    print("In Atlas → Search → Create Search Index → Atlas Vector Search →")
    print("JSON editor → paste:")
    print()
    print(_json.dumps(VECTOR_INDEX_CONFIG["definition"], indent=2))
    print()
    print("Atlas auto-embed: `type: autoEmbed` + `model: voyage-4`. Atlas embeds")
    print("on insert and on query (raw text in $vectorSearch.query). Until this")
    print("index is Active, simulate_qos_change runs in graph-only mode and the")
    print("'similar past scenarios' panel will be empty.")
    print("━" * 72)


def main():
    parser = argparse.ArgumentParser(description="DTW demo seed loader")
    parser.add_argument("--reset", action="store_true", help="drop dtw_* collections first")
    args = parser.parse_args()

    print(f"🍃 Connecting to {DB_NAME}...")
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]

    if args.reset:
        reset(db)

    ensure_indexes(db)
    insert_all(db)
    create_vector_index(db)

    print("✅ Seed complete.")
    client.close()


if __name__ == "__main__":
    main()
