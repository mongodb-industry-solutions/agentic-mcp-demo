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
  POST /api/fleet/requote       the 15:30 re-quote: revised offers and the fleet's picks, written to Atlas
  WS  /ws/fleet/requote         ... delivered by a change stream, resumable from a token

Seeded by seed/ran_seed.py. Each scenario is simulated in its page from
these documents; the agents acting on them come in a later version.
?session=<token> is accepted for the shell's link and kept for that version
(the reference data is shared).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import datetime  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import uuid  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from bson import ObjectId  # noqa: E402
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pymongo import AsyncMongoClient  # noqa: E402

from web import fleet_network, ran_network  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("network_dashboard")

MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME = os.environ.get("NETWORK_DB", "network_twin")   # seed/ran_seed.py writes here
HTML_PATH = Path(__file__).parent / "network.html"
FLEET_HTML_PATH = Path(__file__).parent / "fleet.html"


def _json(v):
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json(x) for x in v]
    if isinstance(v, datetime.datetime):   # stored as UTC, shown in New York time like the pages
        return (v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)).astimezone(ran_network.NY).isoformat()
    if isinstance(v, ObjectId):
        return str(v)
    return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGO_URI, tz_aware=True, tzinfo=ran_network.NY)
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
    seeded = {"campaign_id": fleet_network.CAMPAIGN_ID, "run_id": {"$exists": False}}   # not the live re-quote runs
    offers = await db["ota_offers"].find(seeded).to_list(None)
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
        "ota_fleet_choices": await db["ota_fleet_choices"].find(seeded).to_list(None),
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


# ─── Revised offers, live: the 15:30 re-quote over a change stream ──────────
#
# The page sends the cars still waiting at 15:30. The server plays both sides:
# the network agent re-runs the quote for each car and writes a revised offer
# to ota_offers, the fleet writes its pick to ota_fleet_choices. A change
# stream on both collections pushes every insert to the page, with its resume
# token; a consumer that stops and resumes from that token misses nothing.
# Each run's documents carry run_id and expire after an hour (TTL index), so
# visitors do not see each other's runs and the seeded afternoon is untouched.

RUNS: dict[str, dict] = {}
MAX_RUNS = 8
STREAM_COLLS = ["ota_offers", "ota_fleet_choices"]
SITE_NAME = {s[0]: s[1] for s in ran_network.SITES}


@app.post("/api/fleet/requote")
async def fleet_requote(body: dict = Body(...)):
    """Start a re-quote run: body = {minute, vehicles: [{id, lng, lat, pick, eta}]}. The writes start
    when the page's change stream is open (/ws/fleet/requote)."""
    cars = body.get("vehicles") or []
    if not 1 <= len(cars) <= 60:
        raise HTTPException(400, "send 1 to 60 vehicles")
    for c in cars:
        if not (-74.05 < float(c["lng"]) < -73.93 and 40.70 < float(c["lat"]) < 40.80):
            raise HTTPException(400, "position outside Midtown")
    now = datetime.datetime.now(datetime.timezone.utc)
    for k in [k for k, r in RUNS.items() if (now - r["created"]).total_seconds() > 3600]:
        RUNS.pop(k, None)
    if sum(1 for r in RUNS.values() if not r["done"]) >= MAX_RUNS:
        raise HTTPException(429, "too many re-quotes running, try again in a minute")
    run = uuid.uuid4().hex[:10]
    RUNS[run] = {"cars": cars, "minute": max(1, min(fleet_network.STEPS - 1, int(body.get("minute", 150)))),
                 "created": now, "started": False, "done": False, "written": 0, "total": 2 * len(cars)}
    return {"run": run, "events": 2 * len(cars)}


async def _write_run(run: str):
    r, db = RUNS[run], app.state.db
    at = fleet_network.iso(r["minute"])
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    base = {"campaign_id": fleet_network.CAMPAIGN_ID, "run_id": run, "expires_at": expires}
    try:
        for c in r["cars"]:
            lng, lat, vid = float(c["lng"]), float(c["lat"]), str(c["id"])[:12]
            cell = fleet_network.serving(lng, lat)["id"]
            rows = (await _run("ran_cells", fleet_network.quote_pipeline(lng, lat, cell, at)))["rows"]
            own = next((x for x in rows if x["reason"] == "the car's cell now"), {"tier": "congested", "expected_dl_mbps": 0.5})
            offered = [{"site_id": x["site_id"], "area_name": SITE_NAME.get(x["site_id"], x["site_id"]), "cell_id": x["_id"],
                        "tier": x["tier"], "expected_dl_mbps": x["expected_dl_mbps"], "distance_mi": x["distance_mi"]}
                       for x in rows if x["reason"] == "offered"]
            not_offered = [{"site_id": x["site_id"], "area_name": SITE_NAME.get(x["site_id"], x["site_id"]),
                            "reason": x["reason"].removeprefix("not offered: ")} for x in rows if x["reason"].startswith("not offered")]
            offer_id = f"OF-48-{vid}-r2-{run}"
            await db["ota_offers"].insert_one({
                "_id": offer_id, **base, "vehicle_id": vid, "revision": 2, "quoted_at": at, "serving_cell": cell,
                "tier": own["tier"], "expected_dl_mbps": own["expected_dl_mbps"],
                "decision": "not_now" if own["tier"] == "congested" else "download_now",
                "offered_cells": offered, "not_offered": not_offered, "valid_until": fleet_network.iso(300),
                "status": "active", "exposure": "tier and expected throughput only"})
            r["written"] += 1
            await asyncio.sleep(0.25)
            pick = next((m for m in offered if m["site_id"] == c.get("pick")), None)
            await db["ota_fleet_choices"].insert_one({
                "_id": f"CH-48-{vid}-r2-{run}", **base, "vehicle_id": vid, "offer_id": offer_id, "offer_revision": 2,
                "chosen_site": (pick or (offered[0] if offered else {"site_id": None}))["site_id"],
                "area_name": (pick or (offered[0] if offered else {"area_name": None}))["area_name"],
                "why": "the car's next ride ends there" if pick else "the best offered cell (its ride plan has no offered cell)",
                "eta": fleet_network.iso(float(c.get("eta", r["minute"]))), "chosen_at": at, "source": "fleet dispatch"})
            r["written"] += 1
            await asyncio.sleep(0.25)
    except Exception:
        log.exception("re-quote run %s failed", run)
    finally:
        r["done"] = True


def _summary(coll: str, d: dict) -> str:
    if coll == "ota_offers":
        n = len(d.get("offered_cells", []))
        return f"{d['vehicle_id']}: revised offer, {d['decision'].replace('_', ' ')} in its cell ({d['tier']}), {n} cells offered"
    return f"{d['vehicle_id']}: the fleet picks {d.get('area_name')}, {d.get('why')}"


@app.websocket("/ws/fleet/requote")
async def fleet_requote_stream(ws: WebSocket, run: str, resume_after: str = ""):
    """The change stream on ota_offers and ota_fleet_choices for one run, pushed to the page.
    resume_after (a resume token's _data) continues exactly after the last event the page saw."""
    await ws.accept()
    r = RUNS.get(run)
    if not r:
        await ws.send_json({"type": "error", "text": "unknown run"})
        await ws.close()
        return
    pipeline = [{"$match": {"operationType": "insert", "ns.coll": {"$in": STREAM_COLLS}, "fullDocument.run_id": run}}]
    opts = {"resume_after": {"_data": resume_after}} if resume_after else {}
    try:
        async with await app.state.db.watch(pipeline, **opts) as stream:
            await ws.send_json({"type": "ready", "resumed": bool(resume_after)})
            if not r["started"]:
                r["started"] = True
                asyncio.create_task(_write_run(run))
            idle = 0.0
            while True:
                ch = await stream.try_next()
                if ch is None:
                    await asyncio.sleep(0.15)
                    idle += 0.15
                    if idle >= 2:   # a heartbeat also notices a page that has gone
                        await ws.send_json({"type": "tick", "written": r["written"], "total": r["total"], "done": r["done"]})
                        idle = 0.0
                    continue
                idle = 0.0
                coll, d = ch["ns"]["coll"], ch["fullDocument"]
                await ws.send_json({"type": "event", "coll": coll, "vehicle": d["vehicle_id"], "text": _summary(coll, d),
                                    "token": ch["_id"]["_data"], "written": r["written"], "total": r["total"],
                                    "done": r["done"]})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("re-quote stream %s: %s", run, e)
        try:
            await ws.send_json({"type": "error", "text": str(e)[:200]})
        except Exception:
            pass
