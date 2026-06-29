"""Build a self-contained deck.gl web visualization of the simulated traffic.

Reads a MATSim events file, reconstructs per-vehicle trajectories (a sample of
them), reprojects to WGS84, and writes a single standalone HTML file with an
animated deck.gl TripsLayer over a dark CARTO basemap. No server, no OpenGL —
just open the HTML in a browser.

Usage:
    python viz/build_live_viz.py [events.xml.gz] [out.html] [--sample N]
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENTS = ROOT / "scenarios/logan_square/output/ITERS/it.10/10.events.xml.gz"
DEFAULT_OUT = ROOT / "scenarios/logan_square/output/live_traffic.html"
NETWORK_LINKS = ROOT / "data/interim/network_links.gpkg"


def link_endpoints_wgs84() -> dict[str, tuple[list[float], list[float]]]:
    """Map link_id -> ([lon,lat] start, [lon,lat] end) in WGS84."""
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


def build_trips(events_path: Path, endpoints, keep_every: int) -> list[dict]:
    """Stream events; keep ~1/keep_every vehicles; build {path, timestamps}."""
    # vehicle -> list of (time, link)
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
    for veh, seq in traj.items():
        if len(seq) < 2:
            continue
        seq.sort(key=lambda r: r[0])
        path: list[list[float]] = []
        ts: list[float] = []
        for t, link in seq:
            start, _ = endpoints[link]
            path.append(start)
            ts.append(round(t, 1))
        # close out with the end node of the last link shortly after
        _, last_end = endpoints[seq[-1][1]]
        path.append(last_end)
        ts.append(round(seq[-1][0] + 25.0, 1))
        trips.append({"path": path, "timestamps": ts})
    return trips


def center_of(trips: list[dict]) -> tuple[float, float]:
    xs = [p[0] for t in trips for p in t["path"]]
    ys = [p[1] for t in trips for p in t["path"]]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


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
  button{background:#16324a;color:#dff;border:1px solid #2b6;border-radius:6px;padding:6px 12px;cursor:pointer;}
  #speed{width:90px;accent-color:#39c0ff;}
  .pill{background:rgba(10,20,32,.6);backdrop-filter:blur(4px);padding:8px 12px;border-radius:10px;border:1px solid rgba(90,160,255,.25);}
</style>
</head>
<body>
<div id="map"></div>
<div id="hud"><div class="pill"><div id="clock">--:--</div><div id="sub">CitySim · Logan Square · simulated day (10% sample)</div></div></div>
<div id="ctrl"><div class="pill" style="display:flex;gap:10px;align-items:center;width:100%">
  <button id="play">⏸ Pause</button>
  <input id="scrub" type="range" min="0" max="86400" value="18000"/>
  <span>speed</span><input id="speed" type="range" min="50" max="2000" value="600"/>
</div></div>
<script id="trips-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('trips-data').textContent);
const {DeckGL, TripsLayer, TileLayer, BitmapLayer} = deck;

const basemap = new TileLayer({
  id:'carto-dark', data:'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
  minZoom:0, maxZoom:19, tileSize:256,
  renderSubLayers: props => {
    const {boundingBox} = props.tile;
    return new BitmapLayer(props,{data:null,image:props.data,
      bounds:[boundingBox[0][0],boundingBox[0][1],boundingBox[1][0],boundingBox[1][1]]});
  }
});

let currentTime = 18000;        // start ~5:00
let playing = true;
let speed = 600;                // sim-seconds per real second
const TRAIL = 240;
const T_MIN = DATA.tmin, T_MAX = DATA.tmax;

function tripsLayer(){
  return new TripsLayer({
    id:'trips', data:DATA.trips,
    getPath:d=>d.path, getTimestamps:d=>d.timestamps,
    getColor:[60,200,255], opacity:0.85,
    widthMinPixels:2.2, rounded:true, trailLength:TRAIL,
    currentTime, shadowEnabled:false
  });
}

const deckgl = new DeckGL({
  container:'map',
  initialViewState:{longitude:DATA.center[0], latitude:DATA.center[1], zoom:13, pitch:50, bearing:-15},
  controller:true,
  layers:[basemap, tripsLayer()]
});

const clock=document.getElementById('clock'), sub=document.getElementById('sub');
const scrub=document.getElementById('scrub'), playBtn=document.getElementById('play'), speedEl=document.getElementById('speed');
scrub.min=T_MIN; scrub.max=T_MAX;
function fmt(s){s=Math.floor(s)%86400;const h=String(Math.floor(s/3600)).padStart(2,'0');const m=String(Math.floor((s%3600)/60)).padStart(2,'0');return h+':'+m;}
playBtn.onclick=()=>{playing=!playing;playBtn.textContent=playing?'⏸ Pause':'▶ Play';};
scrub.oninput=e=>{currentTime=+e.target.value;};
speedEl.oninput=e=>{speed=+e.target.value;};

let last=performance.now();
function frame(now){
  const dt=(now-last)/1000; last=now;
  if(playing){ currentTime+=dt*speed; if(currentTime>T_MAX) currentTime=T_MIN; }
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
    ap.add_argument("events", nargs="?", default=str(DEFAULT_EVENTS))
    ap.add_argument("out", nargs="?", default=str(DEFAULT_OUT))
    ap.add_argument("--sample", type=int, default=4000, help="approx vehicles to render")
    args = ap.parse_args()

    endpoints = link_endpoints_wgs84()
    # estimate keep_every so we land near --sample (118k agents -> /30 ~ 3900)
    keep_every = max(1, round(118000 / max(args.sample, 1)))
    trips = build_trips(Path(args.events), endpoints, keep_every)
    cx, cy = center_of(trips)
    tmin = min(t["timestamps"][0] for t in trips)
    tmax = max(t["timestamps"][-1] for t in trips)
    payload = {"trips": trips, "center": [round(cx, 5), round(cy, 5)],
               "tmin": round(tmin, 1), "tmax": round(tmax, 1)}
    html = HTML.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    mb = out.stat().st_size / 1e6
    print(f"wrote {out} ({mb:.1f} MB) | trips={len(trips)} | keep_every={keep_every} "
          f"| span={tmin/3600:.1f}-{tmax/3600:.1f}h")


if __name__ == "__main__":
    main()
