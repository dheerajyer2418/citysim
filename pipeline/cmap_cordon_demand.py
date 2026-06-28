"""Stage s2d: CMAP cordon demand synthesis through boundary gateways."""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from io import TextIOWrapper
from itertools import chain
from pathlib import Path

from pipeline.cmap_demand import (
    PLANS_OUTPUT,
    RNG_SEED,
    ROSTER_FIELDS,
    ROSTER_ZIP,
    _parse_positive_trips,
    _read_filtered_cache,
    activity_types_for_trip,
    generate_person_plans,
    sample_departure_seconds,
)
from pipeline.crosswalk import (
    LinkRecord,
    build_taz_link_crosswalk,
    detect_taz_id_column,
    load_links_from_gpkg,
    sample_activity_coord,
)
from pipeline.plans_io import PersonPlan, stochastic_count, write_population


CORDON_CACHE = "cmap_cordon_trips.csv"
GATEWAY_CACHE = "zone_gateways.csv"
INTERNAL_CACHE = "cmap_internal_trips.csv"
GATEWAY_FIELDS = ("external_zone", "node_id", "x", "y")


@dataclass(frozen=True)
class NetworkNode:
    node_id: str
    x: float
    y: float


@dataclass(frozen=True)
class CordonGenerationSummary:
    internal_agents: int = 0
    cordon_in: int = 0
    cordon_out: int = 0

    @property
    def total_agents(self) -> int:
        return self.internal_agents + self.cordon_in + self.cordon_out


def is_one_end_internal_auto_trip(row: dict[str, str], taz_ids: set[str], auto_modes: set[str]) -> bool:
    o_internal = str(row.get("o_zone", "")) in taz_ids
    d_internal = str(row.get("d_zone", "")) in taz_ids
    return str(row.get("mode", "")) in auto_modes and o_internal != d_internal


def iter_cordon_auto_rows(
    rows: Iterable[dict[str, str]],
    taz_ids: set[str],
    auto_modes: set[str],
) -> Iterator[dict[str, str]]:
    for row in rows:
        if is_one_end_internal_auto_trip(row, taz_ids, auto_modes):
            yield {field: str(row.get(field, "")) for field in ROSTER_FIELDS}


def external_zone_for_cordon_row(row: dict[str, str], taz_ids: set[str]) -> str:
    o_zone = str(row["o_zone"])
    d_zone = str(row["d_zone"])
    return d_zone if o_zone in taz_ids else o_zone


def nearest_node(nodes: Iterable[NetworkNode | tuple[str, float, float]], x: float, y: float) -> NetworkNode:
    node_list = [
        node if isinstance(node, NetworkNode) else NetworkNode(str(node[0]), float(node[1]), float(node[2]))
        for node in nodes
    ]
    if not node_list:
        raise ValueError("Cannot find a nearest node from an empty node table.")
    target_x = float(x)
    target_y = float(y)
    return min(node_list, key=lambda node: (node.x - target_x) ** 2 + (node.y - target_y) ** 2)


def extract_network_nodes(links: Iterable[LinkRecord]) -> list[NetworkNode]:
    nodes: dict[str, NetworkNode] = {}
    for link in links:
        coords = list(getattr(link.geometry, "coords", []))
        if len(coords) < 2:
            continue
        from_x, from_y = coords[0][:2]
        to_x, to_y = coords[-1][:2]
        nodes.setdefault(str(link.from_node), NetworkNode(str(link.from_node), float(from_x), float(from_y)))
        nodes.setdefault(str(link.to_node), NetworkNode(str(link.to_node), float(to_x), float(to_y)))
    return list(nodes.values())


def _nearest_nodes_by_strtree(nodes: list[NetworkNode], points_by_zone: dict[str, object]) -> dict[str, NetworkNode]:
    from operator import index as operator_index

    from shapely.geometry import Point
    from shapely.strtree import STRtree

    node_points = [Point(node.x, node.y) for node in nodes]
    if not node_points:
        raise ValueError("Cannot build gateways without network nodes.")
    tree = STRtree(node_points)
    point_id_to_node = {id(point): node for point, node in zip(node_points, nodes)}
    gateways: dict[str, NetworkNode] = {}
    for zone, point in points_by_zone.items():
        nearest = tree.nearest(point)
        try:
            node_index = operator_index(nearest)
        except TypeError:
            node_index = None
        if node_index is not None:
            gateways[str(zone)] = nodes[node_index]
        else:
            gateways[str(zone)] = point_id_to_node[id(nearest)]
    return gateways


def _open_roster_reader(zip_path: Path, member: str) -> Iterator[dict[str, str]]:
    import zipfile

    import zipfile_deflate64  # noqa: F401  # registers DEFLATE64 support with zipfile

    with zipfile.ZipFile(zip_path) as archive:
        with archive.open(member) as binary:
            text = TextIOWrapper(binary, encoding="utf-8", newline="")
            yield from csv.DictReader(text)


def _write_cordon_cache(
    source_rows: Iterable[dict[str, str]],
    cache_path: Path,
    taz_ids: set[str],
    auto_modes: set[str],
) -> int:
    cordon_trips = 0
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROSTER_FIELDS)
        writer.writeheader()
        for row in iter_cordon_auto_rows(source_rows, taz_ids, auto_modes):
            trips = _parse_positive_trips(row.get("trips", "0"))
            if trips <= 0:
                continue
            writer.writerow(row)
            cordon_trips += trips
    temp_path.replace(cache_path)
    return cordon_trips


def _read_gateway_cache(cache_path: Path) -> dict[str, NetworkNode]:
    gateways: dict[str, NetworkNode] = {}
    with cache_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            zone = str(row["external_zone"])
            gateways[zone] = NetworkNode(str(row["node_id"]), float(row["x"]), float(row["y"]))
    return gateways


def _write_gateway_cache(cache_path: Path, gateways: dict[str, NetworkNode]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GATEWAY_FIELDS)
        writer.writeheader()
        for zone in sorted(gateways, key=lambda value: (len(value), value)):
            node = gateways[zone]
            writer.writerow(
                {
                    "external_zone": zone,
                    "node_id": node.node_id,
                    "x": f"{node.x:.3f}",
                    "y": f"{node.y:.3f}",
                }
            )
    temp_path.replace(cache_path)


def _taz_ids_from_table(taz_gdf) -> set[str]:
    column = "taz_id" if "taz_id" in getattr(taz_gdf, "columns", []) else detect_taz_id_column(taz_gdf)
    return {str(value) for value in taz_gdf[column].dropna()}


def _external_zones_from_rows(rows: Iterable[dict[str, str]], taz_ids: set[str]) -> set[str]:
    return {external_zone_for_cordon_row(row, taz_ids) for row in rows}


def _load_or_build_gateways(
    cache_path: Path,
    zones_path: Path,
    external_zones: set[str],
    nodes: list[NetworkNode],
    crs: str,
) -> dict[str, NetworkNode]:
    if cache_path.exists():
        cached = _read_gateway_cache(cache_path)
        if external_zones.issubset(cached):
            return {zone: cached[zone] for zone in external_zones}

    import geopandas as gpd

    zones_gdf = gpd.read_file(zones_path).to_crs(crs)
    if "zone17" not in zones_gdf.columns:
        raise ValueError(f"Missing required zone17 column in {zones_path}")
    needed = zones_gdf[zones_gdf["zone17"].astype(str).isin(external_zones)]
    found_zones = {str(value) for value in needed["zone17"]}
    missing_zones = sorted(external_zones - found_zones)
    if missing_zones:
        raise ValueError(f"Missing external TAZ polygons for zones: {missing_zones[:10]}")

    points_by_zone = {str(row["zone17"]): row.geometry.centroid for _, row in needed.iterrows()}
    gateways = _nearest_nodes_by_strtree(nodes, points_by_zone)
    _write_gateway_cache(cache_path, gateways)
    return gateways


def generate_cordon_person_plans(
    rows: Iterable[dict[str, str]],
    taz_ids: set[str],
    gateways: dict[str, NetworkNode],
    sample_fraction: float,
    tod_windows: dict[str, list[int]],
    rng,
    gateway_activity_type: str = "gateway",
    coord_sampler: Callable[[str], tuple[float, float]] = sample_activity_coord,
) -> Iterator[tuple[PersonPlan, str]]:
    person_number = 0
    for row in rows:
        trips = _parse_positive_trips(row.get("trips", "0"))
        if trips <= 0:
            continue
        count = stochastic_count(trips * sample_fraction, rng)
        for _ in range(count):
            departure = sample_departure_seconds(str(row["timeperiod"]), tod_windows, rng)
            origin_type, dest_type = activity_types_for_trip(
                str(row["purpose"]),
                str(row["o_zone"]),
                str(row["d_zone"]),
                str(row.get("a_zone", "")),
            )
            o_zone = str(row["o_zone"])
            d_zone = str(row["d_zone"])
            if o_zone in taz_ids:
                internal_x, internal_y = coord_sampler(o_zone)
                gateway = gateways[d_zone]
                direction = "out"
                activities = [
                    (origin_type, internal_x, internal_y, departure),
                    (gateway_activity_type, gateway.x, gateway.y, None),
                ]
            else:
                gateway = gateways[o_zone]
                internal_x, internal_y = coord_sampler(d_zone)
                direction = "in"
                activities = [
                    (gateway_activity_type, gateway.x, gateway.y, departure),
                    (dest_type, internal_x, internal_y, None),
                ]
            yield (f"cordon_{person_number:08d}", activities), direction
            person_number += 1


def run(cfg) -> None:
    """INPUT: CMAP roster/internal cache, TAZ polygons, and S1 links. OUTPUT: final plans.xml.gz."""
    import geopandas as gpd
    import numpy as np

    taz_path = cfg.data_interim / "logan_square_taz.gpkg"
    links_path = cfg.data_interim / "network_links.gpkg"
    zones_path = cfg.data_raw / "cmap_taz_zones17.geojson"
    internal_cache_path = cfg.data_interim / INTERNAL_CACHE
    for path in (taz_path, links_path, zones_path, internal_cache_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing required artifact: {path}")

    taz_gdf = gpd.read_file(taz_path).to_crs(cfg.crs)
    links = load_links_from_gpkg(links_path)
    build_taz_link_crosswalk(taz_gdf, links)
    taz_ids = _taz_ids_from_table(taz_gdf)

    roster_cfg = cfg.sources["cmap"]["roster"]
    cordon_cfg = cfg.sources["cmap"].get("cordon", {})
    member = str(roster_cfg["member"])
    auto_modes = {str(mode) for mode in roster_cfg.get("auto_modes", [1, 2, 3])}
    tod_windows = roster_cfg["tod_windows"]
    gateway_activity_type = str(cordon_cfg.get("gateway_activity_type", "gateway"))

    zip_path = cfg.data_raw / ROSTER_ZIP
    cordon_cache_path = cfg.data_interim / CORDON_CACHE
    gateway_cache_path = cfg.data_interim / GATEWAY_CACHE
    plans_path = cfg.project_root / "scenarios" / "logan_square" / PLANS_OUTPUT

    if not cordon_cache_path.exists():
        if not zip_path.exists():
            raise FileNotFoundError(f"Missing CMAP roster zip: {zip_path}")
        _write_cordon_cache(_open_roster_reader(zip_path, member), cordon_cache_path, taz_ids, auto_modes)

    external_zones = _external_zones_from_rows(_read_filtered_cache(cordon_cache_path), taz_ids)
    nodes = extract_network_nodes(links)
    gateways = _load_or_build_gateways(gateway_cache_path, zones_path, external_zones, nodes, cfg.crs)

    rng = np.random.default_rng(RNG_SEED)
    internal_plans = generate_person_plans(
        _read_filtered_cache(internal_cache_path),
        cfg.scenario.sample_fraction,
        tod_windows,
        rng,
    )
    cordon_plans = generate_cordon_person_plans(
        _read_filtered_cache(cordon_cache_path),
        taz_ids,
        gateways,
        cfg.scenario.sample_fraction,
        tod_windows,
        rng,
        gateway_activity_type=gateway_activity_type,
    )
    summary = CordonGenerationSummary()

    def counted_internal() -> Iterator[PersonPlan]:
        nonlocal summary
        for plan in internal_plans:
            summary = CordonGenerationSummary(
                internal_agents=summary.internal_agents + 1,
                cordon_in=summary.cordon_in,
                cordon_out=summary.cordon_out,
            )
            yield plan

    def counted_cordon() -> Iterator[PersonPlan]:
        nonlocal summary
        for plan, direction in cordon_plans:
            if direction == "in":
                summary = CordonGenerationSummary(
                    internal_agents=summary.internal_agents,
                    cordon_in=summary.cordon_in + 1,
                    cordon_out=summary.cordon_out,
                )
            else:
                summary = CordonGenerationSummary(
                    internal_agents=summary.internal_agents,
                    cordon_in=summary.cordon_in,
                    cordon_out=summary.cordon_out + 1,
                )
            yield plan

    write_population(chain(counted_internal(), counted_cordon()), plans_path)

    print(
        "s2d complete: "
        f"internal_agents={summary.internal_agents}; "
        f"cordon_in={summary.cordon_in}; "
        f"cordon_out={summary.cordon_out}; "
        f"total_agents={summary.total_agents}; "
        f"n_gateways={len(gateways)}; "
        f"distinct_external_zones={len(external_zones)}"
    )
