"""Stage s5: pothole intervention network generation."""

from __future__ import annotations

import csv
import gzip
from collections import Counter
from dataclasses import dataclass
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


@dataclass(frozen=True)
class NetworkLink:
    link_id: str
    geometry: Any
    length_m: float
    freespeed: float | None = None


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
        table, geom_col = _gpkg_geometry_columns(conn)
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
        required = {"link_id", geom_col}
        missing = required.difference(columns)
        if missing:
            raise ValueError(f"Missing required link columns in {path}: {sorted(missing)}")
        select_columns = ["link_id", _quote_identifier(geom_col)]
        has_length = "length" in columns
        has_freespeed = "freespeed" in columns
        if has_length:
            select_columns.append("length")
        if has_freespeed:
            select_columns.append("freespeed")
        rows = conn.execute(
            f"SELECT {', '.join(select_columns)} FROM {_quote_identifier(table)} "
            f"WHERE {_quote_identifier(geom_col)} IS NOT NULL"
        ).fetchall()

    links: list[NetworkLink] = []
    for row in rows:
        link_id = str(row[0])
        geometry = wkb.loads(_gpkg_wkb(bytes(row[1])))
        length_m = float(row[2]) if has_length else float(geometry.length)
        freespeed = float(row[3]) if has_length and has_freespeed else (float(row[2]) if has_freespeed else None)
        if not geometry.is_empty:
            links.append(NetworkLink(link_id=link_id, geometry=geometry, length_m=length_m, freespeed=freespeed))
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


def _valid_potholes(records: list[dict[str, Any]]) -> list[PotholeRecord]:
    potholes: list[PotholeRecord] = []
    for record in records:
        request_type = str(record.get("type_of_service_request", ""))
        if request_type and "pothole" not in request_type.lower():
            continue
        try:
            lat = float(record["latitude"])
            lon = float(record["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        potholes.append(PotholeRecord(lat=lat, lon=lon))
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
    project_root = Path(cfg.project_root)
    scenario_dir = project_root / "scenarios" / "logan_square"
    network_path = scenario_dir / "network.xml.gz"
    degraded_network_path = scenario_dir / "network_potholes.xml.gz"
    config_template = scenario_dir / "config.xml"
    pothole_links_csv = cfg.data_interim / "pothole_links.csv"
    boundary_path = cfg.data_interim / "logan_square_boundary.gpkg"
    links_path = cfg.data_interim / "network_links.gpkg"

    pothole_cfg = cfg.interventions.get("pothole", {})
    speed_per = float(pothole_cfg.get("speed_penalty_per_pothole", 0.05))
    max_penalty = float(pothole_cfg.get("max_penalty", 0.50))

    bounds = _buffered_boundary_bounds_4326(
        boundary_path,
        cfg.crs,
        float(cfg.sources.get("osm", {}).get("network_buffer_m", cfg.boundary.buffer_m)),
    )
    raw_records = _fetch_pothole_records(cfg, bounds)
    potholes = _valid_potholes(raw_records)
    points = _project_potholes(potholes, cfg.crs)
    links = _read_network_links(links_path)
    potholes_by_link = snap_potholes_to_links(points, links, SNAP_DISTANCE_M)

    speeds = _degrade_network(network_path, degraded_network_path, potholes_by_link, speed_per, max_penalty)
    _write_pothole_links_csv(pothole_links_csv, links, potholes_by_link, speeds)
    _write_run_config(config_template, scenario_dir / "config_baseline.xml", "network_potholes.xml.gz", "output_baseline")
    _write_run_config(config_template, scenario_dir / "config_fixed.xml", "network.xml.gz", "output_fixed")

    penalties = [speed_penalty(count, speed_per, max_penalty) for count in potholes_by_link.values()]
    median_penalty = median(penalties) if penalties else 0.0
    max_observed_penalty = max(penalties) if penalties else 0.0
    print(
        "s5 pothole intervention: "
        f"fetched={len(raw_records)}, valid={len(potholes)}, snapped={sum(potholes_by_link.values())}, "
        f"affected_links={len(potholes_by_link)}, total_potholes_on_network={sum(potholes_by_link.values())}, "
        f"median_penalty={median_penalty:.3f}, max_penalty={max_observed_penalty:.3f}"
    )
