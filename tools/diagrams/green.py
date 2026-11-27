"""A capacity carrier and the energy policy beside the query "Can this carrier sleep?".

Part of tools/diagrams (render.py draws every diagram into web/help_img/)."""
import html, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from web import ran_network as fn

out = sys.argv[1]
docs = fn.documents()
E = html.escape
def key(k): return f'<span class="k">{E(k)}</span>: '
def s(v): return f'<span class="s">"{E(v)}"</span>'
def n(v): return f'<span class="n">{v}</span>'
def dt(v): return f'<span class="d">ISODate("{v.strftime("%Y-%m-%dT%H:%M:%S")}-05:00")</span>'
def pt(p): return "{ " + key("type") + s(p["type"]) + ", " + key("coordinates") + f'[{n(p["coordinates"][0])}, {n(p["coordinates"][1])}] }}'
L = []   # (indent, html, css class)
add = lambda i, h, c="": L.append((i, h, c))
docs = fn.documents() if hasattr(fn, "documents") else fn.model()
c = next(x for x in docs["ran_cells"] if x["_id"] == "NYC-MTN-0412-N77-B")
pol = docs["energy_policy"][0]
add(0, '<span class="cm">// the carrier · ran_cells</span>')
add(0, "{")
add(1, key("_id") + s(c["_id"]) + ",")
add(1, key("site_id") + s(c["site_id"]) + ", " + key("sector") + s(c["sector"]) + ",")
add(1, key("location") + "{ " + key("type") + s(c["location"]["type"]) + ",")
add(1, " " * len("location: { ") + key("coordinates") + f'[{n(c["location"]["coordinates"][0])}, {n(c["location"]["coordinates"][1])}] }},')
add(1, key("layer") + s(c["layer"]) + ", " + key("band") + s(c["band"]) + ",")
add(1, key("energy_profile") + "{ " + key("idle_w") + n(c["energy_profile"]["idle_w"]) + ", " + key("deep_sleep_w") + n(c["energy_profile"]["deep_sleep_w"]) + ",")
add(1, " " * len("energy_profile: { ") + key("wake_time_s") + n(c["energy_profile"]["wake_time_s"]) + " },")
add(1, key("isESCoveredBy") + "[" + s(c["isESCoveredBy"][0]) + "],")
add(1, '<span class="cm">// …</span>')
add(0, "}")
add(0, "&nbsp;")
add(0, '<span class="cm">// the rules · energy_policy</span>')
add(0, "{")
add(1, key("_id") + s(pol["_id"]) + ", " + key("level") + s(pol["level"]) + ",")
add(1, key("sleep_if") + "{")
add(2, key("forecast_load_next_hour_lt_pct") + n(pol["sleep_if"]["forecast_load_next_hour_lt_pct"]) + ",")
add(2, key("coverage_after_offload_lt_pct") + n(pol["sleep_if"]["coverage_after_offload_lt_pct"]))
add(1, "},")
add(1, key("wake_if") + "{ " + key("coverage_load_gt_pct") + n(pol["wake_if"]["coverage_load_gt_pct"]) + ",")
add(1, " " * len("wake_if: { ") + key("for_minutes") + n(pol["wake_if"]["for_minutes"]) + " },")
add(1, key("max_capacity_asleep_pct") + n(pol["max_capacity_asleep_pct"]) + ",")
add(1, key("never_sleep") + "[" + s(pol["never_sleep"][0]) + ", " + s(pol["never_sleep"][1]) + "]")
add(0, "}")

nlines = len(L)
H, PAD = 540, 22
lh = (H - 2 * PAD) / nlines
import re
widest = max(len(re.sub(r'<[^>]+>', '', html.unescape(h))) + i * 2 for i, h, _ in L)
fs = min(13, lh * 0.84, (520 - 28) / (widest * 0.6))
code = "".join(f'<div class="ln {c}" style="padding-left:{i * 2}ch">{h}</div>' for i, h, c in L)

page = f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Lexend+Deca:wght@400;500;600&family=Source+Code+Pro:wght@400;500&display=swap" rel="stylesheet">
<style>
html,body{{margin:0;background:#0D1B26;width:960px;height:540px;overflow:hidden;font-family:"Lexend Deca","Helvetica Neue",Arial,sans-serif;color:#E6EEF2}}
.wrap{{display:flex;height:540px}}
.json{{flex:none;width:520px;background:#0A141C;padding:{PAD}px 14px;box-sizing:border-box;font-family:"Source Code Pro",Menlo,monospace;font-size:{fs:.2f}px;line-height:{lh:.2f}px;color:#C9D4DC;overflow:hidden}}
.ln{{white-space:pre}}
.ln{{border-radius:3px}}
.k{{color:#4FD8EB;font-weight:500}} .s{{color:#7EE787}} .n{{color:#F2CC60}} .d{{color:#A5B4FC}} .cm{{color:#6B8394;font-style:italic}}

.right{{flex:1;padding:22px 24px 16px 26px;box-sizing:border-box;display:flex;flex-direction:column}}
.eyebrow{{font-size:10px;font-weight:600;letter-spacing:.09em;color:#00ED64}}
h1{{font-size:19px;font-weight:600;margin:4px 0 2px}}
.sub{{font-size:10.5px;color:#93A9B8;margin-bottom:12px}}
.step{{display:flex;gap:10px;margin-bottom:9px;align-items:flex-start}}
.num{{flex:none;width:20px;height:20px;border-radius:50%;font-size:10.5px;font-weight:600;display:flex;align-items:center;justify-content:center;color:#0D1B26}}
.st{{font-size:12px;font-weight:500;line-height:1.25}} .sd{{font-size:10px;color:#93A9B8;line-height:1.3;margin-top:1px}}
table{{border-collapse:collapse;width:100%;margin-top:6px;font-size:10.5px}}
th{{text-align:left;font-weight:500;color:#93A9B8;font-size:9.5px;padding:3px 6px;border-bottom:1px solid #22384A}}
td{{white-space:nowrap;padding:5px 6px;border-bottom:1px solid #1A2E3D}}
tr.win td{{background:rgba(0,237,100,.12)}} .bad{{color:#FF7A7A}} .ok{{color:#00ED64;font-weight:600}}
tr.pick td{{background:rgba(245,184,61,.13)}} tr.win td{{background:rgba(0,237,100,.12)}}
.verdict{{margin-top:10px;font-size:11px;line-height:1.4;color:#E6EEF2;background:rgba(110,168,254,.10);border-left:3px solid #6EA8FE;padding:7px 10px;border-radius:4px}} .verdict b{{color:#6EA8FE;font-weight:600}}
.foot{{margin-top:auto;font-size:9px;color:#6B8394;line-height:1.4}} .foot b{{color:#93A9B8;font-weight:500}}
</style></head><body><div class="wrap">
<div class="json">{code}</div>
<div class="right">
  <div class="eyebrow">ONE QUERY ON MONGODB ATLAS</div>
  <h1>Can this carrier sleep?</h1>
  <div class="sub">Asked for every 5G capacity carrier before the agent puts it to sleep. The carrier and the rules on the left are the input.</div>
  <div class="step"><div class="num" style="background:#00ED64">1</div><div><div class="st">Measure how busy it is right now</div><div class="sd">from the live performance counters of the last 15 minutes</div></div></div>
  <div class="step"><div class="num" style="background:#6EA8FE">2</div><div><div class="st">Read the expected load for the next hour</div><div class="sd">from the operator's load forecast, stored in Atlas next to the counters</div></div></div>
  <div class="step"><div class="num" style="background:#B98CFF">3</div><div><div class="st">Find the cell that takes over its users</div><div class="sd">the coverage cell behind it must stay under 55% with them</div></div></div>
  <div class="step"><div class="num" style="background:#F5B83D">4</div><div><div class="st">Check every promise that covers it</div><div class="sd">a hospital slice or another commitment means: never</div></div></div>
  <table>
    <tr><th>Carrier at 01:30</th><th>Now</th><th>Next hour</th><th>Can it sleep?</th></tr>
    <tr><td>Penn Station · B</td><td class="bad">85%</td><td>10%</td><td class="bad">no: a crowd, now</td></tr>
    <tr><td>Grand Central · A</td><td>9%</td><td>9%</td><td class="bad">no: hospital slice</td></tr>
    <tr class="win"><td>Chelsea · A</td><td>10%</td><td class="ok">12%</td><td class="ok">yes</td></tr>
  </table>
  <div class="verdict"><b>Measured beats forecast:</b> the forecast says Penn Station is quiet, the counters say a subway disruption just sent a crowd up to the street. Atlas returns 37 of 59 carriers as safe to sleep, and Penn Station is not one of them.</div>
  <div class="foot"><b>In MongoDB terms:</b> $lookup into the time series ($percentile) · $lookup the forecast · $graphLookup over isESCoveredBy · $lookup the service commitments. Inventory, live counters, forecast and promises meet in one query, with no copy in a second system.</div>
</div></div></div></body></html>"""
open(out, "w").write(page)
print(Path(out).name, nlines, "lines, font", round(fs, 2), "px at 1x")
