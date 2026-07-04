"""Assemble the public/ static site for Vercel hosting.

Regenerates the needs map, copies the needs map + traffic-sim map + key result
files into public/, injects a Sources button into the copied traffic map, and
writes a landing page. Pure static output; no backend.

Usage:
    python viz/build_site.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_needs_map  # noqa: E402  (viz/ is on sys.path when run as a script)

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
OUT = ROOT / "scenarios" / "logan_square" / "output"
PROCESSED = ROOT / "data" / "processed"

SOURCES = [
    ("OpenStreetMap (road network)", "https://www.openstreetmap.org"),
    ("CMAP c24q4 travel demand model", "https://www.cmap.illinois.gov"),
    ("Chicago Traffic Crashes (85ca-t3if)", "https://data.cityofchicago.org/d/85ca-t3if"),
    ("Chicago 311 Pot Holes (7as2-ds3y)", "https://data.cityofchicago.org/d/7as2-ds3y"),
    ("Chicago Avg Daily Traffic Counts (gc7y-n4xa)", "https://data.cityofchicago.org/d/gc7y-n4xa"),
    ("MATSim 2024.0 traffic simulation engine", "https://www.matsim.org"),
]

RESULT_FILES = [
    "needs_index_summary.json",
    "bike_lane_bca.json",
    "pothole_bca.json",
    "scenario_comparison.json",
    "scenario_comparison.csv",
]


def _sources_widget() -> str:
    links = "".join(
        f'<div><a href="{url}" target="_blank" rel="noopener" '
        f'style="color:#8fd6ff;text-decoration:none;">{name}</a></div>'
        for name, url in SOURCES
    )
    return (
        '<button id="srcBtn" style="position:absolute;top:14px;right:16px;z-index:60;background:#12283c;'
        'color:#dff;border:1px solid #3c6f8c;border-radius:7px;padding:8px 12px;font:13px system-ui;cursor:pointer;">'
        "Sources</button>"
        '<div id="srcPanel" style="display:none;position:absolute;top:52px;right:16px;z-index:60;'
        "width:min(320px,calc(100vw - 32px));background:rgba(6,14,22,.96);border:1px solid rgba(120,160,210,.3);"
        'border-radius:8px;padding:12px 14px;font:12px/1.6 system-ui;color:#e7f1ff;">'
        '<h3 style="margin:0 0 6px 0;font-size:13px;">Data sources</h3>'
        f"{links}"
        '<div style="color:#a9bed2;font-size:11px;margin-top:8px;">Planning tool built from public data. '
        "Sketch-level estimates; before/after differences are more reliable than absolute values.</div></div>"
        "<script>(function(){var b=document.getElementById('srcBtn'),p=document.getElementById('srcPanel');"
        "b.onclick=function(){p.style.display=(p.style.display==='block'?'none':'block');};})();</script>"
    )


def _inject_sources(html: str) -> str:
    widget = _sources_widget()
    if "</body>" in html:
        return html.replace("</body>", widget + "\n</body>", 1)
    return html + widget


INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>CitySim - Logan Square</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  body{margin:0;min-height:100vh;background:#0a0e14;color:#e7f1ff;font-family:system-ui,Segoe UI,Roboto,sans-serif;display:flex;align-items:center;justify-content:center;}
  .wrap{max-width:720px;padding:40px 24px;}
  h1{font-size:30px;margin:0 0 6px 0;}
  .sub{color:#9fb7cc;font-size:15px;margin:0 0 22px 0;}
  p{line-height:1.55;color:#c6d9ec;font-size:14px;}
  .cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:26px 0;}
  a.card{display:block;text-decoration:none;color:#e7f1ff;background:#12233a;border:1px solid #2b5b78;border-radius:12px;padding:20px;transition:border-color .15s;}
  a.card:hover{border-color:#74d7ff;}
  a.card b{display:block;font-size:17px;margin-bottom:6px;}
  a.card span{font-size:13px;color:#a9bed2;line-height:1.4;}
  .note{color:#8ea4bb;font-size:12px;margin-top:20px;line-height:1.5;}
  @media(max-width:560px){.cards{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="wrap">
  <h1>CitySim &mdash; Logan Square</h1>
  <p class="sub">Where do the streets need attention, and what happens if we change them?</p>
  <p>CitySim scores every street in Logan Square, Chicago for how much it needs attention &mdash; using public crash, pothole, and traffic-count data &mdash; and pairs it with an agent-based traffic model for testing changes like bike lanes or road diets.</p>
  <div class="cards">
    <a class="card" href="needs_map.html"><b>Which streets need attention? &rarr;</b><span>A 0-100 priority map built from public crash, pothole, and traffic data.</span></a>
    <a class="card" href="live_traffic.html"><b>See the traffic simulation &rarr;</b><span>An animated day of simulated trips and before/after scenario results.</span></a>
  </div>
  <p class="note">This is a planning tool, not ground truth. Estimates are sketch-level; before/after differences are more reliable than absolute values. See "Sources" for the underlying data.</p>
</div>
__SOURCES__
</body>
</html>
"""


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "data").mkdir(parents=True, exist_ok=True)

    # 1. (Re)build the needs map into the scenario output dir.
    build_needs_map.main()

    # 2. Copy the needs map.
    needs_src = OUT / "needs_map.html"
    if needs_src.exists():
        shutil.copyfile(needs_src, PUBLIC / "needs_map.html")
    else:
        print(f"WARNING: {needs_src} missing")

    # 3. Copy the traffic map with a Sources button injected.
    live_src = OUT / "live_traffic.html"
    if live_src.exists():
        (PUBLIC / "live_traffic.html").write_text(
            _inject_sources(live_src.read_text(encoding="utf-8")), encoding="utf-8"
        )
    else:
        print(f"WARNING: {live_src} missing (run viz/build_live_viz.py first)")

    # 4. Copy result files.
    for name in RESULT_FILES:
        src = PROCESSED / name
        if src.exists():
            shutil.copyfile(src, PUBLIC / "data" / name)

    # 5. Landing page.
    (PUBLIC / "index.html").write_text(INDEX_HTML.replace("__SOURCES__", _sources_widget()), encoding="utf-8")

    files = sorted(p.name for p in PUBLIC.iterdir() if p.is_file())
    print(f"wrote public/ -> {files} + data/{sorted(p.name for p in (PUBLIC/'data').iterdir())}")


if __name__ == "__main__":
    main()
