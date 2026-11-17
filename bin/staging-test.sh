#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# End-to-end test of the staging demo on Atlas Agent Engine
# (agent-engine/tests/staging_e2e.py). The test runs inside the staging
# shell pod, detached, so it doesn't depend on this laptop: close the lid or
# reboot, then fetch the results.
#
#   bin/staging-test.sh [full|ibn|memory] [--cap] [--token T] [--token-b T] [--approve-after S]
#                        copy the test into the pod, start it, follow it
#                        until its SUMMARY line (Ctrl-C stops following,
#                        not the test)
#   bin/staging-test.sh results    print the last run's output
#
# Needs kubectl access to the staging cluster (kanopy-oidc kube login).
# The test uses the shell's own service account; it reads no secrets.
# `--cap` leaves 6 sessions open for 30 minutes (the shell's idle timeout).
set -euo pipefail

CONTEXT="${STAGING_CONTEXT:-api.staging.corp.mongodb.com}"
NAMESPACE="${STAGING_NAMESPACE:-industrysolutions}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST="$ROOT/agent-engine/tests/staging_e2e.py"
REMOTE=/tmp/staging_e2e.py
OUT=/tmp/staging_e2e.out

k() { kubectl --context "$CONTEXT" -n "$NAMESPACE" "$@"; }

pods="$(k get pods -o name)" || { echo "❌ kubectl failed (kanopy-oidc kube login?)" >&2; exit 1; }
pod="$(printf '%s\n' "$pods" | grep '^pod/agentic-mcp-demo-shell-web-app-' | head -n 1 || true)"
[ -n "$pod" ] || { echo "❌ no staging shell pod in $NAMESPACE" >&2; exit 1; }

if [ "${1:-}" = "results" ]; then
    k exec "$pod" -- cat "$OUT"
    exit 0
fi

suite="${1:-full}"
case "$suite" in full|ibn|memory) shift || true ;; -*) suite=full ;; *) echo "unknown suite: $suite" >&2; exit 2 ;; esac
args="--suite $suite"
for a in "$@"; do args="$args $(printf '%q' "$a")"; done

k exec -i "$pod" -- sh -c "cat > $REMOTE" < "$TEST"
# shellcheck disable=SC2086  # $args is built from quoted words above
k exec "$pod" -- sh -c "cd /app && nohup python -u $REMOTE $args > $OUT 2>&1 < /dev/null & echo started"
echo "• running in $pod ($args); output in $OUT there. Ctrl-C stops following, not the test."

lines=0
while sleep 10; do
    out="$(k exec "$pod" -- cat "$OUT" 2>/dev/null || true)"
    now="$(printf '%s\n' "$out" | wc -l)"
    printf '%s\n' "$out" | tail -n "+$((lines + 1))" | cut -c1-240
    lines=$now
    if printf '%s' "$out" | grep -q 'SUMMARY'; then
        printf '%s' "$out" | grep -q '^\[.*\] SUMMARY \([0-9]*\)/\1 passed' && exit 0 || exit 1
    fi
done
