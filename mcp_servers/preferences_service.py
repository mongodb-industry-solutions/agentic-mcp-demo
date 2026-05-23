#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Preferences Service — User self-disclosure: 'I love X', 'I prefer Y', 'remember that I…', 'I'm a X', 'I hate Z'. Stores personal facts, preferences, hobbies, restrictions.

Store, recall, list, and delete user preferences and personal facts
the user EXPLICITLY shares about themselves. Distinct from the
orchestrator's internal `agent_memories` collection (auto-extracted
from closed workstreams) — this service backs the user-facing
"preferences plane": the surface where users say "remember that I…"
and read it back later.

Collection: agent_registry.user_preferences (renamed from the legacy
'episodic_memories' — migrated automatically on first start).

What lives here:
- Personal information (name, location, profession, dietary restrictions)
- Preferences (likes, dislikes, interests, habits)
- Hobbies and activities (sports, series, movies, reading)
- Daily routines (morning habits, evening activities)
- Financial information (investments, portfolio, assets)
- Health information (allergies, conditions, medications)
- Lifestyle choices (vegetarian, vegan, hobbies)
- Shopping preferences (brands, budgets, past purchases)
- Travel preferences (destinations, airline preferences)

Use this service when users say (English):
- Identity / facts:  "I am X", "I'm a X", "I have X", "my name is X",
                     "I live in X", "I work as X", "I invest in X"
- Preferences:       "I like X", "I love X", "I enjoy X", "I prefer X",
                     "I'm into X", "I'm a fan of X", "I'm interested in X",
                     "my favorite X is Y"
- Negative prefs:    "I don't like X", "I hate X", "I avoid X",
                     "I'm allergic to X"
- Explicit remember: "remember that …", "note that …", "keep in mind …",
                     "for the record …", "for future reference …"
- Recall:            "what do you know about me", "what are my preferences",
                     "what did I tell you about X", "do you remember X"
- List all:          "list memories", "list all memories", "show all memories",
                     "show what you remember", "memories"
- Delete one:        "forget X", "I'm not X anymore", "stop remembering X"
- Delete all:        "forget everything", "delete all memories",
                     "clear memories", "wipe memories"

Use this service when users say (German / Deutsch):
- Speichern:         "ich bin X", "ich habe X", "ich heiße X", "ich mag X",
                     "ich liebe X", "ich bevorzuge X", "ich investiere X"
- Erinnern:          "merke dir", "speichere", "erinnere dich an",
                     "behalte im Gedächtnis"
- Abrufen:           "erinnere dich", "was weißt du", "meine Präferenzen",
                     "sage mir was du weißt"
- Löschen:           "vergiss", "lösche", "entferne", "ich bin nicht mehr X"
- Alle löschen:      "vergiss alles", "lösche alles"

Supports both permanent facts and temporary context (auto-expires
after 10 minutes via a TTL index on createdAt).

NOT for: tasks/todos the user wants to do (that's todo_service), nor
for the orchestrator's auto-extracted workstream knowledge (that's
agent_memories, surfaced via workstream_service.list_memories /
recall_facts). This service is the user's deliberate self-disclosure
channel.
"""

import logging, os, datetime
from pymongo import MongoClient, ASCENDING
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp = FastMCP("preferences_service")

logger = logging.getLogger("preferences_service")

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db = mongo_client["agent_registry"]
collection = db["user_preferences"]


def _migrate_from_legacy_collection():
    """
    One-shot: copy any docs from the legacy 'episodic_memories'
    collection into 'user_preferences' if the new collection is
    empty. Then drop the legacy collection so the migration never
    repeats. Non-destructive on re-runs.
    """
    legacy = db["episodic_memories"]
    if "episodic_memories" not in db.list_collection_names():
        return
    if collection.estimated_document_count() > 0:
        return
    docs = list(legacy.find({}))
    if docs:
        collection.insert_many(docs)
        logger.info(f"Migrated {len(docs)} doc(s) from "
                    f"episodic_memories → user_preferences")
    legacy.drop()
    logger.info("Dropped legacy episodic_memories collection.")


_migrate_from_legacy_collection()

def _ensure_ttl_index():
    existing_indexes = collection.index_information()

    has_ttl = False
    for idx_name, idx_info in existing_indexes.items():
        if 'createdAt' in str(idx_info.get('key', [])):
            has_ttl = True
            break

    if not has_ttl:
        logger.info("Creating TTL index on createdAt...")
        collection.create_index(
            [("createdAt", ASCENDING)],
            expireAfterSeconds=600,
            partialFilterExpression={"is_temporary": True},
            name="ttl_index"
        )
        logger.info("TTL index created (10 min expiry for temporary docs)")

def _generate_search_perspectives(user_query: str) -> list[str]:
    """
    Use LLM to generate multiple search perspectives.
    Ensures we catch BOTH long-term facts AND recent context.
    """

    logger.info(f"Generating search perspectives for: '{user_query}'")

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"User query: '{user_query}'\n\n"
                    f"Generate 3-4 diverse search queries to find ALL relevant memories.\n\n"
                    f"CRITICAL: Include BOTH:\n"
                    f"1. Long-term facts (preferences, restrictions, constraints)\n"
                    f"2. Recent context (current requests, recent wants, hunger signals)\n\n"
                    f"Examples:\n\n"
                    f"Query: 'food preferences dietary restrictions allergies'\n"
                    f"Perspectives:\n"
                    f"dietary restrictions allergies intolerances\n"
                    f"food preferences likes dislikes cuisine\n"
                    f"recent food requests hunger eating wants\n"
                    f"meal history recent orders restaurant visits\n\n"
                    f"Query: 'shopping laptop computer'\n"
                    f"Perspectives:\n"
                    f"computer laptop technology preferences\n"
                    f"budget price constraints\n"
                    f"recent shopping requests purchase wants\n"
                    f"past technology purchases brands\n\n"
                    f"Now generate perspectives for the user query above.\n"
                    f"Reply with ONLY the search queries, one per line, no numbering."
                )
            }],
            temperature=0.7,
            max_tokens=200
        )

        perspectives = [
            line.strip()
            for line in response.choices[0].message.content.strip().split('\n')
            if line.strip()
        ]

        # Safety: Add fallback perspective for recent context if missing
        has_recent_context = any(
            keyword in p.lower()
            for p in perspectives
            for keyword in ['recent', 'wants', 'hunger', 'current', 'now', 'today']
        )

        if not has_recent_context:
            perspectives.append("recent requests wants needs hunger current")
            logger.info("  Added fallback perspective for recent context")

        logger.info(f"  Generated {len(perspectives)} perspectives:")
        for p in perspectives:
            logger.info(f"    → {p}")

        return perspectives

    except Exception as e:
        logger.error(f"  Failed to generate perspectives: {e}")
        return [user_query, "recent requests wants needs"]

@mcp.tool()
def recall_preferences(topic: str) -> str:
    """
    Recall relevant user preferences / stated facts using AI-powered
    multi-perspective search. Works for ANY domain (food, shopping,
    travel, etc.). 'Memories' is accepted as a user-facing synonym.

    Args:
        topic: User query or topic to search preferences for
    """

    logger.info(f"Recalling memories for: '{topic}'")
    logger.info(f"Database: {db.name}, Collection: {collection.name}")

    all_memories = list(collection.find(
        {},
        {"_id": 0, "text": 1, "is_temporary": 1, "category": 1}
    ))

    logger.info(f"  Fetched {len(all_memories)} total memories from DB")

    if not all_memories:
        return "No memories found."

    perspectives = _generate_search_perspectives(topic)

    all_relevant_indices = set()

    for perspective in perspectives:
        logger.info(f"  🔍 Evaluating perspective: '{perspective}'")

        memory_list = "\n".join([
            f"{i+1}. {m['text']} (category: {m.get('category', 'unknown')}, "
            f"temporary: {m.get('is_temporary', False)})"
            for i, m in enumerate(all_memories)
        ])

        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search perspective: '{perspective}'\n\n"
                        f"Available memories:\n{memory_list}\n\n"
                        f"Which memories match this search perspective?\n"
                        f"Be INCLUSIVE - if a memory could be even slightly relevant, include it!\n"
                        f"Reply with the numbers of relevant memories (comma-separated).\n"
                        f"If none match, reply 'NONE'."
                    )
                }],
                temperature=0,
                max_tokens=100
            )

            result = response.choices[0].message.content.strip()
            logger.info(f"    AI selected: {result}")

            if result != "NONE":
                try:
                    selected_indices = [int(x.strip()) - 1 for x in result.split(',') if x.strip().isdigit()]
                    all_relevant_indices.update(selected_indices)
                except:
                    logger.warning(f"    Failed to parse AI response: {result}")

        except Exception as e:
            logger.error(f"    AI evaluation failed: {e}")

    if not all_relevant_indices:
        logger.info("  No relevant memories found across all perspectives")
        return "No relevant memories found."

    relevant_memories = [
        all_memories[i]
        for i in sorted(all_relevant_indices)
        if 0 <= i < len(all_memories)
    ]

    logger.info(f"Recalled {len(relevant_memories)} relevant memories")

    permanent = [m["text"] for m in relevant_memories if not m.get("is_temporary")]
    temporary = [m["text"] for m in relevant_memories if m.get("is_temporary")]

    response_text = "🧠 RECALLED:\n"
    if permanent:
        response_text += "PERMANENT:\n" + "\n".join([f"- {m}" for m in permanent]) + "\n"
    if temporary:
        response_text += "TEMPORARY (expires after 10 min):\n" + "\n".join([f"- {m}" for m in temporary])

    return response_text

@mcp.tool()
def remember_fact(fact: str, is_temporary: bool = False) -> str:
    """
    Store a new memory (permanent or temporary).

    Args:
        fact: The information to remember
        is_temporary: If True, expires after 10 minutes (default: False)
    """

    logger.info(f"💾 Storing memory: '{fact}' (temporary={is_temporary})")
    logger.info(f"   Database: {db.name}, Collection: {collection.name}")

    try:
        category_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Fact: '{fact}'\n\n"
                    f"Generate a short, descriptive category tag (1-3 words, lowercase).\n"
                    f"Examples:\n"
                    f"- 'User is vegetarian' → 'dietary_restriction'\n"
                    f"- 'User wants Indian food' → 'cuisine_preference'\n"
                    f"- 'User wants a burger' → 'food_want'\n"
                    f"- 'User lives in Berlin' → 'location'\n\n"
                    f"Reply with ONLY the category tag."
                )
            }],
            temperature=0,
            max_tokens=20
        )

        category = category_response.choices[0].message.content.strip().lower()
        logger.info(f"  AI-generated category: {category}")

    except Exception as e:
        logger.error(f"  Category generation failed: {e}, using 'general'")
        category = "general"

    doc = {
        "text": fact,
        "category": category,
        "is_temporary": is_temporary,
        "createdAt": datetime.datetime.now(datetime.timezone.utc)
    }

    try:
        result = collection.insert_one(doc)
        logger.info(f"Memory stored with _id: {result.inserted_id}")

        if is_temporary:
            logger.info(f"   Will expire in 10 minutes")

        return f"Remembered: {fact}"

    except Exception as e:
        logger.error(f"❌ Failed to store memory: {e}")
        return f"❌ Failed to remember: {fact}"

@mcp.tool()
def list_preferences() -> str:
    """
    List ALL stored user preferences / facts about the user.
    User-facing aliases for invocation: 'list memories',
    'show what you remember', 'memories', 'what do you know about me'.

    Returns:
        Complete list of all permanent and temporary preferences
    """
    logger.info("📋 Listing all memories")

    all_memories = list(collection.find(
        {},
        {"_id": 0, "text": 1, "category": 1, "is_temporary": 1, "createdAt": 1}
    ).sort("createdAt", -1))  # Newest first

    if not all_memories:
        return "No memories stored yet."

    # Separate by type
    permanent = [m for m in all_memories if not m.get("is_temporary", False)]
    temporary = [m for m in all_memories if m.get("is_temporary", False)]

    response_text = f"📋 Total: {len(all_memories)} memory(ies)\n\n"

    if permanent:
        response_text += f"PERMANENT ({len(permanent)}):\n"
        for m in permanent:
            category = m.get("category", "general")
            response_text += f"- {m['text']} [{category}]\n"
        response_text += "\n"

    if temporary:
        response_text += f"TEMPORARY ({len(temporary)}, expires after 10 min):\n"
        for m in temporary:
            category = m.get("category", "general")
            response_text += f"- {m['text']} [{category}]\n"

    return response_text

@mcp.tool()
def forget_preference(topic: str) -> str:
    """
    Delete a user preference / stored fact matching a topic, using
    AI-powered semantic search.

    Args:
        topic: Description of what to forget (e.g., "vegetarian",
               "Solana investment", "tennis")

    Returns:
        Confirmation of deleted preferences
    """
    logger.info(f"🗑️ Forgetting memories about: '{topic}'")

    # Reuse existing search logic to find relevant memories
    all_memories = list(collection.find(
        {},
        {"_id": 1, "text": 1, "category": 1, "is_temporary": 1}
    ))

    if not all_memories:
        return "No memories found to delete."

    # Generate search perspectives for finding memories to delete
    perspectives = _generate_search_perspectives(topic)
    all_relevant_indices = set()

    for perspective in perspectives:
        logger.info(f" 🔍 Searching perspective: '{perspective}'")
        memory_list = "\n".join([
            f"{i+1}. {m['text']} (category: {m.get('category', 'unknown')})"
            for i, m in enumerate(all_memories)
        ])

        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"User wants to forget: '{perspective}'\n\n"
                        f"Available memories:\n{memory_list}\n\n"
                        f"Which memories should be DELETED?\n"
                        f"Be CONSERVATIVE - only select memories that clearly match.\n"
                        f"Reply with the numbers of memories to delete (comma-separated).\n"
                        f"If none match, reply 'NONE'."
                    )
                }],
                temperature=0,
                max_tokens=100
            )

            result = response.choices[0].message.content.strip()
            logger.info(f" AI selected for deletion: {result}")

            if result != "NONE":
                try:
                    selected_indices = [int(x.strip()) - 1 for x in result.split(',') if x.strip().isdigit()]
                    all_relevant_indices.update(selected_indices)
                except:
                    logger.warning(f" Failed to parse AI response: {result}")

        except Exception as e:
            logger.error(f" AI evaluation failed: {e}")

    if not all_relevant_indices:
        logger.info(" No matching memories found to delete")
        return f"No memories found matching '{topic}'."

    # Collect memories to delete
    memories_to_delete = [
        all_memories[i]
        for i in sorted(all_relevant_indices)
        if 0 <= i < len(all_memories)
    ]

    # Delete from MongoDB
    deleted_count = 0
    deleted_texts = []

    for mem in memories_to_delete:
        try:
            result = collection.delete_one({"_id": mem["_id"]})
            if result.deleted_count > 0:
                deleted_count += 1
                deleted_texts.append(mem["text"])
                logger.info(f" ✓ Deleted: {mem['text']}")
        except Exception as e:
            logger.error(f" ❌ Failed to delete {mem['_id']}: {e}")

    if deleted_count == 0:
        return "Failed to delete any memories."

    response_text = f"🗑️ Deleted {deleted_count} memory(ies):\n"
    response_text += "\n".join([f"- {text}" for text in deleted_texts])

    return response_text

@mcp.tool()
def forget_all_preferences() -> str:
    """
    NUCLEAR: Delete ALL stored preferences / facts about the user.
    Use when the user says: 'forget everything', 'delete all
    memories', 'wipe my preferences', 'vergiss alles', 'lösche alles'.

    ⚠️ WARNING: This is irreversible!
    """

    try:
        result = collection.delete_many({})
        deleted_count = result.deleted_count
        logger.info(f"Deleted {deleted_count} memories")

        if deleted_count == 0:
            return "No memories found to delete."

        return f"🗑️ Deleted ALL memories ({deleted_count} total)"

    except Exception as e:
        logger.error(f"Failed to delete all memories: {e}")
        return f"❌ Failed to delete memories: {e}"

if __name__ == "__main__":
    logger.info("🚀 Starting Memory Service...")
    _ensure_ttl_index()
    mcp.run()
