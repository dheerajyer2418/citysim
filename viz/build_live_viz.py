"""Build a self-contained deck.gl web visualization of the simulated traffic.

Reads MATSim events for each available scenario (clean network vs pothole-
degraded), reconstructs a sample of per-vehicle trajectories, reprojects to
WGS84, and writes ONE standalone HTML with an animated deck.gl TripsLayer over a
dark CARTO basemap and a scenario switcher. No server, no OpenGL — open in a
browser.

Usage:
    python viz/build_live_viz.py [out.html] [--sample N]
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
SCEN_DIR = ROOT / "scenarios/logan_square"
DEFAULT_OUT = SCEN_DIR / "output/live_traffic.html"
NETWORK_LINKS = ROOT / "data/interim/network_links.gpkg"

# label -> MATSim output directory (only those present are included)
SCENARIOS = [
    ("Potholes fixed (clean network)", SCEN_DIR / "output"),
    ("With potholes (baseline)", SCEN_DIR / "output_baseline"),
]


def link_endpoints_wgs84() -> dict[str, tuple[list[float], list[float]]]:
    import geopandas as gpd

    links = gpd.read_file(NETWORK_LINKS).to_crs(4326)
    out: dict[str, tuple[list[float], list[float]]] = {}
    for link_id, geom in zip(links["link_id"].astype(str), links.geometry):
        coords = list(geom.coords)
        if len(coords) < 2:
            continue
        sx, sy = coords[0]
        ex, ey = coords[-1]
        out[link_id] = ([round(sx, 5), round(sy, 5)], [round(ex, 5), round(ey, 5)])
    return out


def last_events_file(output_dir: Path) -> Path | None:
    iters = output_dir / "ITERS"
    if not iters.exists():
        return None
    best = -1
    best_path = None
    for child in iters.iterdir():
        m = re.fullmatch(r"it\.(\d+)", child.name)
        if m and child.is_dir():
            n = int(m.group(1))
            ev = child / f"{n}.events.xml.gz"
            if ev.exists() and n > best:
                best, best_path = n, ev
    return best_path


def build_trips(events_path: Path, endpoints, keep_every: int) -> list[dict]:
    traj: dict[str, list[tuple[float, str]]] = {}
    move_types = {"entered link", "vehicle enters traffic"}
    with gzip.open(events_path, "rb") as fh:
        for _, el in etree.iterparse(fh, tag="event"):
            if el.get("type") in move_types:
                veh = el.get("vehicle")
                if veh is not None and (hash(veh) % keep_every == 0):
                    link = el.get("link")
                    if link in endpoints:
                        traj.setdefault(veh, []).append((float(el.get("time")), link))
            el.clear()

    trips: list[dict] = []
    for seq in traj.values():
        if len(seq) < 2:
            continue
        seq.sort(key=lambda r: r[0])
        path = [endpoints[link][0] for _, link in seq]
        ts = [round(t, 1) for t, _ in seq]
        path.append(endpoints[seq[-1][1]][1])
        ts.append(round(seq[-1][0] + 25.0, 1))
        trips.append({"path": path, "timestamps": ts})
    return trips


HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>CitySim — Logan Square live traffic</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://unpkg.com/deck.gl@9.0.0/dist.min.js"></script>
<style>
  html,body,#map{margin:0;width:100%;height:100%;background:#0a0e14;overflow:hidden;font-family:system-ui,Segoe UI,Roboto,sans-serif;}
  #hud{position:absolute;top:14px;left:16px;z-index:5;color:#e8f0ff;}
  #clock{font-size:30px;font-weight:700;letter-spacing:1px;text-shadow:0 0 12px rgba(80,180,255,.6);}
  #sub{font-size:12px;opacity:.7;margin-top:2px;}
  #ctrl{position:absolute;bottom:16px;left:16px;right:16px;z-index:5;display:flex;gap:10px;align-items:center;color:#cfe0ff;}
  #scrub{flex:1;accent-color:#39c0ff;}
  button,select{background:#16324a;color:#dff;border:1px solid #2b6;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px;}
  #speed{width:90px;accent-color:#39c0ff;}
  .pill{background:rgba(10,20,32,.6);backdrop-filter:blur(4px);padding:8px 12px;border-radius:10px;border:1px solid rgba(90,160,255,.25);}
  #scenwrap{position:absolute;top:14px;right:16px;z-index:5;}
</style>
</head>
<body>
<div id="map"></div>
<div id="hud"><div class="pill"><div id="clock">--:--</div><div id="sub">CitySim · Logan Square · simulated day (10% sample)</div></div></div>
<div id="scenwrap"><div class="pill">Scenario&nbsp;<select id="scenario"></select> &nbsp;<span id="ntrips"></span></div></div>
<div id="ctrl"><div class="pill" style="display:flex;gap:10px;align-items:center;width:100%">
  <button id="play">⏸ Pause</button>
  <input id="scrub" type="range" min="0" max="86400" value="21600"/>
  <span>speed</span><input id="speed" type="range" min="50" max="2000" value="600"/>
</div></div>
<script id="trips-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('trips-data').textContent);
const {DeckGL, TripsLayer, TileLayer, BitmapLayer} = deck;

let scen = 0;
let currentTime = DATA.scenarios[0].tmin;
let playing = true, speed = 600;
const TRAIL = 240;

const basemap = new TileLayer({
  id:'carto-dark', data:'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
  minZoom:0, maxZoom:19, tileSize:256,
  renderSubLayers: props => {
    const {boundingBox} = props.tile;
    return new BitmapLayer(props,{data:null,image:props.data,
      bounds:[boundingBox[0][0],boundingBox[0][1],boundingBox[1][0],boundingBox[1][1]]});
  }
});
function tripsLayer(){
  return new TripsLayer({
    id:'trips', data:DATA.scenarios[scen].trips,
    getPath:d=>d.path, getTimestamps:d=>d.timestamps,
    getColor: scen===0 ? [60,200,255] : [255,150,60], opacity:0.85,
    widthMinPixels:2.2, rounded:true, trailLength:TRAIL, currentTime
  });
}
const deckgl = new DeckGL({
  container:'map',
  initialViewState:{longitude:DATA.center[0], latitude:DATA.center[1], zoom:13, pitch:50, bearing:-15},
  controller:true, layers:[basemap, tripsLayer()]
});

const clock=document.getElementById('clock'), scrub=document.getElementById('scrub');
const playBtn=document.getElementById('play'), speedEl=document.getElementById('speed');
const sel=document.getElementById('scenario'), ntrips=document.getElementById('ntrips');
DATA.scenarios.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=s.name;sel.appendChild(o);});
function refreshScen(){const s=DATA.scenarios[scen];scrub.min=s.tmin;scrub.max=s.tmax;ntrips.textContent=s.trips.length+' vehicles';}
refreshScen();
function fmt(s){s=Math.floor(s)%86400;return String(Math.floor(s/3600)).padStart(2,'0')+':'+String(Math.floor((s%3600)/60)).padStart(2,'0');}
playBtn.onclick=()=>{playing=!playing;playBtn.textContent=playing?'⏸ Pause':'▶ Play';};
scrub.oninput=e=>{currentTime=+e.target.value;};
speedEl.oninput=e=>{speed=+e.target.value;};
sel.onchange=e=>{scen=+e.target.value;refreshScen();currentTime=DATA.scenarios[scen].tmin;};

let last=performance.now();
function frame(now){
  const dt=(now-last)/1000; last=now;
  const s=DATA.scenarios[scen];
  if(playing){currentTime+=dt*speed; if(currentTime>s.tmax) currentTime=s.tmin;}
  scrub.value=currentTime; clock.textContent=fmt(currentTime);
  deckgl.setProps({layers:[basemap, tripsLayer()]});
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(DEFAULT_OUT))
    ap.add_argument("--sample", type=int, default=3500, help="approx vehicles per scenario")
    args = ap.parse_args()

    endpoints = link_endpoints_wgs84()
    keep_every = max(1, round(118000 / max(args.sample, 1)))

    scenarios = []
    all_pts = []
    for label, out_dir in SCENARIOS:
        ev = last_events_file(out_dir)
        if ev is None:
            print(f"  skip '{label}' (no events at {out_dir})")
            continue
        trips = build_trips(ev, endpoints, keep_every)
        if not trips:
            continue
        tmin = min(t["timestamps"][0] for t in trips)
        tmax = max(t["timestamps"][-1] for t in trips)
        scenarios.append({"name": label, "trips": trips,
                          "tmin": round(tmin, 1), "tmax": round(tmax, 1)})
        all_pts += [p for t in trips for p in t["path"]]
        print(f"  '{label}': {len(trips)} trips from {ev}")

    if not scenarios:
        raise SystemExit("No scenarios with events found. Run the sim first.")

    cx = sum(p[0] for p in all_pts) / len(all_pts)
    cy = sum(p[1] for p in all_pts) / len(all_pts)
    payload = {"scenarios": scenarios, "center": [round(cx, 5), round(cy, 5)]}
    html = HTML.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size/1e6:.1f} MB) | scenarios={len(scenarios)} "
          f"| keep_every={keep_every}")


if __name__ == "__main__":
    main()
