#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Seed the Midtown radio network of the network map into network_twin (NETWORK_DB):
the night shift (Green Autonomous Networks, web/ran_network.py: the
carrier's inventory, 3GPP PM counters, forecasts, service promises, energy
policy and intent, grid and transit signals) and the robotaxi afternoon
(web/fleet_network.py: the fleet's cars, the OTA campaign and the network's
per-vehicle offers, backhaul topology, price tiers, the ride request, and
the afternoon's counters and forecasts in the same collections). Shared
reference data, not per lane.

    MONGODB_URI=... python seed/ran_seed.py [--reset]

Refuses a cluster whose host does not contain "oss-demos" unless
--any-cluster is given: the legacy demo's cluster must not get these.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import MongoClient  # noqa: E402

from web import fleet_network, ran_network  # noqa: E402

DB = os.environ.get("NETWORK_DB", "network_twin")   # the network scenarios' own database (until 2026-11-28: agent_registry)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop the collections first")
    ap.add_argument("--any-cluster", action="store_true", help="allow a cluster other than oss-demos")
    args = ap.parse_args()
    uri = os.environ["MONGODB_URI"]
    if "oss-demos" not in uri and not args.any_cluster:
        sys.exit("refusing: MONGODB_URI is not the oss-demos cluster (use --any-cluster to override)")
    db = MongoClient(uri)[DB]
    docs = ran_network.documents()
    for name, rows in fleet_network.documents().items():   # shared collections get both scenarios' rows
        docs.setdefault(name, []).extend(rows)
    existing = set(db.list_collection_names())
    for name, rows in docs.items():
        if args.reset and name in existing:
            db.drop_collection(name)
            existing.discard(name)
        if name in ran_network.TIMESERIES and name not in existing:
            db.create_collection(name, timeseries={"timeField": "ts", "metaField": "meta",
                                                   "granularity": ran_network.TIMESERIES[name]})
        coll = db[name]
        if name in ran_network.TIMESERIES:
            coll.delete_many({})
            coll.insert_many(rows)
        else:
            for row in rows:
                coll.replace_one({"_id": row["_id"]}, row, upsert=True)
        print(f"{name}: {len(rows)} documents")
    db["ran_sites"].create_index([("location", "2dsphere")])
    db["ran_cells"].create_index([("location", "2dsphere")])
    db["ran_cells"].create_index([("cluster", 1), ("layer", 1)])
    db["ran_load_forecast"].create_index([("cell_id", 1)])
    db["service_commitments"].create_index([("cell_ids", 1)])
    db["fleet_vehicles"].create_index([("position", "2dsphere")])
    db["ota_offers"].create_index([("campaign_id", 1), ("vehicle_id", 1)])
    db["ota_fleet_choices"].create_index([("campaign_id", 1), ("vehicle_id", 1)])
    for name in ("ota_offers", "ota_fleet_choices"):   # the live re-quote runs expire after an hour
        db[name].create_index([("expires_at", 1)], expireAfterSeconds=0)
    db["transport_links"].create_index([("from", 1)])
    print("indexes ready")


if __name__ == "__main__":
    main()
