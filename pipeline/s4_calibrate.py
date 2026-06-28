"""Stage s4: count validation and global volume correction."""

from __future__ import annotations

import csv
import gzip
import json
import math
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from operator import index as operator_index
from pathlib import Path
from statistics import mean, median
from typing import Any
from xml.sax.saxutils import escape


DIRECTION_BEARINGS = {"NB": 0.0, "EB": 90.0, "SB": 180.0, "WB": 270.0}
SNAP_DISTANCE_M = 40.0
DIRECTION_TOLERANCE_DEG = 45.0


@dataclass(frozen=True)
class CountStation:
    lat: float
    lon: float
    direction: str
    roadname: str
    observed: float


@dataclass(frozen=True)
class MatchedCount:
    link_id: str
    roadname: str
    direction: str
    observed: float


@dataclass(frozen=True)
class RuntimeLink:
    link_id: str
    from_node: str
    to_node: str
    geometry: Any
    bearing: float


def bearing_from_coords(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Return compass bearing in degrees, where 0=N, 90=E, 180=S, 270=W."""
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    if dx == 0.0 and dy == 0.0:
        raise ValueError("Cannot compute bearing for a zero-length segment.")
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def bearing(geometry: Any) -> float:
    """Compute compass bearing from a LineString-like geometry's endpoints."""
    coords = list(geometry.coords)
    if len(coords) < 2:
        raise ValueError("Line geometry must have at least two coordinates.")
    return bearing_from_coords((coords[0][0], coords[0][1]), (coords[-1][0], coords[-1][1]))


def circular_difference_degrees(a: float, b: float) -> float:
    """Return the absolute smallest angular difference between two bearings."""
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def direction_matches(link_bearing: float, count_direction: str, tolerance: float = DIRECTION_TOLERANCE_DEG) -> bool:
    """Return whether a link bearing is within tolerance of a count direction."""
    target = DIRECTION_BEARINGS.get(count_direction.upper())
    if target is None:
        return False
    return circular_difference_degrees(link_bearing, target) <= tolerance


def geh(observed: float, simulated: float) -> float:
    """Compute GEH statistic for traffic count comparison."""
    observed = float(observed)
    simulated = float(simulated)
    if observed + simulated == 0.0:
        return 0.0
    return math.sqrt(2.0 * (simulated - observed) ** 2 / (observed + simulated))


def dedupe_count_records(records: list[dict[str, Any]]) -> list[CountStation]:
    """Deduplicate ADT records by rounded station coordinate and direction.

    Multiple snapshots for the same station are averaged. The first non-empty
    road name in each group is retained for output labels.
    """
    grouped: dict[tuple[float, float, str], dict[str, Any]] = {}
    for record in records:
        try:
            lat = float(record["midpointlat"])
            lon = float(record["midpointlon"])
            vehicle_count = float(str(record["vehiclecount"]).replace(",", ""))
        except (KeyError, TypeError, ValueError):
            continue

        direction = str(record.get("direction", "")).strip().upper()
        if direction not in DIRECTION_BEARINGS:
            continue

        key = (round(lat, 4), round(lon, 4), direction)
        bucket = grouped.setdefault(
            key,
            {
                "lat": lat,
                "lon": lon,
                "direction": direction,
                "roadname": str(record.get("roadname", "")).strip(),
                "values": [],
            },
        )
        if not bucket["roadname"]:
            bucket["roadname"] = str(record.get("roadname", "")).strip()
        bucket["values"].append(vehicle_count)

    stations: list[CountStation] = []
    for bucket in grouped.values():
        if bucket["values"]:
            stations.append(
                CountStation(
                    lat=float(bucket["lat"]),
                    lon=float(bucket["lon"]),
                    direction=str(bucket["direction"]),
                    roadname=str(bucket["roadname"]),
                    observed=mean(bucket["values"]),
                )
            )
    return stations


def select_directional_link(point: Any, links: list[Any], direction: str, max_distance_m: float = SNAP_DISTANCE_M) -> Any | None:
    """Pick the nearest link within distance whose bearing matches direction."""
    best_link = None
    best_distance = float("inf")
    for link in links:
        link_bearing = getattr(link, "bearing", None)
        if link_bearing is None:
            link_bearing = bearing(link.geometry)
        if not direction_matches(link_bearing, direction):
            continue
        distance = float(link.geometry.distance(point))
        if distance <= max_distance_m and distance < best_distance:
            best_link = link
            best_distance = distance
    return best_link


def _gpkg_wkb(blob: bytes) -> bytes:
    if len(blob) >= 8 and blob[:2] == b"GP":
        flags = blob[3]
        envelope_code = (flags >> 1) & 0b111
        envelope_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
        return blob[8 + envelope_sizes.get(envelope_code, 0) :]
    return blob


def _gpkg_geometry_columns(conn: sqlite3.Connection, table_name: str | None = None) -> tuple[str, str]:
    rows = conn.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchall()
    if not rows:
        raise ValueError("GeoPackage has no gpkg_geometry_columns entries.")
    if table_name is not None:
        for row_table, row_geom in rows:
            if row_table == table_name:
                return str(row_table), str(row_geom)
        raise ValueError(f"GeoPackage table {table_name!r} has no geometry column.")
    return str(rows[0][0]), str(rows[0][1])


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _xml_attr(value: Any) -> str:
    return escape(str(value), {"'": "&apos;", '"': "&quot;"})


def _read_gpkg_geometries(path: Path, table_name: str | None = None) -> list[Any]:
    from shapely import wkb

    with sqlite3.connect(path) as conn:
        table, geom_col = _gpkg_geometry_columns(conn, table_name)
        rows = conn.execute(
            f"SELECT {_quote_identifier(geom_col)} FROM {_quote_identifier(table)} WHERE {_quote_identifier(geom_col)} IS NOT NULL"
        ).fetchall()
    return [wkb.loads(_gpkg_wkb(bytes(row[0]))) for row in rows]


def _read_network_links(path: Path) -> list[RuntimeLink]:
    from shapely import wkb

    with sqlite3.connect(path) as conn:
        table, geom_col = _gpkg_geometry_columns(conn)
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
        required = {"link_id", "from_node", "to_node", geom_col}
        missing = required.difference(columns)
        if missing:
            raise ValueError(f"Missing required link columns in {path}: {sorted(missing)}")
        rows = conn.execute(
            "SELECT link_id, from_node, to_node, "
            f"{_quote_identifier(geom_col)} FROM {_quote_identifier(table)} "
            f"WHERE {_quote_identifier(geom_col)} IS NOT NULL"
        ).fetchall()

    links: list[RuntimeLink] = []
    for link_id, from_node, to_node, geom_blob in rows:
        geometry = wkb.loads(_gpkg_wkb(bytes(geom_blob)))
        if geometry.is_empty:
            continue
        links.append(
            RuntimeLink(
                link_id=str(link_id),
                from_node=str(from_node),
                to_node=str(to_node),
                geometry=geometry,
                bearing=bearing(geometry),
            )
        )
    return links


def _buffered_boundary_bounds_4326(boundary_path: Path, projected_crs: str, buffer_m: float) -> tuple[float, float, float, float]:
    from pyproj import Transformer
    from shapely.ops import transform, unary_union

    geometries = _read_gpkg_geometries(boundary_path)
    if not geometries:
        raise ValueError(f"No boundary geometries found in {boundary_path}")

    # The boundary gpkg is already in the projected CRS (EPSG:26971), so buffer
    # directly in meters and only transform the result to WGS84 for the bbox.
    boundary = unary_union(geometries)
    to_wgs84 = Transformer.from_crs(projected_crs, "EPSG:4326", always_xy=True).transform
    return transform(to_wgs84, boundary.buffer(buffer_m)).bounds


def _fetch_adt_records(cfg: Any, bounds: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    import requests

    min_lon, min_lat, max_lon, max_lat = bounds
    socrata = cfg.sources["socrata"]
    domain = socrata.get("domain", "data.cityofchicago.org")
    dataset_id = socrata["adt_counts"]["dataset_id"]
    endpoint = f"https://{domain}/resource/{dataset_id}.json"
    params = {
        "$limit": 50_000,
        "$select": "midpointlat,midpointlon,direction,vehiclecount,roadname",
        "$where": (
            f"midpointlat between {min_lat} and {max_lat} "
            f"and midpointlon between {min_lon} and {max_lon}"
        ),
    }
    response = requests.get(endpoint, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected Socrata response for {dataset_id}: expected a JSON list.")
    return payload


def _find_last_iteration_dir(output_dir: Path) -> tuple[int, Path]:
    iters_dir = output_dir / "ITERS"
    candidates: list[tuple[int, Path]] = []
    if iters_dir.exists():
        for child in iters_dir.iterdir():
            match = re.fullmatch(r"it\.(\d+)", child.name)
            if child.is_dir() and match:
                candidates.append((int(match.group(1)), child))
    if candidates:
        return max(candidates, key=lambda item: item[0])

    config_xml = output_dir / "config.xml"
    if config_xml.exists():
        root = ET.parse(config_xml).getroot()
        for param in root.findall(".//param"):
            if param.get("name") == "lastIteration" and param.get("value"):
                iteration = int(param.get("value", "0"))
                return iteration, iters_dir / f"it.{iteration}"

    raise FileNotFoundError(f"Could not find MATSim iteration outputs under {output_dir}")


def _load_simulated_volumes(output_dir: Path, sample_fraction: float) -> dict[str, float]:
    iteration, iteration_dir = _find_last_iteration_dir(output_dir)
    events_path = iteration_dir / f"{iteration}.events.xml.gz"
    if not events_path.exists():
        raise FileNotFoundError(f"Missing MATSim events file: {events_path}")
    if sample_fraction <= 0:
        raise ValueError("scenario.sample_fraction must be positive.")

    counts: Counter[str] = Counter()
    with gzip.open(events_path, "rb") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag == "event" and element.get("type") == "entered link":
                link_id = element.get("link")
                if link_id:
                    counts[str(link_id)] += 1
            element.clear()
    return {link_id: count / sample_fraction for link_id, count in counts.items()}


def _match_counts_to_links(counts: list[CountStation], links: list[RuntimeLink], projected_crs: str) -> list[MatchedCount]:
    from pyproj import Transformer
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    transformer = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
    geometries = [link.geometry for link in links]
    tree = STRtree(geometries)
    geometry_id_to_link = {id(geometry): link for geometry, link in zip(geometries, links)}

    matched_by_link: dict[str, MatchedCount] = {}
    for count in counts:
        x, y = transformer.transform(count.lon, count.lat)
        point = Point(x, y)
        query_result = tree.query(point.buffer(SNAP_DISTANCE_M))
        candidates: list[RuntimeLink] = []
        for item in query_result:
            try:
                link_index = operator_index(item)
            except TypeError:
                link_index = None
            if link_index is None:
                candidates.append(geometry_id_to_link[id(item)])
            else:
                candidates.append(links[link_index])
        link = select_directional_link(point, candidates, count.direction)
        if link is None:
            continue

        existing = matched_by_link.get(link.link_id)
        if existing is None or count.observed > existing.observed:
            matched_by_link[link.link_id] = MatchedCount(
                link_id=link.link_id,
                roadname=count.roadname,
                direction=count.direction,
                observed=count.observed,
            )
    return list(matched_by_link.values())


def _linear_fit(rows: list[dict[str, float]]) -> tuple[float | None, float | None]:
    if len(rows) < 2:
        return None, None
    xs = [row["observed"] for row in rows]
    ys = [row["simulated"] for row in rows]
    mean_x = mean(xs)
    mean_y = mean(ys)
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0.0:
        return None, None

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / ss_xx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0.0:
        return slope, None
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    return slope, 1.0 - ss_res / ss_tot


def _factor(observed_sum: float, simulated_sum: float) -> float | None:
    return observed_sum / simulated_sum if simulated_sum else None


def _build_validation_rows(matches: list[MatchedCount], simulated: dict[str, float], global_factor: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in matches:
        sim = float(simulated.get(match.link_id, 0.0))
        corrected = sim * global_factor
        rows.append(
            {
                "link_id": match.link_id,
                "roadname": match.roadname,
                "direction": match.direction,
                "observed": float(match.observed),
                "simulated": sim,
                "sim_corrected": corrected,
                "geh_raw": geh(match.observed, sim),
                "geh_corrected": geh(match.observed, corrected),
                "ratio": sim / match.observed if match.observed else None,
            }
        )
    return rows


def _summary_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    raw_gehs = [float(row["geh_raw"]) for row in rows]
    corrected_gehs = [float(row["geh_corrected"]) for row in rows]
    ratios = [float(row["ratio"]) for row in rows if row["ratio"] is not None]
    slope, r2 = _linear_fit(rows)
    return {
        "n_matched": n,
        "pct_geh_lt_5": 100.0 * sum(value < 5.0 for value in raw_gehs) / n if n else 0.0,
        "pct_geh_lt_10": 100.0 * sum(value < 10.0 for value in raw_gehs) / n if n else 0.0,
        "pct_geh_corrected_lt_5": 100.0 * sum(value < 5.0 for value in corrected_gehs) / n if n else 0.0,
        "pct_geh_corrected_lt_10": 100.0 * sum(value < 10.0 for value in corrected_gehs) / n if n else 0.0,
        "median_geh": median(raw_gehs) if raw_gehs else None,
        "mean_geh": mean(raw_gehs) if raw_gehs else None,
        "median_sim_obs_ratio": median(ratios) if ratios else None,
        "linear_fit_slope": slope,
        "linear_fit_r2": r2,
    }


def _per_direction_factors(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["direction"])].append(row)
    return {
        direction: _factor(sum(float(row["observed"]) for row in group), sum(float(row["simulated"]) for row in group))
        for direction, group in sorted(grouped.items())
    }


def _write_validation_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "link_id",
        "roadname",
        "direction",
        "observed",
        "simulated",
        "sim_corrected",
        "geh_raw",
        "geh_corrected",
        "ratio",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_counts_xml(path: Path, matches: list[MatchedCount], year: int | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<!DOCTYPE counts SYSTEM 'http://www.matsim.org/files/dtd/counts_v1.dtd'>",
        "<!-- Daily ADT is distributed evenly across 24 hours as a flat placeholder profile. -->",
        f"<counts name='Chicago ADT validation counts' year='{_xml_attr(year)}' layer='0'>",
    ]
    for match in sorted(matches, key=lambda item: item.link_id):
        cs_id = f"{match.roadname}-{match.direction}".strip("-") or match.direction
        lines.append(f"  <count loc_id='{_xml_attr(match.link_id)}' cs_id='{_xml_attr(cs_id)}'>")
        hourly = match.observed / 24.0
        for hour in range(1, 25):
            lines.append(f"    <volume h='{hour}' val='{hourly:.6f}'/>")
        lines.append("  </count>")
    lines.append("</counts>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(cfg: Any) -> None:
    """Validate simulated link volumes against Chicago ADT counts and write correction factors."""
    project_root = Path(cfg.project_root)
    boundary_path = cfg.data_interim / "logan_square_boundary.gpkg"
    network_path = cfg.data_interim / "network_links.gpkg"
    output_dir = project_root / "scenarios" / "logan_square" / "output"

    bounds = _buffered_boundary_bounds_4326(
        boundary_path,
        cfg.crs,
        float(cfg.sources.get("osm", {}).get("network_buffer_m", cfg.boundary.buffer_m)),
    )
    records = _fetch_adt_records(cfg, bounds)
    counts = dedupe_count_records(records)
    links = _read_network_links(network_path)
    matches = _match_counts_to_links(counts, links, cfg.crs)
    simulated = _load_simulated_volumes(output_dir, cfg.scenario.sample_fraction)

    observed_sum = sum(match.observed for match in matches)
    simulated_sum = sum(simulated.get(match.link_id, 0.0) for match in matches)
    global_factor = _factor(observed_sum, simulated_sum) or 1.0
    rows = _build_validation_rows(matches, simulated, global_factor)
    metrics = _summary_metrics(rows)

    validation_csv = cfg.data_processed / "calibration_validation.csv"
    factors_json = cfg.data_processed / "correction_factors.json"
    counts_xml = project_root / "scenarios" / "logan_square" / "counts.xml"

    _write_validation_csv(validation_csv, rows)
    factors_json.parent.mkdir(parents=True, exist_ok=True)
    factors = {
        "global_factor": global_factor,
        "per_direction": _per_direction_factors(rows),
        "per_road_class": {},
        "n_matched": len(rows),
        "summary_metrics": metrics,
    }
    factors_json.write_text(json.dumps(factors, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    year = cfg.sources.get("socrata", {}).get("adt_counts", {}).get("year", cfg.sources.get("osm", {}).get("year", ""))
    _write_counts_xml(counts_xml, matches, year)

    print(
        "s4 calibration validation: "
        f"matched={metrics['n_matched']}, "
        f"GEH<5 raw={metrics['pct_geh_lt_5']:.1f}% corrected={metrics['pct_geh_corrected_lt_5']:.1f}%, "
        f"GEH<10 raw={metrics['pct_geh_lt_10']:.1f}% corrected={metrics['pct_geh_corrected_lt_10']:.1f}%, "
        f"global_factor={global_factor:.4f}"
    )
