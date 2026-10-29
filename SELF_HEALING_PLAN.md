# Self-Healing Network: from TM Forum Autonomy Level 3 to Level 4

**Status:** plan, 2026-10-16. S1 (sense + detect), S2 (respond in the twin), S3 (learn + prevent) and S4 (the dashboard) are built, see the "as built" sections at the end. Work continues on `feature/agent-engine-migration` (ISTTH-969).

## The pitch

The morning rush hits the Munich store. Nobody types anything. The network
notices that the POS latency intent is slipping. It finds the incident it
has seen before with one Atlas query, applies the runbook that fixed it last
time, proves the fix from live telemetry, and writes what it learned back
into its own memory. Before tomorrow's rush, it proposes protecting the
sister stores too. That proposal goes to a human, because it changes the
whole fleet.

**One loop, one database: sense, detect, diagnose, decide, act, verify,
learn.** Every step is a document in Atlas, and you can watch each one
happen.

## Where we are, honestly

Today's IBN flow (help page, steps 4–6) is driven by people:

| Step | Who does it today | Reality check |
|---|---|---|
| "inject morning rush" | NOC engineer types it | `inject_event` writes 8 samples **and declares the verdict** itself: it writes the `violation` event and sets the intent to `violated`. Nothing *detects* anything. |
| "diagnose violation" | NOC engineer types it | The hero query is real (`$vectorSearch` + kind + time + geo box). The symptom fingerprint, however, is hard-coded text ("uplink utilization remains low"), not measured. |
| "apply runbook" | NOC engineer types it | Writes one healthy sample and a `recovery` event. Recovery is asserted, not observed. |

Against the TM Forum Autonomous Networks matrix (P = people, S = system):

| Dimension | L3 (today, at best) | **L4 (target)** | Our L4 mechanism |
|---|---|---|---|
| Intent / experience | P | P | The NOC states the intent (TMF921); unchanged |
| Awareness (sense) | P/S | **S** | Morning-rush telemetry ingest plus a continuous SLO evaluator |
| Analysis (diagnose) | P triggers it | **S** | The system calls the hero query itself, with a *measured* fingerprint |
| Decision | P | **S, within an envelope** | Autonomy policy as data; anything outside it goes to a human |
| Execution | P triggers it | **S** | `apply_runbook`, then verified from telemetry |

What keeps this L4 and not L5 is stated openly: people still own the
intent, fleet-wide changes, actions outside the envelope, and novel
incidents with no precedent.

## Design principles (what makes it work and stay credible)

1. **Deterministic where it should be.** SLO breach detection is
   arithmetic (p95 over a window, hysteresis), not an LLM call. The LLM
   reasons over the diagnosis and the runbook; it never decides whether a
   number is above 40.
2. **Bounded autonomy, enforced in code.** The envelope is a policy
   document in Atlas, and the tool checks it server-side. No prompt can
   talk the agent past it.
3. **Recovery is observed, not asserted.** The telemetry generator reads
   the intent's live configuration. Once RB-007's EF mapping is in place,
   latency falls because the simulated queue no longer collides, and the
   detector, not the agent, declares the intent green.
4. **One open incident per intent.** A unique partial index, hysteresis
   and a cooldown prevent duplicate loops and flapping.
5. **A visible kill switch.** The dashboard toggles the autonomy level
   between L3 (detect, diagnose, recommend, wait for a human) and L4 (act
   within the envelope). Running the same rush once in each mode is the
   comparison the audience remembers.
6. **Everything auditable.** The incident document carries the full
   timeline (who or what did each step, when, why), shaped like a TMF642
   alarm plus a TMF921 intent report.

## The closed loop

```
 ☀ morning rush         SENSE / DETECT                 DIAGNOSE / DECIDE / ACT / VERIFY          LEARN / PREVENT
 (telemetry ingest) ─▶  SLO evaluator (every 2 s)  ─▶  IBN agent, autonomous mode             ─▶ new incident chunk
  multi-metric,          p95 window + hysteresis        1. diagnose_violation (hero query)        (auto-embedded)
  config-aware           → ibn_incidents (open)         2. check_autonomy (envelope, code)        → sister-site
                         → violation event              3. apply_runbook                          template proposal
                         → intent: degraded             4. await_recovery (detector says green)   → NOC review (human)
```

### 1. Sense: ingest the morning rush (replaces "inject morning rush")

- **`oss_tools/ibn/rush.py`: a telemetry replay.** Four metrics per site at
  2–4 Hz: `pos_latency_ms`, `uplink_util_pct`, `be_queue_depth`,
  `guest_throughput_mbps`. The curve follows the seeded Schwabing incident
  (09:00–09:45 rush, latency to 51 ms at under 30 % uplink utilization,
  against the opener's 40 ms target).
  70 simulated minutes (08:55 → 10:05, the last 20 a cool-down so the
  detector sees the SLO recover) play in about 105 seconds; the dashboard
  shows the simulated clock and labels it as compressed.
- **Configuration-aware.** Before every sample, the generator reads the
  intent's applied runbooks and policy snapshot. With the POS class still
  in the best-effort queue, latency climbs with load. With EF + PIR uplift
  it stays at 25–30 ms, matching the RB-007 text. That gives causal
  recovery instead of a scripted one, and a second rush ("tomorrow") stays
  green on its own data.
- **Where it runs:** the IBN dashboard process (long-lived, already
  session-aware, already polls `ibn_telemetry`). Kanopy needs no new
  service. Start it from a "☀ Start morning rush" button on the dashboard,
  or by saying "start the morning rush" in the shell (a tool that writes a
  `ibn_sim_runs` request the dashboard picks up).
- **`inject_event` stays** as the old one-shot "L3 mode", so the current
  script keeps working.
- **Production path (slide, not build):** streaming telemetry (gNMI or
  IPFIX) → Kafka → Atlas Stream Processing → the same time-series
  collection.

### 2. Detect: the SLO evaluator

- **`oss_tools/ibn/slo.py`, deterministic.** Every 2 seconds, one
  aggregation over `ibn_telemetry` for the last 30 s, grouped by intent:
  `$percentile` p95 for latency (MongoDB 7.0+; Kanopy runs 9.0.4, the twin
  8.0.34), plus averages for utilization and queue depth.
- **Hysteresis.** A violation is three consecutive breached windows; a
  recovery is three consecutive green ones. State is kept in
  `ibn_slo_state`.
- **Polling is required.** Time-series collections can't be watched (the
  dashboard already polls `ibn_telemetry` for the same reason).
- **On the breach transition:**
  - **Open an incident.** `ibn_incidents` gets
    `{intent_id, status: open, severity, measured: {p95, util, queue},
    timeline: [...]}`, with a unique partial index on
    `{intent_id}` where `status ∈ {open, mitigating}`.
  - **Raise the alarm.** A `violation` compliance event with a *measured*
    fingerprint, and the intent's status set to `violated`.
- **On recovery:** the incident moves to `recovered`, MTTR is stamped, and
  the intent goes back to `active`.
- **Where it runs:** next to the generator in the dashboard process; one
  evaluator per lane with live intents.

### 3. Diagnose, decide, act, verify: the autonomous responder

**Trigger.** A change stream on `ibn_incidents` (a regular collection, so
it can be watched) fires on each new open incident.

**Legacy runtime (Kanopy).**
- **Runs in the shell.** A per-session task in `web/shell.py` watches that
  lane's incidents. It takes the session lock, so it queues behind a turn
  the user is running, then runs the IBN `DomainAgent` with a
  system-authored task: the alarm, the measured values, and
  "autonomous mode, envelope v1".
- **Shows in the browser.** The output streams into the chat as a new
  `autonomous` message type, with a "🤖 Self-healing: no human prompt"
  banner and Agent Log lines.

**Agent Engine twin.**
- **A local daemon for now.** `bin/twin.sh autopilot` watches the twin
  cluster's incidents and calls ibn-agent through `agentengine invoke`
  with the presenter's CLI login, since there are no platform triggers and
  service accounts are blocked in JeffN.
- **Phase 8 replaces it** with the shell's service account.
- **Each autonomous run is its own session in Agent Engine Traces:** "the
  trace of a repair nobody asked for".

**New tools (in `oss_tools`, so both runtimes get them):**
- **`check_autonomy(intent_id, runbook_id, similarity)`** reads
  `ibn_autonomy_policy` (v1, as data) and returns allow or escalate with
  reasons. Allow means all of these hold:
  - one site;
  - the runbook has at least one recorded success on a past incident;
  - the top match's similarity is at least 0.80 *and* that match names
    this runbook;
  - the runbook is not on the deny list;
  - at most 3 autonomous actions per hour per lane;
  - no change freeze;
  - the dashboard toggle is set to L4.
- **`apply_runbook(…, autonomous=True)`** re-runs the same check inside
  the tool. Outside the envelope it refuses and opens a NOC review instead.
  On Agent Engine this is the Phase 3 approval gate, with a policy that can
  now answer "yes" (the approval is recorded as `decided_by:
  autonomy-policy v1`).
- **`await_recovery(incident_id, timeout_s=60)`** waits until the
  *detector* marks the incident recovered. On timeout it escalates:
  status `escalated`, NOC review.

**Prompt.** The autonomous task is short and fixed: diagnose → check
autonomy → act or escalate → await recovery → report in three lines.
Model choice never widens the envelope: picking a runbook other than the
top match fails the check.

### 4. Learn and prevent

- **Learn.** On recovery, the responder writes a new `kind: incident`
  document to `ibn_knowledge_chunks`, using the seeded incidents' shape:
  `{site_name, ts, lng, lat, fingerprint (measured), runbook_id, outcome,
  mttr_s, learned: true, lane}`. Its text is generated from the measured
  values. Atlas auto-embeds it, so the next rush at a sister site finds
  *this* incident as its closest precedent, with higher similarity.
  - Index and lanes: the shared collection and its index (also used by the
    live Kanopy demo) are **not changed**. Lanes are separated by a
    `$match` on `lane ∈ {"", <prefix>}` after `$vectorSearch`, and Reset
    deletes a lane's learned documents.
- **Prevent (the predictive part of L4).** The responder lists the other
  active intents on `strict-retail-v3` and proposes
  `update_template_version` as a canary before the next rush window. This
  stays a human decision: fleet blast radius, the existing Phase 3
  canary review. The dashboard says it plainly: "System proposes, NOC
  approves."

### 5. Make it look great: the dashboard

- **Self-healing ribbon:** sense → detect → diagnose → decide → act →
  verify → learn. Each stage lights up from the incident timeline with its
  timestamp, and a live **MTTR counter** reads "38 s · 0 human actions".
- **Latency chart:** the rush band, the SLO line, the p95 window, and
  markers for "breach detected", "runbook applied" and "recovered", all
  from data.
- **Autonomy switch (L3 ⇄ L4):** L3 stops at a recommendation card
  ("awaiting NOC"); L4 acts. Same rush, two outcomes.
- **Incident card** shaped like a TMF642 alarm (perceived severity,
  probable cause filled in after diagnosis), next to the TMF921 intent
  report.
- **Reused:** the existing diagnose modal (the raw pipeline in
  `?mode=eng`) opens automatically when the agent runs the hero query.
  `?mode=exec` shows the narrative only.

## Demo storyboard (about 3 minutes)

1. "Munich Marienplatz is live, intent green." Click **☀ Start morning
   rush**. The clock runs 08:55 → 09:10, and latency climbs while uplink
   utilization stays low.
2. **Autonomy L3:** the breach is detected after three windows, and the
   system diagnoses and recommends RB-007, then *waits*. "That's today's
   best practice: a human in the loop."
3. Reset the lane and switch to **L4**. Same rush. Detect, then the hero
   query fires by itself (the pipeline is on screen), the envelope check
   passes, RB-007 is applied, latency falls on live data, and the
   detector declares green. The MTTR counter stops at about 40 s with 0
   human actions.
4. **Learn:** the new incident appears in the knowledge base. **Prevent:**
   "3 sister stores on strict-retail-v3 face the same rush tomorrow
   (Hamburg, Berlin, Stuttgart; Schwabing already has RB-007); canary
   proposed", and it waits for the NOC.
5. "Tomorrow": run the rush again. Green throughout. The fix holds, on
   data.
6. (Optional) An out-of-envelope case, a site without precedent, is
   escalated with the diagnosis attached. "It knows what it doesn't know."

## Risks and how we handle them

| Risk | Handling |
|---|---|
| Time compression looks fake | Label the simulated clock; keep magnitudes faithful to the seeded incident and the RB-007 text |
| LLM picks the wrong runbook or loops | Envelope check in code; a fixed autonomous prompt; one open incident per intent; a 60 s timeout escalates |
| Duplicate or flapping incidents | Hysteresis (3/3 windows), a unique partial index, cooldown |
| Autonomous run collides with a user's turn | Session lock in the shell; the autonomous run queues and is announced |
| Learned documents leak across visitors | `lane` field, post-filter, Reset deletes them; no vector-index change on the live cluster |
| Cost of one agent run per rush per session | 3–6 LLM calls per run; `DEMO_MAX_SESSIONS` caps it; autonomous actions limited per hour |
| Agent Engine has no triggers; service accounts blocked in JeffN | Twin: local autopilot over the CLI login. Phase 8: the shell's service account |
| Polling the time series | 2 s, one aggregation per lane; the same pattern the dashboard already uses |
| Kanopy production | Legacy changes go through `staging` first; `inject_event` keeps working for the existing script |

## Phasing (realistic: about 7 working days)

| # | Slice | Days | Done when |
|---|---|---|---|
| S1 | Sense + detect: `rush.py`, `slo.py`, `ibn_incidents` and its indexes, measured fingerprint in `diagnose_violation` | 1.5 | A rush opens exactly one incident about 6–8 s after a sustained breach; no flapping in 10 runs |
| S2 | Respond in the twin: autonomy policy plus `check_autonomy` / `apply_runbook(autonomous)` / `await_recovery`, `bin/twin.sh autopilot` | 1.5 | Rush to recovered with 0 human input; an out-of-envelope site escalates; the trace shows the run |
| S3 | Learn + prevent: learned incident chunks (lane-scoped), sister-site canary proposal | 1 | The second site's diagnosis cites the learned incident; the proposal waits for review |
| S4 | Dashboard: ribbon, MTTR, chart markers, L3/L4 switch, TMF642 card | 1.5 | Storyboard steps 1–5 run end to end in both modes |
| S5 | Legacy runtime: shell responder task, `autonomous` message type, help page | 1 | The same storyboard in the browser shell, multi-session |
| S6 | Staging check, then production with the user's go-ahead | 0.5 | Kanopy staging runs the storyboard twice in parallel sessions |

The work builds on the Agent Engine stack (`oss_tools` from Phase 1, the
approval gate from Phase 3, the twin), so its branches sit on top of
`agent-engine/phase-6-governance`. The twin goes first (S2), so we iterate
without risk to the live Kanopy demo. The shared pieces (sim, detector, tools, dashboard) are
runtime-neutral, and only the responder exists twice.

## Beyond L4: what "L4.5" and L5 would take

TM Forum defines levels L0–L5. "L4.5" is not a TMF level, but it is a
common shorthand for L4 in more scenarios with fewer human checkpoints,
on the way to L5. L5 means the system handles every dimension, intent
included, for all services and domains across the whole lifecycle. No
operator claims L5 today; it's a north star. These are the steps that
would move *this* demo there, most credible first:

| Step | What changes | How in this demo | Realism |
|---|---|---|---|
| **1. Predict, don't react** (L4.5) | Mitigate before the breach: zero violations instead of a short MTTR | Seed two weeks of telemetry history; forecast the 08:55–09:45 window per site (`$setWindowFields` over same-weekday history) and apply the proven fix ahead of the rush, inside the envelope | High: data and math we control; about 1.5 days |
| **2. Simulate before touching** (L4.5) | Novel incidents without a precedent are no longer escalated by default | The S1 generator *is* a queue model of the site, i.e. a digital twin. Run candidate runbooks through it, apply the best one only if the twin shows the SLO holding, and record "validated in twin" in the incident | High: reuses S1; about 2 days. The DTW network twin models a mobile operator, so it doesn't fit these stores |
| **3. Fleet rollout without approving each step** (L4.5) | People approve a rollout *policy* once, not every canary | After the canary holds through a real rush, promote in waves (1 → 3 → all) with automatic rollback when any wave breaches; the NOC can stop it at any time | Medium: Phase 3 canary plumbing exists; needs the wave scheduler and rollback; about 2 days |
| **4. Earned autonomy** (L4.5) | The envelope adapts: each runbook's success rate on recovered incidents raises or lowers its autonomy | `ibn_autonomy_policy` gets a trust score per runbook; people set the risk appetite, not the rules | Medium: simple to build; needs enough runs to be meaningful (seeded history) |
| **5. Learn new runbooks** (toward L5) | Repeated successful repairs become a new procedure without anyone writing it | Consolidate learned incidents into a runbook proposal (procedural memory). Platform procedural extraction is broken today (reported 2026-10-16), so do it in our own code, or wait for the fix | Medium; partly blocked by the platform |
| **6. Cross-domain correlation** (L5 trait) | One root cause across fixed retail access (IBN) and mobile (DTW) | Requires a shared topology (for example a common transport/backhaul node). IBN (German retail sites) and DTW (US mobile markets) are separate worlds today, so this needs new seed data | Low without a data redesign; about 3 days or more |
| **7. Intent self-management** (L5: the intent dimension also handled by the system) | The system negotiates and re-plans intents: offers 30 ms or an uplink upgrade when 20 ms isn't feasible, and adjusts targets as business conditions change | Feasibility already returns gaps; add negotiation (TMF921 intent proposals) and re-planning on sustained drift. People set business goals only | Demo-able as a slice; the claim must stay "L5 for one service", not "L5 network" |

**Recommended next step after L4:** steps 1 + 2. Together they give a
clean "L4.5" story: *it saw the rush coming, tested the fix in its twin,
and the store never went red*. Both are built on things we control.
Steps 6–7 are what an honest L5 conversation is about, and the right way
to show them is as a slice for one service.

## Acceptance criteria

- **Detection:** within 10 s of a sustained breach; no detection without
  one.
- **Response:** diagnosis plus action within 30 s of detection;
  recovery verified by the detector within 60 s.
- **Zero touch:** no human input in L4 mode for an in-envelope incident;
  exactly one recommendation and no action in L3 mode.
- **Learning:** the repeat rush stays green, and the learned incident is
  the top match at a sister site.
- **Escalation:** an out-of-envelope incident escalates with its
  diagnosis attached and executes nothing.

## S1 as built (2026-10-16, branch `self-healing/s1-sense-detect`)

- **Sense:**
  - **`oss_tools/ibn/rush.py`**, a pure model: four metrics at 1 Hz, 08:55 → 10:05 in about 105 s (the last 20 simulated minutes are a cool-down, so the monitor sees the SLO recover before the stream stops). The peak is the SLO + 11 ms at 10–26 % uplink utilization. With RB-007 in the intent's history, latency stays at 25–30 ms.
  - **`start_morning_rush`** (IBN telemetry tools; added to the IBN prompt, the routing docstring and ibn-agent's `agent.yaml`) and the dashboard's **☀ Start morning rush** button both write an `ibn_sim_runs` request.
- **Detect:**
  - **`oss_tools/ibn/slo.py`** (pure): p95 over 20 s every 2 s; 3 breached windows open an incident, 3 windows below 0.95 × target close it.
  - **`web/ibn_assurance.py`** runs the rush runner and the evaluator per lane inside the IBN dashboard (so only while a tab watches the lane).
  - **Incidents** live in `ibn_incidents`, one open per intent (a unique partial index on `open`), TMF642-shaped, with a timeline. Opening raises the usual `violation` event and sets the intent to `violated`; closing writes `recovery` with the time to recover. Neither is repeated when `inject_event` or `apply_runbook` wrote one in the last 60 s.
  - **Reset** clears both new collections.
- **Diagnose input:** `diagnose_violation` builds the fingerprint from the measurements (open incident, else the last window) when uplink data exists, and keeps the old text for the `inject_event` path. It writes the diagnose stage, the probable cause and the top match onto the open incident.
- **Verified** on the twin cluster, test lane `s_shs1test01_`, dashboard with the new code:
  - **Unmitigated rushes:** 15 across IBN-001/002/003, each with exactly one incident, opened and then recovered by itself; detection 4.1–7.7 s after the first breached window; recovery 66–70 s after opening.
  - **Mitigated rushes:** 5 on IBN-004 (RB-007), with no incident.
  - **No duplicates:** 15 violation and 15 recovery events in total.
  - **Tool path:** `start_morning_rush` → incident → measured diagnosis → recovery.
  - **L3 path:** `inject_event` → incident without a duplicate alarm → `apply_runbook` → closed with one recovery event.
  - **Similarity:** the measured fingerprint finds the same matches as the template (Schwabing 0.726 vs 0.74, Berlin 0.751 vs 0.758).
- **Found on the way (fixed separately, #39):** the seeded incidents had aged out of the hero query's 180-day window, so Marienplatz got RB-003 instead of RB-007.
- **Input for S2:** Berlin's nearest precedent is a camera-uplink/PoE incident (RB-005, similarity 0.75), a weak match. The envelope's similarity threshold (0.80) should escalate it, which makes it the natural "it knows what it doesn't know" case.

## S2 as built (2026-10-20, branch `feature/agent-engine-migration`)

- **The envelope as data:** `oss_tools/ibn/autonomy.py`.
  - One policy document per lane in `ibn_autonomy_policy`. Defaults (v1): level **L3**, similarity ≥ 0.80, at most 3 autonomous actions per hour, no deny list, no change freeze. Reset clears it, so a lane starts at L3.
  - `evaluate()` is the only decision point. It reads the open incident, its recorded diagnosis (top match, runbook, similarity), the knowledge base (the runbook resolved a past incident) and the lane's past actions, never the model's arguments.
  - Each decision lands once on the incident's timeline (stage `decide`, by `autonomy-policy v1`). An escalation sets the status `escalated` with its reasons.
- **The tools** (in `oss_tools/ibn/assurance.py`, so both runtimes get them):
  - **`check_autonomy(runbook_id, intent_id)`:** returns JSON with allow/escalate and every check with its result.
  - **`apply_runbook(…, autonomous=True)`:** runs the check again. Outside the envelope it applies nothing. Inside, it writes the `runbook_applied` history entry (which the rush model reads), sets the incident to `mitigating` and asserts nothing; the SLO monitor declares recovery. Without `autonomous` it behaves exactly as before.
  - **`await_recovery(intent_id, timeout_s=60)`:** waits up to 75 s until the monitor closes the incident; on timeout it escalates. Its Agent Engine tool timeout is 90 s (`ToolSet.tool(timeout_s=…)`; the default is 30 s).
- **On Agent Engine with the NOC review on:** for an autonomous `apply_runbook`, the review node calls `check_autonomy` first.
  - Inside the envelope, it records the approval with `decided_by: autonomy-policy v1` and doesn't pause.
  - Outside, a NOC reviewer decides as usual, and an approved call runs as an ordinary one.
  - The twin runs with `NOC_REVIEW=off`, so there the tool's own check decides.
- **The responder:** `bin/twin.sh autopilot [L4|L3]` (`agent-engine/autopilot.py`), a foreground process.
  - It sets the lane's level (default L4) and watches the twin's `ibn_incidents` with a change stream. Each new open incident is claimed once (an `autopilot` field) and handed to the deployed ibn-agent via `agentengine invoke` with a fixed task: diagnose → check_autonomy → apply (autonomous) → await_recovery, or stop on escalate → three-line report.
  - Session and execution ids, the answer and a `report` timeline stage are written back to the incident.
  - Default lane only: the CLI can't send the `demo-prefix` header.
- **Verified at the tool level** on the twin cluster (test lane `s_shs2test01_`, dashboard with the new code, both rushes in parallel at L4):
  - **Hamburg (IBN-001):** top match Hamburg Altona RB-007 at 0.91 → allow → applied 2.7 s after detection → recovered 27.6 s later as seen by the monitor (time to recover 29 s). Timeline detect → diagnose → decide → act → verify; one violation and one recovery event.
  - **Berlin (IBN-002):** RB-005 at 0.75 → escalate ("similarity 0.75 < 0.80"); `apply_runbook(autonomous)` refused, also for RB-007 ("top match recommends RB-005"); nothing applied; the incident recovered by itself in the cool-down (68.5 s).
  - **Policy settings:** L3, deny list, change freeze and a rate limit of 0 each add their own reason.
  - **Measured-fingerprint similarities:** Marienplatz 0.917 and Hamburg 0.907 (RB-007, allow); Berlin 0.750 (RB-005) and Stuttgart 0.725 (no runbook), both escalate.
- **Verified end to end** through the deployed ibn-agent (JeffN, `deploy-b75a78b1`) on the twin's default lane: twin dashboards plus `bin/twin.sh autopilot L4`, rushes at Hamburg and Berlin started together, nobody typing.
  - **Hamburg:** detect → diagnose (RB-007, 0.91) → decide (allow) → act → verify → report. The runbook went in 14 s after detection; the monitor confirmed recovery 38 s after detection. One agent session in Traces; its three-line report is on the incident.
  - **Berlin:** detect → diagnose (RB-005, 0.75) → decide (escalate: "similarity 0.75 < 0.80") → report, with nothing applied. The rush's cool-down cleared it after 66 s.
  - **No duplicate events:** one violation, one recovery and one diagnose event per incident.
- **Found and fixed on the way:**
  - **Clocks.** The tool sandboxes write UTC times (naive `datetime.now()`, like every writer here), while the twin's laptop processes wrote local time. That broke time comparisons across the two, such as the monitor's 60 s duplicate check, and it predates S2. `bin/twin.sh` now runs everything with `TZ=UTC`, so the twin dashboards show UTC. On Kanopy all processes share one timezone.
  - **Concurrent invokes.** Two `agentengine invoke`s started together both refreshed the CLI login, and one failed with "session expired". The autopilot retries (2×, 3 s apart).
  - **Stopping the autopilot.** `bin/twin.sh autopilot` now `exec`s Python, so stopping it doesn't leave an orphaned autopilot that keeps claiming incidents.

## S3 as built (2026-10-21, branch `feature/agent-engine-migration`)

- **Learn: the SLO monitor writes it, not the agent.** The monitor is where recovery is verified, so `close_incident` (`web/ibn_assurance.py`) writes the precedent as soon as it closes an incident the system fixed on its own (the incident has `action`, set by `apply_runbook(autonomous=True)`). That doesn't depend on `await_recovery` returning in time.
  - **The document** (`oss_tools/ibn/learning.py`): a `kind: incident` chunk in the shared `ibn_knowledge_chunks`, `_id` `INC-L-<incident>`, shaped like the seeded ones (title, site, customer, ts, lng/lat, fingerprint, runbook_id, outcome, mttr_s) plus `learned`, `lane`, `incident_id`.
  - **Its text opens with the measured fingerprint** the hero query searches with (`slo.fingerprint_text`), then root cause, runbook, time to act and time to recover. A narrative text only tied with the seeded Hamburg incident (0.913 vs 0.910); this one ranks clearly first (0.931).
  - Upserted by id, so several watchers of a lane write it once; the incident's timeline gets a `learn` stage.
  - Only autonomous fixes are learned. A NOC fix via `apply_runbook` asserts its recovery, so it isn't evidence of the same kind.
- **Lanes without touching the index:** learned chunks carry the lane (the session prefix, `''` for the default lane).
  - The hero query keeps its four pre-filters and adds `$match` on lane after `$vectorSearch` (limit 10, then `$limit` 3). The track-record check in `autonomy.evaluate` counts the same set.
  - Reset deletes the lane's learned chunks; `refresh_incident_dates` already leaves them alone.
- **Also in platform memory (Agent Engine, 2026-10-22):** after an autonomous fix, `await_recovery` adds a "Lesson for memory" line (`learning.lesson`: site, measured symptom, runbook, policy, time to recover), and the domain agent's graph saves it as an org-visible semantic memory (`telco_runtime.memory.save_lesson`, label `learned-incident-<incident>`, upserted, `metadata.kind: learned_incident`; a `remember` node after the tool node).
  - The chunk stays the system of record: the hero query needs its time and geo filters, which memory search doesn't have. The memory is the agents' recollection: the coordinator recalls the fix in plain conversation ("have we seen this before?").
  - `org` visibility means the whole Agent Engine project (memory never crosses projects), so in JeffN other teams' agents with memory could recall it too. Memory isn't lane-scoped, and Reset can't delete it (no delete-by-user API).
  - **Verified deployed** (2026-10-29, JeffN, ibn-agent `deploy-72eb6f88`, twin default lane, `bin/twin.sh autopilot L4`, a Hamburg rush, nobody typing): RB-007 applied 18 s after the incident opened, recovered after 45 s, prevention proposed. The agent log shows `Created semantic memory: label=learned-incident-6ae36cd3…, visibility=org`, and the coordinator answers "what has the network learned at Hamburg Altona?" from it.
  - The first run of the day failed before the fix: SDK 0.11.11 validates tool arguments, and the graph's internal `check_autonomy` call passed `site=None` to a `str` parameter. `graph.call` now leaves `None` arguments out.
  - The autopilot runs as the presenter's user, so memory extraction also files its fixed task as that user's preference ("prefers autonomous handling … at L4"). Harmless in the twin; a service account would keep it apart (Phase 8).
- **Prevent:** a new tool, `propose_prevention(intent_id)`, the autopilot's step 4 after a recovery.
  - It works from data only: the runbook of the incident's autonomous action; when that runbook is a template-level fix (`rush.MITIGATING_RUNBOOKS`), the other live intents on the same template without it, and the runbook's lane-scoped track record as evidence.
  - It records the proposal on the incident (`prevention`: canary site, the rest, evidence, and the exact `update_template_version(…, intent_ids=[canary])` call), adds a `prevent` stage, and sets a `proposed_change` marker on the sister intents. It changes nothing on the network, and calling it again changes nothing.
  - The NOC decides: running that `update_template_version` marks the proposal `approved` and clears the canary's marker. On Agent Engine with the NOC review on, that call pauses for the reviewer as before. The twin runs without the review, so there the decision is the NOC engineer asking for it.
  - The marker is not `pending_approval`, because the dashboard renders that one as "the agent is paused"; S4 renders proposals.
- **Prod safety (Kanopy runs the old code until the BIG SWITCH, on the same database as staging):** only `bin/twin.sh autopilot` sets L4, and only on the twin cluster. Kanopy staging therefore never acts autonomously, never learns, and the live prod code (no lane filter) never sees a learned chunk. **S4's L3/L4 switch and S5 have to keep it that way** until prod has the lane filter, for example by refusing L4 on the shared cluster.
- **What "sister site" can mean here:** the hero query's geo box is 10 km, and the only seeded sites that close are Marienplatz and Schwabing (3 km, and Schwabing already has RB-007). So a learned incident is the top match for the next rush at the same site or in the same city. The chain-wide step is the template proposal, which is the plan's "prevent".
- **Verified** on the twin cluster (test lane `s_shs3test01_`, dashboard with the S3 code, tools called in the autopilot's order), twice:
  - **Learn:** Hamburg at L4, fixed 4 s after detection, recovered in 28 s; timeline detect → diagnose → decide → act → verify → learn. The learned chunk was searchable 3 s after the monitor closed the incident.
  - **Prevent:** evidence "RB-007 resolved 3 incident(s) at Hamburg Altona, Munich Schwabing (1 learned)"; canary Berlin Mitte, then Stuttgart; Schwabing (has RB-007) left out; markers on IBN-002/003; no `migration_queued` anywhere until the NOC call.
  - **Lanes:** the raw hero query in this lane returns the learned chunk first (0.932, seeded 0.909); another lane and the default lane don't see it.
  - **Next rush at Hamburg without the fix:** the diagnosis's top match is the learned incident (0.93, "learned by the self-healing loop"), the track record counts it (3 past incidents), and a second chunk is learned.
  - **Approval:** `update_template_version(intent_ids=['IBN-002'])` marked both proposals approved, cleared IBN-002's marker and queued its migration; IBN-003's marker stays.
  - **Reset:** removed the lane's 2 learned chunks and none of anyone else's. One violation and one recovery event per incident.
- **Verified end to end** through the deployed ibn-agent (JeffN, `deploy-1301b889`) on the twin's default lane: twin dashboards plus `bin/twin.sh autopilot L4`, rushes at Hamburg and Berlin started together, nobody typing.
  - **Hamburg:** detect → diagnose (RB-007, 0.91) → decide (allow) → act → verify → learn → prevent → report. The runbook went in 16 s after detection; the monitor confirmed recovery after 40 s; the learned chunk landed 1 s later; the agent proposed the canary (Berlin Mitte, then Stuttgart; evidence 3 incidents, 1 learned) and said so in its report.
  - **Berlin:** escalated (similarity 0.75 < 0.80), nothing applied, nothing learned or proposed; the cool-down cleared it after 69 s.
  - One violation, one recovery and one diagnose event per incident; markers on IBN-002/003 only, no migration queued. `bin/twin.sh reset` then removed the learned chunk.

## S4 as built (2026-10-21, branch `feature/agent-engine-migration`)

- **The page:** a "Self-healing loop" panel under the three IBN panels, for the intent in focus (`web/ibn.html`).
  - **Ribbon:** sense → detect → diagnose → decide → act → verify → learn → prevent, each lit from the incident's timeline with its time. While the incident is open, the next stage pulses. An escalation turns decide and act amber ("→ NOC"), and so does an `await_recovery` timeout on verify.
  - **Clock:** seconds since detection while open, then "MTTR 40 s · 0 human actions", or "recovered by itself after 69 s · escalated to the NOC". It runs on the data's clock: the dashboard sends `server_now`, because the twin stores UTC and the browser runs on local time.
  - **Alarm card (TMF642):** severity, type, probable cause, problem, root cause, the precedent (with its similarity only in `?mode=eng`), status; then the decision (allowed by the policy, or the recommendation for the NOC with its reasons) and the agent's report. The incident and trace session ids are shown in eng mode only.
  - **Latency chart:** the last two minutes of POS latency with the SLO line and markers for "breach detected", "runbook applied" and "recovered" from the timeline.
  - **Proposal:** "The system proposes, the NOC approves" with the canary, the rest and the evidence; sister intents get 📋 on their tab and a note in the panel.
  - **Switch:** L3 · recommend / L4 · act.
- **The dashboard** (`web/ibn_dashboard.py`): change streams on the lane's `ibn_incidents` (every stage, from any writer) and `ibn_autonomy_policy` (the switch, the autopilot, Reset); the snapshot adds the newest incident per intent, the autonomy state and `server_now`; the page's `set_autonomy` message sets the level.
- **The L4 gate:** L4 can only be set where `SELF_HEALING_L4=1`, checked by `autonomy.set_level` (the autopilot) and the page's switch. `bin/twin.sh` exports it; the Kanopy environment files don't, so the Kanopy demo stays at L3, never acts on its own and never learns (see S3, prod safety). The tools read the level as stored, so the Agent Engine tool sandboxes need no setting.
- **Verified** on the twin cluster:
  - **The gate** (scratch dashboards with and without the setting, test lanes): without it the page shows L4 disabled, a `set_autonomy` L4 request changes nothing, and `set_level("L4")` raises; with it the switch sets L4 and every open tab is told.
  - **Tool level** (test lane, screenshots via headless Chrome over CDP): L3 at Hamburg stops at decide with the recommendation; after Reset, L4 lights all eight stages; Berlin at L4 escalates with its reason and shows the 📋 proposal for its own site; no JS errors.
  - **The storyboard with the real autopilot** and the deployed ibn-agent, on the twin's default lane: L3 at Hamburg: detect → diagnose → decide (escalate: level L3) → report, nothing applied, the clock shows "awaiting the NOC". Reset, L4: all eight stages plus the report, MTTR 40 s, 0 human actions, the canary proposal for Berlin and Stuttgart. "Tomorrow": the same rush peaks at 86 % load and stays green, no incident. `bin/twin.sh reset` afterwards.
- **Not in S4:** approving the proposal from the page (the NOC still asks for `update_template_version`), and the legacy runtime's responder (S5).

