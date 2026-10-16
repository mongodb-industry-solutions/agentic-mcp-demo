#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
The NOC coordinator on Atlas Agent Engine (Phase 4 of AGENT_ENGINE_PLAN.md):
the LangGraph port of the legacy shell's multi-agent turn
(agents/dispatch.py). Per user message:

1. route: Stage 1 picks the agent domains in scope (an explicit "ibn"/"dtw"
   mention, else gpt-4o-mini, with the previous turn's domains as the
   sticky hint for follow-ups). Two or more → rank them by $vectorSearch
   over agent_cards (Atlas is the switchboard), then split the request
   into one `delegate` call per agent that has something to do (gpt-4o-mini
   with tool_choice=required; null bias as in the legacy splitter).
2. delegate: the calls run concurrently over A2A (the wire), each in a
   child execution of the target agent, nested in this session's trace.
3. respond: one answer passes through unchanged; two or more are
   synthesized (gpt-4o). The turn is recorded in routing_decisions.

A2A child executions don't share this conversation, so each delegated
message carries the last turns as context, and, since Phase 5, the user's
recalled memory (preferences, episodes, procedures, glossary): a child
has no user identity to recall it itself. A preference the user states is
saved as semantic memory in the same classifier call.
"""

import json
import re
import time
from typing import Annotated, Any, TypedDict

from agent_engine_sdk_langgraph import App
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from telco_runtime import memory as mem
from telco_runtime.a2a import A2A_TIMEOUT_S, AGENTS, DOMAIN_AGENT, call_agent
from telco_runtime.graph import _llm

CONTEXT_TURNS = 3

SYNTHESIS_PROMPT = (
    "You are the coordinator of domain-specialist agents in a network "
    "operations system. Combine the specialist answers below into ONE "
    "coherent response to the user's request.\n"
    "- Use ONLY facts from the specialist answers — do not invent data.\n"
    "- Keep concrete details: IDs, metrics, statuses, tables, runbook steps.\n"
    "- If a specialist reported an error or no data, say so plainly.\n"
    "- Operational tone for NOC engineers; speak in third person about "
    "customers."
)


class CoordinatorState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    # This turn's routing record (JSON-safe), written by route and respond.
    turn: dict[str, Any]
    # Domains the previous turn went to: the sticky hint for follow-ups.
    last_domains: list[str]


def register_delegate(app: App) -> None:
    """Register `delegate`, the coordinator's only LLM-visible tool. It runs
    in the agent sandbox (A2A lives there) and is recorded like any tool."""

    def delegate(agent: str, message: str) -> str:
        """Hand one focused, self-contained sub-task to a domain-specialist
        agent and return its answer as JSON."""
        return json.dumps(call_agent(app, agent, message, depth=0))

    app.tool(is_local=True, timeout=A2A_TIMEOUT_S + 10)(delegate)


@tool
def split(agent: str, subtask: str) -> str:
    """Give one domain-specialist agent its part of the user's request,
    phrased as a self-contained instruction. Call once per agent that must
    act; never for an agent with nothing to do."""
    return subtask  # schema only: the route node turns these into delegate calls


def _turns(messages: list[AnyMessage]) -> list[tuple[str, str]]:
    """Completed (user, answer) turns, oldest first."""
    out, user = [], None
    for m in messages:
        if isinstance(m, HumanMessage):
            user = str(m.content)
        elif isinstance(m, AIMessage) and not m.tool_calls and user is not None:
            out.append((user, str(m.content)))
            user = None
    return out


def build_coordinator_graph(app: App) -> CompiledStateGraph:
    """Call from inside the coordinator's @app.entrypoint. Needs the
    telco_runtime.routing tools and `delegate` registered."""
    router = app.llm(_llm("gpt-4o-mini"), llm_id="router")
    synthesizer = app.llm(_llm(), llm_id="synthesizer")
    tools = {t.name: t for t in app.get_tools()}
    names = list(AGENTS)

    def internal(call_id: str, name: str, **args: Any) -> Any:
        """Run an internal tool-sandbox tool from a node (durable, see
        graph.build_domain_graph.call)."""
        message = tools[name].invoke({"type": "tool_call", "name": name, "args": args,
                                      "id": call_id})
        return json.loads(getattr(message, "content", message))

    def classify(query: str, sticky: list[str]) -> tuple[list[str], str, dict]:
        """(domains, method, memory intent). The memory intent holds a
        `preference` {label, text} to save and/or `list_preferences`; a
        message that is only that has no domains."""
        # A literal "ibn"/"dtw" is a sure signal for that domain, but unlike
        # the legacy shortcut it doesn't rule the other one out: "simulate
        # the uplift and list the IBN intents" needs both.
        explicit = [d for d in DOMAIN_AGENT if re.search(rf"\b{d}s?\b", query, re.IGNORECASE)]
        if len(explicit) == len(DOMAIN_AGENT):
            return explicit, "explicit_mention", {}
        catalog = "\n".join(f"- {s['domain']}: {s['description'][:400]}" for s in AGENTS.values())
        hint = (f"\nThe previous turn went to: {', '.join(sticky)}. A short follow-up that "
                "continues it ('is it feasible?', 'run it', 'show me') stays there.\n"
                if sticky else "")
        picker = router.bind_tools([_route_schema(list(DOMAIN_AGENT))], tool_choice="required")
        msg = picker.invoke([HumanMessage(content=(
            "Pick the specialist domains this NOC request needs. Include a domain only "
            "when the request concerns something that domain handles; pick both when it "
            "spans them.\n"
            "Also set `memory`: 'remember' when this request itself states something "
            "lasting to keep about the user (how they want answers, what they look after; "
            "put it in `preference`, in their own words), 'recall' when they ask what you "
            "remember about them, else 'none'. Never invent a preference. A message that "
            "is only 'remember' or 'recall' (no question or task about the network, its "
            "intents or simulations) gets an "
            "empty `domains` list; who the user is or works for is not a task.\n\n"
            f"Domains:\n{catalog}\n{hint}\nRequest: {query!r}"))])
        args = msg.tool_calls[0]["args"] if msg.tool_calls else {}
        action = args.get("memory")
        intent: dict[str, Any] = {}
        if action == "remember" and args.get("preference"):
            intent["preference"] = args["preference"]
        elif action == "recall":
            intent["list_preferences"] = True
        picked = explicit + [d for d in args.get("domains", []) if d in DOMAIN_AGENT]
        method = "explicit_mention+llm" if explicit else "llm"
        if not picked and intent:
            return [], method, intent
        return (list(dict.fromkeys(picked)) or sticky or list(DOMAIN_AGENT)), method, intent

    def with_context(subtask: str, agent: str, messages: list[AnyMessage], memory: str) -> str:
        history = _turns(messages)[-CONTEXT_TURNS:]
        parts = []
        if memory:
            parts.append(f"{mem.MEMORY_PREAMBLE}\n{memory}")
        if history:
            lines = "\n".join(f"User: {u}\nAnswer: {a[:1200]}" for u, a in history)
            parts.append(f"Earlier turns of this NOC conversation (for context; {agent} may "
                         f"have handled them):\n{lines}")
        if not parts:
            return subtask
        return "\n\n".join(parts) + f"\n\nCurrent request: {subtask}"

    def route(state: CoordinatorState) -> dict:
        human = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        query = str(human.content)
        t0 = time.time()
        seeded = mem.ensure_seeded(
            app, lambda: internal(f"{human.id}:runbooks", "runbooks_for_memory"))
        memory = mem.recall(app, query)
        domains, method, intent = classify(query, state.get("last_domains") or [])
        turn: dict[str, Any] = {"query": query, "t0": t0,
                                "stage1": {"method": method, "domains_selected": domains},
                                "memory": {"recalled_chars": len(memory), "seeded": seeded}}
        notes = []
        pref = intent.get("preference") or {}
        if (pref.get("label") and pref.get("text")
                and mem.save_preference(app, str(pref["label"]), str(pref["text"]))):
            notes.append(f"Noted for next time: {pref['text']}")
            turn["memory"]["preference_saved"] = pref["label"]
        if intent.get("list_preferences"):
            prefs = mem.list_preferences(app)
            notes.append("What I remember about you:\n" + "\n".join(f"- {p}" for p in prefs)
                         if prefs else "I don't remember anything about you yet.")
        turn["notes"] = notes
        if not domains:
            turn["dispatched_agents"] = []
            return {"turn": turn}
        if len(domains) == 1:
            subtasks = {DOMAIN_AGENT[domains[0]]: query}
        else:
            ranked = internal(f"{human.id}:rank", "rank_agents", query=query, domains=domains)
            turn["agent_cards"] = {"ranked": ranked}
            order = [r["agent"] for r in ranked]
            msg = router.bind_tools([split], tool_choice="required").invoke([
                SystemMessage(content=(
                    "You are the coordinator of domain-specialist agents. Split the user's "
                    "request into one focused sub-task per agent that has something to do, "
                    "phrased as a self-contained instruction.\n"
                    "Default to leaving an agent out. Only give an agent a sub-task when the "
                    "request names something that agent specifically must act on. Never "
                    "invent work for an agent because the wording resembles its domain; one "
                    "sub-task for one agent is a normal outcome.\n\nAgents (best match "
                    "first):\n" + "\n".join(
                        f"- {a}: {AGENTS[a]['description'][:220]}" for a in order))),
                HumanMessage(content=query),
            ])
            subtasks = {}
            for c in msg.tool_calls:
                agent, subtask = c["args"].get("agent"), str(c["args"].get("subtask") or "")
                if agent in names and subtask.strip() and agent not in subtasks:
                    subtasks[agent] = subtask.strip()
            subtasks = subtasks or {a: query for a in order}
            turn["multi"] = {"subtasks": subtasks}
        calls = [{"type": "tool_call", "name": "delegate", "id": f"{human.id}:delegate:{agent}",
                  "args": {"agent": agent,
                           "message": with_context(subtask, agent, state["messages"], memory)}}
                 for agent, subtask in subtasks.items()]
        turn["dispatched_agents"] = list(subtasks)
        return {"messages": [AIMessage(content="", tool_calls=calls)], "turn": turn}

    def respond(state: CoordinatorState) -> dict:
        turn = dict(state.get("turn") or {})
        human = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        results = []
        for m in state["messages"]:
            if isinstance(m, ToolMessage) and m.tool_call_id.startswith(f"{human.id}:delegate:"):
                try:
                    results.append(json.loads(str(m.content)))
                except ValueError:
                    results.append({"agent": m.tool_call_id.rsplit(":", 1)[-1],
                                    "status": "failed", "error": str(m.content)})

        # A delegated agent that paused for a NOC review keeps the review:
        # A2A can't resume a child, so the reviewer answers it on that
        # agent (Pending Reviews, or the shell via its session). Name it.
        paused = [r["execution_id"] for r in results
                  if r.get("status") == "input-required" and r.get("execution_id")]
        reviews = {}
        if paused:
            for rv in internal(f"{human.id}:reviews", "pending_reviews", execution_ids=paused):
                reviews.setdefault(rv["execution_id"], []).append(rv)

        def text(r: dict) -> str:
            if r.get("status") == "completed":
                return r.get("result") or "(no answer)"
            if r.get("status") == "input-required":
                found = reviews.get(r.get("execution_id"), [])
                if not found:
                    return (f"⏸ {r['agent']} paused for a NOC review "
                            f"(execution {r.get('execution_id')}).")
                return "\n".join(
                    f"⏸ {rv['approval_id']} awaits NOC approval on {r['agent']}: "
                    f"{rv['title']}. {rv['summary']} Approve or reject it in "
                    f"{r['agent']}'s pending reviews; it has NOT run yet." for rv in found)
            return f"({r['agent']} failed: {r.get('error') or r.get('status')})"

        t_synth = time.time()
        notes = turn.pop("notes", [])
        if not results:
            answer = "\n\n".join(notes) or "Nothing to do."
        elif len(results) == 1:
            answer = text(results[0])
        else:
            answer = str(synthesizer.invoke([
                SystemMessage(content=SYNTHESIS_PROMPT),
                HumanMessage(content=f"User request: {turn.get('query')}\n\n" + "\n\n".join(
                    f"### {r['agent']}\n{text(r)}" for r in results)),
            ]).content)
            turn["synthesis_ms"] = int((time.time() - t_synth) * 1000)
        if results and notes:
            answer = "\n\n".join(notes) + "\n\n" + answer
        turn["agents"] = {r["agent"]: {
            "status": r.get("status"), "execution_id": r.get("execution_id"),
            **({"pending_reviews": [{k: rv[k] for k in ("approval_id", "session_id")
                                     if k in rv}
                                    for rv in reviews.get(r.get("execution_id"), [])]}
               if r.get("status") == "input-required" else {}),
        } for r in results}
        turn["duration_ms"] = int((time.time() - turn.pop("t0", t_synth)) * 1000)
        internal(f"{human.id}:record", "record_routing", decision=turn)
        domains = [s["domain"] for a, s in AGENTS.items() if a in turn["agents"]]
        return {"messages": [AIMessage(content=answer)], "turn": {}, "last_domains": domains}

    builder = StateGraph(CoordinatorState)
    builder.add_node("route", route)
    builder.add_node("delegate", ToolNode([tools["delegate"]]))
    builder.add_node("respond", respond)
    builder.add_edge(START, "route")
    builder.add_conditional_edges(
        "route",
        lambda s: "delegate" if isinstance(s["messages"][-1], AIMessage)
        and s["messages"][-1].tool_calls else "respond",
        {"delegate": "delegate", "respond": "respond"},
    )
    builder.add_edge("delegate", "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=app.checkpointer())


def _route_schema(domains: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": "route",
        "description": "The specialist domains this request needs, and whether it states "
                       "or asks about something to remember.",
        "parameters": {"type": "object", "required": ["domains", "memory"], "properties": {
            "domains": {"type": "array", "items": {"type": "string", "enum": domains},
                        "description": "Empty when the message only states or asks about "
                                       "what to remember and requests no network work."},
            "memory": {"type": "string", "enum": ["none", "remember", "recall"],
                       "description": "remember: the user states something lasting about "
                                      "themselves to keep. recall: they ask what you "
                                      "remember about them. none: neither."},
            "preference": {
                "type": "object",
                "description": "With memory=remember: what to keep.",
                "required": ["label", "text"],
                "properties": {
                    "label": {"type": "string",
                              "description": "Short stable kebab-case key for it."},
                    "text": {"type": "string",
                             "description": "It as one sentence, in the user's words."}},
            },
        }},
    }}
