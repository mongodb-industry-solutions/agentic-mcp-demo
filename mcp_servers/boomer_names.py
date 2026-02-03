#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SERVER: Boomer Naming Service
Provides classic names for older generations (born pre-1980).
Use this for mature adults, retirees, and people over 45.
"""

import logging, random
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("boomer_names")

logging.basicConfig(level=logging.ERROR)

BOOMER_NAMES = ["Robert", "Michael", "James", "Mary", "Patricia", "Linda", "Barbara", "William"]

@mcp.resource("boomer://manifesto")
def get_manifesto() -> str:
    """Philosophy of Boomer naming conventions"""
    return (
        "BOOMER MANIFESTO:\n"
        "1. Tradition and biblical roots.\n"
        "2. Strong gender distinction.\n"
        "3. Professional, CV-friendly names."
    )

@mcp.tool()
def get_boomer_name() -> str:
    """
    Returns a classic name for Baby Boomers (ages 45+).
    Use this for mature, older, or retired individuals.
    """
    return random.choice(BOOMER_NAMES)

if __name__ == "__main__":
    mcp.run()
