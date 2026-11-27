#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Draw the help page's diagrams into web/help_img/ (1920x1080 PNGs, the
network map's dark style). Run after a change to the scenarios' data:

    python tools/diagrams/render.py [--base http://localhost:8190]

  ride-request-route-query.png   ride.py         the ride request beside "Which route keeps the video call?"
  ota-vehicle-quote-query.png    ota.py          a car and the campaign beside "Where should this car download?"
  green-carrier-sleep-query.png  green.py        a carrier and the energy policy beside "Can this carrier sleep?"
  green-night-incident.png       night.py        the transit alert, the lesson, the night's timeline and power
  fleet-architecture.png         architecture.html  data in and out of the operational data layer

The documents come from web/fleet_network.py and web/ran_network.py. The
night's power curve comes from the night page's own simulation, read from a
running network map (--base) in headless Chrome; the OTA picture's table is
the live quote for FA-0001 at 13:01, so check it against /api/fleet/quote
when the data changes. Needs Google Chrome (CHROME= to point elsewhere) and
the Python packages in requirements.txt (websockets).
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "web" / "help_img"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def shot(html: Path, png: Path, profile: str) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                    "--force-device-scale-factor=2", "--window-size=960,540", "--virtual-time-budget=4000",
                    f"--screenshot={png}", html.as_uri()], check=True, capture_output=True, timeout=90)
    print("drew", png.relative_to(HERE.parents[1]))


async def night_power(base: str, profile: str) -> list:
    """The night page's simulated power, 00:00 to 03:00 every 3 minutes: [minute, with agent W, always on W]."""
    import websockets
    p = subprocess.Popen([CHROME, "--headless=new", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                          f"--user-data-dir={profile}", "--remote-debugging-port=9334", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json"))
                break
            except OSError:
                time.sleep(0.2)
        url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        async with websockets.connect(url, max_size=None) as ws:
            n = 0

            async def call(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == n:
                        return r
            await call("Page.navigate", url=f"{base}/night")
            for _ in range(60):
                await asyncio.sleep(0.5)
                r = await call("Runtime.evaluate", returnByValue=True, expression="""(() => {
                    const S = window.__sim; if (!S) return null; const f = S.frames, out = [];
                    for (let t = 360; t < f.length; t += 3) out.push([t, Math.round(f[t].totalW), Math.round(f[t].baseW)]);
                    return out; })()""")
                v = r["result"]["result"].get("value")
                if v:
                    return v
            sys.exit(f"the night page at {base}/night did not simulate (is the network map running?)")
    finally:
        p.terminate()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8190", help="a running network map, for the night's power curve")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        profile = str(tmp / "chrome")
        for gen, name in [("ride", "ride-request-route-query"), ("ota", "ota-vehicle-quote-query"),
                          ("green", "green-carrier-sleep-query")]:
            html = tmp / f"{name}.html"
            subprocess.run([sys.executable, str(HERE / f"{gen}.py"), str(html)], check=True)
            shot(html, OUT / f"{name}.png", profile)
        power = tmp / "power.json"
        power.write_text(json.dumps(asyncio.run(night_power(args.base, str(tmp / "chrome-cdp")))))
        html = tmp / "green-night-incident.html"
        subprocess.run([sys.executable, str(HERE / "night.py"), str(html), str(power)], check=True)
        shot(html, OUT / "green-night-incident.png", profile)
        shot(HERE / "architecture.html", OUT / "fleet-architecture.png", profile)


if __name__ == "__main__":
    main()
