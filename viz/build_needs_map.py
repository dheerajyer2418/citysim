"""Build a self-contained deck.gl "needs priority" choropleth for Logan Square.

Reads the s7 needs index (data/processed/needs_index.geojson + .csv + summary) and
writes one standalone HTML that colors every street segment by its 0-100 need
score, with street-name hover, a click popup with details, a basemap switcher,
and a corner "Sources" button.

Usage:
    python viz/build_needs_map.py [out.html]
"""

from __future__ import annotations

import csv as _csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # allow importing pipeline.* when run as a script

# Only show the click popup for segments at/above this score (green/quiet streets stay silent).
POPUP_MIN_SCORE = 20.0

BASE_SOURCES = [
    {"name": "OpenStreetMap (road network)", "url": "https://www.openstreetmap.org"},
    {"name": "CMAP c24q4 travel demand model", "url": "https://www.cmap.illinois.gov"},
    {"name": "MATSim 2024.0 traffic simulation engine", "url": "https://www.matsim.org"},
]


def _linestring_coords(geometry: dict) -> list[list[float]]:
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [[round(float(x), 6), round(float(y), 6)] for x, y, *_ in coords]
    if gtype == "MultiLineString" and coords:
        return [[round(float(x), 6), round(float(y), 6)] for x, y, *_ in coords[0]]
    return []


def _osm_way_id(link_id: str) -> int | None:
    """The leading integer of a MATSim link_id is its OSM way id."""
    try:
        return int(str(link_id).split("_")[0].split("-")[0])
    except (ValueError, IndexError):
        return None


def load_link_names(cfg, link_ids: list[str]) -> dict[str, str]:
    """Map link_id -> street name via the OSM extract (cached to data/interim/link_names.json)."""
    names_cache = cfg.data_interim / "link_names.json"
    if names_cache.exists():
        try:
            return json.loads(names_cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    try:
        import glob

        import geopandas as gpd

        from pipeline.s4_calibrate import _buffered_boundary_bounds_4326

        dataset = cfg.sources["osm"].get("pyrosm_dataset", "Chicago")
        pbf = cfg.data_raw / f"{dataset}.osm.pbf"
        if not pbf.exists():
            matches = glob.glob(str(cfg.data_raw / "*.pbf"))
            if not matches:
                raise FileNotFoundError("no OSM .pbf in data/raw")
            pbf = Path(matches[0])
        bounds = _buffered_boundary_bounds_4326(
            cfg.boundary_path,
            cfg.crs,
            cfg.sources["osm"]["network_buffer_m"],
        )
        # GDAL's OSM driver reads the pbf directly (pyrosm's C-extension is blocked here).
        lines = gpd.read_file(str(pbf), layer="lines", bbox=(bounds[0], bounds[1], bounds[2], bounds[3]))
        way_name: dict[int, str] = {}
        if "osm_id" in lines.columns and "name" in lines.columns:
            for wid, name in zip(lines["osm_id"], lines["name"]):
                if name is None or wid is None:
                    continue
                text = str(name).strip()
                if not text or text.lower() == "nan":
                    continue
                try:
                    way_name[int(wid)] = text
                except (TypeError, ValueError):
                    continue
        names: dict[str, str] = {}
        for lid in link_ids:
            wid = _osm_way_id(lid)
            if wid is not None and wid in way_name:
                names[lid] = way_name[wid]
        names_cache.parent.mkdir(parents=True, exist_ok=True)
        names_cache.write_text(json.dumps(names), encoding="utf-8")
        return names
    except Exception as exc:  # pragma: no cover - names are a nice-to-have, never fatal
        print(f"  (street names unavailable: {exc})")
        return {}


def build_payload(cfg) -> dict:
    geojson = cfg.data_processed / "needs_index.geojson"
    csv_path = cfg.data_processed / "needs_index.csv"
    summary_path = cfg.data_processed / "needs_index_summary.json"
    geo = json.loads(geojson.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}

    raw: dict[str, dict] = {}
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in _csv.DictReader(handle):
                raw[row["link_id"]] = row

    def _num(row: dict, key: str) -> float:
        try:
            return float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    features = []
    all_points: list[list[float]] = []
    for feat in geo.get("features", []):
        path = _linestring_coords(feat.get("geometry") or {})
        if len(path) < 2:
            continue
        props = feat.get("properties") or {}
        lid = str(props.get("link_id", ""))
        row = raw.get(lid, {})
        features.append(
            {
                "path": path,
                "link_id": lid,
                "score": round(float(props.get("need_score", 0.0)), 1),
                "safety": round(float(props.get("safety_norm", 0.0)), 3),
                "pavement": round(float(props.get("pavement_norm", 0.0)), 3),
                "congestion": round(float(props.get("congestion_norm", 0.0)), 3),
                "crashes": round(_num(row, "crashes_raw"), 1),
                "potholes": int(_num(row, "potholes_raw")),
                "adt": int(_num(row, "adt_raw")),
            }
        )
        all_points.extend(path)

    names = load_link_names(cfg, [f["link_id"] for f in features])
    for f in features:
        f["name"] = names.get(f["link_id"], "Unnamed street")

    order = sorted(range(len(features)), key=lambda i: features[i]["score"], reverse=True)
    for rank, i in enumerate(order, 1):
        features[i]["rank"] = rank

    center = [-87.7075, 41.9235]
    if all_points:
        center = [
            round(sum(p[0] for p in all_points) / len(all_points), 5),
            round(sum(p[1] for p in all_points) / len(all_points), 5),
        ]

    return {
        "area_slug": getattr(cfg, "area_slug", "logan_square"),
        "area_name": getattr(cfg, "area_name", "Logan Square"),
        "features": features,
        "center": center,
        "total": len(features),
        "popupMinScore": POPUP_MIN_SCORE,
        "weights": summary.get("weights", {}),
        "coverage": summary.get("layer_coverage", {}),
        "sources": list(BASE_SOURCES) + list(summary.get("sources", [])),
    }


HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>CitySim - Where do Logan Square streets need attention?</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://unpkg.com/deck.gl@9.0.0/dist.min.js"></script>
<style>
  html,body,#map{margin:0;width:100%;height:100%;background:#0a0e14;overflow:hidden;font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#e7f1ff;}
  #banner{position:absolute;top:16px;left:50%;transform:translateX(-50%);z-index:7;background:rgba(10,20,32,.82);border:1px solid rgba(116,215,255,.4);border-radius:10px;padding:12px 22px;text-align:center;backdrop-filter:blur(4px);}
  #banner b{font-size:20px;display:block;}
  #banner span{font-size:12.5px;color:#9fb7cc;}
  #areaName{position:absolute;left:50%;bottom:78px;transform:translateX(-50%);z-index:7;background:rgba(6,14,22,.74);border:1px solid rgba(116,215,255,.42);border-radius:8px;padding:7px 14px;color:#fff;font-size:18px;font-weight:700;letter-spacing:0;text-shadow:0 1px 8px rgba(0,0,0,.75);pointer-events:none;}
  #hud{position:absolute;top:14px;left:16px;z-index:5;max-width:300px;background:rgba(10,20,32,.72);border:1px solid rgba(90,160,255,.25);border-radius:9px;padding:12px 14px;backdrop-filter:blur(4px);}
  #hud p{font-size:12px;line-height:1.4;color:#c6d9ec;margin:4px 0;}
  #ramp{height:12px;border-radius:6px;background:linear-gradient(90deg,rgb(40,190,120),rgb(245,220,80),rgb(235,80,55));margin-top:8px;}
  #legendrow{display:flex;justify-content:space-between;font-size:10px;color:#9fb7cc;margin-top:3px;text-transform:uppercase;}
  #views{position:absolute;bottom:16px;left:16px;z-index:6;display:flex;gap:6px;}
  #views button{background:rgba(10,20,32,.8);color:#dff;border:1px solid #3c6f8c;border-radius:7px;padding:7px 12px;font-size:12px;cursor:pointer;}
  #views button:hover{border-color:#74d7ff;}
  #views button.active{background:#1f7fbf;border-color:#39c0ff;color:#fff;}
  #popup{position:absolute;bottom:16px;right:16px;z-index:6;display:none;max-width:300px;background:rgba(6,14,22,.94);border:1px solid rgba(120,160,210,.35);border-radius:9px;padding:12px 14px;font-size:12.5px;line-height:1.45;}
  #popup h3{margin:0 0 4px 0;font-size:15px;color:#fff;}
  #popup .score{font-size:13px;color:#8fd6ff;margin-bottom:8px;}
  #popup .bar{height:7px;border-radius:4px;background:#16324a;margin:2px 0 8px 0;}
  #popup .bar span{display:block;height:7px;border-radius:4px;background:#39c0ff;}
  #popup .raw{color:#c6d9ec;font-size:11.5px;margin:2px 0;}
  #popup .lbl{font-size:11px;color:#9fb7cc;}
  #srcBtn{position:absolute;top:14px;right:16px;z-index:8;background:#12283c;color:#dff;border:1px solid #3c6f8c;border-radius:7px;padding:8px 12px;font-size:12.5px;cursor:pointer;}
  #srcBtn:hover{border-color:#74d7ff;}
  #srcPanel{display:none;position:absolute;top:52px;right:16px;z-index:8;width:min(320px,calc(100vw - 32px));background:rgba(6,14,22,.96);border:1px solid rgba(120,160,210,.3);border-radius:8px;padding:12px 14px;font-size:12px;line-height:1.6;}
  #srcPanel.open{display:block;}
  #srcPanel h3{font-size:13px;margin:0 0 6px 0;color:#fff;}
  #srcPanel a{color:#8fd6ff;text-decoration:none;}
  #srcPanel a:hover{text-decoration:underline;}
  #srcPanel .note{color:#a9bed2;font-size:11px;margin-top:8px;}
  #areaWrap{position:absolute;top:14px;right:96px;z-index:8;color:#dff;background:rgba(10,20,32,.82);border:1px solid rgba(90,160,255,.25);border-radius:8px;padding:7px 10px;font-size:12.5px;display:flex;gap:7px;align-items:center;}
  #areaButtons{display:flex;gap:5px;}
  #areaButtons button{background:#16324a;color:#dff;border:1px solid #3c6f8c;border-radius:6px;padding:5px 8px;font-size:12.5px;cursor:pointer;}
  #areaButtons button.active{background:#1f7fbf;border-color:#74d7ff;color:#fff;}
  #nbhdHint{position:absolute;left:50%;top:20px;transform:translateX(-50%);z-index:8;display:none;background:rgba(10,20,32,.82);border:1px solid rgba(116,215,255,.4);border-radius:999px;padding:9px 18px;font-size:13.5px;color:#e7f3ff;backdrop-filter:blur(5px);box-shadow:0 8px 26px rgba(0,0,0,.45);pointer-events:none;letter-spacing:.01em;animation:hintIn .5s ease both;}
  #nbhdHint b{color:#8fd6ff;font-weight:700;}
  @keyframes hintIn{from{opacity:0;transform:translate(-50%,-6px);}to{opacity:1;transform:translate(-50%,0);}}
</style>
</head>
<body>
<div id="map"></div>
<div id="banner"><b>Click a street to see why it needs attention</b><span>Hover to highlight &middot; warmer color = higher priority</span></div>
<div id="nbhdHint">Pick a <b>neighborhood</b> to explore</div>
<div id="areaName"></div>
<div id="hud">
  <p style="font-size:13px;color:#e7f1ff;">Streets scored 0-100 from public crash, pothole, and traffic data.</p>
  <p style="color:#9fb7cc;font-size:11px;">A planning signal, not ground truth.</p>
  <div id="ramp"></div><div id="legendrow"><span>lower need</span><span>higher need</span></div>
</div>
<div id="areaWrap">Neighborhood <span id="areaButtons"></span></div>
<button id="srcBtn">Sources</button>
<div id="srcPanel"></div>
<div id="views">
  <button data-base="dark" class="active">Dark</button>
  <button data-base="light">Streets</button>
  <button data-base="satellite">Satellite</button>
</div>
<div id="popup"></div>
<script id="payload" type="application/json">__DATA__</script>
<script>
const ROOT_DATA = JSON.parse(document.getElementById('payload').textContent);
const AREAS = ROOT_DATA.areas || {[ROOT_DATA.area_slug || 'logan_square']: ROOT_DATA};
const AREA_ORDER = ROOT_DATA.area_order || Object.keys(AREAS);
let areaSlug = localStorage.getItem('citysim_needs_area') || ROOT_DATA.default_area || AREA_ORDER[0];
if(!AREAS[areaSlug]) areaSlug = AREA_ORDER[0];
let DATA = AREAS[areaSlug];
const BOUNDARIES = ROOT_DATA.boundaries || [];
const BOUNDS = ROOT_DATA.bounds || null;
let curZoom = 11;
function grayFor(i){const n=Math.max(1,BOUNDARIES.length-1);const v=Math.round(64+72*(i/n));return v;}
function overlayOpacity(){return BOUNDARIES.length>1?Math.max(0,Math.min(1,(12.7-curZoom)/1.5)):0;}
const {DeckGL, TileLayer, BitmapLayer, PathLayer} = deck;
const BASEMAPS = {
  dark:'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
  light:'https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png',
  satellite:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
};
let currentBase='dark';
function scoreColor(s){
  const t=Math.max(0,Math.min(1,s/100));
  let r,g,b;
  if(t<0.5){const u=t/0.5; r=Math.round(40+205*u); g=Math.round(190+30*u); b=Math.round(120-40*u);}
  else{const u=(t-0.5)/0.5; r=Math.round(245-10*u); g=Math.round(220-140*u); b=Math.round(80-25*u);}
  return [r,g,b,150+Math.round(t*90)];
}
function baseLayer(){
  return new TileLayer({id:'base-'+currentBase,data:BASEMAPS[currentBase],minZoom:0,maxZoom:19,tileSize:256,
    renderSubLayers:props=>new BitmapLayer(props,{data:null,image:props.data,bounds:[props.tile.boundingBox[0][0],props.tile.boundingBox[0][1],props.tile.boundingBox[1][0],props.tile.boundingBox[1][1]]})});
}
function needsLayer(){
  return new PathLayer({id:'needs',data:DATA.features,pickable:true,autoHighlight:true,highlightColor:[120,220,255,255],
    getPath:d=>d.path,getColor:d=>scoreColor(d.score),getWidth:d=>1.2+2.8*(d.score/100),
    widthUnits:'pixels',widthMinPixels:1.4,widthMaxPixels:6,rounded:true,
    onClick:info=>{if(info.object)clickStreet(info.object);}});
}
function boundaryFill(op){
  return new deck.PolygonLayer({id:'nbhd-fill',data:BOUNDARIES,pickable:true,opacity:op,
    autoHighlight:true,highlightColor:[150,210,255,50],parameters:{depthTest:false},
    getPolygon:d=>d.polygon,filled:true,stroked:true,lineJointRounded:true,lineCapRounded:true,
    getFillColor:(d,{index})=>{const v=grayFor(index);return d.slug===areaSlug?[96,170,226,34]:[v,v,v,94];},
    getLineColor:d=>d.slug===areaSlug?[140,226,255,255]:[212,220,234,150],
    getLineWidth:d=>d.slug===areaSlug?2.6:1,lineWidthUnits:'pixels',lineWidthMinPixels:0.9,
    updateTriggers:{getFillColor:areaSlug,getLineColor:areaSlug,getLineWidth:areaSlug},
    onClick:info=>{if(info.object)selectArea(info.object.slug);}});
}
function boundaryLabels(op){
  return new deck.TextLayer({id:'nbhd-labels',data:BOUNDARIES,pickable:true,opacity:op,
    parameters:{depthTest:false},
    getPosition:d=>d.label,getText:d=>d.name,
    getSize:d=>d.slug===areaSlug?18:13,sizeUnits:'pixels',
    getColor:d=>d.slug===areaSlug?[255,255,255,255]:[221,230,242,225],
    fontFamily:'Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif',fontWeight:700,
    fontSettings:{sdf:true},outlineWidth:2.6,outlineColor:[4,10,18,240],
    getTextAnchor:'middle',getAlignmentBaseline:'center',billboard:true,characterSet:'auto',
    updateTriggers:{getColor:areaSlug,getSize:areaSlug},
    onClick:info=>{if(info.object)selectArea(info.object.slug);}});
}
function nbhdFill(){const op=overlayOpacity();return op<0.04?[]:[boundaryFill(op)];}
function nbhdLabels(){const op=overlayOpacity();return op<0.04?[]:[boundaryLabels(op)];}
function mapLayers(){return [baseLayer(), ...nbhdFill(), needsLayer(), ...nbhdLabels()];}
const deckgl=new DeckGL({container:'map',
  initialViewState:{longitude:DATA.center[0],latitude:DATA.center[1],zoom:13.2,pitch:0,bearing:0},
  controller:true,layers:mapLayers(),
  onViewStateChange:({viewState})=>{curZoom=viewState.zoom; render();},
  getTooltip:({object})=>object && object.path && {html:'<b>'+object.name+'</b> &middot; need '+object.score.toFixed(0),
    style:{background:'rgba(6,14,22,.95)',color:'#e7f1ff',fontSize:'12px',padding:'6px 9px',borderRadius:'6px',border:'1px solid rgba(116,215,255,.5)'}},
  getCursor:({isHovering})=>isHovering?'pointer':'grab'});
if(BOUNDARIES.length>1 && BOUNDS){
  try{
    const vp=new deck.WebMercatorViewport({width:window.innerWidth,height:window.innerHeight});
    const fitted=vp.fitBounds(BOUNDS,{padding:{top:70,bottom:96,left:80,right:76}});
    curZoom=Math.min(fitted.zoom,12.5);
    deckgl.setProps({initialViewState:{longitude:fitted.longitude,latitude:fitted.latitude,zoom:curZoom,pitch:0,bearing:0}});
  }catch(e){}
}
function render(){deckgl.setProps({layers:mapLayers()});}
function selectArea(nextSlug){
  areaSlug = nextSlug;
  DATA = AREAS[areaSlug];
  localStorage.setItem('citysim_needs_area', areaSlug);
  document.title='CitySim - Where do '+DATA.area_name+' streets need attention?';
  document.getElementById('areaName').textContent=DATA.area_name;
  document.getElementById('popup').style.display='none';
  deckgl.setProps({initialViewState:{longitude:DATA.center[0],latitude:DATA.center[1],zoom:13.2,pitch:0,bearing:0,transitionInterpolator:new deck.FlyToInterpolator({speed:1.8}),transitionDuration:'auto'}});
  render();
  renderSources();
  const hint=document.getElementById('nbhdHint'); if(hint) hint.style.display='none';
  const bn=document.getElementById('banner'); if(bn) bn.style.display='';
  const an=document.getElementById('areaName'); if(an) an.style.display='';
  document.querySelectorAll('#areaButtons button').forEach(b=>b.classList.toggle('active', b.dataset.area===areaSlug));
}
function bar(v){return '<div class="bar"><span style="width:'+Math.round(v*100)+'%"></span></div>';}
function clickStreet(d){
  const p=document.getElementById('popup');
  if(d.score < DATA.popupMinScore){p.style.display='none';return;}
  const pct=Math.round(100*d.rank/DATA.total);
  p.innerHTML='<h3>'+d.name+'</h3>'+
    '<div class="score">Need score '+d.score.toFixed(0)+'/100 &middot; rank #'+d.rank+' of '+DATA.total+' (top '+pct+'%)</div>'+
    '<div class="lbl">Safety</div>'+bar(d.safety)+
    '<div class="lbl">Pavement</div>'+bar(d.pavement)+
    '<div class="lbl">Congestion</div>'+bar(d.congestion)+
    '<div class="raw">Reported crashes (severity-weighted): '+d.crashes+'</div>'+
    '<div class="raw">Open potholes: '+d.potholes+'</div>'+
    '<div class="raw">Avg daily traffic (nearby count): '+(d.adt?d.adt.toLocaleString():'no count')+'</div>';
  p.style.display='block';
}
document.querySelectorAll('#views button').forEach(b=>b.onclick=()=>{
  currentBase=b.dataset.base;
  document.querySelectorAll('#views button').forEach(x=>x.classList.toggle('active',x===b));
  render();
});
const areaButtons=document.getElementById('areaButtons');
AREA_ORDER.forEach(slug=>{const b=document.createElement('button');b.type='button';b.dataset.area=slug;b.textContent=AREAS[slug].area_name||slug;b.onclick=()=>selectArea(slug);areaButtons.appendChild(b);});
document.querySelectorAll('#areaButtons button').forEach(b=>b.classList.toggle('active', b.dataset.area===areaSlug));
if(BOUNDARIES.length>1){
  const aw=document.getElementById('areaWrap'); if(aw) aw.style.display='none';
  const hint=document.getElementById('nbhdHint'); if(hint) hint.style.display='block';
  const bn=document.getElementById('banner'); if(bn) bn.style.display='none';
  const an=document.getElementById('areaName'); if(an) an.style.display='none';
}
const btn=document.getElementById('srcBtn'), panel=document.getElementById('srcPanel');
function renderSources(){
let html='<h3>Data sources</h3>';
DATA.sources.forEach(s=>{html+='<div>'+(s.url?('<a href="'+s.url+'" target="_blank" rel="noopener">'+s.name+'</a>'):s.name)+'</div>';});
const w=DATA.weights||{}, c=DATA.coverage||{};
html+='<div class="note">Weights &mdash; safety '+((w.safety||0)*100).toFixed(0)+'%, pavement '+((w.pavement||0)*100).toFixed(0)+'%, congestion '+((w.congestion||0)*100).toFixed(0)+'%.</div>';
html+='<div class="note">Coverage &mdash; safety '+((c.safety||0)*100).toFixed(0)+'%, pavement '+((c.pavement||0)*100).toFixed(0)+'%, congestion '+((c.congestion||0)*100).toFixed(0)+'%. Pavement and traffic-count coverage is sparse, so the score is currently safety-weighted.</div>';
panel.innerHTML=html;
}
document.title='CitySim - Where do '+DATA.area_name+' streets need attention?';
document.getElementById('areaName').textContent=DATA.area_name;
renderSources();
btn.onclick=()=>panel.classList.toggle('open');
</script>
</body>
</html>
"""


def main() -> None:
    import argparse

    from pipeline.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?")
    parser.add_argument("--area", help="configured area slug to use")
    args = parser.parse_args()
    cfg = load_config(area=args.area)
    out_path = Path(args.out) if args.out else cfg.scenario_dir / "output" / "needs_map.html"
    geojson = cfg.data_processed / "needs_index.geojson"
    if not geojson.exists():
        raise SystemExit(f"Missing {geojson}. Run: python cli.py run --stage s7 --area {cfg.area_slug}")
    payload = build_payload(cfg)
    html = HTML.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    html = html.replace("Logan Square", cfg.area_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    named = sum(1 for f in payload["features"] if f["name"] != "Unnamed street")
    print(f"wrote {out_path} ({out_path.stat().st_size/1e6:.1f} MB) | segments={len(payload['features'])} named={named}")


if __name__ == "__main__":
    main()
