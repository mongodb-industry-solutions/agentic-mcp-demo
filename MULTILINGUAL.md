# Multilingual support — current situation and proposed approach

**Question.** Can the shell be talked with in a language other than English (German, French, Spanish, etc.) without rewriting the framework?

**Short answer.** Most of the framework is already language-agnostic. Three friction layers in `agents/orchestrator.py` hold it back; all three are fixable with prompt changes plus one extra LLM call per turn, no schema change. ~1 day of work.

---

## 1. What works today without any change

| Layer | Why it's already multilingual |
|---|---|
| **Vector search routing** (Stage 2) | The Atlas vector indexes use **voyage-4**, which is genuinely multilingual. A German query *"was kann ich für meine Aufgaben tun?"* embeds close to `todo_service`'s English description — cross-lingual cosine works. |
| **`agent_workstreams` / `agent_memories` / `agent_history` schemas** | All language-neutral fields. Only `text` / `title` / `summary` carry natural language, and they store whatever the producer wrote. |
| **MCP service catalogue** | Docstrings stay English; voyage-4 bridges them to multilingual queries. The LLM tie-break sees English docstrings but reasons over multilingual input — `gpt-4o-mini` handles that fine. |
| **The graph, geo, time-series, change-streams layers** | No language anywhere. |
| **`$graphLookup`, `$vectorSearch`, `$lookup`** | Query operators, not text. |

## 2. Where English is hardcoded (and how it bites)

Three friction layers in `agents/orchestrator.py`:

### 2.1 Keyword heuristics (the brittle ones)

**Pure-closure guard.** `_classify_workstream` runs a Python regex on a literal list:
```python
closure_verbs = ("done", "finished", "wrap up", "wrap-up",
                 "that's it", "thats it", "we're done", "we are done",
                 "all done", "complete", "completed", "no more")
```
A German user typing *"fertig mit den TODOs"* or *"erledigt"* matches none of these → the closure-short-circuit fast path doesn't fire → the orchestrator runs Stage 1 / Stage 2 / ReAct and the LLM speculates a tool call ("let me list_todos to confirm").

**Self-contained verb gate.** `process_query` decides whether to run the context-enrichment branch based on the first word of the query:
```python
_SELF_CONTAINED = {"list", "show", "add", "update", "delete", "remove",
                   "change", "set", "refresh", "display", "what", "how",
                   "get", "find", "create", "book", "confirm", "cancel",
                   "check", "search", "buy"}
```
*"zeig mir meine Aufgaben"* is self-contained semantically but starts with `zeig` → wrong branch chosen.

**Deterministic Stage 1 pre-check.** Matches literal domain names like `ibn`, `dtw`, `todo`:
```python
explicit = [d for d in by_domain
            if re.search(rf"\b{re.escape(d.lower())}\b", ql)]
```
This one mostly works without changes — domain names are language-neutral identifiers and users typically code-switch the technical noun: *"was sind meine todos"* still hits `todo`. No fix needed.

### 2.2 LLM prompts (the salvageable layer)

Every classifier / extractor prompt is hand-written in English with English-only examples:

- `_classify_workstream` shows *"I'm opening a new store"* and *"done with the setup of Marienplatz network"* as illustrative cues.
- `_classify_domain` describes the taxonomy in English.
- `_extract_memories` lists English GOOD/BAD fact examples.
- `_propose_new_workstream` proposes titles in English.

`gpt-4o-mini` and `gpt-4o` speak German (and most other languages) natively, but their *behaviour* is anchored on the English examples in the prompt. In practice:
- They often respond in English even to German input.
- They match closure patterns by lexical similarity to the English examples.
- Workstream titles and extracted facts come out in English regardless of user language.

### 2.3 System prompt (the agent's voice)

`_SYSTEM_PROMPT` is in English and doesn't explicitly tell the agent to mirror the user's language. Net effect: the agent narrates in English even when the user is in German.

## 3. Proposed approach — pragmatic, no schema change

In order of payoff:

### Step 1 — Detect language once per turn

A small `gpt-4o-mini` call at the top of `process_query`:
> *"Return JSON: {"lang": "en|de|fr|es|...", "lang_full": "English|German|…"}"*

~50 tokens, ~150 ms. Cache on `self.current_language` for the turn. The Stage 1 / Stage 2 / extractor LLM calls reuse it.

### Step 2 — Tell every LLM prompt about the user's language

Every classifier / extractor / agent-system prompt gets a one-line hint:
> *"The user is speaking in **{lang_full}**. Recognise closure cues, action verbs, and entity references in that language. Your JSON output uses English keys; your free-text content (titles, summaries, fact texts) uses {lang_full}."*

This single addition rescues every LLM-based decision in the framework — classifier closure detection, workstream titles, memory extraction, ReAct narration.

### Step 3 — Make the English keyword heuristics conditional on `lang == "en"`

The Python regex paths (closure verbs, self-contained verbs) are *optimisations* that skip an LLM call on common English inputs. Keep them only when the detected language is English; for everything else, let the classifier's own LLM response do the work.

Concretely:
- Pure-closure detection moves out of the Python regex and into the classifier's JSON return — the prompt already asks for `closes_workstream`; just add a `was_pure_closure` boolean it returns.
- `_SELF_CONTAINED` gate gets an `if lang == "en"` wrapper; non-English queries always go through the enrichment path, which is multilingual via the LLM.

### Step 4 — Tell the agent's system prompt to mirror the user's language

One additional line at the end of `_SYSTEM_PROMPT`:
> *"Respond in the user's language ({lang_full}). MCP tool results are English-formatted strings; narrate around them in the user's language."*

The Markdown panels then read like:
```
You: was sind meine offenen Workstreams?
🤖 Es sind 3 Workstreams aktiv: WS-2026-05-23-001 (IBN), …
```

### Step 5 — Leave service docstrings in English

voyage-4's cross-lingual embeddings already bridge the gap. Translating ~30 docstrings into each supported language is high cost / low marginal benefit. The LLM tie-break can read English docstrings while reasoning about a German query without confusion.

## 4. What does NOT work even after these changes

A few honest caveats:

- **MCP tool-call results** are English-formatted strings (e.g. *"📝 Intent IBN-005 captured for Marienplatz Munich. Targets: POS ≤40ms · 99.95% availability · strict segmentation …"*). These are emitted by Python f-strings in the MCP services themselves. The agent's narration around them is multilingual after Step 4, but the inline result quotation stays English unless we also translate the MCP services' string formatters (large-scope change, not worth it for a demo).
- **Domain tags** (`ibn`, `dtw`, `todo`) stay as language-neutral identifiers — they're not display strings.
- **Service names** (`ibn_intent_service`, etc.) stay English — they're code identifiers.
- **Workstream IDs and entity IDs** (`WS-2026-05-23-001`, `IBN-005`) stay format-neutral — they're audit-trail handles.

This is the right boundary. The user-facing conversation is multilingual; the operational identifiers are stable English/code throughout, which is exactly what an enterprise customer wants for audit and cross-team observability.

## 5. Cost summary

| Change | One-time cost | Per-turn cost |
|---|---|---|
| Step 1 (language detect) | ~50 lines of code | +1 small LLM call (~50 tokens, ~150 ms) |
| Step 2 (prompt hints) | Touch ~5 prompts | 0 |
| Step 3 (English-only heuristic gate) | Touch 2 call sites | Net zero — English fast-path retained, non-English uses existing LLM path |
| Step 4 (agent system prompt) | 1 line | 0 |
| Step 5 (leave docstrings alone) | 0 | 0 |

**Total: ~1 engineer-day. No schema migration. No reindexing. No MCP service changes.**

A non-English customer pays one extra ~150 ms LLM call per turn (the language detect) — well below the per-turn budget already spent in Stage 1 + Stage 2 + ReAct.

## 6. Demo angle

If MongoDB sales wants a multilingual flourish in the live demo:

> *"Watch this — same orchestrator, same Atlas cluster, no reindexing."*
>
> ```
> You: I'm opening a new Alpenmarkt store at Marienplatz Munich…
> 🤖 Intent IBN-005 has been submitted…
>
> You: Ich eröffne einen neuen Alpenmarkt am Marienplatz München…
> 🤖 Intent IBN-006 wurde für den neuen Alpenmarkt am Marienplatz erfasst…
> ```
>
> *"The vector search routes both queries the same way — voyage-4 is multilingual, so the English docstrings match German queries via cross-lingual cosine. The classifier detects the language, the agent narrates in that language, and the workstream summary is auto-stored in German. Same memory plane, same routing brain, two languages with no extra infrastructure."*

It also lands the *"agent-state in Atlas is language-agnostic"* point: every workstream, every memory, every history entry can be queried regardless of which language produced it, because the schema fields are language-neutral and the embedded text is matched by a multilingual model.
