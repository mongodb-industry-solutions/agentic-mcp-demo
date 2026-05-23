#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

import logging, asyncio, os, sys, readline, datetime, time
from urllib.parse import urlparse
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.markdown import Markdown
from rich import box
from pymongo import MongoClient
from agents.orchestrator import OrchestratorAgent, BROADCAST_RECEIVE_URL
from agents import history as shell_history

console = Console()

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
- `preferences` - View stored user preferences
- `exit` - Quit
"""
    console.print(Panel(Markdown(banner), border_style="green", box=box.DOUBLE))
    console.print(f"📱 [bold bright_cyan]Live Feed:[/] "
                  f"[cyan]curl -sN {BROADCAST_RECEIVE_URL} | sed -n 's/^data: //p'",
                  style="dim")
    _show_mongo_info()

def _show_mongo_info():
    """Render a single condensed line: MongoDB target + vector indexes."""
    uri = os.environ.get("MONGODB_URI", "")
    parsed = urlparse(uri)
    user = parsed.username or "?"
    host = parsed.hostname or "?"

    vector_idx = []
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        db = client["agent_registry"]
        for coll_name in db.list_collection_names():
            try:
                for idx in db[coll_name].list_search_indexes():
                    if idx.get("type") == "vectorSearch":
                        vector_idx.append(f"{coll_name}.{idx['name']}")
            except Exception:
                pass
        client.close()
    except Exception:
        pass

    idx_text = ", ".join(vector_idx) if vector_idx else "none"
    console.print(f"🍃 [bold bright_cyan]MongoDB:[/] [cyan]{user}@{host}",
                  style="dim")
    console.print(f"🔍 [bold bright_cyan]Vector Indexes:[/] [cyan]{idx_text}\n",
                  style="dim")

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

async def show_preferences(agent):
    try:
        from pymongo import MongoClient
        client = MongoClient(os.environ["MONGODB_URI"])
        prefs = list(
            client["agent_registry"]["user_preferences"]
            .find({}, {"_id": 0, "text": 1, "category": 1, "createdAt": 1, "is_temporary": 1})
            .limit(10)
        )

        if prefs:
            table = Table(title="🧠 User Preferences", box=box.SIMPLE)
            table.add_column("Timestamp", style="dim")
            table.add_column("Preference", style="cyan")
            table.add_column("Category", style="magenta")
            table.add_column("Type", style="yellow")

            for p in prefs:
                created_at = p.get('createdAt', None)
                if isinstance(created_at, datetime.datetime):
                    ts = created_at.isoformat()[:19]
                else:
                    ts = 'unknown'
                p_type = "Temporary" if p.get('is_temporary') else "Permanent"
                table.add_row(ts, p['text'], p.get('category', 'N/A'), p_type)
            console.print(table)
        else:
            console.print("[yellow]No preferences stored yet[/]")

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

    # Ensure Ctrl+R reverse-search is bound — libedit (macOS default) does
    # not always register it automatically the way GNU readline does.
    try:
        readline.parse_and_bind(r'"\C-r": reverse-search-history')
    except Exception:
        pass

    # Seed readline's in-memory history from the shared MongoDB collection
    # so cursor-up works from the first prompt — including across machines.
    try:
        seed = shell_history.read_recent(limit=500)
        # readline.add_history wants oldest-first; read_recent returns
        # newest-first, so reverse.
        for entry in reversed(seed):
            readline.add_history(entry)
    except Exception as e:
        console.print(f"[dim yellow](history seed skipped: {e})[/]")

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

                # Persist into the shared MongoDB history collection so the
                # web shell, and future terminal sessions on any host, see
                # this entry. readline already added it to its in-memory
                # buffer when the user pressed Enter, so cursor-up works
                # within this session for free.
                shell_history.append(user_input, source="terminal")

                if user_input.lower() in ['exit', 'quit']:
                    console.print("\n[yellow]👋 Goodbye![/]")
                    break

                if user_input.lower() == 'status':
                    await show_status(agent)
                    continue

                if user_input.lower() in ('preferences', 'memory'):
                    # 'memory' kept as a backward-compatible alias
                    await show_preferences(agent)
                    continue

                t0 = time.monotonic()
                with console.status("[dim]Thinking...[/]"):
                    response = await agent.process_query(user_input)
                elapsed = time.monotonic() - t0

                if response is None:
                    response = "I encountered an issue processing your request."

                console.print(Panel(
                    Markdown(response),
                    title=f"🤖 Agent Response (time needed: {elapsed:.1f} seconds)",
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
    # Note: history is persisted into MongoDB on every accepted query via
    # shell_history.append, so no file write is needed at exit.
