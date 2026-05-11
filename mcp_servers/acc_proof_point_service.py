#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
ACC Proof Point Service — Create, edit and preview MongoDB sales proof points and one-pagers.

Use for: "new proof point", "create proof point for <customer>", "start a proof point",
"add content to proof point", "what's missing", "render it", "preview the proof point",
"list proof points". This is a sales content creation tool, NOT a network or monitoring tool.

This service captures customer proof point content in the structure Problem → Solution →
Results, guides the user through completing all required sections, and previews the story.

A proof point is a structured sales asset (slide / one-pager) following the pattern:
Problem → Solution (Why MongoDB) → Results. It documents how a specific customer
used MongoDB/Atlas to solve a business problem and what measurable outcomes resulted.

Route ALL of the following here — regardless of the technical domain mentioned:
- Start:    "new proof point", "create proof point for <customer>",
            "start a proof point", "ACC", "new content for <company>"
- Ingest:   "the customer had...", "they used MongoDB for...", "the result was...",
            "add this to the proof point", "their KPI was...", "why MongoDB here is..."
            ANY content being fed into an in-progress proof point
- Status:   "what do we have", "what's missing in the proof point",
            "how complete is this", "show proof point status"
- Preview:  "render it", "show me the story", "preview", "tell me the storyline",
            "what does the proof point look like"
- List:     "list proof points", "show all proof points", "what have we created"

IMPORTANT: when a proof point is already in progress (active), route follow-up
messages here even if they sound technical — the user is feeding domain content
into the proof point, not asking to operate a network.

Does NOT export files — use acc_export_service for pptx/html/doc generation.
"""

import datetime
import json
import logging
import os
from pymongo import MongoClient, DESCENDING
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("acc_proof_point_service")
logger = logging.getLogger("acc_proof_point_service")

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
proof_points  = db["acc_proof_points"]

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
PARSE_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _next_id() -> str:
    last = proof_points.find_one(
        {"_id": {"$regex": r"^PP-\d+$"}},
        sort=[("_id", DESCENDING)],
    )
    if not last:
        return "PP-001"
    return f"PP-{int(last['_id'].split('-')[1]) + 1:03d}"


def _active() -> dict | None:
    """Most complete non-published proof point, falling back to most recently updated."""
    drafts = list(proof_points.find({"status": {"$ne": "published"}}))
    if not drafts:
        return None
    # Prefer the draft with the most fields filled in
    return max(drafts, key=lambda d: (_pct(d), d.get("updated_at") or datetime.datetime.min))


REQUIRED_FIELDS = {
    "customer":                    "customer / company name",
    "use_case":                    "use case (what they built with MongoDB)",
    "problem.situation":           "problem situation — current state before MongoDB",
    "problem.negative_outcomes":   "negative business consequences of the problem (≥1)",
    "solution.what_was_built":     "solution — what was built with MongoDB",
    "solution.mongodb_capabilities": "MongoDB/Atlas capabilities used (≥1)",
    "solution.why_mongodb":        "why MongoDB — the specific differentiator",
    "results.kpis":                "measurable KPI or quantified result (≥1)",
}


def _check(doc: dict) -> dict[str, bool]:
    prob = doc.get("problem") or {}
    sol  = doc.get("solution") or {}
    res  = doc.get("results") or {}
    return {
        "customer":                    bool(doc.get("customer")),
        "use_case":                    bool(doc.get("use_case")),
        "problem.situation":           bool(prob.get("situation")),
        "problem.negative_outcomes":   bool(prob.get("negative_outcomes")),
        "solution.what_was_built":     bool(sol.get("what_was_built")),
        "solution.mongodb_capabilities": bool(sol.get("mongodb_capabilities")),
        "solution.why_mongodb":        bool(sol.get("why_mongodb")),
        "results.kpis":                bool(res.get("kpis")),
    }


def _missing(doc: dict) -> list[str]:
    checks = _check(doc)
    return [REQUIRED_FIELDS[k] for k, ok in checks.items() if not ok]


def _pct(doc: dict) -> int:
    checks = _check(doc)
    return round(sum(checks.values()) / len(checks) * 100)


def _parse_chunk(text: str, existing: dict) -> dict:
    """GPT-4o extracts structured proof point fields from free-form text."""
    state = json.dumps({
        "customer": existing.get("customer"),
        "use_case": existing.get("use_case"),
        "problem":  existing.get("problem") or {},
        "solution": existing.get("solution") or {},
        "results":  existing.get("results") or {},
    }, default=str, indent=2)

    prompt = (
        "You are an expert at extracting MongoDB customer proof point content.\n\n"
        f"Current proof point state:\n{state}\n\n"
        f"New content from user:\n{text!r}\n\n"
        "Extract any new or updated information. Return ONLY valid JSON:\n"
        "{\n"
        '  "customer":  string | null,\n'
        '  "use_case":  string | null,\n'
        '  "problem": {\n'
        '    "situation": string | null,\n'
        '    "negative_outcomes": [string] | null\n'
        "  } | null,\n"
        '  "solution": {\n'
        '    "what_was_built": string | null,\n'
        '    "mongodb_capabilities": [string] | null,\n'
        '    "why_mongodb": string | null\n'
        "  } | null,\n"
        '  "results": {\n'
        '    "outcomes": [string] | null,\n'
        '    "kpis": [string] | null\n'
        "  } | null\n"
        "}\n\n"
        "Rules:\n"
        "- null means 'not mentioned in this chunk' — do not repeat existing state\n"
        "- mongodb_capabilities: extract specific technologies (e.g. 'Atlas Vector Search', "
        "'Aggregation Framework', 'Document Model', 'Change Streams', 'Atlas Search', "
        "'Time Series Collections', 'Atlas Charts', 'Voyage AI', '$vectorSearch')\n"
        "- kpis: only measurable/quantified outcomes (e.g. '40% reduction in query time')\n"
        "- outcomes: qualitative results are fine here\n"
        "- Be liberal — infer implicit capabilities (e.g. 'digital twin' implies Document Model)"
    )

    resp = openai_client.chat.completions.create(
        model=PARSE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def _merge(existing: dict, extracted: dict) -> dict:
    doc = dict(existing)
    if extracted.get("customer"): doc["customer"] = extracted["customer"]
    if extracted.get("use_case"): doc["use_case"] = extracted["use_case"]

    if extracted.get("problem"):
        prob = dict(doc.get("problem") or {})
        ep = extracted["problem"]
        if ep.get("situation"):
            prob["situation"] = ep["situation"]
        if ep.get("negative_outcomes"):
            prob["negative_outcomes"] = list(dict.fromkeys(
                (prob.get("negative_outcomes") or []) + ep["negative_outcomes"]
            ))
        doc["problem"] = prob

    if extracted.get("solution"):
        sol = dict(doc.get("solution") or {})
        es = extracted["solution"]
        if es.get("what_was_built"):
            sol["what_was_built"] = es["what_was_built"]
        if es.get("mongodb_capabilities"):
            sol["mongodb_capabilities"] = list(dict.fromkeys(
                (sol.get("mongodb_capabilities") or []) + es["mongodb_capabilities"]
            ))
        if es.get("why_mongodb"):
            sol["why_mongodb"] = es["why_mongodb"]
        doc["solution"] = sol

    if extracted.get("results"):
        res = dict(doc.get("results") or {})
        er = extracted["results"]
        if er.get("outcomes"):
            res["outcomes"] = list(dict.fromkeys(
                (res.get("outcomes") or []) + er["outcomes"]
            ))
        if er.get("kpis"):
            res["kpis"] = list(dict.fromkeys(
                (res.get("kpis") or []) + er["kpis"]
            ))
        doc["results"] = res

    return doc


def _guidance(doc: dict, captured: str) -> str:
    missing = _missing(doc)
    pct     = _pct(doc)
    lines   = []

    if captured:
        lines.append(f"✓ Captured: {captured}\n")

    if not missing:
        lines.append(f"✅ **{doc['_id']} is complete** — {pct}%, all sections filled.")
        lines.append("  Say **'render it'** to preview the storyline, or **'export'** to generate a file.")
    else:
        lines.append(f"**{doc['_id']}** · {pct}% complete · still needed:")
        for m in missing[:3]:
            lines.append(f"  · {m}")
        if len(missing) > 3:
            lines.append(f"  · …and {len(missing) - 3} more")
        lines.append(f"\n→ Tell me about: **{missing[0]}**")

    return "\n".join(lines)


def _render_text(doc: dict) -> str:
    """Render the proof point as a readable narrative."""
    prob = doc.get("problem") or {}
    sol  = doc.get("solution") or {}
    res  = doc.get("results") or {}

    lines = [
        f"## {doc.get('customer', '—')} — {doc.get('use_case', '—')}",
        f"*{doc['_id']} · {_pct(doc)}% complete*\n",
        "### 🔴 Problem",
    ]
    if prob.get("situation"):
        lines.append(prob["situation"])
    if prob.get("negative_outcomes"):
        lines.append("")
        for o in prob["negative_outcomes"]:
            lines.append(f"- {o}")

    lines += ["", "### 🔵 Solution — Why MongoDB"]
    if sol.get("what_was_built"):
        lines.append(sol["what_was_built"])
    if sol.get("mongodb_capabilities"):
        lines.append(f"\nMongoDB/Atlas: **{' · '.join(sol['mongodb_capabilities'])}**")
    if sol.get("why_mongodb"):
        lines.append(f"\n_{sol['why_mongodb']}_")

    lines += ["", "### 🟢 Results"]
    if res.get("outcomes"):
        for o in res["outcomes"]:
            lines.append(f"- {o}")
    if res.get("kpis"):
        lines.append("")
        for k in res["kpis"]:
            lines.append(f"**▶ {k}**")

    missing = _missing(doc)
    if missing:
        lines += ["", "---", f"⚠️  Still missing: {'; '.join(missing)}"]

    return "\n".join(lines)


# ─── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def ingest_content(text: str, proof_point_id: str = None) -> str:
    """
    Main conversational intake tool. Parse free-form text and add it to the
    active (or specified) proof point. Returns guidance on what was captured
    and what is still needed. Creates a new proof point automatically if none
    is active.

    Args:
        text:            Free-form description — customer situation, problem,
                         MongoDB solution, capabilities, KPIs, or any mix.
        proof_point_id:  Optional. Target a specific proof point by ID (e.g.
                         'PP-003'). Defaults to the most recently active one.
    """
    if proof_point_id:
        doc = proof_points.find_one({"_id": proof_point_id})
        if not doc:
            return f"❌ Proof point {proof_point_id} not found."
    else:
        doc = _active()
        if not doc:
            doc = {
                "_id":        _next_id(),
                "status":     "draft",
                "customer":   None,
                "use_case":   None,
                "problem":    {},
                "solution":   {},
                "results":    {},
                "created_at": datetime.datetime.now(),
                "updated_at": datetime.datetime.now(),
            }
            proof_points.insert_one(doc)

    extracted = _parse_chunk(text, doc)
    merged    = _merge(doc, extracted)
    merged["updated_at"] = datetime.datetime.now()
    proof_points.update_one({"_id": doc["_id"]}, {"$set": merged})

    # Build human-readable capture summary
    parts = []
    if extracted.get("customer"):
        parts.append(f"customer: **{extracted['customer']}**")
    if extracted.get("use_case"):
        parts.append(f"use case: {extracted['use_case']}")
    ep = (extracted.get("problem") or {})
    if ep.get("situation"):
        parts.append("problem situation")
    if ep.get("negative_outcomes"):
        parts.append(f"{len(ep['negative_outcomes'])} negative outcome(s)")
    es = (extracted.get("solution") or {})
    if es.get("what_was_built"):
        parts.append("solution description")
    if es.get("mongodb_capabilities"):
        parts.append(f"capabilities: {', '.join(es['mongodb_capabilities'])}")
    if es.get("why_mongodb"):
        parts.append("why MongoDB")
    er = (extracted.get("results") or {})
    if er.get("kpis"):
        parts.append(f"{len(er['kpis'])} KPI(s)")
    if er.get("outcomes"):
        parts.append("business outcomes")

    captured = ", ".join(parts) if parts else "noted (no new structured fields extracted — try being more specific)"
    return _guidance(merged, captured)


@mcp.tool()
def new_proof_point(customer: str = None) -> str:
    """
    Explicitly start a fresh proof point, even if one is already in progress.
    Optionally provide the customer/company name to pre-fill it.

    Args:
        customer: Optional company name (e.g. 'AT&T', 'Vodafone').
    """
    doc = {
        "_id":        _next_id(),
        "status":     "draft",
        "customer":   customer,
        "use_case":   None,
        "problem":    {},
        "solution":   {},
        "results":    {},
        "created_at": datetime.datetime.now(),
        "updated_at": datetime.datetime.now(),
    }
    proof_points.insert_one(doc)

    msg = f"📝 Started **{doc['_id']}**"
    if customer:
        msg += f" for **{customer}**"
    msg += ".\n\n" + _guidance(doc, "")
    return msg


@mcp.tool()
def get_status(proof_point_id: str = None) -> str:
    """
    Show completeness status of the active or specified proof point — what
    has been captured so far and what is still missing.

    Args:
        proof_point_id: Optional. Defaults to the most recently active proof point.
    """
    doc = proof_points.find_one({"_id": proof_point_id}) if proof_point_id else _active()
    if not doc:
        return "No active proof point. Say 'new proof point for <customer>' to start."

    prob = doc.get("problem") or {}
    sol  = doc.get("solution") or {}
    res  = doc.get("results") or {}
    checks = _check(doc)

    def tick(key): return "✓" if checks[key] else "✗"

    lines = [
        f"## {doc['_id']} — {doc.get('customer') or '(customer TBD)'} · {_pct(doc)}%\n",
        f"**Customer:**  {tick('customer')} {doc.get('customer') or '—'}",
        f"**Use case:**  {tick('use_case')} {doc.get('use_case') or '—'}",
        "",
        "**🔴 Problem**",
        f"  Situation:         {tick('problem.situation')} {(prob.get('situation') or '—')[:80]}{'…' if len(prob.get('situation') or '') > 80 else ''}",
        f"  Negative outcomes: {tick('problem.negative_outcomes')} {len(prob.get('negative_outcomes') or [])} captured",
        "",
        "**🔵 Solution**",
        f"  What was built:    {tick('solution.what_was_built')} {(sol.get('what_was_built') or '—')[:80]}{'…' if len(sol.get('what_was_built') or '') > 80 else ''}",
        f"  MongoDB tech:      {tick('solution.mongodb_capabilities')} {', '.join(sol.get('mongodb_capabilities') or []) or '—'}",
        f"  Why MongoDB:       {tick('solution.why_mongodb')} {(sol.get('why_mongodb') or '—')[:80]}{'…' if len(sol.get('why_mongodb') or '') > 80 else ''}",
        "",
        "**🟢 Results**",
        f"  KPIs:              {tick('results.kpis')} {len(res.get('kpis') or [])} captured",
        f"  Outcomes:          {'✓' if res.get('outcomes') else '○'} {len(res.get('outcomes') or [])} captured",
    ]

    missing = _missing(doc)
    if missing:
        lines += ["", f"**Still needed:** {'; '.join(missing[:4])}"]
    else:
        lines += ["", "✅ All required sections complete — ready to render or export."]

    return "\n".join(lines)


@mcp.tool()
def render_preview(proof_point_id: str = None) -> str:
    """
    Render the proof point as a polished narrative storyline. Shows the full
    Problem → Solution → Results structure so the user can review the content
    before exporting to a file. IMPORTANT: pass the output verbatim to the user —
    do not summarize it.

    Args:
        proof_point_id: Optional. Defaults to the most recently active proof point.
    """
    doc = proof_points.find_one({"_id": proof_point_id}) if proof_point_id else _active()
    if not doc:
        return "No active proof point."

    preview = _render_text(doc)

    missing = _missing(doc)
    if missing:
        preview += f"\n\n⚠️  Export blocked until complete. Missing: {', '.join(missing[:3])}"
    else:
        preview += "\n\n✅ Complete — say **'export to pptx'** or **'export to html'** to generate the file."

    return "VERBATIM:\n" + preview


@mcp.tool()
def list_proof_points(status_filter: str = None) -> str:
    """
    List all proof points with their completeness status.

    Args:
        status_filter: Optional — 'draft', 'complete', or 'published'.
    """
    query = {"status": status_filter} if status_filter else {}
    docs  = list(proof_points.find(query).sort("updated_at", DESCENDING))
    if not docs:
        return "No proof points found."

    lines = [f"**{len(docs)} proof point{'s' if len(docs) != 1 else ''}:**\n"]
    for d in docs:
        pct     = _pct(d)
        missing = len(_missing(d))
        bar     = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(
            f"- **{d['_id']}** · {d.get('customer') or '—'} · {d.get('use_case') or '—'}\n"
            f"  {bar} {pct}%  ·  {d.get('status', 'draft')}"
            + (f"  ·  {missing} field(s) missing" if missing else "  ·  ✅ complete")
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
