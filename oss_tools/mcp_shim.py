#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""Serve an oss_tools ToolSet as a stdio FastMCP server (mcp_servers/ shims)."""

from mcp.server.fastmcp import FastMCP


def serve(name: str, tools) -> None:
    mcp = FastMCP(name)
    for fn in tools:
        mcp.tool()(fn)
    mcp.run()
