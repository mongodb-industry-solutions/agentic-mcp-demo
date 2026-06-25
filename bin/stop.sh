#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# Stop the whole browser demo: the three web-server processes, plus any
# leftover MCP server subprocesses they spawned. Uses pidfiles when
# present, falls back to matching the exact script path. SIGTERM first
# (so the shell's lifespan shutdown closes MCP sessions cleanly), then
# SIGKILL for anything that won't go.

set -u
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

# Kill the given PIDs gently, then forcibly. Args: pid...
_kill_pids() {
    local pids="$*" p still i
    [ -n "$pids" ] || return 0
    kill $pids 2>/dev/null
    for i in 1 2 3 4 5; do
        sleep 1
        still=""
        for p in $pids; do kill -0 "$p" 2>/dev/null && still="$still $p"; done
        [ -n "$still" ] || return 0
        pids="$still"
    done
    for p in $pids; do
        kill -0 "$p" 2>/dev/null && { echo "    force-killing $p"; kill -9 "$p" 2>/dev/null; }
    done
}

stop_one() {
    local name="$1" script="$2" pf pid
    pf="$(pidfile "$name")"
    pid=""
    [ -f "$pf" ] && pid="$(cat "$pf" 2>/dev/null)"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        # No live pidfile — match the exact script path instead.
        pid="$(pgrep -f "$ROOT/$script" 2>/dev/null | tr '\n' ' ')"
    fi
    if [ -z "${pid// /}" ]; then
        echo "  • $name not running"
        rm -f "$pf"
        return
    fi
    echo "  • stopping $name (pid$pid)"
    _kill_pids $pid
    rm -f "$pf"
}

echo "Stopping Agentic AI demo…"
for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name port script <<< "$entry"
    stop_one "$name" "$script"
done

# Sweep orphaned MCP server subprocesses (uv run <ROOT>/mcp_servers/*.py).
# A clean shell shutdown closes these itself; this catches crash
# leftovers. Scoped to THIS repo's path so nothing else is touched.
orphans="$(pgrep -f "$ROOT/mcp_servers/" 2>/dev/null | tr '\n' ' ')"
if [ -n "${orphans// /}" ]; then
    echo "  • sweeping leftover MCP servers (pid$orphans)"
    _kill_pids $orphans
fi

echo "All stopped."
