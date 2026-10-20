#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Telemetry Simulator — Push-Button Scenario Injection

Drives synthetic telemetry into ibn_telemetry (a MongoDB time-series
collection) so the dashboard feels live during demos. Push-button: the
operator triggers scenarios on cue rather than running a continuous
simulation. Writes also fire ibn_compliance_events that the dashboard
picks up via Change Streams.

Use this service when users say:
- Inject:   "inject morning rush", "inject morning rush at <site>",
           "trigger violation", "simulate <scenario>", "stress test <site>"
- Live rush: "start the morning rush", "start the morning rush at <site>",
           "let the store open", "run a realistic rush" (live telemetry; the
           SLO monitor detects any violation itself)
- Baseline: "seed baseline telemetry", "seed telemetry for <intent>",
           "send healthy samples"
- Reset:   "reset telemetry", "clear simulated data", "wipe telemetry"

This service does NOT diagnose violations, apply runbooks, or update intent
status (the assurance service does that). It only writes raw telemetry and
violation events.
"""

# Thin FastMCP shim: the tool code lives in oss_tools/ so the Atlas Agent
# Engine agents can register the same functions (AGENT_ENGINE_PLAN.md,
# Phase 1). This docstring stays here: the registry embeds it for routing.

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oss_tools.ibn import telemetry  # noqa: E402
from oss_tools.mcp_shim import serve  # noqa: E402

if __name__ == "__main__":
    serve("ibn_telemetry_simulator", telemetry.tools)
