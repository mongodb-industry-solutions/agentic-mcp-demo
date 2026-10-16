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

Added in Phase 4: consult_agent, one focused question to the other
domain agent over A2A (telco_runtime.a2a). Two per turn, and only at depth
0: a consulted agent gets no consult tool and a 3-iteration budget, as in
the legacy shell-mediated consultation.

Added in Phase 5: when called directly (not over A2A), the agent recalls
the user's Agent Engine memory once per user message and adds it to the
system prompt (telco_runtime.memory). A2A children get it from the caller.

Not ported (later phases): the shell-injected workstream context and the
replay recipe (Phase 8), and the VERBATIM short-circuit (no IBN/DTW tool
returns VERBATIM).
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

from telco_runtime import memory as mem
from telco_runtime.a2a import A2A_TIMEOUT_S, AGENTS, call_agent, consult_depth, is_a2a_child

MAX_ITERATIONS = 5
CONSULTED_MAX_ITERATIONS = 3
CONSULT_BUDGET = 2


APPROVAL_RULES = (
    "\n\n🛂 NOC APPROVAL:\n"
    "Some actions (activating a plan, applying a runbook, bumping a "
    "template, cancelling an intent, deleting scenarios) pause for a NOC "
    "reviewer before they run. Call the tool as usual; its result tells "
    "you the outcome. If the reviewer rejected it, say the action was NOT "
    "executed, pass on the reviewer's notes, and do not retry unless the "
    "user asks again."
)


CONSULT_GUIDANCE = (
    "\n\n🤝 CONSULTATION:\n"
    "You can ask another domain specialist ONE focused question with "
    "the consult_agent tool when the user's request needs information "
    "outside your own domain. Budget: 2 consultations per turn. "
    "Phrase the question self-contained — the specialist sees nothing "
    "of this conversation. Attribute consulted facts in your answer "
    "(e.g. 'per the IBN agent, …'). Never consult for information "
    "your own tools can provide."
)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    # Pending canary rollout of a template bump (JSON-safe): set by the
    # review node, consumed by the canary node after the canary call ran.
    canary: dict[str, Any] | None
    # The user's recalled memory for the current user message ('' if none).
    memory: str


def register_tools(app: App, modules) -> None:
    """Register every function of each oss_tools module's ToolSet as an
    Agent Engine tool running in the tool sandbox."""
    for module in modules:
        for fn in module.tools:
            app.tool(is_local=False)(fn)


def register_consult(app: App, own: str) -> None:
    """Register consult_agent (agent sandbox: A2A lives there) for the
    agent named `own`, offering the other catalog agents."""
    peers = {name: spec for name, spec in AGENTS.items() if name != own}

    def consult_agent(agent: str, question: str) -> str:
        if consult_depth() >= 1:
            return "❌ Consultations can't be nested: answer from your own tools."
        if agent not in peers:
            return f"❌ No such agent: {agent!r}. Available: {', '.join(peers)}"
        r = call_agent(app, agent, question, depth=1)
        if r["status"] == "completed":
            return r.get("result") or "(no answer)"
        if r["status"] == "input-required":
            return (f"❌ {agent} paused for a NOC review instead of answering; "
                    "consultations are questions, not actions.")
        return f"❌ Consultation failed: {r.get('error') or r['status']}"

    consult_agent.__doc__ = (
        "Ask another domain-specialist agent one focused, self-contained "
        "question and get its answer back. Args: agent (one of: "
        f"{', '.join(peers)}); question (self-contained: that agent has no "
        "access to this conversation). Available specialists: "
        + "; ".join(f"{n} ({s['domain']}): {s['description'][:140]}" for n, s in peers.items())
    )
    app.tool(is_local=True, timeout=A2A_TIMEOUT_S + 10)(consult_agent)


def _llm(model: str | None = None) -> ChatOpenAI:
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is missing; add it to .env or project secrets")
    # Pinned: a shared project may hold an OPENAI_BASE_URL secret for
    # another agent's gateway, which the openai client would otherwise pick
    # up, and api.openai.com is the tool sandbox's only egress.
    return ChatOpenAI(
        model=model or os.environ.get("OPENAI_MODEL", "gpt-4o"),
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


def _turn_consults(messages: list[AnyMessage]) -> int:
    """consult_agent calls since the latest user message."""
    count = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage) and m.name == "consult_agent":
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
    consult_agent is offered when the agent registered it (register_consult).
    Call from inside the agent's @app.entrypoint (app.llm needs it)."""
    internal = {fn.__name__ for fn in review.tools} if review else set()
    reviewed = set(review.REVIEWED_ACTIONS) if review else set()
    model = app.llm(_llm())
    schemas = [t for t in app.get_tool_schemas() if t.name not in internal]
    can_consult = any(t.name == "consult_agent" for t in schemas)
    with_consult = model.bind_tools(schemas)
    without_consult = model.bind_tools([t for t in schemas if t.name != "consult_agent"])
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

    def consult_offered(state: AgentState) -> bool:
        return (can_consult and consult_depth() == 0
                and _turn_consults(state["messages"]) < CONSULT_BUDGET)

    def prompt(state: AgentState) -> list[AnyMessage]:
        system = SystemMessage(
            content=spec["system_prompt"]
            + (APPROVAL_RULES if review else "")
            + (CONSULT_GUIDANCE if consult_offered(state) else "")
            + f"\n\nToday's date is {datetime.date.today().isoformat()}."
            + (f"\n\n🧠 {mem.MEMORY_PREAMBLE}\n{state['memory']}"
               if state.get("memory") else "")
        )
        return [system] + [m for m in state["messages"] if not isinstance(m, SystemMessage)]

    def recall(state: AgentState) -> dict:
        if is_a2a_child():
            return {"memory": ""}
        human = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        return {"memory": mem.recall(app, str(human.content))}

    def agent(state: AgentState) -> dict:
        bound = with_consult if consult_offered(state) else without_consult
        # One tool call per model turn, so the review node sees every call.
        # An invocation option: the SDK drops it when passed to bind_tools.
        return {"messages": [bound.invoke(prompt(state), parallel_tool_calls=False)]}

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
        # Belt and braces for parallel_tool_calls=False: run only the first
        # call, so nothing reaches the tool node unreviewed. The model sees
        # one result and re-issues the rest.
        single = ([AIMessage(content=last.content, tool_calls=[tool_call], id=last.id)]
                  if len(last.tool_calls) > 1 else [])
        if tool_call["name"] not in reviewed:
            return {"messages": single} if single else {}
        opened, decision, notes = ask(tool_call["id"], tool_call["name"], tool_call["args"])
        if opened is None or decision == "approve":
            return {"messages": single} if single else {}
        if decision == "reject":
            return {"messages": single + [ToolMessage(
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
        limit = CONSULTED_MAX_ITERATIONS if consult_depth() >= 1 else MAX_ITERATIONS
        if _turn_tool_iterations(state["messages"]) >= limit:
            return "finalize"
        return "agent"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("finalize", finalize)
    builder.add_node("recall", recall)
    builder.add_edge(START, "recall")
    builder.add_edge("recall", "agent")
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
