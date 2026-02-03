#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SERVER: Gen Z Naming Service
Provides trendy names for young generations (born 1997+).
Use this for teenagers, young adults, and people under 29.
"""

import logging, random
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gen_z_names")

logging.basicConfig(level=logging.ERROR)

GEN_Z_NAMES = ["Aria", "Luna", "Kai", "Nova", "Zara", "Axel", "River", "Phoenix", "Sage", "Atlas"]

@mcp.resource("genz://manifesto")
def get_manifesto() -> str:
    """Philosophy of Gen Z naming conventions"""
    return (
        "GEN Z MANIFESTO:\n"
        "1. Uniqueness over tradition.\n"
        "2. Gender-neutral names preferred.\n"
        "3. Nature and celestial themes dominant."
    )

@mcp.tool()
def get_gen_z_name() -> str:
    """
    Returns a trendy name for Generation Z (ages 0-29).
    Use this for young people, teenagers, or young adults.
    """
    return random.choice(GEN_Z_NAMES)

if __name__ == "__main__":
    mcp.run()
