#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN Intent Service — Customer Intent Lifecycle Management

The customer-facing surface of the Intent-Based Networking demo. Owns the
ibn_intents collection. Captures natural-language business intents for retail
network services (POS priority, guest segmentation, availability targets,
deadlines), parses them into structured form via gpt-4o, and tracks lifecycle
state (submitted → feasible → planned → active → violated → closed).

Use this service when users say:
- Submit:   "I'm opening a new <store> at <location>", "new branch", "submit intent",
           "new connectivity request", "we need POS / guest WiFi / camera at ..."
- List:    "show all intents", "list active intents", "show all active intents",
           "what's running", "fleet status"
- Detail:  "get intent <id>", "show details for IBN-...", "intent details"
- Cancel:  "cancel intent <id>", "withdraw intent", "stop intent"

This service does NOT check feasibility, allocate resources, manage
inventory, compute compliance, or simulate telemetry — those belong to the
feasibility, inventory, assurance, and telemetry services respectively.
"""

import json
import logging
import os
import datetime
from pymongo import MongoClient, ASCENDING, DESCENDING
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("ibn_intent_service")
logger = logging.getLogger("ibn_intent_service")

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db           = mongo_client["agent_registry"]
intents      = db["ibn_intents"]
sites        = db["ibn_sites"]
customers    = db["ibn_customers"]

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
PARSE_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o")


def _next_intent_id() -> str:
    """Allocate the next IBN-NNN identifier."""
    last = intents.find_one(
        {"_id": {"$regex": r"^IBN-\d+$"}},
        sort=[("_id", DESCENDING)],
    )
    if not last:
        return "IBN-001"
    n = int(last["_id"].split("-")[1])
    return f"IBN-{n + 1:03d}"


def _resolve_site(site_hint: str) -> dict | None:
    """
    Resolve a site by fragments, tolerating word-order variations.
    'Munich Marienplatz', 'Marienplatz Munich', and 'Marienplatz' all hit
    the same site. 'Alpenmarkt Stuttgart' resolves via the 'Stuttgart' token
    even though 'Alpenmarkt' (the store brand) isn't part of the site name.
    """
    if not site_hint:
        return None
    direct = sites.find_one({"name": {"$regex": site_hint, "$options": "i"}})
    if direct:
        return direct
    tokens = [t for t in site_hint.split() if len(t) >= 3]
    if not tokens:
        return None
    result = sites.find_one(
        {"$and": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )
    if result:
        return result
    return sites.find_one(
        {"$or": [{"name": {"$regex": t, "$options": "i"}} for t in tokens]}
    )


def _parse_natural_language(text: str) -> dict:
    """Use gpt-4o to extract structured intent fields from natural language."""
    today = datetime.date.today().isoformat()
    prompt = (
        f"You are an Intent-Based Networking parser. Today's date is {today}.\n\n"
        f"Extract structured fields from this customer request:\n\n"
        f"{text!r}\n\n"
        f"Return ONLY valid JSON, no prose, with this schema:\n"
        "{\n"
        '  "site_name":   string  // full site name: district + city, e.g. "Munich Marienplatz". Always include both city and district/neighbourhood if mentioned, in the form "<city> <district>".\n'
        '  "customer":    string  // company name if mentioned, else null\n'
        '  "services":    string[]  // subset of ["pos","guest_wifi","camera_uplink","kiosk","voip"]\n'
        '  "targets": {\n'
        '    "pos_latency_ms":    number|null,\n'
        '    "availability_pct":  number|null,\n'
        '    "segmentation":      "strict"|"relaxed"|null,\n'
        '    "kiosk_count":       number|null\n'
        "  },\n"
        '  "deadline":    string|null  // ISO datetime; resolve relative phrases against today\n'
        "}\n\n"
        "Resolve relative deadlines (e.g. 'by 18:00', 'by tomorrow') against today's date.\n"
        "Use null for fields not mentioned. Do not invent values."
    )

    resp = openai_client.chat.completions.create(
        model=PARSE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    parsed = json.loads(raw)

    # Coerce deadline string to datetime if present
    if parsed.get("deadline"):
        try:
            parsed["deadline"] = datetime.datetime.fromisoformat(
                parsed["deadline"].replace("Z", "+00:00")
            )
        except Exception:
            parsed["deadline"] = None

    return parsed


def _format_intent_card(intent: dict) -> str:
    """Render a single intent as a readable Markdown block."""
    p = intent.get("parsed", {})
    t = p.get("targets", {})
    site = sites.find_one({"_id": intent.get("site_id")}) if intent.get("site_id") else None
    site_name = site["name"] if site else (p.get("site_name") or "—")

    days_active = None
    if intent.get("activated_at"):
        delta = datetime.datetime.now() - intent["activated_at"]
        days_active = max(0, delta.days)

    status = intent.get("status", "—")
    status_emoji = {
        "submitted":  "📝",
        "feasible":   "✓",
        "planned":    "📋",
        "active":     "🟢",
        "violated":   "🔴",
        "cancelled":  "⊗",
        "closed":     "⏹",
    }.get(status, "•")

    lines = [f"**{intent['_id']}** · {site_name} · {status_emoji} {status}"]

    if days_active is not None:
        lines.append(f"  active {days_active}d")

    target_bits = []
    if t.get("pos_latency_ms") is not None:
        target_bits.append(f"POS ≤{t['pos_latency_ms']}ms")
    if t.get("availability_pct") is not None:
        target_bits.append(f"{t['availability_pct']}% availability")
    if t.get("segmentation"):
        target_bits.append(f"{t['segmentation']} segmentation")
    if t.get("kiosk_count"):
        target_bits.append(f"{t['kiosk_count']} kiosks")
    if target_bits:
        lines.append("  " + " · ".join(target_bits))

    services = p.get("services") or []
    if services:
        lines.append(f"  services: {', '.join(services)}")

    if intent.get("history"):
        last = intent["history"][-1]
        if last.get("event") == "runbook_applied":
            lines.append(f"  ⓘ runbook {last.get('runbook_id')} applied "
                         f"{last['ts'].strftime('%Y-%m-%d')}")

    return "\n".join(lines)


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def submit_intent(text: str) -> str:
    """
    Submit a new natural-language customer intent. The service parses the
    request via LLM, stores both the raw text and the structured fields,
    resolves the target site, and returns the new intent ID with a parsed
    summary. Status is set to 'submitted' — call check_feasibility next.

    Use this when the user says something like:
      "I'm opening a new <store> at <location>. POS priority, guest WiFi
       strict, camera uplink, max <N>ms POS latency, <X>% availability."

    Args:
        text: The customer's natural-language request (full sentence).
    """
    parsed = _parse_natural_language(text)
    site = _resolve_site(parsed.get("site_name", ""))

    intent_id = _next_intent_id()
    doc = {
        "_id":          intent_id,
        "customer_id":  "cust-alpenmarkt",  # demo single-customer
        "site_id":      site["_id"] if site else None,
        "raw_text":     text,
        "parsed":       parsed,
        "status":       "submitted",
        "submitted_at": datetime.datetime.now(),
        "activated_at": None,
        "version":      1,
        "history":      [],
        "template":     None,
    }
    intents.insert_one(doc)

    logger.info(f"Submitted {intent_id} for site {site['_id'] if site else '<unresolved>'}")

    bits = []
    t = parsed.get("targets", {})
    if t.get("pos_latency_ms"):    bits.append(f"POS ≤{t['pos_latency_ms']}ms")
    if t.get("availability_pct"):  bits.append(f"{t['availability_pct']}% availability")
    if t.get("segmentation"):      bits.append(f"{t['segmentation']} segmentation")

    site_str = site["name"] if site else f"⚠️ unresolved site '{parsed.get('site_name')}'"
    deadline = parsed.get("deadline")
    deadline_str = deadline.strftime("%Y-%m-%d %H:%M") if isinstance(deadline, datetime.datetime) else "—"

    return (
        f"📝 Intent **{intent_id}** captured for **{site_str}**.\n"
        f"  Targets: {' · '.join(bits) if bits else '—'}\n"
        f"  Services: {', '.join(parsed.get('services') or []) or '—'}\n"
        f"  Deadline: {deadline_str}\n"
        f"  Status: submitted — run feasibility check next."
    )


@mcp.tool()
def list_intents(status_filter: str = None) -> str:
    """
    List intents, optionally filtered by status. Returns a fleet-style summary
    with status badges and key targets per intent. By default lists ALL
    intents; pass status_filter='active' (or 'submitted', 'planned', etc.)
    to narrow.

    Args:
        status_filter: Optional intent status. Common values: 'active',
                       'submitted', 'planned', 'violated', 'closed'.
    """
    query = {"status": status_filter} if status_filter else {}
    docs = list(intents.find(query).sort("submitted_at", DESCENDING))

    if not docs:
        scope = f" with status '{status_filter}'" if status_filter else ""
        return f"No intents found{scope}."

    cards = [_format_intent_card(d) for d in docs]

    fleet_summary = ""
    if not status_filter:
        active = [d for d in docs if d.get("status") == "active"]
        violated = [d for d in docs if d.get("status") == "violated"]
        if active or violated:
            fleet_summary = (
                f"\n\nFleet compliance: "
                f"{len(active) - len(violated)}/{len(active)} green"
                + (f" · {len(violated)} violated" if violated else "")
            )

    header = (
        f"**{len(docs)} intent{'s' if len(docs) != 1 else ''}"
        + (f" with status '{status_filter}'" if status_filter else "")
        + ":**"
    )
    return header + "\n\n" + "\n\n".join(cards) + fleet_summary


@mcp.tool()
def get_intent(intent_id: str) -> str:
    """
    Get full details for a specific intent: raw text, parsed structure,
    lifecycle status, history of events (e.g. runbook applications),
    earmarked resources (if planned/active).

    Args:
        intent_id: The intent ID, e.g. 'IBN-005'.
    """
    doc = intents.find_one({"_id": intent_id})
    if not doc:
        return f"❌ Intent {intent_id} not found."

    site = sites.find_one({"_id": doc.get("site_id")}) if doc.get("site_id") else None
    p = doc.get("parsed", {})
    t = p.get("targets", {})

    lines = [
        f"## Intent {doc['_id']}",
        f"**Status:** {doc.get('status')}",
        f"**Customer:** {doc.get('customer_id')}",
        f"**Site:** {site['name'] if site else '—'}",
        f"**Submitted:** {doc.get('submitted_at').strftime('%Y-%m-%d %H:%M') if doc.get('submitted_at') else '—'}",
    ]
    if doc.get("activated_at"):
        lines.append(f"**Activated:** {doc['activated_at'].strftime('%Y-%m-%d %H:%M')}")

    lines.append("")
    lines.append("**Customer request (verbatim):**")
    lines.append(f"> {doc.get('raw_text', '—')}")
    lines.append("")
    lines.append("**Parsed targets:**")
    if t.get("pos_latency_ms") is not None:
        lines.append(f"- POS latency ≤ {t['pos_latency_ms']}ms")
    if t.get("availability_pct") is not None:
        lines.append(f"- Availability ≥ {t['availability_pct']}%")
    if t.get("segmentation"):
        lines.append(f"- Segmentation: {t['segmentation']}")
    if t.get("kiosk_count"):
        lines.append(f"- Kiosks: {t['kiosk_count']}")
    if p.get("services"):
        lines.append(f"- Services: {', '.join(p['services'])}")
    if p.get("deadline"):
        lines.append(f"- Deadline: {p['deadline'].strftime('%Y-%m-%d %H:%M') if isinstance(p['deadline'], datetime.datetime) else p['deadline']}")

    if doc.get("template"):
        lines.append(f"\n**Provisioned from template:** `{doc['template']}`")

    if doc.get("history"):
        lines.append("\n**History:**")
        for h in doc["history"]:
            ts = h["ts"].strftime("%Y-%m-%d %H:%M") if isinstance(h.get("ts"), datetime.datetime) else h.get("ts", "—")
            lines.append(f"- {ts} · {h.get('event')} · {h.get('note', '')}")

    return "\n".join(lines)


@mcp.tool()
def cancel_intent(intent_id: str, reason: str = "customer request") -> str:
    """
    Cancel an intent. Sets status='cancelled' and records the reason in
    history. Does not free resources — feasibility service handles that
    on activation/deactivation.

    Args:
        intent_id: The intent ID to cancel.
        reason:    Optional reason recorded in the intent's history.
    """
    doc = intents.find_one({"_id": intent_id})
    if not doc:
        return f"❌ Intent {intent_id} not found."

    if doc.get("status") in ("cancelled", "closed"):
        return f"ℹ️  Intent {intent_id} already in terminal state '{doc['status']}'."

    intents.update_one(
        {"_id": intent_id},
        {
            "$set":  {"status": "cancelled"},
            "$push": {"history": {
                "ts":     datetime.datetime.now(),
                "event":  "cancelled",
                "note":   reason,
            }},
            "$inc":  {"version": 1},
        },
    )
    return f"⊗ Intent {intent_id} cancelled. Reason: {reason}"


if __name__ == "__main__":
    mcp.run()
