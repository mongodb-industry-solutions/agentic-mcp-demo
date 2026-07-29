#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# Start the whole browser demo DETACHED: web shell (:8070) + IBN
# dashboard (:8060) + DTW dashboard (:8080). PIDs are written to run/ so
# bin/stop.sh can find them; logs go to logs/. Idempotent — already
# running services are left alone. Activate your venv first (or set
# PYTHON=/path/to/venv/bin/python).

set -u
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

: "${MONGODB_URI:?MONGODB_URI is not set}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is not set}"
[ -n "${VOYAGE_API_KEY:-}" ] || \
    echo "⚠  VOYAGE_API_KEY not set — restaurant_guide embedding unavailable."

mkdir -p "$LOGDIR" "$RUNDIR"

echo "🧠 Starting Agentic AI demo (detached)…"
for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name port script <<< "$entry"
    if pid="$(running_pid "$name")"; then
        echo "  • $name already running (pid $pid) — skipping"
        continue
    fi
    nohup "$PYTHON" "$ROOT/$script" > "$LOGDIR/$name.log" 2>&1 &
    echo $! > "$(pidfile "$name")"
    printf '  • started %-14s pid %-6s http://localhost:%s   (log: %s/%s.log)\n' \
        "$name" "$!" "$port" "$LOGDIR" "$name"
done

# Give them a beat to bind / fail fast, then verify each is still alive.
sleep 3
ok=1
for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name port script <<< "$entry"
    if ! running_pid "$name" >/dev/null; then
        echo
        echo "❌ $name exited during startup — last 15 log lines:"
        tail -n 15 "$LOGDIR/$name.log" 2>/dev/null
        rm -f "$(pidfile "$name")"
        ok=0
    fi
done
[ "$ok" = 1 ] || { echo; echo "Startup failed. See $LOGDIR/."; exit 1; }

cat <<EOF

✓ All services up (detached). Stop with: bin/stop.sh   (restart: bin/restart.sh)

  Web shell:      http://localhost:8070
  IBN dashboard:  http://localhost:8060
  DTW dashboard:  http://localhost:8080

Open the shell, then use its banner "📊 IBN/DTW dashboard" links — they
carry your session token so the dashboards mirror your own lane.

Behind nginx (agentic.example.com): start with DEMO_BIND_HOST=127.0.0.1 so
the ports are reachable only through the proxy.
EOF
