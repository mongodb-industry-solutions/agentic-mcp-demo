/*
 * Copyright (c) 2026 MongoDB Inc.
 * Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
 *
 * The dashboards' "Data model" and "Hero queries" popups. Data from
 * GET <base>/api/showcase (web/showcase.py); documents and pipelines are
 * rendered in mongosh syntax with highlighting and numbered callouts.
 *
 *   Showcase.mount({container, base, query, mode})
 */
(function () {
  "use strict";

  const CSS = `
  .sc-btn { font: 600 12px/1 var(--sc-mono); color: #E8F4EE; background: rgba(0,237,100,.10);
    border: 1px solid rgba(0,237,100,.45); border-radius: 6px; padding: 6px 10px; cursor: pointer;
    white-space: nowrap; }
  .sc-btn:hover { background: rgba(0,237,100,.20); }
  .sc-btn:focus-visible { outline: 2px solid #00ED64; outline-offset: 2px; }
  .sc-overlay { position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,.72);
    display: flex; align-items: center; justify-content: center; padding: 16px; }
  .sc-modal { --sc-mono: 'JetBrains Mono','SF Mono','Cascadia Code',Menlo,monospace;
    width: min(1180px, 100%); height: min(86vh, 900px); display: grid;
    grid-template-columns: 230px minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); background: #0B0F0D;
    color: #E8E8E8; border: 1px solid #1F2A24; border-radius: 12px; overflow: hidden;
    font: 13px/1.45 Inter,-apple-system,'Segoe UI',sans-serif; box-shadow: 0 20px 60px rgba(0,0,0,.6); }
  .sc-head { grid-column: 1 / -1; display: flex; align-items: center; gap: 12px;
    padding: 12px 16px; border-bottom: 1px solid #1F2A24; background: #0F1512; }
  .sc-head h2 { margin: 0; font-size: 15px; font-weight: 700; color: #fff; }
  .sc-head .sc-leaf { color: #00ED64; }
  .sc-head .sc-sp { flex: 1; }
  .sc-copy, .sc-close { font: 12px var(--sc-mono); color: #C8D3CC; background: transparent;
    border: 1px solid #2A3A31; border-radius: 6px; padding: 5px 9px; cursor: pointer; }
  .sc-copy:hover, .sc-close:hover { border-color: #00ED64; color: #fff; }
  .sc-tabs { min-width: 0; overflow-y: auto; border-right: 1px solid #1F2A24; padding: 8px; background: #0D120F; }
  .sc-tab { display: block; width: 100%; text-align: left; background: none; border: 0;
    border-left: 3px solid transparent; color: #C8D3CC; padding: 8px 10px; border-radius: 4px;
    cursor: pointer; font: inherit; }
  .sc-tab small { display: block; color: #7D8C83; font-size: 11px; margin-top: 2px; }
  .sc-tab:hover { background: #141C17; }
  .sc-tab.on { background: #13211A; border-left-color: #00ED64; color: #fff; }
  .sc-body { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .sc-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 16px;
    border-bottom: 1px solid #1A231E; color: #9AA8A0; font-size: 12px; }
  .sc-meta code { font-family: var(--sc-mono); color: #E8E8E8; }
  .sc-badge { font: 700 10px/1 var(--sc-mono); letter-spacing: .06em; padding: 4px 7px; border-radius: 999px; }
  .sc-badge.live { background: rgba(0,237,100,.15); color: #00ED64; border: 1px solid rgba(0,237,100,.5); }
  .sc-badge.example { background: rgba(255,209,102,.10); color: #FFD166; border: 1px solid rgba(255,209,102,.45); }
  .sc-code { flex: 1; min-height: 0; overflow: auto; margin: 0; padding: 14px 16px;
    font: 12.5px/1.6 var(--sc-mono); background: #080B09; white-space: pre; tab-size: 2; }
  .sc-explain { padding: 12px 16px; border-top: 1px solid #1A231E; background: #0D120F; }
  .sc-explain p { margin: 0 0 8px; color: #D5DDD8; max-width: 90ch; }
  .sc-legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 0; padding: 0; list-style: none;
    color: #B8C4BD; font-size: 12px; }
  .sc-mk { display: inline-flex; align-items: center; justify-content: center; min-width: 18px;
    height: 18px; padding: 0 4px; margin-left: 10px; border-radius: 9px; background: #FFD166;
    color: #111; font: 700 11px/1 var(--sc-mono); vertical-align: 1px; }
  .sc-legend .sc-mk { margin: 0 6px 0 0; }
  .sc-hl { background: rgba(255,209,102,.07); display: inline-block; min-width: 100%; }
  .sc-empty { color: #7D8C83; font-style: italic; }
  .sc-modal.exec .sc-explain { order: -1; border-top: 0; border-bottom: 1px solid #1A231E; }
  .sc-k { color: #74DEFF; } .sc-s { color: #00ED64; } .sc-n { color: #FFD166; }
  .sc-b { color: #FF7AC6; } .sc-op { color: #C792FF; font-weight: 700; } .sc-path { color: #FF9F5A; }
  .sc-d { color: #8AB4FF; } .sc-p { color: #6F7C74; } .sc-c { color: #5F6E66; font-style: italic; }
  .sc-fn { color: #E8E8E8; }
  @media (max-width: 760px) {
    .sc-modal { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto auto minmax(0, 1fr); height: 92vh; }
    .sc-head { flex-wrap: wrap; padding: 10px 12px; } .sc-head h2 { font-size: 14px; }
    .sc-overlay { padding: 8px; }
    .sc-tabs { display: flex; overflow-x: auto; border-right: 0; border-bottom: 1px solid #1F2A24; padding: 6px; }
    .sc-tab { width: auto; white-space: nowrap; border-left: 0; border-bottom: 3px solid transparent; }
    .sc-tab.on { border-bottom-color: #00ED64; }
    .sc-tab small { display: none; }
  }`;

  const MARKS = "①②③④⑤⑥⑦⑧⑨";
  const IDENT = /^[A-Za-z_$][\w$]*$/;
  const DATE_STR = /^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d/;
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // ── mongosh rendering: a token list, then lines ─────────────────────────
  // `full` (when set) is what Copy puts on the clipboard; `text` is shown.
  function tok(text, cls, full) { return { text, cls, full }; }
  const SHOW_STR = 80;    // characters of a string shown on one line

  function scalar(v) {
    if (v === null) return [tok("null", "b")];
    if (typeof v === "boolean") return [tok(String(v), "b")];
    if (typeof v === "number") {   // long decimals shortened on screen, whole when copied
      const short = Number.isInteger(v) ? String(v) : String(+v.toFixed(4));
      return [tok(short, "n", short === String(v) ? undefined : String(v))];
    }
    if (typeof v === "string") {
      if (DATE_STR.test(v)) return [tok("ISODate(", "fn"), tok(JSON.stringify(v), "d"), tok(")", "fn")];
      if (v.startsWith("$") && IDENT.test(v.slice(1).replace(/\./g, "_"))) return [tok(JSON.stringify(v), "path")];
      if (v.startsWith("… ") && v.endsWith(" more")) return [tok("/* " + v + " */", "c")];
      if (v.length > SHOW_STR) return [tok(JSON.stringify(v.slice(0, SHOW_STR - 1) + "…"), "s", JSON.stringify(v))];
      return [tok(JSON.stringify(v), "s")];
    }
    return [tok(JSON.stringify(v), "s")];
  }

  function special(v) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const keys = Object.keys(v);
      if (keys.length === 1 && keys[0] === "$date") return [tok("ISODate(", "fn"), tok(JSON.stringify(v.$date), "d"), tok(")", "fn")];
      if (keys.length === 1 && keys[0] === "$oid") return [tok("ObjectId(", "fn"), tok(JSON.stringify(v.$oid), "d"), tok(")", "fn")];
    }
    return null;
  }

  function keyTok(k) {
    const txt = IDENT.test(k) ? k : JSON.stringify(k);
    return tok(txt, k.startsWith("$") ? "op" : "k");
  }

  function flat(tokens) { return tokens.map((t) => t.text).join(""); }
  function full(tokens) { return tokens.map((t) => t.full || t.text).join(""); }

  // Inline form of a value, or null when it would be too long / nested deep.
  function inline(v, depth) {
    const sp = special(v);
    if (sp) return sp;
    if (v === null || typeof v !== "object") return scalar(v);
    if (depth > 2) return null;
    const out = [];
    if (Array.isArray(v)) {
      if (!v.length) return [tok("[]", "p")];
      out.push(tok("[ ", "p"));
      for (let i = 0; i < v.length; i++) {
        const t = inline(v[i], depth + 1); if (!t) return null;
        out.push(...t); if (i < v.length - 1) out.push(tok(", ", "p"));
      }
      out.push(tok(" ]", "p"));
    } else {
      const keys = Object.keys(v);
      if (!keys.length) return [tok("{}", "p")];
      out.push(tok("{ ", "p"));
      for (let i = 0; i < keys.length; i++) {
        const t = inline(v[keys[i]], depth + 1); if (!t) return null;
        out.push(keyTok(keys[i]), tok(": ", "p"), ...t); if (i < keys.length - 1) out.push(tok(", ", "p"));
      }
      out.push(tok(" }", "p"));
    }
    return out;
  }

  // Lines (token arrays) of a value at an indent; `lead` goes before the value.
  function block(v, ind, lead, trail) {
    const pad = "  ".repeat(ind);
    const one = inline(v, 0);
    if (v === null || typeof v !== "object" || special(v)) {   // a scalar stays on its line
      return [[tok(pad, "p"), ...lead, ...one, tok(trail, "p")]];
    }
    if (one && pad.length + flat(lead).length + flat(one).length + trail.length <= 92) {
      return [[tok(pad, "p"), ...lead, ...one, tok(trail, "p")]];
    }
    const isArr = Array.isArray(v);
    const lines = [[tok(pad, "p"), ...lead, tok(isArr ? "[" : "{", "p")]];
    const entries = isArr ? v.map((x) => [null, x]) : Object.entries(v);
    entries.forEach(([k, x], i) => {
      const comma = i < entries.length - 1 ? "," : "";
      lines.push(...block(x, ind + 1, k === null ? [] : [keyTok(k), tok(": ", "p")], comma));
    });
    lines.push([tok(pad, "p"), tok((isArr ? "]" : "}") + trail, "p")]);
    return lines;
  }

  function comment(text) { return [tok("// " + text, "c")]; }

  function queryLines(q) {
    const coll = /^[A-Za-z_][\w]*$/.test(q.collection) ? q.collection : `getCollection(${JSON.stringify(q.collection)})`;
    if (q.op === "watch") {
      const head = [tok("db", "fn"), tok("." + coll, "k"), tok(".watch(", "fn")];
      const opts = inline(q.options || {}, 0);
      return [[...head, tok("[], ", "p"), ...opts, tok(")", "fn")]];
    }
    const lines = [[tok("db", "fn"), tok("." + coll, "k"), tok(".aggregate([", "fn")]];
    (q.pipeline || []).forEach((stage, i) => {
      lines.push(...block(stage, 1, [], i < q.pipeline.length - 1 ? "," : ""));
    });
    lines.push([tok("])", "fn")]);
    return lines;
  }

  function modelLines(m) {   // the index or collection options first: they are the point
    const lines = [];
    const gap = () => { if (lines.length) lines.push([]); };
    (m.extras || []).forEach((e) => {
      gap(); lines.push(comment(e.label)); lines.push(...block(e.value, 0, [], ""));
    });
    const names = String(m.collection).split(" · ");
    (m.docs || []).forEach((d, i) => {
      gap(); lines.push(comment(`a document in ${names[Math.min(i, names.length - 1)]}`));
      lines.push(...block(d, 0, [], ""));
    });
    if (!lines.length) lines.push(comment("nothing in this lane yet"));
    return lines;
  }

  function render(lines, callouts) {
    const plain = lines.map(flat);
    const at = new Map();   // line index → callout, first unmarked line that contains `find`
    (callouts || []).forEach((c) => {
      const j = plain.findIndex((p, k) => !at.has(k) && !p.trimStart().startsWith("//") && p.includes(c.find));
      if (j >= 0) at.set(j, c);
    });
    const order = [...at.keys()].sort((a, b) => a - b);   // numbered in reading order
    const num = new Map(order.map((j, i) => [j, i]));
    const html = lines.map((ln, j) => {
      const body = ln.map((t) => `<span class="sc-${t.cls}">${esc(t.text)}</span>`).join("");
      if (!at.has(j)) return body;
      return `<span class="sc-hl">${body}<span class="sc-mk">${MARKS[num.get(j)] || num.get(j) + 1}</span></span>`;
    }).join("\n");
    const found = order.map((j, i) => ({ ...at.get(j), n: i }));
    return { html, text: lines.map(full).join("\n"), found };
  }

  // ── the popup ────────────────────────────────────────────────────────────
  let opts = null;

  function open(kind) {
    const ov = document.createElement("div");
    ov.className = "sc-overlay";
    ov.innerHTML = `<div class="sc-modal ${opts.mode === "exec" ? "exec" : ""}" role="dialog" aria-modal="true">
      <div class="sc-head"><h2><span class="sc-leaf">◆</span> ${kind === "model" ? "Data model: the network twin in MongoDB" : "Hero queries"}</h2>
        <span class="sc-sp"></span><button class="sc-copy" type="button">Copy</button>
        <button class="sc-close" type="button" aria-label="Close">✕</button></div>
      <nav class="sc-tabs"></nav>
      <div class="sc-body"><div class="sc-meta"></div><pre class="sc-code"><span class="sc-empty">loading…</span></pre>
        <div class="sc-explain"></div></div></div>`;
    document.body.appendChild(ov);
    const $ = (s) => ov.querySelector(s);
    const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
    $(".sc-close").onclick = close;
    let current = "";
    $(".sc-copy").onclick = async () => {
      try { await navigator.clipboard.writeText(current); $(".sc-copy").textContent = "Copied"; }
      catch (e) { $(".sc-copy").textContent = "Copy failed"; }
      setTimeout(() => { $(".sc-copy").textContent = kind === "model" ? "Copy" : "Copy as mongosh"; }, 1500);
    };
    $(".sc-copy").textContent = kind === "model" ? "Copy" : "Copy as mongosh";

    fetch(`${opts.base}/api/showcase${opts.query ? "?" + opts.query : ""}`)
      .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then((data) => {
        const items = kind === "model" ? data.model : data.queries;
        const tabs = $(".sc-tabs");
        items.forEach((it, i) => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "sc-tab";
          b.innerHTML = `${esc(it.title)}<small>${esc(it.subtitle || "")}</small>`;
          b.onclick = () => show(i);
          tabs.appendChild(b);
        });
        function show(i) {
          const it = items[i];
          tabs.querySelectorAll(".sc-tab").forEach((b, j) => b.classList.toggle("on", j === i));
          const out = render(kind === "model" ? modelLines(it) : queryLines(it), it.callouts);
          current = out.text;
          $(".sc-code").innerHTML = out.html;
          $(".sc-code").scrollTop = 0;
          const badge = kind === "model" ? `<span class="sc-badge live">LIVE DATA</span>`
            : `<span class="sc-badge ${it.source}">${it.source === "live" ? "LIVE" : "EXAMPLE"}</span>`;
          const ran = it.ran_at && it.ran_at.$date ? ` · ran ${esc(it.ran_at.$date.replace("T", " "))}` : "";
          $(".sc-meta").innerHTML = `${badge}<code>${esc(it.collection)}</code>`
            + (it.note || ran ? `<span>${esc(it.note || "")}${ran}</span>` : "");
          $(".sc-explain").innerHTML = `<p>${esc(it.caption || "")}</p>`
            + (out.found.length ? `<ul class="sc-legend">${out.found.map((c) =>
                `<li><span class="sc-mk">${MARKS[c.n] || c.n + 1}</span>${esc(c.label)}</li>`).join("")}</ul>` : "");
        }
        if (items.length) show(0);
      })
      .catch((e) => { $(".sc-code").innerHTML = `<span class="sc-empty">Could not load: ${esc(e.message)}</span>`; });
  }

  window.Showcase = {
    mount(o) {
      opts = { base: "", query: "", mode: "eng", ...o };
      if (!document.getElementById("sc-style")) {
        const st = document.createElement("style");
        st.id = "sc-style"; st.textContent = ":root{--sc-mono:'JetBrains Mono','SF Mono',Menlo,monospace}" + CSS;
        document.head.appendChild(st);
      }
      [["model", "{ } Data model"], ["queries", "⚡ Hero queries"]].forEach(([kind, label]) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "sc-btn"; b.textContent = label;
        b.onclick = () => open(kind);
        o.container.appendChild(b);
      });
    },
    _render: { queryLines, modelLines, render },   // for tests
  };
})();
