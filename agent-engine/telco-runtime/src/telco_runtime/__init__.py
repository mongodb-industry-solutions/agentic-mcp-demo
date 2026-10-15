#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Shared runtime for the Telco OSS Atlas Agent Engine agents (Phase 2 of
AGENT_ENGINE_PLAN.md): registers oss_tools functions as Agent Engine tools
and builds the LangGraph equivalent of the legacy DomainAgent ReAct loop.
"""

from telco_runtime.graph import build_domain_graph, register_tools

__all__ = ["build_domain_graph", "register_tools"]
