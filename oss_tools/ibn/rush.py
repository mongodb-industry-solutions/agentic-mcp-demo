#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Morning-rush telemetry model for the self-healing network
(SELF_HEALING_PLAN.md, S1 "Sense").

A store's morning rush as live, multi-metric telemetry, instead of
inject_event's eight samples plus a declared violation. The curve follows
the seeded Schwabing incident (INC-2026-04-13-MUC-SCH): customers arrive
from 09:00, POS latency reaches about the SLO + 11 ms (51 ms against 40)
while uplink utilization stays under 30 %, and the rush is over by 09:45.

The model reads the intent's applied runbooks on every sample, so a fix
takes effect in the data: with RB-007 (POS to the EF queue + PIR uplift)
in place, POS latency stays at 25-30 ms, as the runbook says, and a
repeat rush stays green on its own.

Pure: no database access. The IBN dashboard plays a rush
(web/ibn_assurance.py) and writes the samples; time is compressed to
SECONDS_PER_SIM_MINUTE.
"""

import math
import random

SIM_START_MIN = 8 * 60 + 55      # 08:55, five quiet minutes before opening
SIM_END_MIN = 10 * 60 + 5        # 10:05: a 20-minute cool-down lets the detector see the
                                 # SLO recover before the stream stops
SECONDS_PER_SIM_MINUTE = 1.5     # 70 simulated minutes in 105 s
TICK_S = 1.0                     # one sample per metric per second

METRICS = ("pos_latency_ms", "uplink_util_pct", "be_queue_depth_pkts",
           "guest_throughput_mbps")

# Runbooks that take POS out of the best-effort queue (seed/ibn_seed.py).
MITIGATING_RUNBOOKS = {"RB-007"}

BASE_LATENCY_MS = 22.0           # quiet-store POS round trip
PEAK_OVER_SLO_MS = 11.0          # Schwabing: 51 ms against a 40 ms SLO


def duration_s() -> float:
    return (SIM_END_MIN - SIM_START_MIN) * SECONDS_PER_SIM_MINUTE


def sim_minute(elapsed_s: float) -> float:
    return SIM_START_MIN + elapsed_s / SECONDS_PER_SIM_MINUTE


def sim_clock(elapsed_s: float) -> str:
    m = int(sim_minute(elapsed_s))
    return f"{m // 60:02d}:{m % 60:02d}"


def _ramp(x: float) -> float:
    """Smoothstep from 0 to 1 over x in [0, 1]."""
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def load(minute: float) -> float:
    """Customer load 0..1: opening at 09:00, full from 09:10 to 09:35, gone
    by 09:45, with a 0.15 floor (staff, background traffic)."""
    up = _ramp((minute - (9 * 60)) / 10)
    down = 1 - _ramp((minute - (9 * 60 + 35)) / 10)
    return 0.15 + 0.85 * min(up, down)


def mitigated(intent: dict) -> bool:
    """True once a runbook that moves POS to the EF queue was applied."""
    return any(h.get("event") == "runbook_applied" and h.get("runbook_id") in MITIGATING_RUNBOOKS
               for h in intent.get("history") or [])


def threshold(intent: dict) -> float:
    return ((intent.get("parsed") or {}).get("targets") or {}).get("pos_latency_ms", 40)


def sample(intent: dict, elapsed_s: float, rng: random.Random | None = None) -> dict[str, float]:
    """One value per METRICS entry at `elapsed_s` into the rush."""
    rng = rng or random.Random()
    ld = load(sim_minute(elapsed_s)) * (1 + rng.uniform(-0.04, 0.04))
    util = 10 + 16 * ld + rng.gauss(0, 0.8)           # stays under 30 %: not a capacity problem
    if mitigated(intent):
        # EF queue: POS no longer waits behind bulk camera uploads.
        latency = BASE_LATENCY_MS + 6 * ld + rng.gauss(0, 1.0)
        queue = 2 + 10 * ld + rng.gauss(0, 1.0)
        guest = 80 - 22 * ld + rng.gauss(0, 1.5)        # the runbook's accepted side effect
    else:
        peak = threshold(intent) + PEAK_OVER_SLO_MS
        latency = BASE_LATENCY_MS + (peak - BASE_LATENCY_MS) * math.pow(ld, 1.6) + rng.gauss(0, 1.2)
        queue = 4 + 140 * ld * ld + rng.gauss(0, 3.0)   # head-of-line blocking in best effort
        guest = 80 - 8 * ld + rng.gauss(0, 1.5)
    return {
        "pos_latency_ms": round(max(5.0, latency), 1),
        "uplink_util_pct": round(max(1.0, util), 1),
        "be_queue_depth_pkts": round(max(0.0, queue), 1),
        "guest_throughput_mbps": round(max(1.0, guest), 1),
    }
