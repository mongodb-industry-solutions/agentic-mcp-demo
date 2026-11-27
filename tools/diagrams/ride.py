"""The ride request (from the seed) beside the route-scoring query, explained for IT decision makers.

Part of tools/diagrams (render.py draws every diagram into web/help_img/)."""
import html, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from web import fleet_network as fn

out = sys.argv[1]
d = fn.documents()["ride_requests"][0]
E = html.escape
def key(k): return f'<span class="k">{E(k)}</span>: '
def s(v): return f'<span class="s">"{E(v)}"</span>'
def n(v): return f'<span class="n">{v}</span>'
def dt(v): return f'<span class="d">ISODate("{v.strftime("%Y-%m-%dT%H:%M:%S")}-05:00")</span>'
def pt(p): return "{ " + key("type") + s(p["type"]) + ", " + key("coordinates") + f'[{n(p["coordinates"][0])}, {n(p["coordinates"][1])}] }}'
L = []   # (indent, html, css class)
add = lambda i, h, c="": L.append((i, h, c))
add(0, "{")
add(1, key("_id") + s(d["_id"]) + ",")
add(1, key("fleet_id") + s(d["fleet_id"]) + ",")
add(1, key("requested_at") + dt(d["requested_at"]) + ",")
for k in ("pickup", "dropoff"):
    add(1, key(k) + "{ " + key("type") + s(d[k]["type"]) + ",")
    add(1, " " * len(k + ": { ") + key("coordinates") + f'[{n(d[k]["coordinates"][0])}, {n(d[k]["coordinates"][1])}] }},')
add(1, key("window") + "{")
add(2, key("start") + dt(d["window"]["start"]) + ",")
add(2, key("end") + dt(d["window"]["end"]))
add(1, "},")
ap = d["app_profile"]
add(1, key("app_profile") + "{")
add(2, key("name") + s(ap["name"]) + ",")
add(2, key("min_dl_mbps") + n(ap["min_dl_mbps"]) + ",")
add(2, key("duration_min") + n(ap["duration_min"]))
add(1, "},")
add(1, key("candidates") + "[")
c = d["candidates"][0]
add(2, "{ " + key("route_id") + s(c["route_id"]) + ", " + key("label") + s(c["label"]) + ",")
add(3, key("duration_min") + n(c["duration_min"]) + ", " + key("distance_m") + n(c["distance_m"]) + ",")
add(3, key("legs") + "[")
legs = c["legs"]
t0 = legs[0]["enter"]
j = next(i for i, g in enumerate(legs) if (g["enter"] - t0).total_seconds() >= 600)   # a leg ten minutes on
def leg(g):
    add(4, "{ " + key("cell_id") + s(g["cell_id"]) + ",")
    add(5, key("enter") + dt(g["enter"]) + " },")
leg(legs[0])
add(4, f'<span class="cm">// … {j - 1} more legs</span>')
leg(legs[j])
add(4, f'<span class="cm">// … {len(legs) - j - 1} more legs, until {legs[-1]["enter"].strftime("%H:%M")}</span>')
add(3, "]")
add(2, "},")
add(2, '<span class="cm">// … routes B and C, same shape</span>')
add(1, "]")
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
.json{{width:520px;background:#0A141C;padding:{PAD}px 14px;box-sizing:border-box;font-family:"Source Code Pro",Menlo,monospace;font-size:{fs:.2f}px;line-height:{lh:.2f}px;color:#C9D4DC;overflow:hidden}}
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
td{{padding:5px 6px;border-bottom:1px solid #1A2E3D}}
tr.win td{{background:rgba(0,237,100,.12)}} .bad{{color:#FF7A7A}} .ok{{color:#00ED64;font-weight:600}}
.foot{{margin-top:auto;font-size:9px;color:#6B8394;line-height:1.4}} .foot b{{color:#93A9B8;font-weight:500}}
</style></head><body><div class="wrap">
<div class="json">{code}</div>
<div class="right">
  <div class="eyebrow">ONE QUERY ON MONGODB ATLAS</div>
  <h1>Which route keeps the video call?</h1>
  <div class="sub">The ride request on the left is the input. Atlas answers in a fraction of a second.</div>
  <div class="step"><div class="num" style="background:#00ED64">1</div><div><div class="st">Read what the passenger needs</div><div class="sd">a video call: at least 8 Mbit/s, for 20 minutes</div></div></div>
  <div class="step"><div class="num" style="background:#6EA8FE">2</div><div><div class="st">Follow each candidate route, leg by leg</div><div class="sd">a leg is a cell on the route, and the time the car enters it</div></div></div>
  <div class="step"><div class="num" style="background:#B98CFF">3</div><div><div class="st">Look up the expected load for every leg</div><div class="sd">from the operator's load forecast for that cell at that time, stored in Atlas next to the live counters</div></div></div>
  <div class="step"><div class="num" style="background:#F5B83D">4</div><div><div class="st">Turn the expected load into download speed for the car</div><div class="sd">the busier the cell, the fewer Mbit/s</div></div></div>
  <div class="step"><div class="num" style="background:#E6EEF2">5</div><div><div class="st">Keep each route's weakest moment</div><div class="sd">one dip below 8 Mbit/s is enough to drop the call</div></div></div>
  <table>
    <tr><th>Route</th><th>Time</th><th>Weakest moment</th><th>Video call</th></tr>
    <tr><td>A · via 34th St</td><td>14 min</td><td class="bad">0.5 Mbit/s</td><td class="bad">drops</td></tr>
    <tr><td>C · via 42nd St</td><td>19 min</td><td class="bad">0.5 Mbit/s</td><td class="bad">drops</td></tr>
    <tr class="win"><td>B · via 23rd St and 1st Ave</td><td>20 min</td><td class="ok">10.8 Mbit/s</td><td class="ok">Connected ride</td></tr>
  </table>
  <div class="foot"><b>In MongoDB terms:</b> $unwind the routes and legs · $lookup the load forecast · $min per route · compare with the need. The ride request (partner data) and the load forecast (network data) meet in one query, with no copy in a second system.</div>
</div></div></body></html>"""
open(out, "w").write(page)
print(Path(out).name, nlines, "lines, font", round(fs, 2), "px at 1x")
