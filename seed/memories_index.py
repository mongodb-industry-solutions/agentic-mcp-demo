#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Print (and try to create) the Atlas Vector Search index for the
agent_memories collection. This index powers semantic recall over the
long-term facts the orchestrator extracts when a workstream closes:

    "what do you remember about Alpenmarkt?"
    "any past learnings about retail SLA templates?"

Without this index, both the orchestrator's auto-recall during ReAct
and the workstream_service.recall_facts tool fall back to regex /
entity-overlap matching — functional but not semantic.

Run:
    python seed/memories_index.py
"""

import json
import os

from pymongo import MongoClient
from pymongo.operations import SearchIndexModel


MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"
COLL_NAME = "agent_memories"
INDEX     = "agent_memories_index"


DEFINITION = {
    "fields": [
        {
            "type":         "autoEmbed",
            "modality":     "text",
            "path":         "text",
            "model":        "voyage-4",
            "quantization": "float",   # full precision; default 'scalar' (int8) compresses scores
        },
        {"type": "filter", "path": "domain"},
        {"type": "filter", "path": "category"},
        {"type": "filter", "path": "entities"},
        {"type": "filter", "path": "workstream_id"},
    ],
}


def main():
    client = MongoClient(MONGO_URI)
    db   = client[DB_NAME]
    coll = db[COLL_NAME]

    if COLL_NAME not in db.list_collection_names():
        db.create_collection(COLL_NAME)

    existing = [i for i in coll.list_search_indexes() if i.get("name") == INDEX]
    if existing:
        print(f"⚡ Vector index '{INDEX}' already exists "
              f"(status={existing[0].get('status')})")
    else:
        try:
            coll.create_search_index(SearchIndexModel(
                definition=DEFINITION, name=INDEX, type="vectorSearch",
            ))
            print(f"⚡ Submitted '{INDEX}' to Atlas — Active in ~30-90s.")
        except Exception as e:
            print(f"⚠ Could not create vector index automatically: {e}")

    print()
    print("━" * 72)
    print(f"  Atlas Vector Search — manual JSON config for '{INDEX}'")
    print("━" * 72)
    print(f"  Database:   {DB_NAME}")
    print(f"  Collection: {COLL_NAME}")
    print(f"  Name:       {INDEX}")
    print()
    print("In Atlas → Search → Create Search Index → Atlas Vector Search →")
    print("JSON editor → paste:")
    print()
    print(json.dumps(DEFINITION, indent=2))
    print("━" * 72)

    client.close()


if __name__ == "__main__":
    main()
