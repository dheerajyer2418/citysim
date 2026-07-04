"""Build a self-contained deck.gl web visualization of simulated traffic.

Reads MATSim events for each available scenario, reconstructs a sample of
per-vehicle trajectories, reprojects to WGS84, and writes one standalone HTML
with a visible road network, animated vehicle dots, speed colors, slower
playback controls, and live counters.

Usage:
    python viz/build_live_viz.py [out.html] [--sample N]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
SCEN_DIR = ROOT / "scenarios" / "logan_square"
DEFAULT_OUT = SCEN_DIR / "output" / "live_traffic.html"
NETWORK_LINKS = ROOT / "data" / "interim" / "network_links.gpkg"
BCA_JSON = ROOT / "data" / "processed" / "pothole_bca.json"
SCENARIO_COMPARISON_CSV = ROOT / "data" / "processed" / "scenario_comparison.csv"

SCENARIOS = [
    ("add_bike_lane", "fixed", "No bike lane (clean network)", SCEN_DIR / "output_fixed"),
    ("add_bike_lane", "bike_lane", "Milwaukee Ave bike lane", SCEN_DIR / "output_bike_lane"),
]

INTERVENTIONS = [
    {
        "id": "add_bike_lane",
        "name": "Add bike lane",
        "plain_language": "Convert part of Milwaukee Ave car capacity into bike-lane space.",
    },
]


def read_pothole_comparison(path: Path = BCA_JSON) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    delta = result.get("scenario_delta", {})
    health = result.get("scenario_health", {})
    annual = result.get("annual_benefits_usd", {})
    return {
        "intervention": "fix_potholes",
        "title": "Fix potholes",
        "status": "Model estimate",
        "daily_vht_delta_hours": round(float(delta.get("vht_delta_hours", 0.0)), 1),
        "mean_trip_minutes_saved": round(float(delta.get("mean_trip_minutes_delta", 0.0)), 2),
        "stuck_trips_delta_scaled": round(float(delta.get("stuck_trips_delta_scaled", 0.0))),
        "annual_benefit_usd": round(float(annual.get("total", 0.0))),
        "cost_usd": round(float(result.get("cost_usd", 0.0))),
        "benefit_cost_ratio": (
            None
            if result.get("benefit_cost_ratio") is None
            else round(float(result.get("benefit_cost_ratio", 0.0)), 2)
        ),
        "warnings": list(health.get("warnings", [])),
    }


def read_scenario_stats(path: Path = SCENARIO_COMPARISON_CSV) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    stats: dict[str, dict[str, Any]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            scenario = row.get("scenario")
            if not scenario:
                continue
            stats[scenario] = {
                key: _coerce_stat(value)
                for key, value in row.items()
                if key is not None
            }
    return stats


def _coerce_stat(value: str | None) -> Any:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return value


def network_links_wgs84() -> tuple[dict[str, tuple[list[float], list[float]]], list[dict[str, Any]], dict[str, list[list[float]]]]:
    import geopandas as gpd

    links = gpd.read_file(NETWORK_LINKS).to_crs(4326)
    endpoints: dict[str, tuple[list[float], list[float]]] = {}
    roads: list[dict[str, Any]] = []
    paths_by_link_id: dict[str, list[list[float]]] = {}
    for _, row in links.iterrows():
        link_id = str(row["link_id"])
        geom = row.geometry
        if geom is None or geom.is_empty or not hasattr(geom, "coords"):
            continue
        coords = list(geom.coords)
        if len(coords) < 2:
            continue
        path = [[round(float(x), 5), round(float(y), 5)] for x, y, *_ in coords]
        capacity = float(row.get("capacity", 0.0) or 0.0)
        endpoints[link_id] = (path[0], path[-1])
        paths_by_link_id[link_id] = path
        roads.append({"path": path, "capacity": round(capacity, 1)})
    return endpoints, roads, paths_by_link_id


def affected_paths_from_csv(path: Path, paths_by_link_id: dict[str, list[list[float]]]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    paths = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            link_id = row.get("link_id")
            if link_id in paths_by_link_id:
                paths.append({"link_id": link_id, "path": paths_by_link_id[link_id]})
    return paths


def last_events_file(output_dir: Path) -> Path | None:
    iters = output_dir / "ITERS"
    if not iters.exists():
        return None
    best = -1
    best_path = None
    for child in iters.iterdir():
        match = re.fullmatch(r"it\.(\d+)", child.name)
        if match and child.is_dir():
            iteration = int(match.group(1))
            events_path = child / f"{iteration}.events.xml.gz"
            if events_path.exists() and iteration > best:
                best = iteration
                best_path = events_path
    return best_path


def build_trips(events_path: Path, endpoints: dict[str, tuple[list[float], list[float]]], keep_every: int) -> dict[str, Any]:
    trajectories: dict[str, list[tuple[float, str]]] = {}
    departures: list[float] = []
    arrivals: list[float] = []
    stuck: list[float] = []
    move_types = {"entered link", "vehicle enters traffic"}

    with gzip.open(events_path, "rb") as handle:
        for _, element in etree.iterparse(handle, tag="event"):
            event_type = element.get("type")
            event_time = float(element.get("time"))
            if event_type == "departure":
                departures.append(event_time)
            elif event_type == "arrival":
                arrivals.append(event_time)
            elif event_type == "stuckAndAbort":
                stuck.append(event_time)

            if event_type in move_types:
                vehicle_id = element.get("vehicle")
                link_id = element.get("link")
                if vehicle_id is not None and link_id in endpoints and _keep_vehicle(vehicle_id, keep_every):
                    trajectories.setdefault(vehicle_id, []).append((event_time, link_id))
            element.clear()

    trips: list[dict[str, Any]] = []
    for sequence in trajectories.values():
        if len(sequence) < 2:
            continue
        sequence.sort(key=lambda item: item[0])
        path = [endpoints[link_id][0] for _, link_id in sequence]
        timestamps = [round(time, 1) for time, _ in sequence]
        path.append(endpoints[sequence[-1][1]][1])
        timestamps.append(round(sequence[-1][0] + 25.0, 1))
        trips.append({"path": path, "timestamps": timestamps})

    return {
        "trips": trips,
        "departures": sorted(round(time, 1) for time in departures),
        "arrivals": sorted(round(time, 1) for time in arrivals),
        "stuck": sorted(round(time, 1) for time in stuck),
    }


def _keep_vehicle(vehicle_id: str, keep_every: int) -> bool:
    digest = hashlib.md5(vehicle_id.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest[:8], 16) % max(1, keep_every) == 0


HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>CitySim - Logan Square Live Traffic</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://unpkg.com/deck.gl@9.0.0/dist.min.js"></script>
<style>
  html,body,#map{margin:0;width:100%;height:100%;background:#0a0e14;overflow:hidden;font-family:system-ui,Segoe UI,Roboto,sans-serif;}
  #hud{position:absolute;top:14px;left:16px;z-index:5;color:#e8f0ff;min-width:250px;}
  #clock{font-size:30px;font-weight:700;letter-spacing:0;text-shadow:0 0 12px rgba(80,180,255,.6);}
  #sub{font-size:12px;opacity:.72;margin-top:2px;margin-bottom:10px;}
  #stats{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px;font-size:12px;}
  .metric span{display:block;color:#8ea4bb;font-size:10px;text-transform:uppercase;letter-spacing:0;}
  .metric b{font-size:16px;color:#f3f8ff;}
  #legend{margin-top:10px;padding-top:9px;border-top:1px solid rgba(120,160,210,.22);display:grid;gap:5px;font-size:12px;color:#cfe0ff;}
  .legend-row{display:flex;align-items:center;gap:8px;}
  .dot{width:10px;height:10px;border-radius:50%;display:inline-block;border:1px solid rgba(8,18,28,.85);}
  .dot-fast{background:rgb(70,210,255);}
  .dot-mid{background:rgb(245,220,80);}
  .dot-slow{background:rgb(255,145,45);}
  .dot-stop{background:rgb(235,65,55);}
  .dot-affected{background:rgb(255,190,65);border-radius:2px;}
  #ctrl{position:absolute;bottom:16px;left:16px;right:16px;z-index:5;display:flex;gap:10px;align-items:center;color:#cfe0ff;}
  #scrub{flex:1;accent-color:#39c0ff;}
  button,select{background:#16324a;color:#dff;border:1px solid #2b6;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px;}
  #speed{width:120px;accent-color:#39c0ff;}
  .preset{padding:5px 8px;}
  .pill{background:rgba(10,20,32,.68);backdrop-filter:blur(4px);padding:8px 12px;border-radius:8px;border:1px solid rgba(90,160,255,.25);}
  #scenwrap{position:absolute;top:14px;right:16px;z-index:5;color:#cfe0ff;}
  #infowrap{position:absolute;right:16px;top:62px;z-index:6;color:#dcecff;max-width:360px;}
  #infoToggle{width:100%;text-align:left;background:#18334d;border-color:#3c7da6;}
  #infoPanel{display:none;margin-top:8px;line-height:1.42;font-size:13px;color:#d7e6f7;}
  #infoPanel.open{display:block;}
  #infoPanel h2{font-size:17px;margin:0 0 8px 0;color:#fff;letter-spacing:0;}
  #infoPanel h3{font-size:12px;margin:12px 0 4px 0;color:#8fd6ff;text-transform:uppercase;letter-spacing:0;}
  #infoPanel p{margin:0 0 8px 0;}
  #infoPanel ul{margin:0 0 8px 18px;padding:0;}
  #infoPanel li{margin:3px 0;}
  #infoPanel .note{color:#aebed0;font-size:12px;}
  #summary{position:absolute;right:16px;bottom:86px;z-index:5;color:#dcecff;width:min(360px,calc(100vw - 32px));max-height:calc(100vh - 190px);overflow:auto;}
  #summary summary{font-size:16px;margin:0 0 8px 0;color:#fff;letter-spacing:0;cursor:pointer;font-weight:700;}
  #summaryGrid{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;}
  .summaryMetric span{display:block;color:#8ea4bb;font-size:10px;text-transform:uppercase;letter-spacing:0;}
  .summaryMetric b{font-size:15px;color:#f3f8ff;}
  #summaryWarn{margin-top:9px;color:#ffd48a;font-size:12px;line-height:1.35;}
  @media (max-width:760px){
    #hud{left:10px;top:10px;min-width:0;width:min(310px,calc(100vw - 20px));}
    #scenwrap{left:10px;right:10px;top:auto;bottom:82px;}
    #scenwrap .pill{display:grid;grid-template-columns:auto 1fr;gap:6px 8px;align-items:center;}
    #summary{left:10px;right:10px;bottom:150px;width:auto;max-height:30vh;}
    #infowrap{right:10px;top:10px;max-width:min(300px,calc(100vw - 20px));}
    #ctrl{left:10px;right:10px;bottom:10px;}
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="hud"><div class="pill">
  <div id="clock">--:--</div>
  <div id="sub">CitySim - Logan Square - simulated day (10% sample)</div>
  <div id="stats">
    <div class="metric"><span>Active</span><b id="active">0</b></div>
    <div class="metric"><span>Completed</span><b id="done">0</b></div>
    <div class="metric"><span>Stuck/Lost</span><b id="lost">0</b></div>
    <div class="metric"><span>Shown</span><b id="shown">0</b></div>
  </div>
  <div id="legend">
    <div class="legend-row"><span class="dot dot-affected"></span><span>Affected links for selected scenario</span></div>
    <div class="legend-row"><span class="dot dot-fast"></span><span>Fast, above 25 mph</span></div>
    <div class="legend-row"><span class="dot dot-mid"></span><span>Moving, 11-25 mph</span></div>
    <div class="legend-row"><span class="dot dot-slow"></span><span>Slow, 3-11 mph</span></div>
    <div class="legend-row"><span class="dot dot-stop"></span><span>Stopped or crawling</span></div>
  </div>
</div></div>
<div id="scenwrap"><div class="pill">
  Intervention&nbsp;<select id="intervention"></select>
  &nbsp;Scenario&nbsp;<select id="scenario"></select>
  &nbsp;<span id="ntrips"></span>
</div></div>
<details id="summary" class="pill" open>
  <summary id="summaryTitle">Scenario Statistics</summary>
  <div id="summaryGrid">
    <div class="summaryMetric"><span>Completion</span><b id="sumVht">--</b></div>
    <div class="summaryMetric"><span>VHT vs clean</span><b id="sumTrip">--</b></div>
    <div class="summaryMetric"><span>Stuck trip change</span><b id="sumStuck">--</b></div>
    <div class="summaryMetric"><span>VMT vs clean</span><b id="sumBcr">--</b></div>
    <div class="summaryMetric"><span>Affected links</span><b id="sumBenefit">--</b></div>
    <div class="summaryMetric"><span>Reference</span><b id="sumCost">--</b></div>
  </div>
  <div id="summaryWarn"></div>
</details>
<div id="infowrap">
  <button id="infoToggle">Info</button>
  <div id="infoPanel" class="pill">
    <h2>What This Shows</h2>
    <p>This is a traffic simulation for Logan Square. Each dot is a sampled car trip moving through the road network during a simulated day.</p>
    <h3>Where The Data Comes From</h3>
    <ul>
      <li>Roads come from OpenStreetMap.</li>
      <li>Trips come from CMAP regional travel model data.</li>
      <li>Traffic checks use City of Chicago traffic count data.</li>
    </ul>
    <h3>How To Read It</h3>
    <p>Dot colors show vehicle speed. Red and orange areas are where traffic is slow or stuck. The counters show how many simulated trips are active, completed, or lost.</p>
    <h3>Why It Helps</h3>
    <p>The model lets us test street changes before building them, such as bike lanes, road diets, traffic-flow changes, or user-drawn rough-pavement scenarios.</p>
    <p class="note">This is a planning tool, not a live traffic feed. Results improve as the model is calibrated.</p>
  </div>
</div>
<div id="ctrl"><div class="pill" style="display:flex;gap:10px;align-items:center;width:100%">
  <button id="play">Pause</button>
  <input id="scrub" type="range" min="0" max="86400" value="21600"/>
  <span>speed</span><input id="speed" type="range" min="1" max="600" value="120"/>
  <button class="preset" data-speed="1">1x</button>
  <button class="preset" data-speed="10">10x</button>
  <button class="preset" data-speed="60">60x</button>
  <button class="preset" data-speed="300">300x</button>
</div></div>
<script id="trips-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('trips-data').textContent);
const {DeckGL, TileLayer, BitmapLayer, PathLayer, ScatterplotLayer} = deck;

let scen = 0;
let currentTime = DATA.scenarios[0].tmin;
let playing = true;
let speed = 120;

const basemap = new TileLayer({
  id:'carto-dark',
  data:'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
  minZoom:0,
  maxZoom:19,
  tileSize:256,
  renderSubLayers: props => {
    const {boundingBox} = props.tile;
    return new BitmapLayer(props,{data:null,image:props.data,
      bounds:[boundingBox[0][0],boundingBox[0][1],boundingBox[1][0],boundingBox[1][1]]});
  }
});

const roads = new PathLayer({
  id:'roads',
  data:DATA.roads,
  getPath:d=>d.path,
  getColor:d=>d.capacity>=1500 ? [132,154,172,158] : [70,86,102,108],
  getWidth:d=>d.capacity>=1500 ? 3.0 : 1.4,
  widthMinPixels:0.7,
  widthMaxPixels:4,
  rounded:true
});

function meters(a,b){
  const R=6371000, toRad=x=>x*Math.PI/180;
  const p1=toRad(a[1]), p2=toRad(b[1]), dp=toRad(b[1]-a[1]), dl=toRad(b[0]-a[0]);
  const h=Math.sin(dp/2)**2+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
  return 2*R*Math.atan2(Math.sqrt(h),Math.sqrt(1-h));
}
function atTime(trip,t){
  const ts=trip.timestamps, path=trip.path;
  if(t<ts[0] || t>ts[ts.length-1]) return null;
  let lo=0, hi=ts.length-1;
  while(lo<hi-1){const mid=(lo+hi)>>1; if(ts[mid]<=t) lo=mid; else hi=mid;}
  const span=Math.max(0.001, ts[lo+1]-ts[lo]);
  const f=Math.max(0, Math.min(1, (t-ts[lo])/span));
  const a=path[lo], b=path[lo+1];
  return {position:[a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f], speed:meters(a,b)/span};
}
function vehicleColor(d){
  if(d.speed < 1.5) return [235,65,55,230];
  if(d.speed < 5) return [255,145,45,230];
  if(d.speed < 11) return [245,220,80,225];
  return [70,210,255,230];
}
function vehicleLayer(points){
  return new ScatterplotLayer({
    id:'vehicles',
    data:points,
    getPosition:d=>d.position,
    getFillColor:vehicleColor,
    getRadius:9,
    radiusUnits:'pixels',
    radiusMinPixels:3,
    radiusMaxPixels:9,
    stroked:true,
    getLineColor:[8,18,28,210],
    lineWidthMinPixels:1
  });
}
function affectedLayer(s){
  return new PathLayer({
    id:'affected-links',
    data:(s&&s.affected_paths)||[],
    getPath:d=>d.path,
    getColor:[255,190,65,235],
    getWidth:5,
    widthUnits:'pixels',
    rounded:true
  });
}

const deckgl = new DeckGL({
  container:'map',
  initialViewState:{longitude:DATA.center[0], latitude:DATA.center[1], zoom:13, pitch:50, bearing:-15},
  controller:true,
  layers:[basemap, roads, affectedLayer(DATA.scenarios[0]), vehicleLayer([])]
});

const clock=document.getElementById('clock'), scrub=document.getElementById('scrub');
const playBtn=document.getElementById('play'), speedEl=document.getElementById('speed');
const interventionSel=document.getElementById('intervention'), sel=document.getElementById('scenario'), ntrips=document.getElementById('ntrips');
const activeEl=document.getElementById('active'), doneEl=document.getElementById('done');
const lostEl=document.getElementById('lost'), shownEl=document.getElementById('shown');
const infoToggle=document.getElementById('infoToggle'), infoPanel=document.getElementById('infoPanel');
const sumTitle=document.getElementById('summaryTitle'), sumVht=document.getElementById('sumVht');
const sumTrip=document.getElementById('sumTrip'), sumStuck=document.getElementById('sumStuck');
const sumBcr=document.getElementById('sumBcr'), sumBenefit=document.getElementById('sumBenefit');
const sumCost=document.getElementById('sumCost'), sumWarn=document.getElementById('summaryWarn');

function fmtNumber(value,digits=0){return value===null||value===undefined||Number.isNaN(Number(value))?'--':Number(value).toLocaleString(undefined,{maximumFractionDigits:digits,minimumFractionDigits:digits});}
function renderSummary(){
  const s=DATA.scenarios[scen];
  const c=s.stats||null;
  document.getElementById('summary').style.display='block';
  sumTitle.textContent=s.name;
  sumBenefit.textContent=((s.affected_paths||[]).length).toLocaleString();
  if(!c){
    sumVht.textContent='--';
    sumTrip.textContent='--';
    sumStuck.textContent='--';
    sumBcr.textContent='--';
    sumCost.textContent='--';
    sumWarn.textContent='No comparison statistics found yet. Run s6 after this scenario completes.';
    return;
  }
  sumVht.textContent=fmtNumber(Number(c.completion_rate)*100,1)+'%';
  const vht=Number(c.vht_delta_vs_reference);
  const stuck=Number(c.stuck_trips_delta_vs_reference);
  const vmt=Number(c.vmt_delta_vs_reference);
  sumTrip.textContent=(vht>=0?'+':'')+fmtNumber(vht,1)+' hr';
  sumStuck.textContent=(stuck>=0?'+':'')+fmtNumber(stuck,0);
  sumBcr.textContent=(vmt>=0?'+':'')+fmtNumber(vmt,0)+' mi';
  sumCost.textContent=String(c.reference_scenario||'fixed');
  sumWarn.textContent='Compared with the clean/fixed network. Positive VHT or stuck-trip deltas mean worse car traffic.';
}

DATA.interventions.forEach((item)=>{const o=document.createElement('option');o.value=item.id;o.textContent=item.name;interventionSel.appendChild(o);});
function populateScenarios(){
  const selected=interventionSel.value || DATA.interventions[0].id;
  sel.replaceChildren();
  DATA.scenarios.forEach((s,i)=>{
    if(s.intervention!==selected) return;
    const o=document.createElement('option');o.value=i;o.textContent=s.name;sel.appendChild(o);
  });
  scen=Number(sel.value || 0);
}
function refreshScen(){const s=DATA.scenarios[scen];scrub.min=s.tmin;scrub.max=s.tmax;ntrips.textContent=s.name+' - '+s.trips.length+' sampled vehicles';renderSummary();}
function fmt(s){s=Math.floor(s)%86400;return String(Math.floor(s/3600)).padStart(2,'0')+':'+String(Math.floor((s%3600)/60)).padStart(2,'0');}
function countLeq(arr,t){let lo=0,hi=arr.length;while(lo<hi){const mid=(lo+hi)>>1;if(arr[mid]<=t)lo=mid+1;else hi=mid;}return lo;}

populateScenarios();
refreshScen();
renderSummary();
playBtn.onclick=()=>{playing=!playing;playBtn.textContent=playing?'Pause':'Play';};
infoToggle.onclick=()=>{infoPanel.classList.toggle('open'); infoToggle.textContent=infoPanel.classList.contains('open')?'Hide Info':'Info';};
scrub.oninput=e=>{currentTime=+e.target.value;};
speedEl.oninput=e=>{speed=+e.target.value;};
document.querySelectorAll('.preset').forEach(btn=>btn.onclick=()=>{speed=+btn.dataset.speed;speedEl.value=speed;});
sel.onchange=e=>{scen=+e.target.value;refreshScen();currentTime=DATA.scenarios[scen].tmin;};
interventionSel.onchange=()=>{populateScenarios();refreshScen();currentTime=DATA.scenarios[scen].tmin;};

let last=performance.now();
function frame(now){
  const dt=(now-last)/1000; last=now;
  const s=DATA.scenarios[scen];
  if(playing){currentTime+=dt*speed; if(currentTime>s.tmax) currentTime=s.tmin;}
  const points=[];
  for(const trip of s.trips){const point=atTime(trip,currentTime); if(point) points.push(point);}
  const completed=countLeq(s.arrivals,currentTime);
  const stuck=countLeq(s.stuck,currentTime);
  const departed=countLeq(s.departures,currentTime);
  scrub.value=currentTime;
  clock.textContent=fmt(currentTime);
  activeEl.textContent=Math.max(0, departed-completed-stuck).toLocaleString();
  doneEl.textContent=completed.toLocaleString();
  lostEl.textContent=stuck.toLocaleString();
  shownEl.textContent=points.length.toLocaleString();
  deckgl.setProps({layers:[basemap, roads, affectedLayer(s), vehicleLayer(points)]});
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default=str(DEFAULT_OUT))
    parser.add_argument("--sample", type=int, default=3500, help="approx vehicles per scenario")
    args = parser.parse_args()

    endpoints, roads, paths_by_link_id = network_links_wgs84()
    keep_every = max(1, round(118000 / max(args.sample, 1)))
    stats_by_scenario = read_scenario_stats()

    scenario_entries = list(SCENARIOS)
    interventions = list(INTERVENTIONS)
    manifest_path = ROOT / "data" / "interim" / "user_scenarios" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except json.JSONDecodeError:
        manifest = {}
    custom_entries = [
        ("user_scenarios", str(item["id"]), str(item["name"]), SCEN_DIR / str(item["output_dir"]))
        for item in manifest.get("scenarios", [])
        if item.get("id") and item.get("name") and item.get("output_dir")
    ]
    if custom_entries:
        scenario_entries.extend(custom_entries)
        interventions.append(
            {
                "id": "user_scenarios",
                "name": "User scenarios",
                "plain_language": "Street changes drawn in the local scenario builder.",
            }
        )

    scenarios = []
    all_points = []
    for intervention_id, scenario_id, label, output_dir in scenario_entries:
        events_path = last_events_file(output_dir)
        if events_path is None:
            print(f"  skip '{label}' (no events at {output_dir})")
            continue
        built = build_trips(events_path, endpoints, keep_every)
        trips = built["trips"]
        if not trips and keep_every > 1:
            built = build_trips(events_path, endpoints, 1)
            trips = built["trips"]
        if not trips:
            print(f"  skip '{label}' (no sampled trajectories at {events_path})")
            continue
        tmin = min(trip["timestamps"][0] for trip in trips)
        tmax = max(trip["timestamps"][-1] for trip in trips)
        affected_csv = None
        if scenario_id == "bike_lane":
            affected_csv = ROOT / "data" / "interim" / "bike_lane_links.csv"
        elif intervention_id == "user_scenarios":
            affected_csv = ROOT / "data" / "interim" / "user_scenarios" / scenario_id / "selected_links.csv"
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "name": label,
                "intervention": intervention_id,
                "trips": trips,
                "tmin": round(tmin, 1),
                "tmax": round(tmax, 1),
                "departures": built["departures"],
                "arrivals": built["arrivals"],
                "stuck": built["stuck"],
                "affected_paths": affected_paths_from_csv(affected_csv, paths_by_link_id) if affected_csv is not None else [],
                "stats": stats_by_scenario.get(scenario_id),
            }
        )
        all_points += [point for trip in trips for point in trip["path"]]
        print(f"  '{label}': {len(trips)} trips from {events_path}")

    if not scenarios:
        raise SystemExit("No scenarios with events found. Run the sim first.")

    center_x = sum(point[0] for point in all_points) / len(all_points)
    center_y = sum(point[1] for point in all_points) / len(all_points)
    payload = {
        "scenarios": scenarios,
        "roads": roads,
        "center": [round(center_x, 5), round(center_y, 5)],
        "interventions": interventions,
        "comparisons": {},
    }
    html = HTML.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    output_path = Path(args.out)
    output_path.write_text(html, encoding="utf-8")
    print(
        f"wrote {output_path} ({output_path.stat().st_size / 1e6:.1f} MB) | "
        f"scenarios={len(scenarios)} | roads={len(roads)} | keep_every={keep_every}"
    )


if __name__ == "__main__":
    main()
