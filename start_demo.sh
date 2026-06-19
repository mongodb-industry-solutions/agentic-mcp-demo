#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# One-shot launcher for the browser demo: the web shell + both live
# dashboards, all three behind the shared Basic-Auth gate (web/auth.py).
#
#   ./start_demo.sh
#
# Activate your venv first (so `python` and `uv` resolve to the project
# environment), then run this. Ctrl-C stops all three.

set -uo pipefail
cd "$(dirname "$0")"

# ── Required environment ──────────────────────────────────────────────────
: "${MONGODB_URI:?MONGODB_URI is not set}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is not set}"
[ -n "${VOYAGE_API_KEY:-}" ] || echo "⚠  VOYAGE_API_KEY not set — restaurant_guide embedding will be unavailable."

PY="${PYTHON:-python}"
LOGDIR="${DEMO_LOG_DIR:-./logs}"
mkdir -p "$LOGDIR"

AUTH_USER="${SHELL_AUTH_USER:-mdb}"
AUTH_PASS="${SHELL_AUTH_PASS:-mdbagentic2026}"

pids=()
_stopped=0
cleanup() {
  [ "$_stopped" -eq 1 ] && return
  _stopped=1
  echo
  echo "Stopping…"
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo "All stopped."
}
trap cleanup INT TERM EXIT

start() {  # name  port  script
  local name=$1 port=$2 script=$3
  "$PY" "$script" > "$LOGDIR/$name.log" 2>&1 &
  pids+=($!)
  printf '  • %-14s http://localhost:%s   (log: %s/%s.log)\n' "$name" "$port" "$LOGDIR" "$name"
}

echo "🧠 Starting Agentic AI demo (web shell + dashboards)…"
echo
start shell         8070 web/shell.py
start ibn_dashboard 8060 web/ibn_dashboard.py
start dtw_dashboard 8080 web/dtw_dashboard.py

# Give them a beat to bind / fail fast, then check they're still alive.
sleep 3
for pid in "${pids[@]}"; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo
    echo "❌ A service exited during startup. Check the logs in $LOGDIR/:"
    tail -n 20 "$LOGDIR"/*.log
    exit 1
  fi
done

if [ -n "${SHELL_AUTH_DISABLE:-}" ]; then
  LOGIN="(auth disabled)"
else
  LOGIN="login: ${AUTH_USER} / ${AUTH_PASS}"
fi

cat <<EOF

✓ All three are up.

  Web shell:      http://localhost:8070     ${LOGIN}
  IBN dashboard:  http://localhost:8060      (same login)
  DTW dashboard:  http://localhost:8080      (same login)

Open the web shell, then use the "📊 IBN/DTW dashboard" links in its
banner — they carry your session token so the dashboards mirror your
own isolated demo lane. (Each dashboard prompts for the login once, the
first time you open it — separate browser origins.)

Press Ctrl-C to stop everything.
EOF

wait
