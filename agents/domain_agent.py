#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
DomainAgent — a specialist agent that owns one domain's MCP services.

Phase 1 of MULTI_AGENT_PLAN.md. A DomainAgent runs its own ReAct loop
over only its domain's MCP tools, with its own system prompt. It shares
the host orchestrator's infrastructure (stdio session pool, OpenAI
client, tool cache, broadcast) but NOT its routing or memory layers —
context (workstream block, recalled memories, preferences, replay
recipe) is prepared by the shell and passed in via AgentContext, and the
tool-call audit flows back via AgentResult. Tool execution goes through
the host's per-session locks so two agents can run concurrently against
the shared pool.

Agent definitions (name, domain, description, system_prompt) live in
agents/catalog/. The description doubles as the agent card text synced
to the vector-indexed agent_registry.agent_cards collection.
"""

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class AgentContext:
    """Per-turn context prepared by the shell and injected into the
    agent's system prompt. Mirrors the context blocks the legacy ReAct
    loop assembles (see react.py)."""
    workstream_block: str = ""
    memory_block: str = ""
    preferences_block: str = ""
    replay_recipe: str = ""
    conversation_tail: List[Dict] = field(default_factory=list)


@dataclass
class AgentResult:
    """What a DomainAgent hands back to the shell after a turn."""
    answer: str
    verbatim: bool = False          # VERBATIM tool short-circuit fired
    status: str = "ok"              # ok | error
    iteration: int = 0
    max_iterations: int = 0
    tool_calls_count: int = 0
    services_used: List[str] = field(default_factory=list)
    # Tool-call audit trail: [{server, tool, args, result}] — consumed by
    # the shell for workstream attachment and (later) agent analytics.
    audit: List[Dict] = field(default_factory=list)


class DomainAgent:
    """A domain-scoped specialist with its own ReAct loop.

    The loop semantics deliberately mirror the legacy loop in react.py
    (max 5 iterations, 8 with a replay recipe; parallel_tool_calls=False;
    VERBATIM short-circuit; forced final answer at iteration exhaustion)
    so the Phase 1 feature-flag path behaves like the legacy path.
    """

    def __init__(self, host, *, name: str, domain: str,
                 description: str, system_prompt: str):
        self.host = host                # OrchestratorAgent (shared infra)
        self.name = name                # e.g. "ibn_agent"
        self.domain = domain            # e.g. "ibn"
        self.description = description  # agent card text (vector-indexed)
        self.system_prompt = system_prompt

    # Per-task server cap: domains with more services than this get
    # narrowed by an in-domain Stage 2 vector search; smaller domains
    # (today's 5-service IBN/DTW domains) activate everything.
    MAX_SERVERS_PER_TASK = 5

    async def servers_in_domain(self) -> List[str]:
        """All registered MCP services claimed by this agent's domain."""
        cursor = self.host.collection.find(
            {"domain": self.domain}, {"server_name": 1})
        return sorted([d["server_name"] async for d in cursor])

    async def _select_servers(self, task: str) -> List[str]:
        """Stage 2, inside the agent (Phase 2 of MULTI_AGENT_PLAN.md):
        decide which of the domain's servers to activate for THIS task.
        Small domains take all servers; larger ones are narrowed by
        $vectorSearch over mcp_services pre-filtered to this domain —
        the same Atlas primitive the shell used to apply globally, now
        scoped to the agent's own catalog slice."""
        names = await self.servers_in_domain()
        if len(names) <= self.MAX_SERVERS_PER_TASK:
            return names
        try:
            hits = await self.host._semantic_search(
                task, limit=self.MAX_SERVERS_PER_TASK,
                domains=[self.domain])
            selected = [h["server_name"] for h in hits
                        if h.get("server_name") in set(names)]
            if selected:
                await self.host._broadcast("ROUTING",
                    f"[{self.name}] in-domain Stage 2 narrowed "
                    f"{len(names)} → {len(selected)} servers")
                return selected
        except Exception as e:
            print(f"⚠️ [{self.name}] in-domain Stage 2 failed, "
                  f"activating all domain servers: {e}")
        return names

    async def _activate_domain_servers(self, task: str) -> List[str]:
        """Activate the servers selected for this task. Returns the
        active names."""
        names = await self._select_servers(task)
        matches = self.host._resolve_server_paths(names)
        if matches:
            await self.host._broadcast("ROUTING",
                f"[{self.name}] domain servers: "
                + ", ".join(m["server_name"] for m in matches))
        await self.host._activate_servers(matches)
        return [m["server_name"] for m in matches
                if m["server_name"] in self.host.sessions]

    async def _tools_for(self, name: str) -> List[Dict]:
        """OpenAI tool dicts for one server, via the host's shared cache
        (same prefixing scheme as the legacy loop)."""
        host = self.host
        if name not in host.tool_cache:
            t_list = await host.sessions[name].list_tools()
            host.tool_cache[name] = [
                {"type": "function", "function": {
                    "name": f"{name}__{t.name}",
                    "description": t.description,
                    "parameters": t.inputSchema,
                }}
                for t in t_list.tools
            ]
        return host.tool_cache[name]

    async def run(self, task: str, context: AgentContext,
                  on_tool_call: Optional[Callable] = None) -> AgentResult:
        """Run the agent's ReAct loop for one task.

        on_tool_call(srv, tool, result_text) — optional async hook the
        shell uses for workstream attachment and stickiness; called after
        every successful tool execution.
        """
        host = self.host

        active = await self._activate_domain_servers(task)
        if not active:
            return AgentResult(
                answer=(f"I couldn't activate any services for the "
                        f"'{self.domain}' domain right now."),
                status="error")

        openai_tools: List[Dict] = []
        for name in active:
            openai_tools.extend(await self._tools_for(name))

        system = (self.system_prompt
                  + context.workstream_block
                  + context.memory_block
                  + context.preferences_block
                  + context.replay_recipe)
        messages: List = [{"role": "system", "content": system}]
        messages.extend(context.conversation_tail)
        messages.append({"role": "user", "content": task})

        max_iterations = 8 if context.replay_recipe else 5
        iteration = 0
        tool_calls_count = 0
        services_used: List[str] = []
        audit: List[Dict] = []
        initial_answer = "I have no response."

        while iteration < max_iterations:
            iteration += 1
            await host._broadcast("AGENT",
                f"[{self.name}] iteration {iteration}/{max_iterations}")

            response = await host.openai.chat.completions.create(
                model=host.model,
                messages=messages,
                tools=openai_tools,
                parallel_tool_calls=False
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                await host._broadcast("AGENT",
                    f"[{self.name}] no more tool calls, agent finished")
                initial_answer = msg.content or "I have no response."
                break

            messages.append(msg)

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                fname = tc.function.name

                res_txt = "Error"

                srv, tool = fname.split("__", 1)
                await host._broadcast("ACTION",
                    f"  [{self.name}] {srv} → {tool}")
                if srv in host.sessions:
                    r = await host._call_tool_locked(srv, tool, args)
                    res_txt = r.content[0].text
                    tool_calls_count += 1
                    if srv not in services_used:
                        services_used.append(srv)
                    audit.append({"server": srv, "tool": tool,
                                  "args": args, "result": res_txt})
                    await host._broadcast("RESULT",
                        host._format_result_preview(res_txt))
                    if on_tool_call is not None:
                        try:
                            await on_tool_call(srv, tool, res_txt)
                        except Exception as e:
                            print(f"⚠️ on_tool_call hook failed: {e}")
                    if res_txt.startswith("VERBATIM:"):
                        return AgentResult(
                            answer=res_txt[len("VERBATIM:\n"):],
                            verbatim=True,
                            iteration=iteration,
                            max_iterations=max_iterations,
                            tool_calls_count=tool_calls_count,
                            services_used=services_used,
                            audit=audit)
                else:
                    print(f"  ❌ Service '{srv}' NOT in active sessions!")
                    print(f"  Available: {list(host.sessions.keys())}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(res_txt)
                })

        # If max iterations reached, force final answer (mirrors legacy)
        if iteration >= max_iterations:
            print("⚠️ Max iterations reached, forcing final answer")
            messages.append({"role": "user",
                             "content": "Provide your final answer now."})
            final = await host.openai.chat.completions.create(
                model=host.model, messages=messages
            )
            initial_answer = (final.choices[0].message.content
                              or "Max iterations reached.")

        return AgentResult(
            answer=initial_answer,
            iteration=iteration,
            max_iterations=max_iterations,
            tool_calls_count=tool_calls_count,
            services_used=services_used,
            audit=audit)
