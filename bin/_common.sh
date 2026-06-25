#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# Shared config + helpers for bin/start.sh, bin/stop.sh, bin/restart.sh.
# Sourced, not executed. Portable bash (works on NetBSD's pkgsrc bash).

# Repo root, derived from this file's location (bin/ -> root). All
# services run with CWD = root so the orchestrator's relative
# `mcp_servers/` lookup resolves.
_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$_COMMON_DIR/.." && pwd)"
cd "$ROOT" || exit 1

PYTHON="${PYTHON:-python}"
LOGDIR="${DEMO_LOG_DIR:-$ROOT/logs}"
RUNDIR="${DEMO_RUN_DIR:-$ROOT/run}"
AUTH_USER="${SHELL_AUTH_USER:-mdb}"
AUTH_PASS="${SHELL_AUTH_PASS:-mdbagentic2026}"

# The web-server processes to manage:  name | port | script (rel. to ROOT)
SERVICES=(
  "shell|8070|web/shell.py"
  "ibn_dashboard|8060|web/ibn_dashboard.py"
  "dtw_dashboard|8080|web/dtw_dashboard.py"
)

pidfile() { echo "$RUNDIR/$1.pid"; }

# Echo the live PID for a service (from its pidfile) and return 0, or
# return 1 if it isn't running.
running_pid() {
  local pf pid
  pf="$(pidfile "$1")"
  [ -f "$pf" ] || return 1
  pid="$(cat "$pf" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid"; return 0
  fi
  return 1
}
