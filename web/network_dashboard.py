#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The network map: one Midtown radio network on the agents' cluster, three
scenarios, each a demo on its own (first version):

  GET / , /night                Green Autonomous Networks, "night shift"
                                (web/network.html, web/ran_network.py)
  GET /ota                      robotaxi fleet: network-aware OTA downloads
  GET /rides                    robotaxi fleet: connectivity-aware rides
                                (both web/fleet.html, web/fleet_network.py)

  GET /api/network/model        night shift: sites, cells, small cells,
                                policy, promises, intent, alert (from Atlas)
  GET /api/network/can-sleep    its hero query, run on Atlas at a sim minute
  GET /api/fleet/model          fleet: cars, campaign, offers, backhaul,
                                tiers, ride request, forecasts (from Atlas)
                                plus the page's simulation parameters
  GET /api/fleet/quote          "Quote one vehicle", run on Atlas
  GET /api/fleet/routes         "Score candidate routes", run on Atlas

Seeded by seed/ran_seed.py. Each scenario is simulated in its page from
these documents; the agents acting on them come in a later version.
?session=<token> is accepted for the shell's link and kept for that version
(the reference data is shared).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from bson import ObjectId  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pymongo import AsyncMongoClient  # noqa: E402

from web import fleet_network, ran_network  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("network_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME = "agent_registry"
HTML_PATH = Path(__file__).parent / "network.html"
FLEET_HTML_PATH = Path(__file__).parent / "fleet.html"


def _json(v):
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json(x) for x in v]
    if isinstance(v, datetime.datetime):
        return v.isoformat() + (ran_network.TZ if v.tzinfo is None else "")
    if isinstance(v, ObjectId):
        return str(v)
    return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGO_URI)
    app.state.db = client[DB_NAME]
    log.info("Network dashboard ready")
    yield
    await client.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)   # the fleet model carries the street geometry


@app.get("/", response_class=HTMLResponse)
@app.get("/night", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


@app.get("/ota", response_class=HTMLResponse)
@app.get("/rides", response_class=HTMLResponse)
async def fleet_page():
    return FLEET_HTML_PATH.read_text()


@app.get("/api/network/model")
async def network_model(session: str = ""):
    """The map's source: the documents in Atlas, plus the simulation's
    parameters (load curves, power model, disruption timing)."""
    db = app.state.db
    q = {"market": ran_network.MARKET, "cluster": ran_network.CLUSTER}
    sites = await db["ran_sites"].find(q).sort("_id", 1).to_list(None)
    if not sites:
        return JSONResponse({"error": "the network is not seeded: run seed/ran_seed.py"}, status_code=503)
    order = {s[0]: i for i, s in enumerate(ran_network.SITES)}
    sites.sort(key=lambda s: order.get(s["_id"], 999))
    cells = await db["ran_cells"].find(q).to_list(None)
    docs = {
        "ran_sites": sites, "ran_cells": cells,
        "service_commitments": await db["service_commitments"].find().to_list(None),
        "energy_policy": await db["energy_policy"].find({"cluster": ran_network.CLUSTER}).to_list(None),
        "energy_intents": await db["energy_intents"].find().to_list(None),
        "transit_alerts": await db["transit_alerts"].find().to_list(None),
    }
    return JSONResponse(_json({
        "docs": docs,
        "sim": {"date": ran_network.DATE.isoformat(), "steps": ran_network.STEPS, "area": ran_network.AREA,
                "power": ran_network.POWER, "disruption": ran_network.DISRUPTION, "grid": ran_network.GRID},
    }))


@app.get("/api/network/can-sleep")
async def can_sleep(minute: int = 300, session: str = ""):
    """Run the hero query on Atlas for the sim minute (0 = 18:00)."""
    minute = max(15, min(ran_network.STEPS - 1, minute))
    at = ran_network.iso(minute)
    pipeline = ran_network.can_sleep_pipeline(at)
    t0 = datetime.datetime.now()
    cursor = await app.state.db["ran_cells"].aggregate(pipeline)
    rows = await cursor.to_list(None)
    ms = (datetime.datetime.now() - t0).total_seconds() * 1000
    return JSONResponse(_json({"at": at, "ms": round(ms), "rows": rows, "pipeline": pipeline}))


@app.get("/api/fleet/model")
async def fleet_model(session: str = ""):
    """The fleet scenarios' source: the documents in Atlas, plus the simulation's
    parameters (afternoon load curves, constants, street geometry, the fleet's plan)."""
    db = app.state.db
    q = {"market": ran_network.MARKET, "cluster": ran_network.CLUSTER}
    sites = await db["ran_sites"].find(q).to_list(None)
    offers = await db["ota_offers"].find({"campaign_id": fleet_network.CAMPAIGN_ID}).to_list(None)
    if not sites or not offers:
        return JSONResponse({"error": "the fleet scenarios are not seeded: run seed/ran_seed.py"}, status_code=503)
    order = {s[0]: i for i, s in enumerate(ran_network.SITES)}
    sites.sort(key=lambda s: order.get(s["_id"], 999))
    docs = {
        "ran_sites": sites,
        "ran_cells": await db["ran_cells"].find({**q, "layer": "capacity", "kind": "macro"}).to_list(None),
        "fleet_vehicles": await db["fleet_vehicles"].find({"fleet_id": "fleet-a"}).sort("_id", 1).to_list(None),
        "ota_campaigns": await db["ota_campaigns"].find({"_id": fleet_network.CAMPAIGN_ID}).to_list(None),
        "ota_offers": offers,
        "transport_links": await db["transport_links"].find().to_list(None),
        "price_tiers": await db["price_tiers"].find().to_list(None),
        "ride_requests": await db["ride_requests"].find({"_id": fleet_network.RIDE_ID}).to_list(None),
        "ran_load_forecast": await db["ran_load_forecast"].find({"window": "13:00-18:00"}).to_list(None),
    }
    return JSONResponse(_json({"docs": docs, "sim": fleet_network.sim_params()}))


async def _run(coll: str, pipeline: list) -> dict:
    t0 = datetime.datetime.now()
    cursor = await app.state.db[coll].aggregate(pipeline)
    rows = await cursor.to_list(None)
    return {"ms": round((datetime.datetime.now() - t0).total_seconds() * 1000), "rows": rows, "pipeline": pipeline}


@app.get("/api/fleet/quote")
async def fleet_quote(vehicle: str, lng: float, lat: float, minute: int = 1, session: str = ""):
    """Run "Quote one vehicle" on Atlas for a car at its position in the page, at a sim minute (0 = 13:00)."""
    if not (-74.05 < lng < -73.93 and 40.70 < lat < 40.80):
        raise HTTPException(400, "position outside Midtown")
    minute = max(1, min(fleet_network.STEPS - 1, minute))
    at = fleet_network.iso(minute)
    cell = fleet_network.serving(lng, lat)["id"]
    out = await _run("ran_cells", fleet_network.quote_pipeline(lng, lat, cell, at))
    return JSONResponse(_json({"at": at, "vehicle": vehicle, "serving_cell": cell, **out}))


@app.get("/api/fleet/routes")
async def fleet_routes(session: str = ""):
    """Run "Score candidate routes" on Atlas for the 17:05 ride request."""
    out = await _run("ride_requests", fleet_network.route_quality_pipeline())
    return JSONResponse(_json({"request_id": fleet_network.RIDE_ID, **out}))
