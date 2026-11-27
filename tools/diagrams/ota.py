"""One car and the OTA campaign beside the quote query: the network's offer and the fleet's pick.

Part of tools/diagrams (render.py draws every diagram into web/help_img/)."""
import html, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from web import fleet_network as fn

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
v = next(x for x in docs["fleet_vehicles"] if x["_id"] == "FA-0001")
c = docs["ota_campaigns"][0]
add(0, '<span class="cm">// the car · fleet_vehicles</span>')
add(0, "{")
add(1, key("_id") + s(v["_id"]) + ", " + key("fleet_id") + s(v["fleet_id"]) + ",")
add(1, key("position") + "{ " + key("type") + s(v["position"]["type"]) + ",")
add(1, " " * len("position: { ") + key("coordinates") + f'[{n(v["position"]["coordinates"][0])}, {n(v["position"]["coordinates"][1])}] }},')
add(1, key("reported_at") + dt(v["reported_at"]) + ",")
add(1, key("state") + s(v["state"]) + ",")
add(1, key("modem") + "{ " + key("serving_cell") + s(v["modem"]["serving_cell"]) + ",")
add(1, " " * len("modem: { ") + key("rat") + s(v["modem"]["rat"]) + " },")
add(1, key("software") + "{ " + key("current") + s(v["software"]["current"]) + ", " + key("target") + s(v["software"]["target"]) + " },")
add(1, key("telemetry_reserve_mbps") + n(v["telemetry_reserve_mbps"]))
add(0, "}")
add(0, "&nbsp;")
add(0, '<span class="cm">// the request · ota_campaigns</span>')
add(0, "{")
add(1, key("_id") + s(c["_id"]) + ",")
add(1, key("package") + "{ " + key("version") + s(c["package"]["version"]) + ", " + key("size_gb") + n(c["package"]["size_gb"]) + " },")
add(1, key("vehicles") + n(c["vehicles"]) + ",")
add(1, key("deadline") + dt(c["deadline"]) + ",")
add(1, key("constraints") + "{")
add(2, key("telemetry_reserve_mbps") + n(c["constraints"]["telemetry_reserve_mbps"]) + ",")
add(2, key("retail_prb_cap_pct") + n(c["constraints"]["retail_prb_cap_pct"]))
add(1, "},")
add(1, key("approval") + "{ " + key("threshold_vehicles") + n(c["approval"]["threshold_vehicles"]) + ",")
add(1, " " * len("approval: { ") + key("decided") + s(c["approval"]["decided"]) + " },")
add(1, '<span class="cm">// …</span>')
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
tr.pick td{{background:rgba(245,184,61,.13)}}
.verdict{{margin-top:10px;font-size:11px;line-height:1.4;color:#E6EEF2;background:rgba(245,184,61,.10);border-left:3px solid #F5B83D;padding:7px 10px;border-radius:4px}} .verdict b{{color:#F5B83D;font-weight:600}}
.foot{{margin-top:auto;font-size:9px;color:#6B8394;line-height:1.4}} .foot b{{color:#93A9B8;font-weight:500}}
</style></head><body><div class="wrap">
<div class="json">{code}</div>
<div class="right">
  <div class="eyebrow">ONE QUERY ON MONGODB ATLAS</div>
  <h1>Where should this car download?</h1>
  <div class="sub">The car and the campaign on the left are the input. Atlas answers in a fraction of a second.</div>
  <div class="step"><div class="num" style="background:#00ED64">1</div><div><div class="st">Find the 5G cells around the car</div><div class="sd">every capacity cell within 2 miles of its position</div></div></div>
  <div class="step"><div class="num" style="background:#6EA8FE">2</div><div><div class="st">Measure how busy each cell is right now</div><div class="sd">from the live performance counters of the last 15 minutes</div></div></div>
  <div class="step"><div class="num" style="background:#B98CFF">3</div><div><div class="st">Check the transport path behind each cell</div><div class="sd">a full backhaul link rules a cell out, even when its radio is idle</div></div></div>
  <div class="step"><div class="num" style="background:#F5B83D">4</div><div><div class="st">Price each cell for the car</div><div class="sd">a tier and the expected Mbit/s, never the raw load</div></div></div>
  <!-- the result of the live quote for FA-0001 at 13:01 (GET /api/fleet/quote), checked when drawn -->
  <table>
    <tr><th>Cell</th><th>Tier</th><th>Expected</th><th>Network's offer</th></tr>
    <tr><td>Herald Square<br><span style="color:#93A9B8;font-size:9px">the car's cell now</span></td><td class="bad">congested</td><td class="bad">6.6 Mbit/s</td><td class="bad">not now</td></tr>
    <tr><td>Hell's Kitchen</td><td>discounted</td><td>52.0 Mbit/s</td><td class="bad">not offered: backhaul full</td></tr>
    <tr><td>Chelsea</td><td>discounted</td><td>47.2 Mbit/s</td><td>offered</td></tr>
    <tr class="pick"><td>Murray Hill</td><td>discounted</td><td class="ok">44.8 Mbit/s</td><td class="ok">offered</td></tr>
  </table>
  <div class="verdict"><b>Fleet's choice:</b> Murray Hill. FA-0001's next ride ends there at 14:09, so the car downloads on its way, with no extra driving. The fleet knows its rides; the network never sees them.</div>
  <div class="foot"><b>In MongoDB terms:</b> $geoNear · $lookup into the time series ($percentile) · $graphLookup over the backhaul · $switch for the tier. The fleet's car and campaign meet live network data in one query, with no copy in a second system.</div>
</div></div></div></body></html>"""
open(out, "w").write(page)
print(Path(out).name, nlines, "lines, font", round(fs, 2), "px at 1x")
