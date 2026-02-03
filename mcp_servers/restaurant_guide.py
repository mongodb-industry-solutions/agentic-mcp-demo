#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SERVER: Restaurant & Dining Guide (AI-Powered with Voyage AI)
Finds restaurants based on cuisine and dietary restrictions.
Use this for food recommendations, dining suggestions, and meal planning.
"""

import logging, os, numpy as np, voyageai
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("restaurant_guide")

voyage = voyageai.Client(api_key=os.environ.get("VOYAGE_API_KEY"))

logging.basicConfig(
    level=logging.ERROR,
    format='%(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("restaurant_guide")


RESTAURANTS = [
    {"name": "The Green Leaf", "cuisine": "Vegan", "price": "$$"},
    {"name": "Steakhouse Prime", "cuisine": "Steakhouse", "price": "$$$$"},
    {"name": "Ocean Blue", "cuisine": "Seafood", "price": "$$$"},
    {"name": "Tofu Palace", "cuisine": "Asian/Vegan", "price": "$$"},
    {"name": "Burger Shack", "cuisine": "Fast Food", "price": "$"}
]

# Cache embeddings at startup
RESTAURANT_EMBEDDINGS = []

def _get_embedding(text: str, input_type: str = "document") -> list:
    result = voyage.embed(
        [text],
        model="voyage-3-large",
        input_type=input_type
    )
    return result.embeddings[0]

def _cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

logger.info("Computing restaurant embeddings with Voyage AI...")
for r in RESTAURANTS:
    description = f"{r['name']} {r['cuisine']} restaurant"
    embedding = _get_embedding(description, input_type="document")
    RESTAURANT_EMBEDDINGS.append(embedding)
logger.info("Embeddings ready")

@mcp.tool()
def find_restaurants(preference_filter: str = "all") -> str:
    """
    Finds restaurants using AI-powered semantic search via Voyage AI.
    Args: preference_filter can be any natural language query about food preferences.
    """

    logger.info(f"Searching for: '{preference_filter}'")

    if preference_filter == "all":
        return "\n".join([f"{r['name']} ({r['cuisine']}) - {r['price']}" for r in RESTAURANTS])

    query_embedding = _get_embedding(preference_filter, input_type="query")

    scores = []
    for i, restaurant in enumerate(RESTAURANTS):
        similarity = _cosine_similarity(query_embedding, RESTAURANT_EMBEDDINGS[i])
        scores.append((restaurant, similarity))
        logger.info(f"  {restaurant['name']}: {similarity:.3f}")

    scores.sort(key=lambda x: x[1], reverse=True)

    # Return top matches (similarity > 0.6 threshold for Voyage)
    results = []
    for restaurant, score in scores:
        if score > 0.6:
            results.append(
                f"{restaurant['name']} ({restaurant['cuisine']}) "
                f"- {restaurant['price']} [match: {score:.2f}]"
            )

    if not results:
        # Fallback: return top match
        top = scores[0]
        logger.info(f"No matches > 0.6, returning top: {top[0]['name']} ({top[1]:.2f})")
        return f"{top[0]['name']} ({top[0]['cuisine']}) - {top[0]['price']} [match: {top[1]:.2f}]"

    logger.info(f"Found {len(results)} matches")
    return "\n".join(results)

if __name__ == "__main__":
    mcp.run()
