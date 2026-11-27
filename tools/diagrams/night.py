"""The night's incident: the transit alert and the stored lesson beside the timeline and the power curve.

Part of tools/diagrams (render.py draws every diagram into web/help_img/)."""
import html, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from web import ran_network as rn
out = sys.argv[1]
E = html.escape
def key(k): return f'<span class="k">{E(k)}</span>: '
def s(v): return f'<span class="s">"{E(v)}"</span>'
def n(v): return f'<span class="n">{v}</span>'
def dt(v): return f'<span class="d">ISODate("{v.strftime("%Y-%m-%dT%H:%M:%S")}-05:00")</span>'
L = []
add = lambda i, h: L.append((i, h))
a = rn.documents()["transit_alerts"][0]
add(0, '<span class="cm">// from outside · transit_alerts (GTFS-realtime)</span>')
add(0, "{")
add(1, key("_id") + s(a["_id"]) + ",")
add(1, key("source") + s(a["source"]) + ", " + key("effect") + s(a["effect"]) + ",")
add(1, key("header_text") + s("A C E trains suspended between"))
add(1, " " * len("header_text: ") + '+ ' + s("34 St–Penn Station and 59 St–Columbus Circle") + ",")
add(1, key("informed_entity") + "[{ " + key("route_id") + s("A") + " }, … { " + key("stop_id") + s("A28") + " }],")
add(1, key("active_period") + "{ " + key("start") + dt(a["active_period"]["start"]) + ",")
add(1, " " * len("active_period: { ") + key("end") + dt(a["active_period"]["end"]) + " },")
add(1, key("location") + "{ " + key("type") + s("Point") + ", " + key("coordinates") + f'[{n(a["location"]["coordinates"][0])}, {n(a["location"]["coordinates"][1])}] }}')
add(0, "}")
add(0, "&nbsp;")
add(0, '<span class="cm">// written by the agent at 02:30 · the lesson</span>')
add(0, "{")
add(1, key("_id") + s("LESSON-2026-11-25-PENN") + ", " + key("kind") + s("energy_lesson") + ",")
add(1, key("site_id") + s("NYC-MTN-0412") + ", " + key("area_type") + s("transit") + ", " + key("hour") + n(1) + ",")
add(1, key("text") + s("Transit disruption at 34 St–Penn after midnight:"))
add(1, " " * len("text: ") + "+ " + s(" the coverage layer peaked at 84% before the"))
add(1, " " * len("text: ") + "+ " + s(" capacity carrier woke. Keep one capacity carrier"))
add(1, " " * len("text: ") + "+ " + s(" awake on the 8th Avenue side until 02:00."))
add(1, '<span class="cm">// searchable by meaning: Atlas embeds the text</span>')
add(1, key("proposed_canary") + "{ " + key("market") + s("LA") + ", " + key("name") + s("Union Station") + " }")
add(0, "}")
H, PAD = 540, 22
lh = (H - 2 * PAD) / len(L)
widest = max(len(re.sub(r"<[^>]+>", "", html.unescape(h))) + i * 2 for i, h in L)
fs = min(13, lh * 0.84, (520 - 28) / (widest * 0.6))
code = "".join(f'<div class="ln" style="padding-left:{i * 2}ch">{h}</div>' for i, h in L)

# power curve, 00:00 to 03:00, from the page's simulation
pw = json.load(open(sys.argv[2]))
W, Hc, x0, y0 = 392, 92, 30, 6
t0, t1 = 360, 540; lo, hi = 36000, 62000
X = lambda t: x0 + (t - t0) / (t1 - t0) * (W - x0 - 4)
Y = lambda w: y0 + (hi - w) / (hi - lo) * (Hc - y0 - 14)
tot = " ".join(f"{X(t):.1f},{Y(w):.1f}" for t, w, b in pw)
base = " ".join(f"{X(t):.1f},{Y(b):.1f}" for t, w, b in pw)
area = f"M{X(pw[0][0]):.1f},{Y(pw[0][2]):.1f} " + " ".join(f"L{X(t):.1f},{Y(b):.1f}" for t, w, b in pw) + " " + " ".join(f"L{X(t):.1f},{Y(w):.1f}" for t, w, b in reversed(pw)) + " Z"
ticks = "".join(f'<text x="{X(t):.1f}" y="{Hc - 1}" class="ax" text-anchor="middle">{lab}</text>' for t, lab in [(360, "00:00"), (420, "01:00"), (480, "02:00"), (534, "03:00")])
marks = "".join(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{y0}" y2="{Hc - 13}" stroke="{c}" stroke-dasharray="2 2"/><text x="{X(t) + 3:.1f}" y="{Hc - 17}" class="ax" fill="{c}" style="fill:{c}">{lab}</text>' for t, lab, c in [(430, "alert", "#6EA8FE"), (510, "asleep again", "#00ED64")])
chart = f"""<svg width="{W}" height="{Hc}" viewBox="0 0 {W} {Hc}">
<path d="{area}" fill="rgba(0,237,100,.16)"/>
<polyline points="{base}" fill="none" stroke="#93A9B8" stroke-width="1.2" stroke-dasharray="3 2"/>
<polyline points="{tot}" fill="none" stroke="#00ED64" stroke-width="1.8"/>
<text x="{x0 - 4}" y="{Y(58000) + 3:.1f}" class="ax" text-anchor="end">58</text><text x="{x0 - 4}" y="{Y(41000) + 3:.1f}" class="ax" text-anchor="end">41</text>
{ticks}{marks}</svg>"""

def ev(t, who, txt):
    col = {"EXTERNAL": "#6EA8FE", "REFLEX": "#F5B83D", "NETWORK": "#B98CFF", "AGENT": "#00ED64"}[who]
    return f'<div class="ev"><div class="t">{t}</div><div class="tag" style="color:{col};border-color:{col}">{who}</div><div class="tx">{txt}</div></div>'
events = "".join([
  ev("01:10", "EXTERNAL", "Transit alert: A C E trains suspended at Penn Station. Crowds come up to the street, where the capacity carriers sleep."),
  ev("01:14", "REFLEX", "Penn Station sector B: coverage layer at 75% for 2 minutes, so its capacity carrier wakes in 60 s. No AI model in this loop."),
  ev("01:15", "NETWORK", "Carrier back. The coverage layer peaked at 84%; every service promise kept."),
  ev("01:15", "AGENT", "Meanwhile a quiet carrier in Murray Hill still goes to sleep: the saving stops only where it has to."),
  ev("01:37", "REFLEX", "By now 8 carriers and 5 small cells have woken, one by one, around Penn Station, Herald Square and the Garment District, wherever the crowd went."),
  ev("02:30", "AGENT", "Crowd gone: back to sleep. Lesson stored, and proposed as a canary for a transit hub in Los Angeles. The NOC decides."),
])
page = f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Lexend+Deca:wght@400;500;600&family=Source+Code+Pro:wght@400;500&display=swap" rel="stylesheet">
<style>
html,body{{margin:0;background:#0D1B26;width:960px;height:540px;overflow:hidden;font-family:"Lexend Deca","Helvetica Neue",Arial,sans-serif;color:#E6EEF2}}
.wrap{{display:flex;height:540px}}
.json{{flex:none;width:520px;background:#0A141C;padding:{PAD}px 14px;box-sizing:border-box;font-family:"Source Code Pro",Menlo,monospace;font-size:{fs:.2f}px;line-height:{lh:.2f}px;color:#C9D4DC;overflow:hidden}}
.ln{{white-space:pre}}
.k{{color:#4FD8EB;font-weight:500}} .s{{color:#7EE787}} .n{{color:#F2CC60}} .d{{color:#A5B4FC}} .cm{{color:#6B8394;font-style:italic}}
.right{{flex:1;padding:20px 22px 14px 24px;box-sizing:border-box;display:flex;flex-direction:column}}
.eyebrow{{font-size:10px;font-weight:600;letter-spacing:.09em;color:#00ED64}}
h1{{font-size:18px;font-weight:600;margin:3px 0 1px}}
.sub{{font-size:10px;color:#93A9B8;margin-bottom:6px}}
.ax{{font-size:7.5px;fill:#6B8394;font-family:"Lexend Deca",sans-serif}}
.leg{{font-size:8.5px;color:#93A9B8;margin:3px 0 8px}} .leg b{{color:#00ED64;font-weight:500}}
.ev{{display:grid;grid-template-columns:34px 58px 1fr;gap:7px;align-items:start;margin-bottom:6px}}
.t{{font-size:10px;font-weight:600;color:#E6EEF2;padding-top:1px}}
.tag{{font-size:7px;font-weight:600;letter-spacing:.06em;border:1px solid;border-radius:3px;padding:2px 0;text-align:center;margin-top:1px}}
.tx{{font-size:10px;line-height:1.3;color:#C9D4DC}}
.foot{{margin-top:auto;font-size:9px;color:#6B8394;line-height:1.4}} .foot b{{color:#93A9B8;font-weight:500}}
</style></head><body><div class="wrap">
<div class="json">{code}</div>
<div class="right">
  <div class="eyebrow">THE NIGHT'S SURPRISE · PENN STATION</div>
  <h1>A train stops, the network wakes</h1>
  <div class="sub">Power of the Midtown radio sites, kW: <span style="color:#00ED64">with the agent</span> vs <span style="color:#93A9B8">always on</span></div>
  {chart}
  <div class="leg">29% below always-on at 01:10, still 23% below at the peak of the crowd, back to 29% after 02:30.</div>
  {events}
  <div class="foot"><b>Who decides what:</b> the reflex wakes carriers from the coverage measurements alone, in minutes. The agent decides when to sleep again and what to learn. A person decides whether the lesson goes to another market.</div>
</div></div></body></html>"""
open(out, "w").write(page)
print(len(L), "lines, font", round(fs, 2))
