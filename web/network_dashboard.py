#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The network dashboard: Green Autonomous Networks, "night shift" scenario,
first version (a map of the Midtown radio network on the agents' cluster).

  GET /                         the page (web/network.html)
  GET /api/network/model        sites, cells, small cells, policy, promises,
                                intent and alert, read from Atlas
  GET /api/network/can-sleep    the hero query, run on Atlas at a sim minute

The network is seeded by seed/ran_seed.py (web/ran_network.py). The night
itself is simulated in the page from these documents; the agents acting on
it come in a later version. ?session=<token> is accepted for the shell's
link and kept for that version (the reference data is shared).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from bson import ObjectId  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pymongo import AsyncMongoClient  # noqa: E402

from web import ran_network  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("network_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME = "agent_registry"
HTML_PATH = Path(__file__).parent / "network.html"


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


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PATH.read_text()


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
