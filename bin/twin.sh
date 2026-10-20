#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# The Agent Engine twin of the browser demo (AGENT_ENGINE_PLAN.md, until
# Phase 8): you chat with the deployed noc-coordinator (or ibn-agent /
# dtw-agent) in the Atlas Agent Engine UI, and these two dashboards watch
# the agents' cluster. Single user, default lane (the chat UI can't send
# the demo-prefix header). The Kanopy demo and bin/start.sh are untouched:
# other ports, other pidfiles, and a guard against the legacy database.
#
#   bin/twin.sh start    IBN dashboard :8160, DTW dashboard :8180 (detached)
#   bin/twin.sh stop
#   bin/twin.sh status
#   bin/twin.sh reset    reseed the default lane's demo collections and
#                        clear its approvals/routing decisions (platform
#                        memory stays: there is no delete-by-user API)
#
# TWIN_MONGODB_URI is the agents' cluster (the workspace MONGODB_URI).

set -u
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

: "${TWIN_MONGODB_URI:?TWIN_MONGODB_URI is not set (the cluster of the Agent Engine agents)}"

_host() { "$PYTHON" -c 'import sys, urllib.parse as u; print(u.urlsplit(sys.argv[1]).hostname or "")' "$1"; }
if [ -n "${MONGODB_URI:-}" ] && [ "$(_host "$TWIN_MONGODB_URI")" = "$(_host "$MONGODB_URI")" ]; then
    echo "❌ TWIN_MONGODB_URI points at the same cluster as MONGODB_URI (the legacy demo). Refusing."
    exit 1
fi

TWIN_SERVICES=(
  "twin_ibn_dashboard|8160|web.ibn_dashboard:app"
  "twin_dtw_dashboard|8180|web.dtw_dashboard:app"
)

export PYTHONUNBUFFERED=1
mkdir -p "$LOGDIR" "$RUNDIR"

start() {
    local entry name port app ok=1
    for entry in "${TWIN_SERVICES[@]}"; do
        IFS='|' read -r name port app <<< "$entry"
        if pid="$(running_pid "$name")"; then
            echo "  • $name already running (pid $pid) — skipping"
            continue
        fi
        MONGODB_URI="$TWIN_MONGODB_URI" nohup "$PYTHON" -m uvicorn "$app" \
            --host "${DEMO_BIND_HOST:-127.0.0.1}" --port "$port" \
            > "$LOGDIR/$name.log" 2>&1 &
        echo $! > "$(pidfile "$name")"
        printf '  • started %-20s pid %-6s http://localhost:%s   (log: %s/%s.log)\n' \
            "$name" "$!" "$port" "$LOGDIR" "$name"
    done
    sleep 3
    for entry in "${TWIN_SERVICES[@]}"; do
        IFS='|' read -r name port app <<< "$entry"
        if ! running_pid "$name" >/dev/null; then
            echo "❌ $name exited during startup — last 15 log lines:"
            tail -n 15 "$LOGDIR/$name.log" 2>/dev/null
            rm -f "$(pidfile "$name")"
            ok=0
        fi
    done
    [ "$ok" = 1 ] || exit 1
    echo
    echo "✓ Twin dashboards up. Chat with noc-coordinator in the Atlas Agent Engine UI."
    echo "  IBN dashboard:  http://localhost:8160   (?mode=exec for the exec view)"
    echo "  DTW dashboard:  http://localhost:8180"
}

stop() {
    local entry name port app pid
    for entry in "${TWIN_SERVICES[@]}"; do
        IFS='|' read -r name port app <<< "$entry"
        if pid="$(running_pid "$name")"; then
            kill "$pid" 2>/dev/null
            echo "  • stopped $name (pid $pid)"
        fi
        rm -f "$(pidfile "$name")"
    done
}

status() {
    local entry name port app pid
    for entry in "${TWIN_SERVICES[@]}"; do
        IFS='|' read -r name port app <<< "$entry"
        if pid="$(running_pid "$name")"; then
            echo "  • $name running (pid $pid) http://localhost:$port"
        else
            echo "  • $name stopped"
        fi
    done
}

reset() {
    MONGODB_URI="$TWIN_MONGODB_URI" "$PYTHON" -c '
import os
from pymongo import MongoClient
from web.seed_runner import _seed_session_sync

client = MongoClient(os.environ["MONGODB_URI"])
try:
    _seed_session_sync(client["agent_registry"], "", print)
finally:
    client.close()
' || return 1
    echo "↺ Reload the dashboard tabs (a dropped scenario stays on screen until then)."
}

case "${1:-}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    reset)  reset ;;
    *) echo "usage: bin/twin.sh start|stop|status|reset"; exit 2 ;;
esac
