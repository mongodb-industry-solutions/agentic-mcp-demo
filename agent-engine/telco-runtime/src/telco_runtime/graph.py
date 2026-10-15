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

Added on Agent Engine (Phase 3): a review node between the model and the
tool node. Calls to blast-radius actions (oss_tools.approvals.
REVIEWED_ACTIONS) suspend the run with interrupt() until a NOC reviewer
approves or rejects them; a rejected call never reaches the tool node. A
template bump with more than one site can be approved as a canary: one site
first, its compliance re-checked, then a second review for the rest.

Not ported (later phases): the shell-injected workstream/memory/preference
context and the replay recipe (Phase 5), consult_agent (Phase 4, via A2A),
and the VERBATIM short-circuit (no IBN/DTW tool returns VERBATIM).
"""

import datetime
import json
import os
from types import ModuleType
from typing import Annotated, Any, Literal, TypedDict

from agent_engine_sdk_langgraph import App
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import SecretStr

MAX_ITERATIONS = 5


APPROVAL_RULES = (
    "\n\n🛂 NOC APPROVAL:\n"
    "Some actions (activating a plan, applying a runbook, bumping a "
    "template, cancelling an intent, deleting scenarios) pause for a NOC "
    "reviewer before they run. Call the tool as usual; its result tells "
    "you the outcome. If the reviewer rejected it, say the action was NOT "
    "executed, pass on the reviewer's notes, and do not retry unless the "
    "user asks again."
)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    # Pending canary rollout of a template bump (JSON-safe): set by the
    # review node, consumed by the canary node after the canary call ran.
    canary: dict[str, Any] | None


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
    # Pinned: a shared project may hold an OPENAI_BASE_URL secret for
    # another agent's gateway, which the openai client would otherwise pick
    # up, and api.openai.com is the tool sandbox's only egress.
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        temperature=0,
        api_key=SecretStr(api_key),
        base_url="https://api.openai.com/v1",
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


def _decision(answer: Any, allowed: list[str]) -> tuple[str, str]:
    """(decision, notes) from a resume value: the flat API/UI body
    {"decision", "reviewer_notes"}, or a bare decision string."""
    if isinstance(answer, dict):
        decision, notes = answer.get("decision"), answer.get("reviewer_notes") or ""
    else:
        decision, notes = answer, ""
    decision = str(decision or "").strip().lower()
    if decision not in allowed:
        # Unknown decisions never run a blast-radius action.
        return "reject", f"invalid decision {decision!r}; treated as reject. {notes}".strip()
    return decision, notes


def _review_payload(action: str, review: dict, extra: dict | None = None) -> dict:
    """The interrupt value: what the Pending Reviews page and the shell show.
    Built only from the recorded open_review result, so replay rebuilds it
    identically."""
    allowed = review["allowed_decisions"]
    return {
        "action": action,
        "approval_id": review["approval_id"],
        "title": review["title"],
        "summary": review["summary"],
        "details": review.get("details", {}),
        "blast_radius": review.get("blast_radius", 1),
        **({"canary": {k: review["canary"][k] for k in ("intent", "site")}}
           if review.get("canary") else {}),
        **(extra or {}),
        "allowed_decisions": allowed,
        "response_schema": {
            "type": "object",
            "required": ["decision"],
            "properties": {
                "decision": {"type": "string", "enum": allowed},
                "reviewer_notes": {"type": "string"},
            },
        },
    }


def build_domain_graph(
    app: App, spec: dict, review: ModuleType | None = None
) -> CompiledStateGraph:
    """Build the agent graph for one catalog spec ({name, system_prompt, …}).
    `review` is the oss_tools.approvals module (registered with
    register_tools); its tools stay internal to the review node.
    Call from inside the agent's @app.entrypoint (app.llm needs it)."""
    internal = {fn.__name__ for fn in review.tools} if review else set()
    reviewed = set(review.REVIEWED_ACTIONS) if review else set()
    model = app.llm(_llm())
    model_with_tools = model.bind_tools(
        [t for t in app.get_tool_schemas() if t.name not in internal],
        parallel_tool_calls=False,
    )
    all_tools = {t.name: t for t in app.get_tools()}
    tools = [t for name, t in all_tools.items() if name not in internal]

    def call(call_id: str, name: str, **args: Any) -> str:
        """Run a registered tool from inside a node: a durable tool activity
        in the tool sandbox, replayed from its recorded result on resume.
        The wrapped tools need a full ToolCall, and its id is the activity's
        durable identity, so call_id must be stable across replays: it is
        derived from the model's tool-call id plus the step."""
        message = all_tools[name].invoke(
            {"type": "tool_call", "name": name, "args": args, "id": call_id}
        )
        content = getattr(message, "content", message)
        return content if isinstance(content, str) else json.dumps(content)

    def ask(
        call_id: str, action: str, arguments: dict, extra: dict | None = None
    ) -> tuple[dict | None, str, str]:
        """open_review → interrupt → close_review. (review, decision, notes);
        review is None when there is nothing to approve. `extra` adds
        replay-stable fields (recorded tool results) to the payload."""
        opened = json.loads(
            call(f"{call_id}:open", "open_review", action=action, arguments=arguments)
        )
        if opened.get("skip"):
            return None, "approve", ""
        answer = interrupt(_review_payload(action, opened, extra))
        decision, notes = _decision(answer, opened["allowed_decisions"])
        call(f"{call_id}:close", "close_review", approval_id=opened["approval_id"],
             decision=decision, reviewer_notes=notes)
        return opened, decision, notes

    def prompt(state: AgentState) -> list[AnyMessage]:
        system = SystemMessage(
            content=spec["system_prompt"]
            + (APPROVAL_RULES if review else "")
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

    def review_node(state: AgentState) -> dict:
        """Gate the model's tool call (one per turn: parallel_tool_calls is
        off). Approve → unchanged to the tool node; reject → a ToolMessage
        instead of the call; canary → the call narrowed to one site."""
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        tool_call = last.tool_calls[0]
        if tool_call["name"] not in reviewed:
            return {}
        opened, decision, notes = ask(tool_call["id"], tool_call["name"], tool_call["args"])
        if opened is None or decision == "approve":
            return {}
        if decision == "reject":
            return {"messages": [ToolMessage(
                content=f"⛔ {opened['approval_id']}: the NOC reviewer REJECTED "
                f"{tool_call['name']}; it was NOT executed. Reviewer notes: "
                f"{notes or '(none)'}",
                tool_call_id=tool_call["id"], name=tool_call["name"],
            )]}
        # approve_canary: run the bump for the first site only.
        canary = opened["canary"]
        narrowed = dict(tool_call, args={**tool_call["args"], "intent_ids": [canary["intent"]]})
        return {
            "messages": [AIMessage(content=last.content, tool_calls=[narrowed], id=last.id)],
            "canary": {**canary, "approval_id": opened["approval_id"],
                       "tool_call_id": tool_call["id"], "args": tool_call["args"]},
        }

    def canary_node(state: AgentState) -> dict:
        """After the canary call: re-check the canary site, then a second
        review for the remaining sites. Folds everything into the call's
        ToolMessage so the model sees one tool call and one result."""
        canary = state["canary"] or {}
        result = next(m for m in reversed(state["messages"])
                      if isinstance(m, ToolMessage) and m.tool_call_id == canary["tool_call_id"])
        base = f"{canary['tool_call_id']}:fleet"
        compliance = call(f"{base}:check", "get_compliance", intent_id=canary["intent"])
        lines = [f"Canary ({canary['approval_id']}) at {canary['site']}:", str(result.content),
                 f"Canary compliance re-check: {compliance}"]
        fleet_args = {**canary["args"], "intent_ids": canary["rest"]}
        opened, decision, notes = ask(base, "update_template_version", fleet_args, {
            "canary": {"intent": canary["intent"], "site": canary["site"]},
            "canary_compliance": compliance,
        })
        if opened and decision == "approve":
            lines.append(f"Fleet rollout approved ({opened['approval_id']}): "
                         + call(f"{base}:run", "update_template_version", **fleet_args))
        elif opened:
            lines.append(f"⛔ Fleet rollout ({opened['approval_id']}) REJECTED by the NOC "
                         f"reviewer; only the canary site was migrated. Notes: "
                         f"{notes or '(none)'}")
        return {
            "messages": [ToolMessage(content="\n".join(lines), tool_call_id=result.tool_call_id,
                                     name=result.name, id=result.id)],
            "canary": None,
        }

    def after_agent(state: AgentState) -> Literal["review", "tools", "end"]:
        last = state["messages"][-1]
        if not (isinstance(last, AIMessage) and last.tool_calls):
            return "end"
        return "review" if review else "tools"

    def after_review(state: AgentState) -> Literal["tools", "agent"]:
        # A rejection left a ToolMessage in place of the call.
        return "agent" if isinstance(state["messages"][-1], ToolMessage) else "tools"

    def after_tools(state: AgentState) -> Literal["canary", "agent", "finalize"]:
        if state.get("canary"):
            return "canary"
        if _turn_tool_iterations(state["messages"]) >= MAX_ITERATIONS:
            return "finalize"
        return "agent"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "agent")
    after_tools_paths: dict = {"agent": "agent", "finalize": "finalize"}
    if review:
        builder.add_node("review", review_node)
        builder.add_node("canary", canary_node)
        builder.add_conditional_edges("agent", after_agent, {"review": "review", "end": END})
        builder.add_conditional_edges("review", after_review, {"tools": "tools", "agent": "agent"})
        builder.add_conditional_edges("canary", after_tools, dict(after_tools_paths))
        after_tools_paths["canary"] = "canary"
    else:
        builder.add_conditional_edges("agent", after_agent, {"tools": "tools", "end": END})
    builder.add_conditional_edges("tools", after_tools, after_tools_paths)
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=app.checkpointer())
