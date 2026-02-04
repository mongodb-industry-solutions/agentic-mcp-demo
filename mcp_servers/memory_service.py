#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SERVER: Memory Service (AI-Powered Multi-Perspective Recall with TTL)
Stores and recalls user memories using LLM for multi-perspective relevance judgment.
MongoDB handles automatic expiration of temporary memories (10 minutes).
"""

import logging, os, datetime
from pymongo import MongoClient, ASCENDING
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp = FastMCP("memory_service")

logger = logging.getLogger("memory_service")

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db = mongo_client["agent_registry"]
collection = db["episodic_memories"]

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
def recall_memories(topic: str) -> str:
    """
    Recall relevant memories using AI-powered multi-perspective search.
    Works generically for ANY domain (food, shopping, travel, etc.)

    Args:
        topic: User query or topic to search memories for
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

if __name__ == "__main__":
    logger.info("🚀 Starting Memory Service...")
    _ensure_ttl_index()
    mcp.run()
