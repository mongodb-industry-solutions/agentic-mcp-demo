#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""NOC coordinator on Atlas Agent Engine (Phase 4 of AGENT_ENGINE_PLAN.md):
routes each request to the IBN and/or DTW agent over A2A, ranks them by
$vectorSearch over agent_cards, and synthesizes multi-agent answers."""

from agent_engine_sdk_langgraph import App
from langgraph.graph.state import CompiledStateGraph
from telco_runtime import build_coordinator_graph, register_delegate, register_tools, routing

app = App(app_name="noc-coordinator")
register_tools(app, [routing])
register_delegate(app)


@app.entrypoint
def build_agent() -> CompiledStateGraph:
    return build_coordinator_graph(app)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
