#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Agent catalog — declarative DomainAgent definitions (Phase 1 of
MULTI_AGENT_PLAN.md).

Each module in this package defines one AGENT spec dict:
    {name, domain, description, system_prompt}

`description` doubles as the agent card text synced to the
vector-indexed agent_registry.agent_cards collection — write it the way
you'd write an MCP service docstring: specific, semantic, routable.
"""

from .base import BASE_RULES
from .ibn import AGENT as IBN_AGENT
from .dtw import AGENT as DTW_AGENT

SPECS = [IBN_AGENT, DTW_AGENT]


def build_agents(host) -> dict:
    """Instantiate every catalog agent against the host orchestrator.
    Returns {domain: DomainAgent}."""
    from ..domain_agent import DomainAgent
    return {spec["domain"]: DomainAgent(host, **spec) for spec in SPECS}
