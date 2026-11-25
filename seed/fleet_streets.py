#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Compute the street geometry and the fleet's plan for the robotaxi scenarios
(web/fleet_network.py) once, into web/data/fleet_streets.json:

  - 56 robotaxi loops through Midtown, weighted towards the busy core
  - 24 local loops around the four cheaper cells deferred cars go to
  - the three candidate routes of the 17:05 ride
  - 300 vehicles (loop, offset, speed) and, per car, its trip into a cheaper
    cell: deferred at the 13:01 quote if its cell is congested, else the
    contingency trip the 15:30 re-quote uses

Routes come from the public OSRM demo server (OpenStreetMap data); only
coordinates are sent. Responses are cached (--cache), so a rerun with the
same inputs needs no network. Deterministic: seeded random source.

    python seed/fleet_streets.py [--cache path/to/osrm_cache.json]
"""

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import fleet_network as fn  # noqa: E402
from web import ran_network as rn  # noqa: E402

cache: dict = {}
calls = 0
seed = 11


def rnd():
    global seed
    seed = (seed * 16807) % 2147483647
    return seed / 2147483647


def osrm(points):
    global calls
    key = ";".join(f"{lng:.5f},{lat:.5f}" for lat, lng in points)
    if key not in cache:
        url = f"https://router.project-osrm.org/route/v1/driving/{key}?overview=full&geometries=geojson&steps=false"
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    d = json.load(r)
                break
            except Exception:
                time.sleep(2 + 2 * attempt)
        else:
            raise RuntimeError(f"OSRM failed for {key}")
        assert d["code"] == "Ok", d
        rt = d["routes"][0]
        cache[key] = {"coords": [[round(x, 5), round(y, 5)] for x, y in rt["geometry"]["coordinates"]],
                      "legs": [round(leg["distance"]) for leg in rt["legs"]],
                      "wps": [[round(w["location"][0], 5), round(w["location"][1], 5)] for w in d["waypoints"]],
                      "dur": round(rt["duration"])}
        calls += 1
        time.sleep(1.05)   # the demo server asks for at most one request per second
    return cache[key]


SITES = [{"id": s[0], "name": s[1], "lat": s[2], "lng": s[3]} for s in rn.SITES]
BY_ID = {s["id"]: s for s in SITES}


def near(site, r_max=330, r_min=40):
    a, r = rnd() * 2 * math.pi, r_min + rnd() * (r_max - r_min)
    return (site["lat"] + math.cos(a) * r / fn.M_LAT, site["lng"] + math.sin(a) * r / fn.M_LNG)


def trip_into_cheaper_cell(lp, v, sp, t0, pick, per_target):
    """Leave the loop at the first waypoint after t0, ride to a point in a cheaper cell."""
    L = lp["cum"][-1]
    bounds = [0]
    for leg in lp["legs"][:-1]:
        bounds.append(bounds[-1] + leg)
    d0 = (v["off"] + sp * t0) % L
    j = min(range(len(bounds)), key=lambda b: (bounds[b] - d0) % L)
    t_dep = t0 + ((bounds[j] - d0) % L) / sp
    wx, wy = lp["wps"][j]
    tg = sorted(fn.TARGETS, key=lambda tt: (BY_ID[tt]["lat"] - wy) ** 2 + ((BY_ID[tt]["lng"] - wx) * 0.757) ** 2)[pick]
    k = per_target[tg] % 6
    per_target[tg] += 1
    ty, tx = TARGET_PTS[tg][k]
    tr = osrm([(wy, wx), (ty, tx)])
    return {"tDep": round(t_dep, 2), "target": tg, "tp": k, "tr": tr["coords"],
            "tArr": round(t_dep + fn.cum(tr["coords"])[-1] / sp, 2)}


def main():
    global TARGET_PTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path(__file__).parent / ".osrm_cache.json"))
    args = ap.parse_args()
    cpath = Path(args.cache)
    if cpath.exists():
        cache.update(json.loads(cpath.read_text()))

    loops = []
    for _ in range(56):
        pts = []
        for _k in range(4):
            pool = fn.CORE if rnd() < 0.62 else [s["id"] for s in SITES]
            pts.append(near(BY_ID[pool[int(rnd() * len(pool))]]))
        pts.append(pts[0])
        loops.append(dict(osrm(pts)))
    TARGET_PTS = {t: [near(BY_ID[t], 220, 30) for _ in range(6)] for t in fn.TARGETS}
    locals_ = {t: [] for t in fn.TARGETS}
    for t in fn.TARGETS:
        for p in TARGET_PTS[t]:
            a, b = near(BY_ID[t], 380, 120), near(BY_ID[t], 380, 120)
            locals_[t].append(osrm([p, a, b, p]))
    rides = {k: {"label": v["label"], **osrm([fn.PICK, *v["via"], fn.DROP])} for k, v in fn.RIDES.items()}

    for lp in loops:
        lp["cum"] = fn.cum(lp["coords"])
    vehicles, nd, per_target = [], 0, {t: 0 for t in fn.TARGETS}
    qt = fn.SIM["quote_t"]
    for i in range(fn.SIM["vehicles"]):
        li = i % len(loops)
        lp = loops[li]
        L = lp["cum"][-1]
        v = {"id": f"FA-{i + 1:04d}", "loop": li, "off": round(rnd() * L, 1), "vf": round(0.8 + 0.4 * rnd(), 3)}
        sp = fn.SIM["speed_m_per_min"] * v["vf"]
        x, y = fn.along(lp["coords"], lp["cum"], (v["off"] + sp * qt) % L)
        if fn.load_at(fn.serving(x, y), qt) >= 75:      # congested at the quote: defer into a cheaper cell
            t0 = 3 + int(rnd() * 95)
            v.update({"defer": True, **trip_into_cheaper_cell(lp, v, sp, t0, nd % 2, per_target)})
            nd += 1
        else:                                           # contingency for the 15:30 re-quote
            v["re"] = trip_into_cheaper_cell(lp, v, sp, fn.SIM["requote_t"] + (i * 7) % 20, i % 2, per_target)
        vehicles.append(v)
    for lp in loops:
        lp.pop("cum")
    cpath.write_text(json.dumps(cache))
    out = {"source": "OpenStreetMap via OSRM (router.project-osrm.org), computed by seed/fleet_streets.py",
           "loops": loops, "locals": locals_, "rides": rides, "target_pts": TARGET_PTS, "vehicles": vehicles}
    fn.STREETS_PATH.parent.mkdir(exist_ok=True)
    fn.STREETS_PATH.write_text(json.dumps(out, separators=(",", ":")))
    print(f"{fn.STREETS_PATH}: {fn.STREETS_PATH.stat().st_size // 1024} KB, {len(vehicles)} vehicles, "
          f"{nd} deferred at the quote, {calls} new OSRM requests")


if __name__ == "__main__":
    main()
