#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

import logging, asyncio, os, sys, readline, datetime
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.markdown import Markdown
from rich import box
from agents.orchestrator import OrchestratorAgent, BROADCAST_RECEIVE_URL

console = Console()
history_file = os.path.expanduser("~/.agentic_demo_history")

def show_banner():
    banner = """
# 🧠 Agentic AI Demo

**Multi-Agent System with MCP + MongoDB Atlas + Voyage AI**

## Features
- Semantic Routing (Vector Search)
- Long/Short-Term Agentic Memory (MongoDB)
- Multi-Domain (Identity, Finance, Lifestyle)
- Live Broadcast to Guest Devices

## Commands
- Type queries naturally
- `status` - System health
- `memory` - View stored memories
- `exit` - Quit
"""
    console.print(Panel(Markdown(banner), border_style="green", box=box.DOUBLE))
    console.print(f"📱 [bold bright_cyan]Live Feed:[/] "
                  f"[cyan]{BROADCAST_RECEIVE_URL}\n", style="dim")

async def show_status(agent):
    table = Table(title="System Status", box=box.ROUNDED)
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="green")

    table.add_row("MCP Servers", f"{len(agent.sessions)} active")
    for name in agent.sessions.keys():
        table.add_row(f"  ↳ {name}", "✓ Online")

    table.add_row("Memory Store", "MongoDB Atlas")
    table.add_row("Broadcast", "ntfy.sh")

    console.print(table)

async def show_memories(agent):
    try:
        from pymongo import MongoClient
        client = MongoClient(os.environ["MONGODB_URI"])
        memories = list(
            client["agent_registry"]["episodic_memories"]
            .find({}, {"_id": 0, "text": 1, "category": 1, "createdAt": 1, "is_temporary": 1})
            .limit(10)
        )

        if memories:
            table = Table(title="🧠 Stored Memories", box=box.SIMPLE)
            table.add_column("Timestamp", style="dim")
            table.add_column("Memory", style="cyan")
            table.add_column("Category", style="magenta")
            table.add_column("Type", style="yellow")

            for mem in memories:
                created_at = mem.get('createdAt', None)
                if isinstance(created_at, datetime.datetime):
                    ts = created_at.isoformat()[:19]
                else:
                    ts = 'unknown'
                mem_type = "Temporary" if mem.get('is_temporary') else "Permanent"
                table.add_row(ts, mem['text'], mem.get('category', 'N/A'), mem_type)
            console.print(table)
        else:
            console.print("[yellow]No memories stored yet[/]")

        client.close()
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")

async def interactive_loop():
    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[red]❌ Missing OPENAI_API_KEY[/]")
        return

    if not os.environ.get("MONGODB_URI"):
        console.print("[red]❌ Missing MONGODB_URI[/]")
        return

    try:
        readline.read_history_file(history_file)
    except FileNotFoundError:
        pass

    show_banner()

    # ✅ NUR EINE Initialisierung!
    async with OrchestratorAgent() as agent:
        console.print("\n[bold green]✓ Ready for queries![/]\n")

        PROMPT = "\001\033[1;34m\002You:\001\033[0m\002 "

        while True:
            try:
                user_input = input(PROMPT).strip()

                if not user_input:
                    continue

                if user_input.lower() in ['exit', 'quit']:
                    console.print("\n[yellow]👋 Goodbye![/]")
                    break

                if user_input.lower() == 'status':
                    await show_status(agent)
                    continue

                if user_input.lower() == 'memory':
                    await show_memories(agent)
                    continue

                with console.status("[dim]Thinking...[/]"):
                    response = await agent.process_query(user_input)

                if response is None:
                    response = "I encountered an issue processing your request."

                console.print(Panel(
                    response,
                    title="🤖 Agent Response",
                    border_style="green",
                    box=box.ROUNDED
                ))

            except KeyboardInterrupt:
                console.print("\n[dim](Use 'exit' to quit)[/]")
                continue
            except EOFError:
                break
            except Exception as e:
                console.print(f"[red]❌ Error: {e}[/]")

if __name__ == "__main__":
    try:
        asyncio.run(interactive_loop())
    except KeyboardInterrupt:
        console.print("\n[yellow]Session interrupted.[/]")
    finally:
        readline.write_history_file(history_file)
