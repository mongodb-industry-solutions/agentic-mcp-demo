#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Telco OSS tool implementations (IBN + DTW), runtime-neutral.

Each module exposes `tools`, a ToolSet of plain functions. The FastMCP
servers in mcp_servers/ are thin shims over these modules, and Atlas Agent
Engine agents register the same functions with `@app.tool()` (Phase 1 of
AGENT_ENGINE_PLAN.md).
"""
