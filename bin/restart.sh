#!/usr/bin/env bash
#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# Restart the whole demo: stop everything, then start it again. Thin
# wrapper over bin/stop.sh + bin/start.sh; forwards any args/env to both.

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"

"$DIR/stop.sh" "$@"
echo
sleep 1
exec "$DIR/start.sh" "$@"
