#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
LangGraph port of agents/domain_agent.py:DomainAgent.run, the loop
semantics the demo is tuned for:

- gpt-4o (OPENAI_MODEL) at temperature=0, one tool call per model turn
  (parallel_tool_calls=False)
- today's date appended to the system prompt (agents resolve relative
  deadlines such as "by tomorrow 18:00")
- at most MAX_ITERATIONS tool-calling model turns per user message, then
  one forced final answer without tools

Not ported (later phases): the shell-injected workstream/memory/preference
context and the replay recipe (Phase 5), consult_agent (Phase 4, via A2A),
and the VERBATIM short-circuit (no IBN/DTW tool returns VERBATIM).
"""

import datetime
import os
from typing import Annotated, Literal, TypedDict

from agent_engine_sdk_langgraph import App
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr

MAX_ITERATIONS = 5


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def register_tools(app: App, modules) -> None:
    """Register every function of each oss_tools module's ToolSet as an
    Agent Engine tool running in the tool sandbox."""
    for module in modules:
        for fn in module.tools:
            app.tool(is_local=False)(fn)


def _llm() -> ChatOpenAI:
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is missing; add it to .env or project secrets")
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        temperature=0,
        api_key=SecretStr(api_key),
    )


def _turn_tool_iterations(messages: list[AnyMessage]) -> int:
    """Tool-calling model turns since the latest user message."""
    count = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, AIMessage) and m.tool_calls:
            count += 1
    return count


def build_domain_graph(app: App, spec: dict) -> CompiledStateGraph:
    """Build the agent graph for one catalog spec ({name, system_prompt, …}).
    Call from inside the agent's @app.entrypoint (app.llm needs it)."""
    model = app.llm(_llm())
    model_with_tools = model.bind_tools(app.get_tool_schemas(), parallel_tool_calls=False)
    tools = app.get_tools()

    def prompt(state: AgentState) -> list[AnyMessage]:
        system = SystemMessage(
            content=spec["system_prompt"]
            + f"\n\nToday's date is {datetime.date.today().isoformat()}."
        )
        return [system] + [m for m in state["messages"] if not isinstance(m, SystemMessage)]

    def agent(state: AgentState) -> dict:
        return {"messages": [model_with_tools.invoke(prompt(state))]}

    def finalize(state: AgentState) -> dict:
        # Mirrors the legacy loop: after MAX_ITERATIONS tool turns, ask once
        # more without tools. The nudge is not persisted to the thread.
        nudge = HumanMessage(content="Provide your final answer now.")
        return {"messages": [model.invoke(prompt(state) + [nudge])]}

    def after_agent(state: AgentState) -> Literal["tools", "end"]:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "end"

    def after_tools(state: AgentState) -> Literal["agent", "finalize"]:
        if _turn_tool_iterations(state["messages"]) >= MAX_ITERATIONS:
            return "finalize"
        return "agent"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", after_agent, {"tools": "tools", "end": END})
    builder.add_conditional_edges("tools", after_tools, {"agent": "agent", "finalize": "finalize"})
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=app.checkpointer())
