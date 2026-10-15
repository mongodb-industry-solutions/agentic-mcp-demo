#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""DTW (Digital Twin) domain agent on Atlas Agent Engine: the catalog's DTW
system prompt over the five DTW oss_tools modules (25 tools, all in the tool
sandbox), with NOC approval for blast-radius actions (oss_tools.approvals) and
consult_agent over A2A to the other domain agent."""

from agent_engine_sdk_langgraph import App
from langgraph.graph.state import CompiledStateGraph
from oss_tools import approvals
from oss_tools.dtw import plan, scenario, simulation, topology, traffic
from telco_catalog import DTW_AGENT
from telco_runtime import build_domain_graph, register_consult, register_tools

app = App(app_name="dtw-agent")
register_consult(app, "dtw-agent")
register_tools(app, [plan, topology, traffic, scenario, simulation, approvals])


@app.entrypoint
def build_agent() -> CompiledStateGraph:
    return build_domain_graph(app, DTW_AGENT, review=approvals)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
