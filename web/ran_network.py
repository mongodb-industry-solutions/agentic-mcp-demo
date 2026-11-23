#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The Midtown Manhattan radio network of the "night shift" scenario (Green
Autonomous Networks, first version).

One definition, two users: seed/ran_seed.py writes it to MongoDB as the
documents a carrier's systems would hold (inventory, 3GPP PM counters,
forecasts, service promises, energy policy and intent, grid and transit
signals), and the DTW dashboard's /network page draws its map from those
documents and simulates the night in the browser.

Every value is synthetic. Sites sit near real landmarks but are not
Verizon's network. Field names follow 3GPP (NRM energy-saving attributes,
TS 28.552 PM counters), TM Forum (TMF639, TMF921) and GTFS-realtime.
"""

import datetime
import math

DATE = datetime.date(2026, 11, 24)             # a Tuesday night
START = datetime.datetime(2026, 11, 24, 18, 0)  # 18:00 local (UTC-5)
STEPS = 540                                    # sim minutes, 18:00 → 03:00
TZ = "-05:00"
MARKET, CLUSTER = "NYC", "Midtown"

# capacity-layer demand (PRB %) by hour from 18:00 to 03:00, per area type
AREA = {
    "office":      [55, 40, 22, 14, 10, 7, 5, 4, 4, 4],
    "transit":     [72, 58, 40, 32, 28, 22, 14, 8, 6, 6],
    "residential": [50, 60, 65, 62, 50, 35, 22, 12, 8, 7],
    "nightlife":   [65, 68, 70, 72, 68, 60, 48, 34, 25, 22],
}
SITES = [
    ("NYC-MTN-0412", "Penn Station / MSG", 40.7506, -73.9935, "transit"),
    ("NYC-MTN-0388", "Herald Square", 40.7497, -73.9877, "office"),
    ("NYC-MTN-0151", "Times Square", 40.7580, -73.9855, "nightlife"),
    ("NYC-MTN-0217", "Bryant Park", 40.7536, -73.9832, "office"),
    ("NYC-MTN-0109", "Grand Central", 40.7527, -73.9772, "transit"),
    ("NYC-MTN-0533", "Hudson Yards", 40.7538, -74.0010, "office"),
    ("NYC-MTN-0420", "Garment District", 40.7547, -73.9916, "office"),
    ("NYC-MTN-0301", "Port Authority", 40.7570, -73.9903, "transit"),
    ("NYC-MTN-0355", "Empire State", 40.7484, -73.9857, "office"),
    ("NYC-MTN-0247", "NoMad", 40.7447, -73.9880, "residential"),
    ("NYC-MTN-0612", "Chelsea", 40.7465, -74.0010, "residential"),
    ("NYC-MTN-0702", "Hell's Kitchen", 40.7638, -73.9918, "residential"),
    ("NYC-MTN-0174", "Murray Hill", 40.7479, -73.9757, "residential"),
    ("NYC-MTN-0198", "Midtown East", 40.7560, -73.9710, "office"),
    ("NYC-MTN-0266", "Flatiron", 40.7411, -73.9897, "office"),
    ("NYC-MTN-0731", "Columbus Circle", 40.7681, -73.9819, "residential"),
    ("NYC-MTN-0163", "Rockefeller Center", 40.7587, -73.9787, "office"),
]
SMALLS = [   # lamp-post small cells where crowds gather; amp = extra load in the disruption
    ("SC-TSQ-01", "Times Square", 40.7577, -73.9857, "nightlife", 0),
    ("SC-TSQ-02", "Times Square", 40.7591, -73.9845, "nightlife", 0),
    ("SC-TSQ-03", "Times Square", 40.7566, -73.9871, "nightlife", 0),
    ("SC-PEN-01", "Penn Station", 40.7499, -73.9924, "transit", 60),
    ("SC-PEN-02", "Penn Station", 40.7514, -73.9947, "transit", 55),
    ("SC-PEN-03", "Penn Station", 40.7494, -73.9951, "transit", 45),
    ("SC-HSQ-01", "Herald Square", 40.7504, -73.9881, "office", 25),
    ("SC-HSQ-02", "Herald Square", 40.7489, -73.9893, "office", 20),
]
SECTORS = ["A", "B", "C"]
POWER = {   # watts, illustrative but in the right range
    "site_overhead": 1400,                          # baseband, transport, cooling
    "cov": {"idle": 180, "perPct": 1.2},            # 700 MHz LTE radio, 4T4R
    "cap": {"idle": 420, "perPct": 6.0, "sleep": 70},  # C-band 5G, 64T64R
    "small": {"idle": 60, "perPct": 1.2, "sleep": 8},  # C-band 5G small cell, 4T4R
}
POLICY = {"sleep_fcst_lt": 15, "cov_after_offload_lt": 55, "wake_cov_gt": 65, "wake_intervals": 2,
          "min_awake_min": 30, "max_asleep_pct": 90, "offload_factor": 2.5}
# The train at 01:10: crowds build for 30 min, hold until 02:00, have gone home by about 02:25,
# so the agent's 02:30 check resumes the saving
DISRUPTION = {"start": 430, "peakAt": 460, "holdTo": 480, "endAt": 505}
SURGE = {"NYC-MTN-0412": [70, 85, 55], "NYC-MTN-0388": [30, 25, 35], "NYC-MTN-0420": [20, 28, 15]}
GRID = {"price": [62, 58, 49, 43, 39, 35, 31, 29, 28, 28], "carbon": [310, 300, 290, 282, 276, 270, 262, 258, 255, 255]}


class _Rnd:
    """The page's seeded random source (Park-Miller), so both draw the same night."""
    def __init__(self, seed):
        self.s = seed

    def __call__(self):
        self.s = (self.s * 16807) % 2147483647
        return self.s / 2147483647


def _rot(i):   # avenues run about 29° east of north: one sector up or down the avenue
    return ((29 if i % 2 else 209) + ((i * 7) % 11) - 5 + 360) % 360


def _bearing(a, b):
    return (math.degrees(math.atan2((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])) + 360) % 360


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * math.cos(math.radians(a[0]))) ** 2


def iso(minute: int) -> datetime.datetime:
    return START + datetime.timedelta(minutes=minute)


def model() -> dict:
    """Sites, sectors (with their load factors) and small cells (with their host sector)."""
    rnd = _Rnd(7)
    sites = []
    for i, (sid, name, lat, lng, area) in enumerate(SITES):
        rot = _rot(i)
        sectors = [{"sector": sec, "azimuth_deg": (rot + k * 120) % 360, "load_factor": round(0.8 + rnd() * 0.45, 4),
                    "cov": f"{sid}-B13-{sec}", "cap": f"{sid}-N77-{sec}"} for k, sec in enumerate(SECTORS)]
        sites.append({"id": sid, "name": name, "lat": lat, "lng": lng, "area": area, "sectors": sectors})
    smalls = []
    for i, (sid, area_name, lat, lng, area, amp) in enumerate(SMALLS):
        site = min(sites, key=lambda s: _dist2((s["lat"], s["lng"]), (lat, lng)))
        br = _bearing((site["lat"], site["lng"]), (lat, lng))
        host = min(site["sectors"], key=lambda c: abs(((c["azimuth_deg"] - br + 540) % 360) - 180))
        smalls.append({"id": f"NYC-MTN-{sid}", "area_name": area_name, "lat": lat, "lng": lng, "area": area,
                       "amp": amp, "f": round(0.7 + (i % 3) * 0.12, 4), "host": host["cap"],
                       "host_cov": host["cov"], "host_site": site["id"]})
    return {"sites": sites, "smalls": smalls}


def _curve(area: str, minute: float) -> float:
    arr = AREA[area]
    h = minute / 60
    i = min(8, int(h))
    return arr[i] + (arr[i + 1] - arr[i]) * (h - i)


def _surge(amp: float, minute: int) -> float:
    d = DISRUPTION
    if not amp or minute < d["start"] or minute > d["endAt"]:
        return 0.0
    if minute < d["peakAt"]:
        return amp * (minute - d["start"]) / (d["peakAt"] - d["start"])
    if minute < d["holdTo"]:
        return amp
    return amp * (1 - (minute - d["holdTo"]) / (d["endAt"] - d["holdTo"]))


# ─── The documents ───────────────────────────────────────────────────────────

def documents() -> dict[str, list[dict]]:
    m = model()
    ts = datetime.datetime(2026, 11, 24, 17, 0)
    sites, cells, forecast, pm = [], [], [], []
    for s in m["sites"]:
        sites.append({
            "_id": s["id"], "name": s["name"], "kind": "macro_rooftop", "area_type": s["area"],
            "market": MARKET, "cluster": CLUSTER, "location": {"type": "Point", "coordinates": [s["lng"], s["lat"]]},
            "sectors": [{"sector": c["sector"], "azimuth_deg": c["azimuth_deg"], "antenna_height_m": 38, "mech_tilt_deg": 4}
                        for c in s["sectors"]],
            "power_system": {"grid_meter": f"CONED-{s['id'][-4:]}-M1", "rectifier_kw_max": 12, "battery_kwh": 38,
                             "site_overhead_w": POWER["site_overhead"]},
            "source": "network inventory (simulated)", "updated_at": ts})
        for c in s["sectors"]:
            common = {"site_id": s["id"], "sector": c["sector"], "azimuth_deg": c["azimuth_deg"], "market": MARKET,
                      "cluster": CLUSTER, "location": {"type": "Point", "coordinates": [s["lng"], s["lat"]]},
                      "load_profile": {"area_type": s["area"], "load_factor": c["load_factor"]}}
            cells.append({"_id": c["cap"], **common, "kind": "macro", "layer": "capacity", "rat": "NR",
                          "band": "n77 (C-band 3.7 GHz)", "bandwidth_mhz": 100,
                          "radio": {"vendor": "Ericsson", "model": "AIR 6449", "tx_chains": 64},
                          "energy_profile": {"full_load_w": 1020, "idle_w": 420, "deep_sleep_w": 70, "wake_time_s": 60},
                          "isESCoveredBy": [c["cov"]], "surge_pct": SURGE.get(s["id"], [0, 0, 0])[SECTORS.index(c["sector"])]})
            cells.append({"_id": c["cov"], **common, "kind": "macro", "layer": "coverage", "rat": "LTE",
                          "band": "B13 (700 MHz)", "bandwidth_mhz": 10,
                          "radio": {"vendor": "Ericsson", "model": "Radio 4449", "tx_chains": 4},
                          "energy_profile": {"full_load_w": 300, "idle_w": 180},
                          "energy_saving": "not allowed (coverage layer)"})
    for sc in m["smalls"]:
        cells.append({"_id": f"{sc['id']}-N77", "kind": "small_cell", "mount": "lamp post, 7 m", "area": sc["area_name"],
                      "layer": "capacity", "rat": "NR", "band": "n77 (C-band 3.7 GHz)", "bandwidth_mhz": 40,
                      "market": MARKET, "cluster": CLUSTER, "site_id": sc["host_site"],
                      "radio": {"vendor": "Ericsson", "model": "Micro 4T4R", "tx_chains": 4},
                      "location": {"type": "Point", "coordinates": [sc["lng"], sc["lat"]]},
                      "load_profile": {"area_type": sc["area"], "load_factor": sc["f"]}, "surge_pct": sc["amp"],
                      "energy_profile": {"full_load_w": 180, "idle_w": 60, "deep_sleep_w": 8, "wake_time_s": 20},
                      "isESCoveredBy": [sc["host_cov"], sc["host"]]})
    for c in cells:
        if c["layer"] != "capacity":
            continue
        lp = c["load_profile"]
        hourly = [round(_curve(lp["area_type"], h * 60) * lp["load_factor"], 1) for h in range(10)]
        forecast.append({"_id": f"{c['_id']}:{DATE.isoformat()}", "cell_id": c["_id"], "date": DATE.isoformat(),
                         "starts_at": iso(0), "hourly_load_pct": hourly, "unit": "PRB %, capacity layer",
                         "model": "seasonal profile by area type (planned events included, incidents not)",
                         "generated_at": datetime.datetime(2026, 11, 24, 12, 0)})
        # 15-minute PM counters (3GPP TS 28.552): what the carrier carries when awake
        for q in range(0, STEPS, 15):
            load = min(100.0, _curve(lp["area_type"], q + 7.5) * lp["load_factor"] + _surge(c.get("surge_pct", 0), q + 7))
            pm.append({"ts": iso(q), "meta": {"cell_id": c["_id"], "site_id": c["site_id"], "granularity": "PT15M"},
                       "RRU.PrbTotDl": round(load, 1), "RRC.ConnMean": round(load * 1.3),
                       "DRB.UEThpDl": round(load * 3150), "PEE.AvgPower": round(420 + 6 * load),
                       "PEE.Energy": round((420 + 6 * load) / 4000, 3)})
    grid = [{"ts": iso(h * 60), "meta": {"zone": "NYISO-J (New York City)"}, "lbmp_usd_per_mwh": GRID["price"][h],
             "carbon_g_per_kwh": GRID["carbon"][h], "values": "illustrative"} for h in range(10)]
    promises = [
        {"_id": "SC-PUBLIC-SAFETY", "kind": "rule", "text": "Coverage layer never sleeps; emergency calls always have a cell.",
         "applies_to": "all cells, layer: coverage", "cell_ids": []},
        {"_id": "SC-PLAN-UNLIMITED", "kind": "qos_profile", "plan": "Consumer unlimited", "guaranteed_dl_mbps": 25,
         "measure": "per active user, p95", "cell_ids": []},
        {"_id": "SC-SLICE-ENT-0042", "kind": "enterprise_slice", "customer": "Midtown hospital group (fictional)",
         "rule": "never sleep", "cell_ids": ["NYC-MTN-0109-N77-A", "NYC-MTN-0174-N77-B"]},
    ]
    policy = [{"_id": "EP-NYC-Midtown", "level": "L4", "cluster": CLUSTER,
               "sleep_if": {"forecast_load_next_hour_lt_pct": POLICY["sleep_fcst_lt"],
                            "coverage_after_offload_lt_pct": POLICY["cov_after_offload_lt"]},
               "wake_if": {"coverage_load_gt_pct": POLICY["wake_cov_gt"], "for_minutes": POLICY["wake_intervals"]},
               "min_awake_min": POLICY["min_awake_min"], "max_capacity_asleep_pct": POLICY["max_asleep_pct"],
               "offload_factor": POLICY["offload_factor"], "never_sleep": ["layer: coverage", "SC-SLICE-ENT-0042"]}]
    intent = [{"_id": "EI-NYC-001", "expectation": "Reduce Midtown RAN energy by 15% between 20:00 and 06:00",
               "target": {"metric": "energy_reduction_pct", "value": 15, "window": "20:00-06:00", "scope": "NYC-Midtown"},
               "constraints": [p["_id"] for p in promises], "priority": "below every service promise",
               "autonomy_level": "L4", "status": "active"}]
    alert = [{"_id": "MTA-ALERT-DEMO-0001", "source": "MTA GTFS-realtime", "effect": "NO_SERVICE",
              "header_text": "A C E trains suspended between 34 St–Penn Station and 59 St–Columbus Circle",
              "informed_entity": [{"route_id": "A"}, {"route_id": "C"}, {"route_id": "E"}, {"stop_id": "A28"}],
              "active_period": {"start": iso(DISRUPTION["start"]), "end": iso(DISRUPTION["endAt"])},
              "location": {"type": "Point", "coordinates": [-73.9935, 40.7506]}, "fictional": True}]
    return {"ran_sites": sites, "ran_cells": cells, "ran_load_forecast": forecast, "ran_pm_counters": pm,
            "grid_signals": grid, "service_commitments": promises, "energy_policy": policy,
            "energy_intents": intent, "transit_alerts": alert}


TIMESERIES = {"ran_pm_counters": "minutes", "grid_signals": "hours"}


# ─── The hero query ──────────────────────────────────────────────────────────

def can_sleep_pipeline(at: datetime.datetime) -> list[dict]:
    """'Can this carrier sleep?': inventory, live PM counters (p95 of the
    last 15 minutes), the forecast, the coverage relation and the service
    promises, joined in one aggregation over ran_cells."""
    hour = max(0, min(9, int((at - START).total_seconds() // 3600)))
    return [
        {"$match": {"market": MARKET, "cluster": CLUSTER, "layer": "capacity"}},
        {"$lookup": {
            "from": "ran_pm_counters", "let": {"cell": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$meta.cell_id", "$$cell"]},
                            "ts": {"$gte": at - datetime.timedelta(minutes=15), "$lte": at}}},
                # 3GPP counter names contain a dot, so they are read with $getField
                {"$group": {"_id": None,
                            "load_p95": {"$percentile": {"input": {"$getField": "RRU.PrbTotDl"}, "p": [0.95],
                                                         "method": "approximate"}},
                            "users": {"$avg": {"$getField": "RRC.ConnMean"}}}}],
            "as": "now"}},
        {"$lookup": {
            "from": "ran_load_forecast", "localField": "_id", "foreignField": "cell_id",
            "pipeline": [{"$project": {"next_hour_max": {"$max": {"$slice": ["$hourly_load_pct", hour, 2]}}}}],
            "as": "forecast"}},
        {"$graphLookup": {
            "from": "ran_cells", "startWith": "$isESCoveredBy", "connectFromField": "isESCoveredBy",
            "connectToField": "_id", "maxDepth": 0, "as": "takes_over"}},
        {"$lookup": {"from": "service_commitments", "localField": "_id", "foreignField": "cell_ids", "as": "promises"}},
        {"$set": {
            "load_now": {"$first": {"$first": "$now.load_p95"}},
            "load_next_hour": {"$first": "$forecast.next_hour_max"},
            "coverage_after_offload": {"$add": [10, {"$multiply": [2.65, {"$first": "$forecast.next_hour_max"}]}]}}},
        {"$project": {
            "_id": 1, "site_id": 1, "sector": 1, "kind": 1,
            "load_now": {"$round": ["$load_now", 1]}, "load_next_hour": 1,
            "coverage_after_offload": {"$round": ["$coverage_after_offload", 1]},
            "takes_over": "$takes_over._id", "blocking_promises": "$promises._id",
            "safe_to_sleep": {"$and": [   # no measurement or no forecast means: not safe
                {"$lt": [{"$ifNull": ["$load_now", 999]}, POLICY["sleep_fcst_lt"]]},
                {"$lt": [{"$ifNull": ["$load_next_hour", 999]}, POLICY["sleep_fcst_lt"]]},
                {"$lt": [{"$ifNull": ["$coverage_after_offload", 999]}, POLICY["cov_after_offload_lt"]]},
                {"$eq": [{"$size": "$promises"}, 0]}]}}},
        {"$sort": {"safe_to_sleep": -1, "load_now": 1}},
    ]
