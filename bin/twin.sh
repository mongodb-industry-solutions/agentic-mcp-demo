#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# The Agent Engine twin of the browser demo (AGENT_ENGINE_PLAN.md): you
# chat with the deployed noc-coordinator in the web shell on Agent Engine
# (web/twin_shell.py, over the Agent Engine API with your CLI login, or a
# service account when AE_CLIENT_ID/AE_CLIENT_SECRET are set), each browser
# session in its own demo lane, or in the Atlas Agent Engine UI (default
# lane), and the two dashboards watch the agents' cluster. The Kanopy demo
# and bin/start.sh are untouched: other ports, other pidfiles, and a guard
# against the legacy database.
#
#   bin/twin.sh start    web shell :8170, IBN dashboard :8160, DTW dashboard
#                        :8180 (detached, localhost only)
#   bin/twin.sh stop
#   bin/twin.sh status
#   bin/twin.sh reset    reseed the default lane's demo collections and
#                        clear its approvals/routing decisions (platform
#                        memory stays: there is no delete-by-user API)
#   bin/twin.sh autopilot [L4|L3]
#                        foreground, for the default lane (the web shell
#                        responds in its own lanes): set the autonomy level
#                        (default L4) and hand every incident the SLO
#                        monitor opens to the deployed ibn-agent, no human
#                        prompt (agent-engine/autopilot.py,
#                        SELF_HEALING_PLAN.md S2). The monitor runs in the
#                        IBN dashboard, so keep a tab open on that lane.
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
  "twin_shell|8170|web.twin_shell:app"
  "twin_ibn_dashboard|8160|web.ibn_dashboard:app"
  "twin_dtw_dashboard|8180|web.dtw_dashboard:app"
)

export PYTHONUNBUFFERED=1
# The agents' tool sandboxes run in UTC and every writer stores naive
# datetime.now() times, so the twin's processes use UTC too: otherwise
# windows such as the SLO monitor's 60 s duplicate check compare local
# time with UTC. The twin dashboards therefore show UTC.
export TZ=UTC
# The twin's cluster is the only place the system may act on its own (L4):
# the dashboards' autonomy switch and the autopilot check this.
export SELF_HEALING_L4=1
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
    echo "✓ Twin up. Chat with noc-coordinator in the web shell (or the Atlas Agent Engine UI)."
    echo "  Web shell:      http://localhost:8170   (your agentengine CLI login: agentengine auth status)"
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

autopilot() {
    # exec: Ctrl-C and kill reach the autopilot, not just this shell.
    MONGODB_URI="$TWIN_MONGODB_URI" exec "$PYTHON" "$ROOT/agent-engine/autopilot.py" "$@"
}

case "${1:-}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    reset)  reset ;;
    autopilot) shift; autopilot "$@" ;;
    *) echo "usage: bin/twin.sh start|stop|status|reset|autopilot [L4|L3]"; exit 2 ;;
esac
