"""End-to-end test of the shell on Atlas Agent Engine, as browsers use it.

Runs INSIDE the staging shell pod, where it reaches the shell's WebSocket on
127.0.0.1:8080 and the IBN dashboard over the in-cluster service. It needs
no credentials of its own: everything goes through the shell, which uses
its service account. Start it with `bin/staging-test.sh`, which copies this
file into the pod and runs it detached, so it survives the laptop.

Suites (`--suite`):
  full    two visitors in parallel: A runs the IBN flow with NOC approvals,
          B the DTW what-if; then lane isolation, memory, self-healing after a
          morning rush, reconnect (the default)
  ibn     the IBN flow with its NOC approvals only
  memory  what visitors ask the coordinator to remember, and what it recalls
--cap     also checks that the shell turns away visitor 7. It leaves 6
          sessions open for the shell's idle timeout (30 min), so the next run
          has to wait or reuse tokens.
--token, --token-b
          reuse earlier visitors for A and B instead of taking new sessions
          (the shell allows 6 at once and keeps an idle one for 30 min).

Prints one PASS/FAIL line per check and a SUMMARY line at the end.
Added after the staging test of 2026-11-13/16 (BIG SWITCH readiness).
"""
import argparse
import asyncio
import json
import os
import time
import urllib.request
import uuid

import websockets

SHELL = "ws://127.0.0.1:8080/ws"
IBN_WS = "ws://agentic-mcp-demo-ibn-web-app/ws?session={}"
# The level a new lane starts at: the shell pod's own setting (the test runs there).
EXPECT_LEVEL = os.environ.get("TWIN_AUTONOMY_DEFAULT", "").strip().upper()
TURN_TIMEOUT_S = 360
T0 = time.monotonic()
RESULTS: list[tuple[str, bool, str]] = []

# Text in an answer that means an agent call failed, even if the shell
# delivered it as a normal response.
BROKEN = ("agent failed:", "503 service unavailable", "don't have access to that service",
          "currently busy")


def log(*a):
    print(f"[{time.monotonic() - T0:7.1f}s]", *a, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    log("PASS" if ok else "FAIL", name, "|", str(detail)[:400].replace("\n", " "))


class Visitor:
    """One browser session: its own token, hence its own lane and memory."""

    def __init__(self, label, token=None):
        self.label, self.token = label, token or uuid.uuid4().hex[:12]
        self.q: asyncio.Queue = asyncio.Queue()
        self.approvals: list[dict] = []    # pending review cards, in order
        self.autonomous: list[dict] = []   # self-healing panel messages
        self.broadcasts = 0                # Agent Log lines

    async def open(self):
        self.ws = await websockets.connect(SHELL, max_size=None, ping_interval=20, ping_timeout=60)
        self.reader = asyncio.create_task(self._read())
        await self.ws.send(json.dumps({"type": "init", "token": self.token}))
        return await self.wait(lambda m: m.get("type") in ("hello", "error", "_closed"), 60)

    async def _read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                t = m.get("type")
                if t == "broadcast":
                    self.broadcasts += 1
                    continue
                # The shell also sends a decided card again (to mark it on the
                # page): only pending ones are new reviews.
                if t == "approval" and (m.get("review") or {}).get("status", "pending") == "pending":
                    self.approvals.append(m["review"])
                if t == "autonomous":
                    self.autonomous.append(m)
                await self.q.put(m)
        except Exception as e:
            await self.q.put({"type": "_closed", "error": str(e)})

    async def wait(self, pred, timeout):
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            try:
                m = await asyncio.wait_for(self.q.get(), left)
            except asyncio.TimeoutError:
                return None
            if pred(m):
                return m

    async def _until_idle(self, timeout):
        """The turn's answer (response, error or approval_failed), once the
        shell says it stopped thinking."""
        got, end = None, time.monotonic() + timeout
        kinds = ("response", "error", "approval_failed", "_closed")
        while time.monotonic() < end:
            m = await self.wait(lambda m: m.get("type") in kinds + ("thinking",), end - time.monotonic())
            if m is None:
                break
            if m["type"] in kinds:
                got = m
            if m["type"] == "_closed" or (m["type"] == "thinking" and m.get("active") is False
                                          and got is not None):
                break
        return got

    async def ask(self, text, timeout=TURN_TIMEOUT_S):
        t = time.monotonic()
        await self.ws.send(json.dumps({"type": "query", "text": text}))
        m = await self._until_idle(timeout)
        took = round(time.monotonic() - t, 1)
        if not m:
            return False, f"no answer in {timeout}s", took
        if m["type"] != "response":
            return False, f"{m['type']}: {m.get('message') or m.get('error')}", took
        return True, m.get("markdown") or "", took

    async def decide(self, approval_id, decision="approve", timeout=TURN_TIMEOUT_S):
        """decision: approve, reject or approve_canary (as the card's buttons)."""
        t = time.monotonic()
        await self.ws.send(json.dumps({"type": "approval_decision", "approval_id": approval_id,
                                       "decision": decision, "notes": "staging end-to-end test"}))
        m = await self._until_idle(timeout)
        return m, round(time.monotonic() - t, 1)

    async def reset(self):
        await self.ws.send(json.dumps({"type": "reset_demo"}))
        return await self.wait(lambda m: m.get("type") == "reset_done", 300)

    async def close(self):
        await self.ws.close()
        self.reader.cancel()


async def turn(v, step, text, expect=()):
    ok, out, took = await v.ask(text)
    low = out.lower() if ok else ""
    ok = ok and not any(b in low for b in BROKEN)
    missing = [e for e in expect if e.lower() not in low]
    check(f"{v.label} {step} ({took}s)", ok and not missing, out)
    return ok, out


class Refused(Exception):
    """The shell turned a visitor away (e.g. at its session cap)."""


async def connect(*visitors):
    for v in visitors:
        h = await v.open()
        ok = bool(h and h.get("type") == "hello" and h.get("runtime") == "agent_engine")
        check(f"{v.label} hello", ok, {k: (h or {}).get(k) for k in ("runtime", "session", "message")})
        if not ok:
            raise Refused((h or {}).get("message") or "no hello")
        r = await v.reset()
        check(f"{v.label} reset", r and r.get("ok"), (r or {}).get("message"))
        await asyncio.sleep(2)
        v.seen = len(v.approvals)   # cards shown at connect belong to the lane before the reset


async def pages():
    for path in ("/", "/help"):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:8080{path}", timeout=15)
            body = r.read().decode("utf-8", "replace")
            check(f"page {path}", r.status == 200 and "<html" in body.lower(), f"HTTP {r.status}, {len(body)} bytes")
        except Exception as e:
            check(f"page {path}", False, e)


# ── IBN with NOC approvals ────────────────────────────────────────────────

IBN_STEPS = [
    ("IBN 1 intent", "I'm opening a new Alpenmarkt store at Marienplatz Munich. POS priority, guest WiFi "
     "strictly separated, camera uplink, online by 18:00, max 40ms POS latency, 99.95% availability.",
     ("marienplatz",)),
    ("IBN 2 feasibility", "Check feasibility for it.", ("feasib",)),
    ("IBN 3 plan+activate", "Propose a plan and activate it.", ()),
    ("IBN 4 violation", "Simulate a POS latency violation at Marienplatz.", ()),
    ("IBN 5 diagnose", "Diagnose the violation.", ("runbook",)),
    ("IBN 6 repair+fold", "Apply the recommended runbook and fold the fix into the template for all "
     "Alpenmarkt stores.", ()),
]


async def approve_new(v, first_wait_s, expect_card=False):
    """Approve the review cards that arrived since the last call. The first
    waits `first_wait_s`, past the agent's idle TTL (60 s), so its resume has
    to come back from the durable checkpoint on a new sandbox. The card
    comes from a change stream, so it can follow the paused turn by several
    seconds: with `expect_card` (the answer said a review is waiting), wait
    up to 20 s for it."""
    end = time.monotonic() + (20 if expect_card else 3)
    while time.monotonic() < end and not (expect_card and v.seen < len(v.approvals)):
        await asyncio.sleep(0.5)
    while v.seen < len(v.approvals):
        card = v.approvals[v.seen]
        v.seen += 1
        check(f"{v.label} NOC approval card", True,
              {k: card.get(k) for k in ("approval_id", "action", "summary", "agent")})
        wait = first_wait_s if not v.decided else 5
        log(f"waiting {wait} s before approving {card.get('approval_id')}")
        await asyncio.sleep(wait)
        m, took = await v.decide(card["approval_id"])
        v.decided += 1
        answer = (m or {}).get("markdown") or (m or {}).get("message") or "no answer"
        check(f"{v.label} approval {card.get('approval_id')} resumes the agent ({took}s)",
              m is not None and m.get("type") == "response", answer)
        if "paused again" in answer.lower() or "awaits noc approval" in answer.lower():
            await approve_new(v, 5, expect_card=True)   # the resumed agent asks for the next review
            return


async def ibn_flow(v, first_wait_s):
    v.decided = 0
    for name, text, expect in IBN_STEPS:
        ok, out = await turn(v, name, text, expect)
        if name == "IBN 1 intent":
            check("memory: the opener is not saved", ok and "noted for next time" not in out.lower(), out)
        await approve_new(v, first_wait_s, expect_card="awaits noc approval" in out.lower())
    check(f"{v.label} the IBN flow asked for NOC approval", v.decided > 0, f"{v.decided} approved")


# ── DTW ───────────────────────────────────────────────────────────────────

async def dtw_flow(v):
    await turn(v, "DTW 1 scenario", "Raise prepaid ACME M downlink from 7.2 Mbps to 15 Mbps in NYC and LA "
               "Saturday evening - where do we bottleneck?", ("scenario",))
    await turn(v, "DTW 2 refine", "Make it 20 Mbps instead.", ("20",))
    await turn(v, "DTW 3 simulate", "Run the simulation.", ("cell",))


# ── Memory ────────────────────────────────────────────────────────────────

async def memory_checks(a, b, opener_sent):
    if not opener_sent:
        ok, out, _ = await a.ask(IBN_STEPS[0][1])
        check("memory: the opener is not saved", ok and "noted for next time" not in out.lower(), out)
    note = "Please remember that I present this demo to Vodafone next Tuesday."
    ok, out, _ = await a.ask(note)
    check("memory: A's explicit note is saved", ok and "noted for next time" in out.lower(), out)
    ok, out, _ = await b.ask(note)
    check("memory: B saves the same note (no label clash)", ok and "noted for next time" in out.lower(), out)
    ok, out, _ = await a.ask("Note that Hamburg Altona is my most important site.")
    check("memory: A's second note is saved", ok and "noted for next time" in out.lower(), out)
    ok, out, _ = await a.ask("What do you remember about me?")
    check("memory: A recalls both notes", ok and "vodafone" in out.lower() and "altona" in out.lower(), out)
    ok, out, _ = await b.ask("What do you remember about me?")
    check("memory: B recalls its note, not A's", ok and "vodafone" in out.lower()
          and "altona" not in out.lower(), out)


# ── Self-healing ──────────────────────────────────────────────────────────

async def self_healing(v):
    """A morning rush at Hamburg Altona: the SLO monitor (in the IBN dashboard,
    which must be watching the lane) opens an incident, the shell's responder
    hands it to ibn-agent with no prompt, and the panel reports."""
    try:
        dash = await websockets.connect(IBN_WS.format(v.token), max_size=None)
        snap = json.loads(await asyncio.wait_for(dash.recv(), 30))
        intents = snap.get("intents") or []
        check("self-healing: dashboard lane snapshot", bool(intents),
              f"{len(intents)} intents; autonomy {snap.get('autonomy')}")
        level = (snap.get("autonomy") or {}).get("level")
        if EXPECT_LEVEL:
            check(f"self-healing: the lane starts at {EXPECT_LEVEL}", level == EXPECT_LEVEL,
                  f"level {level}")
        target = next((i for i in intents if "altona" in json.dumps(i).lower()
                       and i.get("status") == "active"), None)
        if not target:
            check("self-healing: rush at Hamburg Altona", False, "no active Hamburg Altona intent")
            return
        n0 = len(v.autonomous)
        await dash.send(json.dumps({"type": "start_rush", "intent_id": target["_id"]}))
        log("rush requested for", target["_id"])
        end = time.monotonic() + 420
        while time.monotonic() < end and len(v.autonomous) == n0:
            try:   # keep the dashboard socket drained
                await asyncio.wait_for(dash.recv(), 2)
            except asyncio.TimeoutError:
                pass
        started = len(v.autonomous) > n0
        check("self-healing: incident handed to the agent", started,
              v.autonomous[-1] if started else "no self-healing panel in 7 min")
        if started:
            m = await v.wait(lambda m: m.get("type") == "autonomous" and m.get("phase") == "done", 420)
            check("self-healing: agent report", m is not None and m.get("ok"), (m or {}).get("markdown") or "no report")
            if level == "L4":   # it acted on its own, and proposed a canary for the sister sites
                await dash.close()
                dash = await websockets.connect(IBN_WS.format(v.token), max_size=None)
                snap = json.loads(await asyncio.wait_for(dash.recv(), 30))
                inc = next((i for i in snap.get("incidents") or []
                            if i.get("intent_id") == target["_id"]), {})
                check("self-healing: L4 applied the runbook on its own",
                      bool(inc.get("autonomous_act_at")),
                      f"incident {inc.get('_id')} status {inc.get('status')}, "
                      f"decision {(inc.get('decision') or {}).get('decision')}")
                check("self-healing: L4 proposed prevention for the sister sites",
                      (inc.get("prevention") or {}).get("status") in ("proposed", "none"),
                      f"prevention {(inc.get('prevention') or {}).get('status')}")
                # A presenter switches this lane to L3 for a human-in-the-loop demo.
                await dash.send(json.dumps({"type": "set_autonomy", "level": "L3"}))
                got, end = None, time.monotonic() + 30
                while time.monotonic() < end and got != "L3":
                    try:
                        msg = json.loads(await asyncio.wait_for(dash.recv(), 5))
                    except asyncio.TimeoutError:
                        continue
                    if msg.get("type") == "autonomy":
                        got = msg.get("level")
                check("self-healing: the dashboard switches the lane to L3", got == "L3",
                      f"level {got}")
        await dash.close()
    except Exception as e:
        check("self-healing", False, repr(e))


# ── Session cap, reconnect ────────────────────────────────────────────────

async def session_cap():
    extra = []
    try:
        for i in range(10):
            v = Visitor(f"cap{i}")
            hello = await v.open()
            extra.append(v)
            if not hello or hello.get("type") != "hello":
                check("session cap: a visitor over the cap is turned away", hello is not None,
                      f"refused after {i} extra sessions: {(hello or {}).get('message')}")
                return
        check("session cap: a visitor over the cap is turned away", False, "10 extra sessions accepted")
    finally:
        for v in extra:
            try:
                await v.close()
            except Exception:
                pass


async def reconnect(v):
    await v.close()
    again = Visitor(f"{v.label} again", token=v.token)
    h = await again.open()
    check("reconnect: same token brings the session back",
          h and h.get("type") == "hello" and len(h.get("history") or []) > 0,
          f"{len((h or {}).get('history') or [])} history entries")
    await again.close()


async def main(args):
    try:
        await run(args)
    except Refused as e:
        log(f"stopped: a visitor was refused ({e}). At the session cap, wait for idle sessions to be "
            "reaped (30 min) or reuse earlier visitors with --token/--token-b.")
    passed = sum(1 for r in RESULTS if r[1])
    log(f"SUMMARY {passed}/{len(RESULTS)} passed")
    for name, ok, _ in RESULTS:
        if not ok:
            log("  failed:", name)


async def run(args):
    a = Visitor("A", token=args.token)
    if args.suite == "ibn":
        await connect(a)
        await ibn_flow(a, args.approve_after)
        await a.close()
    elif args.suite == "memory":
        b = Visitor("B", token=args.token_b)
        await connect(a, b)
        await memory_checks(a, b, opener_sent=False)
        await a.close()
        await b.close()
    else:
        await pages()
        b = Visitor("B", token=args.token_b)
        await connect(a, b)
        await asyncio.gather(ibn_flow(a, args.approve_after), dtw_flow(b))
        ok, out, _ = await b.ask("List all IBN intents with their sites.")
        check("lane isolation: B doesn't see A's Marienplatz store",
              ok and not any(x in out.lower() for x in BROKEN) and "marienplatz" not in out.lower(), out)
        await memory_checks(a, b, opener_sent=True)
        await self_healing(a)
        if args.cap:
            await session_cap()
        await reconnect(a)
        await b.close()
        log("Agent Log lines: A", a.broadcasts, "B", b.broadcasts)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", choices=("full", "ibn", "memory"), default="full")
    p.add_argument("--cap", action="store_true", help="also test the session cap (holds 6 sessions for 30 min)")
    p.add_argument("--token", help="reuse an earlier visitor's token for A (the shell is at its session cap)")
    p.add_argument("--token-b", help="the same for B")
    p.add_argument("--approve-after", type=int, default=90,
                   help="seconds before the first approval (default 90, past the agents' 60 s idle TTL)")
    asyncio.run(main(p.parse_args()))
