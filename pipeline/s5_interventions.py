"""Stage s5: pothole intervention network generation."""

from __future__ import annotations

import csv
import gzip
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from operator import index as operator_index
from pathlib import Path
from statistics import median
from typing import Any

from lxml import etree

from pipeline.s4_calibrate import _buffered_boundary_bounds_4326, _gpkg_geometry_columns, _gpkg_wkb, _quote_identifier


SNAP_DISTANCE_M = 30.0
NETWORK_DTD = '<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">'
CONFIG_DTD = '<!DOCTYPE config SYSTEM "http://www.matsim.org/files/dtd/config_v2.dtd">'


@dataclass(frozen=True)
class PotholeRecord:
    lat: float
    lon: float
    status: str = ""
    creation_date: date | None = None


@dataclass(frozen=True)
class NetworkLink:
    link_id: str
    geometry: Any
    length_m: float
    freespeed: float | None = None
    capacity: float | None = None
    is_connector: bool = False


def speed_penalty(n_potholes: int, speed_penalty_per_pothole: float, max_penalty: float) -> float:
    """Return the capped fractional speed penalty for a link."""
    return min(float(max_penalty), float(speed_penalty_per_pothole) * max(0, int(n_potholes)))


def degraded_freespeed(base_freespeed: float, n_potholes: int, speed_penalty_per_pothole: float, max_penalty: float) -> float:
    """Apply pothole degradation to a MATSim link freespeed."""
    penalty = speed_penalty(n_potholes, speed_penalty_per_pothole, max_penalty)
    return float(base_freespeed) * (1.0 - penalty)


def snap_potholes_to_links(points: list[Any], links: list[NetworkLink], max_distance_m: float = SNAP_DISTANCE_M) -> Counter[str]:
    """Snap projected pothole points to nearest links and count potholes per link."""
    from shapely.strtree import STRtree

    geometries = [link.geometry for link in links if link.geometry is not None and not link.geometry.is_empty]
    indexed_links = [link for link in links if link.geometry is not None and not link.geometry.is_empty]
    if not geometries:
        return Counter()

    tree = STRtree(geometries)
    geometry_id_to_link = {id(geometry): link for geometry, link in zip(geometries, indexed_links)}
    counts: Counter[str] = Counter()

    for point in points:
        query_result = tree.query(point.buffer(max_distance_m))
        best_link: NetworkLink | None = None
        best_distance = float("inf")
        for item in query_result:
            try:
                link_index = operator_index(item)
            except TypeError:
                link_index = None
            link = indexed_links[link_index] if link_index is not None else geometry_id_to_link[id(item)]
            distance = float(link.geometry.distance(point))
            if distance <= max_distance_m and distance < best_distance:
                best_link = link
                best_distance = distance
        if best_link is not None:
            counts[best_link.link_id] += 1
    return counts


def _read_network_links(path: Path) -> list[NetworkLink]:
    import sqlite3

    from shapely import wkb

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        table, geom_col = _gpkg_geometry_columns(conn)
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
        required = {"link_id", geom_col}
        missing = required.difference(columns)
        if missing:
            raise ValueError(f"Missing required link columns in {path}: {sorted(missing)}")
        select_columns = ["link_id", _quote_identifier(geom_col)]
        has_length = "length" in columns
        has_freespeed = "freespeed" in columns
        has_capacity = "capacity" in columns
        has_connector = "is_connector" in columns
        if has_length:
            select_columns.append("length")
        if has_freespeed:
            select_columns.append("freespeed")
        if has_capacity:
            select_columns.append("capacity")
        if has_connector:
            select_columns.append("is_connector")
        rows = conn.execute(
            f"SELECT {', '.join(select_columns)} FROM {_quote_identifier(table)} "
            f"WHERE {_quote_identifier(geom_col)} IS NOT NULL"
        ).fetchall()

    links: list[NetworkLink] = []
    for row in rows:
        link_id = str(row["link_id"])
        geometry = wkb.loads(_gpkg_wkb(bytes(row[geom_col])))
        length_m = float(row["length"]) if has_length else float(geometry.length)
        freespeed = float(row["freespeed"]) if has_freespeed else None
        capacity = float(row["capacity"]) if has_capacity else None
        is_connector = bool(row["is_connector"]) if has_connector else False
        if not geometry.is_empty:
            links.append(
                NetworkLink(
                    link_id=link_id,
                    geometry=geometry,
                    length_m=length_m,
                    freespeed=freespeed,
                    capacity=capacity,
                    is_connector=is_connector,
                )
            )
    return links


def _fetch_pothole_records(cfg: Any, bounds: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    import requests

    min_lon, min_lat, max_lon, max_lat = bounds
    socrata = cfg.sources["socrata"]
    domain = socrata.get("domain", "data.cityofchicago.org")
    dataset_id = socrata["potholes_311"]["dataset_id"]
    endpoint = f"https://{domain}/resource/{dataset_id}.json"
    params = {
        "$limit": 50_000,
        "$select": "latitude,longitude,status,creation_date,type_of_service_request,community_area",
        "$where": (
            f"latitude between {min_lat} and {max_lat} "
            f"and longitude between {min_lon} and {max_lon}"
        ),
    }
    response = requests.get(endpoint, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected Socrata response for {dataset_id}: expected a JSON list.")
    return payload


def _parse_socrata_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _as_of_date(value: Any) -> date:
    if value:
        parsed = _parse_socrata_date(value)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc).date()


def _valid_potholes(
    records: list[dict[str, Any]],
    *,
    recent_days: int | None = None,
    active_statuses: set[str] | None = None,
    as_of: date | None = None,
) -> list[PotholeRecord]:
    potholes: list[PotholeRecord] = []
    cutoff = None if recent_days is None else (as_of or datetime.now(timezone.utc).date()) - timedelta(days=max(0, recent_days))
    normalized_active = {status.strip().lower() for status in (active_statuses or set()) if status.strip()}
    for record in records:
        request_type = str(record.get("type_of_service_request", ""))
        if request_type and "pothole" not in request_type.lower():
            continue
        status = str(record.get("status", "")).strip()
        created = _parse_socrata_date(record.get("creation_date"))
        if cutoff is not None:
            is_active = status.lower() in normalized_active
            is_recent = created is not None and created >= cutoff
            if not is_active and not is_recent:
                continue
        try:
            lat = float(record["latitude"])
            lon = float(record["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        potholes.append(PotholeRecord(lat=lat, lon=lon, status=status, creation_date=created))
    return potholes


def _project_potholes(potholes: list[PotholeRecord], projected_crs: str) -> list[Any]:
    from pyproj import Transformer
    from shapely.geometry import Point

    transformer = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
    points = []
    for pothole in potholes:
        x, y = transformer.transform(pothole.lon, pothole.lat)
        points.append(Point(x, y))
    return points


def _degrade_network(
    input_path: Path,
    output_path: Path,
    potholes_by_link: Counter[str],
    speed_penalty_per_pothole: float,
    max_penalty: float,
) -> dict[str, tuple[float, float]]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(input_path, "rb") as handle:
        tree = etree.parse(handle)

    speeds: dict[str, tuple[float, float]] = {}
    for link_el in tree.findall(".//link"):
        link_id = str(link_el.get("id", ""))
        n_potholes = potholes_by_link.get(link_id, 0)
        if n_potholes <= 0:
            continue
        base = float(link_el.get("freespeed", "0"))
        degraded = degraded_freespeed(base, n_potholes, speed_penalty_per_pothole, max_penalty)
        link_el.set("freespeed", _format_float(degraded))
        speeds[link_id] = (base, degraded)

    with gzip.open(output_path, "wb") as handle:
        tree.write(handle, encoding="UTF-8", xml_declaration=True, pretty_print=True, doctype=NETWORK_DTD)
    return speeds


def select_links_near_corridor(
    links: list[NetworkLink],
    corridor_lonlat: list[list[float]],
    projected_crs: str,
    buffer_m: float,
    min_capacity: float,
) -> list[NetworkLink]:
    """Return non-connector car links near a lon/lat corridor line."""
    from pyproj import Transformer
    from shapely.geometry import LineString

    if len(corridor_lonlat) < 2:
        return []
    transformer = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
    projected = [transformer.transform(float(lon), float(lat)) for lon, lat in corridor_lonlat]
    corridor = LineString(projected).buffer(float(buffer_m))

    selected: list[NetworkLink] = []
    for link in links:
        if link.is_connector:
            continue
        if link.capacity is not None and link.capacity < float(min_capacity):
            continue
        if link.geometry is not None and not link.geometry.is_empty and link.geometry.intersects(corridor):
            selected.append(link)
    return selected


def _write_bike_lane_links_csv(path: Path, selected_links: list[NetworkLink], capacity_factor: float, freespeed_factor: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "link_id",
                "length_m",
                "base_capacity",
                "bike_lane_capacity",
                "base_freespeed",
                "bike_lane_freespeed",
            ],
        )
        writer.writeheader()
        for link in selected_links:
            base_capacity = link.capacity or 0.0
            base_freespeed = link.freespeed or 0.0
            writer.writerow(
                {
                    "link_id": link.link_id,
                    "length_m": _format_float(link.length_m),
                    "base_capacity": _format_float(base_capacity),
                    "bike_lane_capacity": _format_float(base_capacity * capacity_factor),
                    "base_freespeed": _format_float(base_freespeed),
                    "bike_lane_freespeed": _format_float(base_freespeed * freespeed_factor),
                }
            )


def _apply_bike_lane_network(
    input_path: Path,
    output_path: Path,
    selected_link_ids: set[str],
    capacity_factor: float,
    freespeed_factor: float,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(input_path, "rb") as handle:
        tree = etree.parse(handle)

    changed = 0
    for link_el in tree.findall(".//link"):
        link_id = str(link_el.get("id", ""))
        if link_id not in selected_link_ids:
            continue
        if link_el.get("capacity") is not None:
            link_el.set("capacity", _format_float(float(link_el.get("capacity", "0")) * capacity_factor))
        if link_el.get("freespeed") is not None:
            link_el.set("freespeed", _format_float(float(link_el.get("freespeed", "0")) * freespeed_factor))
        changed += 1

    with gzip.open(output_path, "wb") as handle:
        tree.write(handle, encoding="UTF-8", xml_declaration=True, pretty_print=True, doctype=NETWORK_DTD)
    return changed


def read_link_edit_csv(path: Path) -> dict[str, dict[str, float]]:
    """Read generic link edit factors keyed by MATSim link id."""
    edits: dict[str, dict[str, float]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            link_id = str(row.get("link_id", "")).strip()
            if not link_id:
                continue
            edit: dict[str, float] = {}
            for key in ("capacity_factor", "freespeed_factor", "capacity", "freespeed"):
                value = row.get(key)
                if value not in (None, ""):
                    edit[key] = float(value)
            if edit:
                edits[link_id] = edit
    return edits


def _apply_link_edits(input_path: Path, output_path: Path, edits: dict[str, dict[str, float]]) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(input_path, "rb") as handle:
        tree = etree.parse(handle)

    changed = 0
    for link_el in tree.findall(".//link"):
        link_id = str(link_el.get("id", ""))
        edit = edits.get(link_id)
        if not edit:
            continue
        if "capacity" in edit:
            link_el.set("capacity", _format_float(edit["capacity"]))
        elif "capacity_factor" in edit and link_el.get("capacity") is not None:
            link_el.set("capacity", _format_float(float(link_el.get("capacity", "0")) * edit["capacity_factor"]))
        if "freespeed" in edit:
            link_el.set("freespeed", _format_float(edit["freespeed"]))
        elif "freespeed_factor" in edit and link_el.get("freespeed") is not None:
            link_el.set("freespeed", _format_float(float(link_el.get("freespeed", "0")) * edit["freespeed_factor"]))
        changed += 1

    with gzip.open(output_path, "wb") as handle:
        tree.write(handle, encoding="UTF-8", xml_declaration=True, pretty_print=True, doctype=NETWORK_DTD)
    return changed


def _edits_for_corridor_layer(layer: dict[str, Any], links: list[NetworkLink], projected_crs: str) -> dict[str, dict[str, float]]:
    selected = select_links_near_corridor(
        links,
        list(layer.get("corridor_lonlat", [])),
        projected_crs,
        float(layer.get("buffer_m", 45.0)),
        float(layer.get("min_link_capacity", 0.0)),
    )
    edit = {
        "capacity_factor": float(layer.get("capacity_factor", 1.0)),
        "freespeed_factor": float(layer.get("freespeed_factor", 1.0)),
    }
    return {link.link_id: edit for link in selected}


def _run_generic_edit_layers(cfg: Any, scenario_dir: Path, config_template: Path, links: list[NetworkLink]) -> int:
    edit_cfg = cfg.interventions.get("generic_edit_layers", {})
    if not edit_cfg.get("enabled", False):
        return 0

    project_root = Path(cfg.project_root)
    total_changed = 0
    for layer in edit_cfg.get("layers", []):
        if not layer.get("enabled", True):
            continue
        name = str(layer["name"])
        input_network = scenario_dir / str(layer.get("input_network", "network.xml.gz"))
        output_network_name = str(layer.get("output_network", f"network_{name}.xml.gz"))
        output_network = scenario_dir / output_network_name
        output_directory = str(layer.get("output_directory", f"output_{name}"))
        layer_type = str(layer.get("type", "link_csv"))
        if layer_type == "link_csv":
            source = project_root / str(layer["source_csv"])
            edits = read_link_edit_csv(source)
        elif layer_type == "corridor":
            edits = _edits_for_corridor_layer(layer, links, cfg.crs)
        else:
            raise ValueError(f"Unsupported generic edit layer type for {name}: {layer_type}")
        changed = _apply_link_edits(input_network, output_network, edits)
        _write_run_config(config_template, scenario_dir / f"config_{name}.xml", output_network_name, output_directory)
        total_changed += changed
    return total_changed


def _format_float(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _write_pothole_links_csv(
    path: Path,
    links: list[NetworkLink],
    potholes_by_link: Counter[str],
    speeds: dict[str, tuple[float, float]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    links_by_id = {link.link_id: link for link in links}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["link_id", "n_potholes", "length_m", "base_freespeed", "degraded_freespeed"],
        )
        writer.writeheader()
        for link_id in sorted(potholes_by_link):
            base, degraded = speeds.get(link_id, (links_by_id[link_id].freespeed or 0.0, links_by_id[link_id].freespeed or 0.0))
            writer.writerow(
                {
                    "link_id": link_id,
                    "n_potholes": potholes_by_link[link_id],
                    "length_m": _format_float(links_by_id[link_id].length_m),
                    "base_freespeed": _format_float(base),
                    "degraded_freespeed": _format_float(degraded),
                }
            )


def _set_module_param(root: Any, module_name: str, param_name: str, value: str) -> None:
    module = root.find(f"./module[@name='{module_name}']")
    if module is None:
        module = etree.SubElement(root, "module", name=module_name)
    param = module.find(f"./param[@name='{param_name}']")
    if param is None:
        param = etree.SubElement(module, "param", name=param_name)
    param.set("value", value)


def _write_run_config(template_path: Path, output_path: Path, network_file: str, output_directory: str) -> None:
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(template_path), parser)
    root = tree.getroot()
    _set_module_param(root, "network", "inputNetworkFile", network_file)
    _set_module_param(root, "controler", "outputDirectory", output_directory)
    with output_path.open("wb") as handle:
        tree.write(handle, encoding="UTF-8", xml_declaration=True, pretty_print=True, doctype=CONFIG_DTD)


def run(cfg: Any) -> None:
    """Fetch 311 potholes, degrade affected links, and write scenario configs."""
    scenario_dir = cfg.scenario_dir
    network_path = scenario_dir / "network.xml.gz"
    degraded_network_path = scenario_dir / "network_potholes.xml.gz"
    bike_lane_network_path = scenario_dir / "network_bike_lane.xml.gz"
    config_template = scenario_dir / "config.xml"
    pothole_links_csv = cfg.data_interim / "pothole_links.csv"
    pothole_metadata_json = cfg.data_interim / "pothole_filter_metadata.json"
    bike_lane_links_csv = cfg.data_interim / "bike_lane_links.csv"
    boundary_path = cfg.boundary_path
    links_path = cfg.network_links_path

    pothole_cfg = cfg.interventions.get("pothole", {})
    speed_per = float(pothole_cfg.get("speed_penalty_per_pothole", 0.05))
    max_penalty = float(pothole_cfg.get("max_penalty", 0.50))
    recent_days = int(pothole_cfg["recent_days"]) if "recent_days" in pothole_cfg else None
    active_statuses = {str(value) for value in pothole_cfg.get("active_statuses", [])}
    as_of = _as_of_date(pothole_cfg.get("as_of_date"))

    bounds = _buffered_boundary_bounds_4326(
        boundary_path,
        cfg.crs,
        float(cfg.sources.get("osm", {}).get("network_buffer_m", cfg.boundary.buffer_m)),
    )
    raw_records = _fetch_pothole_records(cfg, bounds)
    pothole_type_records = [
        record
        for record in raw_records
        if "pothole" in str(record.get("type_of_service_request", "")).lower()
    ]
    potholes = _valid_potholes(
        raw_records,
        recent_days=recent_days,
        active_statuses=active_statuses,
        as_of=as_of,
    )
    points = _project_potholes(potholes, cfg.crs)
    links = _read_network_links(links_path)
    potholes_by_link = snap_potholes_to_links(points, links, SNAP_DISTANCE_M)

    speeds = _degrade_network(network_path, degraded_network_path, potholes_by_link, speed_per, max_penalty)
    _write_pothole_links_csv(pothole_links_csv, links, potholes_by_link, speeds)
    pothole_metadata_json.write_text(
        json.dumps(
            {
                "raw_records": len(raw_records),
                "pothole_type_records": len(pothole_type_records),
                "filtered_records": len(potholes),
                "snapped_records": sum(potholes_by_link.values()),
                "affected_links": len(potholes_by_link),
                "recent_days": recent_days,
                "active_statuses": sorted(active_statuses),
                "as_of_date": as_of.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_run_config(config_template, scenario_dir / "config_baseline.xml", "network_potholes.xml.gz", "output_baseline")
    _write_run_config(config_template, scenario_dir / "config_fixed.xml", "network.xml.gz", "output_fixed")

    bike_lane_cfg = cfg.interventions.get("bike_lane", {})
    bike_lane_changed = 0
    if bool(bike_lane_cfg.get("enabled", False)):
        selected_links = select_links_near_corridor(
            links,
            list(bike_lane_cfg.get("corridor_lonlat", [])),
            cfg.crs,
            float(bike_lane_cfg.get("buffer_m", 45.0)),
            float(bike_lane_cfg.get("min_link_capacity", 600.0)),
        )
        capacity_factor = float(bike_lane_cfg.get("car_capacity_factor", 0.75))
        freespeed_factor = float(bike_lane_cfg.get("car_freespeed_factor", 1.0))
        bike_lane_changed = _apply_bike_lane_network(
            network_path,
            bike_lane_network_path,
            {link.link_id for link in selected_links},
            capacity_factor,
            freespeed_factor,
        )
        _write_bike_lane_links_csv(bike_lane_links_csv, selected_links, capacity_factor, freespeed_factor)
        _write_run_config(config_template, scenario_dir / "config_bike_lane.xml", "network_bike_lane.xml.gz", "output_bike_lane")
    generic_changed = _run_generic_edit_layers(cfg, scenario_dir, config_template, links)

    penalties = [speed_penalty(count, speed_per, max_penalty) for count in potholes_by_link.values()]
    median_penalty = median(penalties) if penalties else 0.0
    max_observed_penalty = max(penalties) if penalties else 0.0
    print(
        "s5 pothole intervention: "
        f"fetched={len(raw_records)}, pothole_records={len(pothole_type_records)}, filtered={len(potholes)}, "
        f"snapped={sum(potholes_by_link.values())}, "
        f"affected_links={len(potholes_by_link)}, total_potholes_on_network={sum(potholes_by_link.values())}, "
        f"median_penalty={median_penalty:.3f}, max_penalty={max_observed_penalty:.3f}, "
        f"bike_lane_links={bike_lane_changed}, generic_edit_links={generic_changed}"
    )

