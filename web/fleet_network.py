#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The robotaxi fleet scenarios of the network map (Network intelligence for
robotaxi fleets), first version: the same Midtown network as the night shift
(web/ran_network.py), on the Tuesday afternoon before it.

  OTA downloads (/ota)        an anonymized fleet asks before a 2.4 GB update
                              for 300 cars; the network answers per vehicle
                              (download now, paced, or not now with a menu of
                              cheaper cells nearby: ota_offers), the fleet picks
                              the cell its car's next ride ends in
                              (ota_fleet_choices: the network never sees the
                              rides), a NOC review, revised offers at 15:30
                              delivered by change stream, done before 18:00
  Connected rides (/rides)    a passenger needs a video call; three candidate
                              routes are scored on the forecast for the minutes
                              the car is in each cell

Defined once here; seed/ran_seed.py seeds the documents, the page simulates
the afternoon from them. Street geometry (robotaxi loops, deferral trips,
ride candidates) comes from OpenStreetMap via OSRM, computed once by
seed/fleet_streets.py into web/data/fleet_streets.json. Every value is
synthetic; prices are tiers.
"""

import datetime
import json
import math
from functools import lru_cache
from pathlib import Path

from web import ran_network as rn

DATE = rn.DATE
START = datetime.datetime(2026, 11, 24, 13, 0, tzinfo=rn.NY)   # 13:00 in New York, stored as 18:00Z
STEPS = 301                                      # sim minutes, 13:00 → 18:00
TZ = rn.TZ
MARKET, CLUSTER = rn.MARKET, rn.CLUSTER
STREETS_PATH = Path(__file__).parent / "data" / "fleet_streets.json"

# Weekday afternoon, capacity-layer PRB % at 13, 14, 15, 16, 17, 18 h, per site.
LOAD = {
    "NYC-MTN-0412": [78, 80, 82, 86, 94, 90], "NYC-MTN-0388": [80, 82, 80, 82, 88, 80],
    "NYC-MTN-0151": [84, 86, 86, 88, 92, 92], "NYC-MTN-0217": [70, 76, 79, 80, 84, 72],
    "NYC-MTN-0109": [74, 77, 80, 84, 93, 88], "NYC-MTN-0533": [62, 64, 63, 64, 70, 58],
    "NYC-MTN-0420": [68, 70, 68, 70, 74, 62], "NYC-MTN-0301": [72, 74, 76, 80, 90, 84],
    "NYC-MTN-0355": [76, 76, 76, 78, 80, 70], "NYC-MTN-0247": [52, 54, 55, 56, 60, 62],
    "NYC-MTN-0612": [38, 40, 42, 44, 52, 58], "NYC-MTN-0702": [36, 38, 40, 42, 50, 58],
    "NYC-MTN-0174": [40, 42, 44, 46, 55, 62], "NYC-MTN-0198": [66, 68, 68, 70, 72, 60],
    "NYC-MTN-0266": [58, 60, 60, 62, 66, 60], "NYC-MTN-0731": [42, 44, 46, 48, 56, 60],
    "NYC-MTN-0163": [68, 74, 78, 80, 84, 74],
}
# Backhaul: site → aggregation router → provider edge. AGG-MTN-W2's uplink is full.
AGG = {
    "AGG-MTN-W1": {"pos": [-73.9968, 40.7518], "util": 58, "pe": "PE-NYC-1",
                   "sites": ["NYC-MTN-0412", "NYC-MTN-0533", "NYC-MTN-0612", "NYC-MTN-0420"]},
    "AGG-MTN-W2": {"pos": [-73.9925, 40.7612], "util": 91, "pe": "PE-NYC-1",
                   "sites": ["NYC-MTN-0702", "NYC-MTN-0301", "NYC-MTN-0151"]},
    "AGG-MTN-N1": {"pos": [-73.9805, 40.7645], "util": 52, "pe": "PE-NYC-1", "sites": ["NYC-MTN-0731", "NYC-MTN-0163"]},
    "AGG-MTN-C1": {"pos": [-73.9868, 40.7468], "util": 66, "pe": "PE-NYC-2",
                   "sites": ["NYC-MTN-0388", "NYC-MTN-0355", "NYC-MTN-0217", "NYC-MTN-0247", "NYC-MTN-0266"]},
    "AGG-MTN-E1": {"pos": [-73.9738, 40.7522], "util": 63, "pe": "PE-NYC-2",
                   "sites": ["NYC-MTN-0109", "NYC-MTN-0174", "NYC-MTN-0198"]},
}
PE = {"PE-NYC-1": {"pos": [-73.9890, 40.7560], "util": 64}, "PE-NYC-2": {"pos": [-73.9800, 40.7500], "util": 58}}
AGG_OF = {s: a for a, g in AGG.items() for s in g["sites"]}
TARGETS = ["NYC-MTN-0612", "NYC-MTN-0174", "NYC-MTN-0731", "NYC-MTN-0247"]   # cheaper cells deferred cars go to
CORE = ["NYC-MTN-0151", "NYC-MTN-0412", "NYC-MTN-0109", "NYC-MTN-0388", "NYC-MTN-0217", "NYC-MTN-0163",
        "NYC-MTN-0301", "NYC-MTN-0355"]
TIERS = [{"name": "discounted", "if_load_pct_lt": 55}, {"name": "standard", "if_load_pct_lt": 75},
         {"name": "congested", "note": "no campaign traffic"}]
SIM = {"cap_mbps": 900, "retail_cap_pct": 80, "headroom_mbps": 5, "max_rate_mbps": 40, "size_gb": 2.4,
       "speed_m_per_min": 150, "vehicles": 300, "quote_t": 1, "review_t": 2, "approve_t": 4, "go_t": 5,
       "requote_t": 150, "ota_end_t": 240, "ride_book_t": 243, "ride_pick_t": 244, "ride_go_t": 245,
       "approval_threshold": 100}
CAMPAIGN_ID = "OTA-FA-2026-48"
RIDE_ID = "RR-FA-20261124-1703"
PROFILE = {"name": "video_call", "min_dl_mbps": 8, "duration_min": 20}
RIDES = {   # candidate routes for the 17:05 ride, Chelsea → near the UN; traffic factor on OSRM's free-flow time
    "A": {"label": "via 34th St", "factor": 1.25, "via": [(40.7509, -73.9906), (40.7469, -73.9803)]},
    "B": {"label": "via 23rd St and 1st Ave", "factor": 1.85, "via": [(40.7428, -73.9927), (40.7366, -73.9782)]},
    "C": {"label": "via 42nd St", "factor": 1.8, "via": [(40.7576, -73.9899), (40.7517, -73.9757)]},
}
PICK, DROP = (40.7472, -74.0025), (40.7538, -73.9672)
M_LAT, M_LNG = 111320, 111320 * math.cos(math.radians(40.752))


def iso(minute: float) -> datetime.datetime:
    return START + datetime.timedelta(minutes=round(minute))


def afternoon_factor(night_load_factor: float) -> float:
    """A sector's afternoon load factor, derived from its night-shift one (0.8–1.25 → 0.92–1.08)."""
    return round(0.92 + (night_load_factor - 0.8) / 0.45 * 0.16, 3)


@lru_cache(maxsize=1)
def sectors() -> tuple:
    out = []
    for s in rn.model()["sites"]:
        for c in s["sectors"]:
            out.append({"id": c["cap"], "site": s["id"], "name": s["name"], "sec": c["sector"], "az": c["azimuth_deg"],
                        "lat": s["lat"], "lng": s["lng"], "f": afternoon_factor(c["load_factor"])})
    return tuple(out)


def load_at(sc: dict, t: float) -> float:
    arr, h = LOAD[sc["site"]], min(4.999, max(0.0, t) / 60)
    i = int(h)
    return min(99.0, max(5.0, (arr[i] + (arr[i + 1] - arr[i]) * (h - i)) * sc["f"]))


def tier_of(load: float) -> str:
    return "discounted" if load < 55 else "standard" if load < 75 else "congested"


def dl_of(load: float) -> float:
    """What one more device can expect in a cell at this load, Mbit/s."""
    return max(0.5, 120 * (1 - load / 100) ** 2)


def serving(lng: float, lat: float) -> dict:
    """The serving sector: the nearest site, the sector whose azimuth points at the car."""
    sites = rn.SITES
    best = min(sites, key=lambda s: (s[2] - lat) ** 2 + ((s[3] - lng) * 0.757) ** 2)
    br = (math.degrees(math.atan2((lng - best[3]) * 0.757, lat - best[2])) + 360) % 360
    return min((c for c in sectors() if c["site"] == best[0]), key=lambda c: abs(((c["az"] - br + 540) % 360) - 180))


def cum(coords: list) -> list:
    out, d = [0.0], 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        d += math.hypot((x2 - x1) * M_LNG, (y2 - y1) * M_LAT)
        out.append(d)
    return out


def along(coords: list, cm: list, d: float) -> tuple:
    d = max(0.0, min(cm[-1], d))
    lo, hi = 0, len(cm) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if cm[mid] <= d else (lo, mid)
    k = (d - cm[lo]) / ((cm[hi] - cm[lo]) or 1)
    return coords[lo][0] + (coords[hi][0] - coords[lo][0]) * k, coords[lo][1] + (coords[hi][1] - coords[lo][1]) * k


def miles(a, b) -> float:
    return math.hypot((a[0] - b[0]) * M_LNG, (a[1] - b[1]) * M_LAT) / 1609.34


@lru_cache(maxsize=1)
def streets() -> dict:
    return json.loads(STREETS_PATH.read_text())


def ride_minutes(k: str) -> int:
    return round(streets()["rides"][k]["dur"] / 60 * RIDES[k]["factor"])


def ride_legs(k: str) -> list[dict]:
    """The cells a candidate route crosses, with the time the car enters each one; a cell gets a second
    leg when the car is still in it as the forecast's quarter hour changes (sampled every 40 m; the page
    samples the same way). The route query finds each leg's forecast slot from its time."""
    r = streets()["rides"][k]
    coords, cm, mins = r["coords"], cum(r["coords"]), ride_minutes(k)
    legs, last, d = [], None, 0.0
    while d <= cm[-1]:
        x, y = along(coords, cm, d)
        tt = SIM["ride_go_t"] + d / cm[-1] * mins
        key = (serving(x, y)["id"], int(tt // 15))
        if key != last:
            legs.append({"cell_id": key[0], "enter": iso(tt)})
            last = key
        d += 40
    return legs


# ─── Documents ───────────────────────────────────────────────────────────────

def _pt(lng, lat):
    return {"type": "Point", "coordinates": [round(lng, 5), round(lat, 5)]}


def vehicle_at_quote(v: dict) -> tuple:
    lp = streets()["loops"][v["loop"]]
    cm = cum(lp["coords"])
    return along(lp["coords"], cm, (v["off"] + SIM["speed_m_per_min"] * v["vf"] * SIM["quote_t"]) % cm[-1])


def counter_load(sc: dict, t: float) -> float:
    """The load the latest 15-minute PM counter at or before minute t reports (what the quote query reads)."""
    return load_at(sc, (int(t) // 15) * 15 + 7.5)


def offer_menu(lng: float, lat: float, serving_cell: str, t: float, radius_mi: float = 2.0) -> tuple[list, list]:
    """The network's menu for a car, as the quote query builds it: the best sector of each site within
    2 miles, offered unless congested or behind a full backhaul; the car's own site is not on it."""
    site = {s[0]: s for s in rn.SITES}
    serving_site = serving_cell.rsplit("-", 2)[0]
    best = {}
    for c in sectors():
        d = miles((lng, lat), (c["lng"], c["lat"]))
        if d > radius_mi or c["site"] == serving_site:
            continue
        load = counter_load(c, t)
        if c["site"] not in best or dl_of(load) > best[c["site"]][1]:
            best[c["site"]] = (c, dl_of(load), load, d)
    offered, excluded = [], []
    # like the query: the car's cell, then the six best non-congested sites (a full backhaul shows, with its reason)
    for sid, (c, dl, load, d) in sorted(((k, b) for k, b in best.items() if tier_of(b[2]) != "congested"),
                                        key=lambda kv: -kv[1][1])[:6]:
        if AGG[AGG_OF[sid]]["util"] >= 80:
            excluded.append({"site_id": sid, "area_name": site[sid][1], "reason": "backhaul full"})
        else:
            offered.append({"site_id": sid, "area_name": site[sid][1], "cell_id": c["id"], "tier": tier_of(load),
                            "expected_dl_mbps": round(dl, 1), "distance_mi": round(d, 1)})
    return offered, excluded


def documents() -> dict[str, list[dict]]:
    st, qt = streets(), SIM["quote_t"]
    quote_at = iso(qt)
    vehicles, offers, choices = [], [], []
    for v in st["vehicles"]:
        x, y = vehicle_at_quote(v)
        c = serving(x, y)
        load = load_at(c, qt)
        vehicles.append({"_id": v["id"], "fleet_id": "fleet-a", "kind": "robotaxi", "position": _pt(x, y),
                         "reported_at": quote_at, "state": "on_ride", "modem": {"serving_cell": c["id"], "rat": "NR"},
                         "software": {"current": "2026.44.2", "target": "2026.48.1"},
                         "telemetry_reserve_mbps": SIM["headroom_mbps"]})
        # the network's answer: what it offers this car, nothing about where the car will go
        offer = {"_id": f"OF-48-{v['id']}", "campaign_id": CAMPAIGN_ID, "vehicle_id": v["id"], "revision": 1,
                 "quoted_at": quote_at, "serving_cell": c["id"], "tier": tier_of(load),
                 "expected_dl_mbps": round(dl_of(load), 1),
                 "decision": "not_now" if v.get("defer") else "download_now",
                 "valid_until": iso(300), "status": "active", "exposure": "tier and expected throughput only"}
        if v.get("defer"):
            offer["offered_cells"], offer["not_offered"] = offer_menu(x, y, c["id"], qt)
            # the fleet's choice: it knows the car's next ride, the network never sees it
            pick = next((m for m in offer["offered_cells"] if m["site_id"] == v["target"]), None)
            assert pick, f"{v['id']}: the fleet's cell {v['target']} is not on the network's menu"
            choices.append({"_id": f"CH-48-{v['id']}", "campaign_id": CAMPAIGN_ID, "vehicle_id": v["id"],
                            "offer_id": offer["_id"], "offer_revision": 1, "chosen_site": v["target"],
                            "area_name": pick["area_name"], "why": "the car's next ride ends there",
                            "eta": iso(v["tArr"]), "chosen_at": quote_at, "source": "fleet dispatch"})
        else:
            offer["pacing"] = {"max_mbps": SIM["max_rate_mbps"], "pause_in": "congested"}
        offers.append(offer)
    campaign = [{"_id": CAMPAIGN_ID, "fleet_id": "fleet-a", "fleet_name": "Fleet A (anonymized robotaxi operator)",
                 "package": {"version": "2026.48.1", "size_gb": SIM["size_gb"], "safety_critical": False, "delta": True},
                 "vehicles": len(vehicles), "requested_at": iso(0), "deadline": iso(300),
                 "constraints": {"telemetry_reserve_mbps": SIM["headroom_mbps"], "retail_prb_cap_pct": SIM["retail_cap_pct"]},
                 "approval": {"required": True, "threshold_vehicles": SIM["approval_threshold"], "decided": "approved",
                              "by": "NOC on-call", "at": iso(SIM["approve_t"])},
                 "status": "quoted", "source": "fleet console (scripted request)"}]
    transport = []
    for a, g in AGG.items():
        transport.append({"_id": a, "kind": "node", "role": "aggregation router", "location": _pt(*g["pos"]),
                          "uplink_util_pct": g["util"]})
        transport.append({"_id": f"{a}>{g['pe']}", "kind": "link", "from": a, "to": g["pe"], "medium": "fiber",
                          "capacity_gbps": 40, "util_pct": g["util"]})
        for s in g["sites"]:
            transport.append({"_id": f"{s}>{a}", "kind": "link", "from": s, "to": a, "medium": "fiber",
                              "capacity_gbps": 10, "util_pct": 30 + (int(s[-3:]) % 17)})
    for p, g in PE.items():
        transport.append({"_id": p, "kind": "node", "role": "provider edge", "location": _pt(*g["pos"]), "uplink_util_pct": g["util"]})
    tiers = [{"_id": "BDT-TIERS-2026", "product": "Network-aware OTA delivery", "tiers": TIERS,
              "exposure": {"show": ["tier", "expected_dl_mbps"], "hide": ["raw load", "user counts"]},
              "prices": "placeholders, by tier"}]
    ride = [{"_id": RIDE_ID, "fleet_id": "fleet-a", "requested_at": iso(SIM["ride_book_t"]),
             "pickup": _pt(PICK[1], PICK[0]), "dropoff": _pt(DROP[1], DROP[0]),
             "window": {"start": iso(SIM["ride_go_t"]), "end": iso(SIM["ride_go_t"] + 30)},
             "app_profile": PROFILE,
             "candidates": [{"route_id": k, "label": RIDES[k]["label"], "duration_min": ride_minutes(k),
                             "distance_m": sum(st["rides"][k]["legs"]), "legs": ride_legs(k)} for k in RIDES],
             "privacy": "candidate routes and the application's need; no passenger identity"}]
    forecast, pm = [], []
    for c in sectors():
        forecast.append({"_id": f"{c['id']}:{DATE.isoformat()}T13", "cell_id": c["id"], "date": DATE.isoformat(),
                         "window": "13:00-18:00", "starts_at": iso(0),
                         "quarter_hour_load_pct": [round(load_at(c, q * 15 + 7.5), 1) for q in range(20)],
                         "unit": "PRB %, capacity layer", "model": "seasonal profile + planned events",
                         "generated_at": datetime.datetime(2026, 11, 24, 12, 0, tzinfo=rn.NY)})
        for q in range(0, 300, 15):
            load = load_at(c, q + 7.5)
            pm.append({"ts": iso(q), "meta": {"cell_id": c["id"], "site_id": c["site"], "granularity": "PT15M"},
                       "RRU.PrbTotDl": round(load, 1), "RRC.ConnMean": round(load * 1.3),
                       "DRB.UEThpDl": round(load * 3150), "PEE.AvgPower": round(420 + 6 * load),
                       "PEE.Energy": round((420 + 6 * load) / 4000, 3)})
    return {"fleet_vehicles": vehicles, "ota_campaigns": campaign, "ota_offers": offers, "ota_fleet_choices": choices,
            "transport_links": transport,
            "price_tiers": tiers, "ride_requests": ride, "ran_load_forecast": forecast, "ran_pm_counters": pm}


def sim_params() -> dict:
    """What the page needs besides the documents: load curves, constants, street geometry, the fleet's plan."""
    st = streets()
    return {"date": DATE.isoformat(), "steps": STEPS, "load": LOAD, "agg": AGG, "pe": PE, "targets": TARGETS,
            "params": SIM, "rides": {k: {**RIDES[k], "minutes": ride_minutes(k), "coords": st["rides"][k]["coords"]} for k in RIDES},
            "loops": [{"coords": lp["coords"]} for lp in st["loops"]],
            "locals": {t: [{"coords": lp["coords"]} for lp in v] for t, v in st["locals"].items()},
            "vehicles": st["vehicles"]}


# ─── Hero queries ────────────────────────────────────────────────────────────

def _dl_expr(load):
    return {"$max": [0.5, {"$multiply": [120, {"$pow": [{"$subtract": [1, {"$divide": [load, 100]}]}, 2]}]}]}


def quote_pipeline(lng: float, lat: float, serving_cell: str, at: datetime.datetime) -> list[dict]:
    """'Quote one vehicle': the car's cell now and better cells within 2 miles: geospatial ($geoNear),
    live load (p95 of 3GPP counters, time series), backhaul ($graphLookup), the tier rule."""
    return [
        {"$geoNear": {"near": {"type": "Point", "coordinates": [lng, lat]}, "distanceField": "distance_m",
                      "maxDistance": 3219, "spherical": True,
                      "query": {"layer": "capacity", "kind": "macro", "cluster": CLUSTER}}},
        {"$lookup": {
            "from": "ran_pm_counters", "let": {"cell": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$meta.cell_id", "$$cell"]},
                            "ts": {"$gte": at - datetime.timedelta(minutes=15), "$lte": at}}},
                {"$group": {"_id": None, "load_p95": {"$percentile": {"input": {"$getField": "RRU.PrbTotDl"},
                                                                      "p": [0.95], "method": "approximate"}}}}],
            "as": "now"}},
        {"$graphLookup": {"from": "transport_links", "startWith": "$site_id", "connectFromField": "to",
                          "connectToField": "from", "maxDepth": 2, "restrictSearchWithMatch": {"kind": "link"},
                          "as": "backhaul"}},
        {"$set": {"load": {"$first": {"$first": "$now.load_p95"}},
                  "backhaul_max_util": {"$max": "$backhaul.util_pct"},
                  "serving": {"$eq": ["$_id", serving_cell]}}},
        {"$set": {"tier": {"$switch": {"branches": [{"case": {"$lt": ["$load", TIERS[0]["if_load_pct_lt"]]}, "then": "discounted"},
                                                    {"case": {"$lt": ["$load", TIERS[1]["if_load_pct_lt"]]}, "then": "standard"}],
                                       "default": "congested"}},
                  "expected_dl_mbps": {"$round": [_dl_expr("$load"), 1]}}},
        {"$set": {"reason": {"$switch": {"branches": [
            {"case": "$serving", "then": "the car's cell now"},
            {"case": {"$gte": ["$backhaul_max_util", 80]}, "then": "not offered: backhaul full"},
            {"case": {"$eq": ["$tier", "congested"]}, "then": "not offered: congested"}], "default": "offered"}}}},
        {"$sort": {"serving": -1, "expected_dl_mbps": -1}},
        {"$group": {"_id": "$site_id", "best": {"$first": "$$ROOT"}}},   # one sector per site: the best one
        {"$replaceRoot": {"newRoot": "$best"}},
        # the car's cell, then cheaper cells (a full backhaul shows, with its reason), congested cells last
        {"$set": {"rank": {"$switch": {"branches": [{"case": "$serving", "then": 0},
                                                    {"case": {"$ne": ["$tier", "congested"]}, "then": 1}], "default": 2}}}},
        {"$sort": {"rank": 1, "expected_dl_mbps": -1}},
        {"$limit": 7},
        {"$project": {"_id": 1, "site_id": 1, "tier": 1, "expected_dl_mbps": 1, "reason": 1,   # never the raw load
                      "distance_mi": {"$round": [{"$divide": ["$distance_m", 1609.34]}, 1]}}},
    ]


def route_quality_pipeline(request_id: str = RIDE_ID) -> list[dict]:
    """'Score candidate routes': the lowest predicted downlink per route, over the minutes the car is in each cell.
    Each leg reads the forecast slot its entry time falls in; a leg no forecast covers counts as a dip."""
    return [
        {"$match": {"_id": request_id}},
        {"$unwind": "$candidates"},
        {"$unwind": "$candidates.legs"},
        {"$lookup": {
            "from": "ran_load_forecast", "let": {"cell": "$candidates.legs.cell_id", "at": "$candidates.legs.enter"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$cell_id", "$$cell"]}, {"$lte": ["$starts_at", "$$at"]},
                    {"$lt": ["$$at", {"$dateAdd": {"startDate": "$starts_at", "unit": "minute",
                                                   "amount": {"$multiply": [15, {"$size": "$quarter_hour_load_pct"}]}}}]}]}}},
                {"$project": {"_id": 0, "load": {"$arrayElemAt": ["$quarter_hour_load_pct", {"$toInt": {"$floor": {
                    "$divide": [{"$dateDiff": {"startDate": "$starts_at", "endDate": "$$at", "unit": "minute"}}, 15]}}}]}}}],
            "as": "fc"}},
        {"$set": {"load": {"$first": "$fc.load"}}},
        {"$set": {"dl": {"$cond": [{"$eq": [{"$ifNull": ["$load", None]}, None]}, 0, _dl_expr("$load")]}}},
        {"$group": {"_id": "$candidates.route_id", "label": {"$first": "$candidates.label"},
                    "minutes": {"$first": "$candidates.duration_min"}, "lowest_dl_mbps": {"$min": "$dl"},
                    "need": {"$first": "$app_profile.min_dl_mbps"}, "cells": {"$addToSet": "$candidates.legs.cell_id"},
                    "dips": {"$addToSet": {"$cond": [{"$lt": ["$dl", "$app_profile.min_dl_mbps"]},
                                                     "$candidates.legs.cell_id", None]}}}},
        {"$set": {"dips": {"$setDifference": ["$dips", [None]]}, "cells": {"$size": "$cells"},
                  "call_ok": {"$gte": ["$lowest_dl_mbps", "$need"]},
                  "lowest_dl_mbps": {"$round": ["$lowest_dl_mbps", 1]}}},
        {"$sort": {"call_ok": -1, "minutes": 1}},
    ]
