#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""IBN domain agent on Atlas Agent Engine: the catalog's IBN system prompt
over the five IBN oss_tools modules (19 tools, all in the tool sandbox),
with NOC approval for blast-radius actions (oss_tools.approvals) and
consult_agent over A2A to the other domain agent."""

from agent_engine_sdk_langgraph import App
from langgraph.graph.state import CompiledStateGraph
from oss_tools import approvals
from oss_tools.ibn import assurance, feasibility, intent, inventory, telemetry
from telco_catalog import IBN_AGENT
from telco_runtime import build_domain_graph, register_consult, register_tools

app = App(app_name="ibn-agent")
register_consult(app, "ibn-agent")
register_tools(app, [intent, inventory, feasibility, assurance, telemetry, approvals])


@app.entrypoint
def build_agent() -> CompiledStateGraph:
    return build_domain_graph(app, IBN_AGENT, review=approvals)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
