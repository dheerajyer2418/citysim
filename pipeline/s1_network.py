"""Stage s1: OSM-to-MATSim road network preparation."""

from __future__ import annotations

import gzip
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import etree

from pipeline.download import download_to_raw


# MATSim-style OSM defaults. Capacity is per lane in vehicles/hour.
HIGHWAY_DEFAULTS: dict[str, dict[str, float]] = {
    "motorway": {"freespeed": 29.06, "capacity": 2000.0, "lanes": 2.0},
    "motorway_link": {"freespeed": 22.35, "capacity": 1500.0, "lanes": 1.0},
    "trunk": {"freespeed": 22.35, "capacity": 1800.0, "lanes": 2.0},
    "trunk_link": {"freespeed": 17.88, "capacity": 1500.0, "lanes": 1.0},
    "primary": {"freespeed": 17.88, "capacity": 1500.0, "lanes": 1.0},
    "primary_link": {"freespeed": 13.41, "capacity": 1200.0, "lanes": 1.0},
    "secondary": {"freespeed": 13.41, "capacity": 1200.0, "lanes": 1.0},
    "secondary_link": {"freespeed": 11.18, "capacity": 1000.0, "lanes": 1.0},
    "tertiary": {"freespeed": 11.18, "capacity": 1000.0, "lanes": 1.0},
    "tertiary_link": {"freespeed": 8.94, "capacity": 900.0, "lanes": 1.0},
    "residential": {"freespeed": 8.94, "capacity": 600.0, "lanes": 1.0},
    "living_street": {"freespeed": 4.47, "capacity": 300.0, "lanes": 1.0},
    "unclassified": {"freespeed": 8.94, "capacity": 600.0, "lanes": 1.0},
    "service": {"freespeed": 6.71, "capacity": 300.0, "lanes": 1.0},
}
FALLBACK_HIGHWAY = "residential"
NETWORK_OUTPUT = "network.xml.gz"
LINKS_OUTPUT = "network_links.gpkg"


@dataclass(frozen=True)
class MatsimNode:
    node_id: str
    x: float
    y: float


@dataclass(frozen=True)
class MatsimLink:
    link_id: str
    from_node: str
    to_node: str
    length: float
    freespeed: float
    capacity: float
    permlanes: float
    modes: str
    geometry: Any
    is_connector: bool = False


def _first_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return next(iter(value), None)
    return value


def normalize_highway(value: Any) -> str:
    highway = str(_first_value(value) or FALLBACK_HIGHWAY).strip()
    return highway if highway in HIGHWAY_DEFAULTS else FALLBACK_HIGHWAY


def parse_lanes(value: Any, highway: str) -> float:
    default_lanes = HIGHWAY_DEFAULTS[normalize_highway(highway)]["lanes"]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default_lanes
    match = re.search(r"\d+(?:\.\d+)?", str(_first_value(value)))
    if not match:
        return default_lanes
    return max(float(match.group(0)), 1.0)


def parse_maxspeed_mps(value: Any, highway: str) -> float:
    """Parse OSM maxspeed into m/s, falling back to highway defaults."""
    default_speed = HIGHWAY_DEFAULTS[normalize_highway(highway)]["freespeed"]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default_speed

    text = str(_first_value(value)).strip().lower()
    if not text or text in {"none", "signals", "walk", "nan"}:
        return default_speed

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return default_speed

    speed = float(match.group(0))
    if "mph" in text:
        return speed * 0.44704
    return speed / 3.6


def oneway_direction(value: Any) -> str:
    """Return forward, reverse, or both for an OSM oneway value."""
    if isinstance(value, bool):
        return "forward" if value else "both"
    text = str(_first_value(value)).strip().lower()
    if text == "-1":
        return "reverse"
    if text in {"yes", "true", "1"}:
        return "forward"
    return "both"


def is_oneway(value: Any) -> bool:
    return oneway_direction(value) != "both"


def _reverse_geometry(geometry: Any) -> Any:
    from shapely.geometry import LineString, MultiLineString

    if isinstance(geometry, LineString):
        return LineString(list(geometry.coords)[::-1])
    if isinstance(geometry, MultiLineString):
        return MultiLineString([LineString(list(line.coords)[::-1]) for line in reversed(geometry.geoms)])
    return geometry


def _format_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _link_coords(link: MatsimLink, nodes_by_id: dict[str, MatsimNode]) -> list[tuple[float, float]]:
    geometry = link.geometry
    if geometry is not None and not getattr(geometry, "is_empty", False):
        if hasattr(geometry, "coords"):
            return [(float(x), float(y)) for x, y, *_ in geometry.coords]
        if hasattr(geometry, "geoms"):
            coords: list[tuple[float, float]] = []
            for line in geometry.geoms:
                line_coords = [(float(x), float(y)) for x, y, *_ in line.coords]
                if coords and line_coords and coords[-1] == line_coords[0]:
                    coords.extend(line_coords[1:])
                else:
                    coords.extend(line_coords)
            if coords:
                return coords

    from_node = nodes_by_id[link.from_node]
    to_node = nodes_by_id[link.to_node]
    return [(from_node.x, from_node.y), (to_node.x, to_node.y)]


def _merge_geometry(
    in_link: MatsimLink,
    out_link: MatsimLink,
    nodes_by_id: dict[str, MatsimNode],
) -> Any:
    from shapely.geometry import LineString

    coords = _link_coords(in_link, nodes_by_id)
    out_coords = _link_coords(out_link, nodes_by_id)
    if coords and out_coords and coords[-1] == out_coords[0]:
        coords.extend(out_coords[1:])
    else:
        coords.extend(out_coords)

    deduped: list[tuple[float, float]] = []
    for coord in coords:
        if not deduped or deduped[-1] != coord:
            deduped.append(coord)

    if len(deduped) < 2:
        from_node = nodes_by_id[in_link.from_node]
        to_node = nodes_by_id[out_link.to_node]
        deduped = [(from_node.x, from_node.y), (to_node.x, to_node.y)]
    return LineString(deduped)


def _links_compatible(in_link: MatsimLink, out_link: MatsimLink) -> bool:
    return in_link.modes == out_link.modes and in_link.permlanes == out_link.permlanes


def simplify_network(nodes: list[MatsimNode], links: list[MatsimLink]) -> tuple[list[MatsimNode], list[MatsimLink]]:
    """Merge compatible pass-through road chains into longer MATSim links."""
    nodes_by_id = {node.node_id: node for node in nodes}
    current_links = [link for link in links if link.length > 0 and link.from_node != link.to_node]

    while True:
        incoming: dict[str, list[MatsimLink]] = {}
        outgoing: dict[str, list[MatsimLink]] = {}
        for link in current_links:
            incoming.setdefault(link.to_node, []).append(link)
            outgoing.setdefault(link.from_node, []).append(link)

        merge_pairs: list[tuple[str, MatsimLink, MatsimLink]] = []
        for node_id in sorted(nodes_by_id):
            node_incoming = incoming.get(node_id, [])
            node_outgoing = outgoing.get(node_id, [])
            if not node_incoming or not node_outgoing:
                continue

            for in_link in sorted(node_incoming, key=lambda item: item.link_id):
                compatible_outgoing = [
                    out_link
                    for out_link in node_outgoing
                    if in_link.from_node != out_link.to_node and _links_compatible(in_link, out_link)
                ]
                if len(compatible_outgoing) != 1:
                    continue
                out_link = compatible_outgoing[0]
                compatible_incoming = [
                    candidate
                    for candidate in node_incoming
                    if candidate.from_node != out_link.to_node and _links_compatible(candidate, out_link)
                ]
                if compatible_incoming == [in_link]:
                    merge_pairs.append((node_id, in_link, out_link))

        used_link_ids: set[str] = set()
        selected_pairs: list[tuple[str, MatsimLink, MatsimLink]] = []
        for node_id, in_link, out_link in merge_pairs:
            if in_link.link_id in used_link_ids or out_link.link_id in used_link_ids:
                continue
            used_link_ids.update({in_link.link_id, out_link.link_id})
            selected_pairs.append((node_id, in_link, out_link))

        if not selected_pairs:
            break

        consumed = {link_id for _, in_link, out_link in selected_pairs for link_id in (in_link.link_id, out_link.link_id)}
        next_links = [link for link in current_links if link.link_id not in consumed]
        existing_link_ids = {link.link_id for link in next_links}
        for _, in_link, out_link in selected_pairs:
            total_length = in_link.length + out_link.length
            travel_time = 0.0
            if in_link.freespeed > 0:
                travel_time += in_link.length / in_link.freespeed
            if out_link.freespeed > 0:
                travel_time += out_link.length / out_link.freespeed
            new_freespeed = total_length / travel_time if travel_time > 0 else min(in_link.freespeed, out_link.freespeed)
            new_id = _unique_link_id(f"{in_link.link_id}-{out_link.link_id}", existing_link_ids)
            next_links.append(
                MatsimLink(
                    link_id=new_id,
                    from_node=in_link.from_node,
                    to_node=out_link.to_node,
                    length=total_length,
                    freespeed=new_freespeed,
                    capacity=min(in_link.capacity, out_link.capacity),
                    permlanes=in_link.permlanes,
                    modes=in_link.modes,
                    geometry=_merge_geometry(in_link, out_link, nodes_by_id),
                    is_connector=in_link.is_connector or out_link.is_connector,
                )
            )

        current_links = [link for link in next_links if link.length > 0 and link.from_node != link.to_node]

    used_nodes = {node_id for link in current_links for node_id in (link.from_node, link.to_node)}
    return [node for node in nodes if node.node_id in used_nodes], current_links


def _median_link_length(links: list[MatsimLink]) -> float:
    if not links:
        return 0.0
    return float(statistics.median(link.length for link in links))


def write_matsim_network_xml(nodes: list[MatsimNode], links: list[MatsimLink], output_path: str | Path) -> Path:
    """Write a gzipped MATSim network_v2 XML file."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    root = etree.Element("network")
    nodes_el = etree.SubElement(root, "nodes")
    for node in sorted(nodes, key=lambda item: item.node_id):
        etree.SubElement(
            nodes_el,
            "node",
            id=node.node_id,
            x=_format_float(node.x),
            y=_format_float(node.y),
        )

    links_el = etree.SubElement(root, "links")
    for link in sorted(links, key=lambda item: item.link_id):
        etree.SubElement(
            links_el,
            "link",
            id=link.link_id,
            **{
                "from": link.from_node,
                "to": link.to_node,
                "length": _format_float(max(link.length, 1.0)),
                "freespeed": _format_float(link.freespeed),
                "capacity": _format_float(link.capacity),
                "permlanes": _format_float(link.permlanes),
                "modes": link.modes,
            },
        )

    tree = etree.ElementTree(root)
    with gzip.open(output, "wb") as handle:
        tree.write(
            handle,
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
            doctype='<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">',
        )
    return output


def write_links_gpkg(links: list[MatsimLink], output_path: str | Path, crs: str) -> Path:
    import geopandas as gpd

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    links_gdf = gpd.GeoDataFrame(
        [
            {
                "link_id": link.link_id,
                "from_node": link.from_node,
                "to_node": link.to_node,
                "length": link.length,
                "freespeed": link.freespeed,
                "capacity": link.capacity,
                "permlanes": link.permlanes,
                "modes": link.modes,
                "is_connector": link.is_connector,
                "geometry": link.geometry,
            }
            for link in links
        ],
        crs=crs,
    )
    links_gdf.to_file(output, driver="GPKG")
    return output


def _osm_download_path(cfg) -> Path:
    return cfg.data_raw / Path(cfg.sources["osm"]["url"]).name


def _buffered_boundary_wgs84(cfg):
    import geopandas as gpd

    boundary_path = cfg.data_interim / "logan_square_boundary.gpkg"
    if not boundary_path.exists():
        raise FileNotFoundError(f"Missing S0 boundary output: {boundary_path}")

    boundary = gpd.read_file(boundary_path).to_crs(cfg.crs)
    buffer_m = float(cfg.sources["osm"].get("network_buffer_m", cfg.boundary.buffer_m))
    buffered = boundary.geometry.union_all().buffer(buffer_m)
    buffered_gdf = gpd.GeoDataFrame({"geometry": [buffered]}, crs=cfg.crs).to_crs("EPSG:4326")
    return buffered_gdf.geometry.iloc[0]


def _node_id_column(nodes_gdf) -> str | None:
    for candidate in ("id", "osmid", "osm_id", "node_id"):
        if candidate in nodes_gdf.columns:
            return candidate
    return None


def _edge_node_value(row, candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in row and row[candidate] is not None:
            return str(row[candidate])
    raise ValueError(f"Could not find edge node id in columns: {candidates}")


def _edge_base_id(row, index: Any) -> str:
    for candidate in ("id", "osm_id", "osmid"):
        if candidate in row and row[candidate] is not None:
            return str(row[candidate])
    return str(index)


def build_matsim_network_from_gdfs(nodes_gdf, edges_gdf, crs: str) -> tuple[list[MatsimNode], list[MatsimLink]]:
    """Convert pyrosm node/edge GeoDataFrames to MATSim node/link records."""
    nodes_projected = nodes_gdf.to_crs(crs)
    edges_projected = edges_gdf.to_crs(crs)
    node_col = _node_id_column(nodes_projected)

    nodes_by_id: dict[str, MatsimNode] = {}
    for index, row in nodes_projected.iterrows():
        node_id = str(row[node_col] if node_col is not None else index)
        nodes_by_id[node_id] = MatsimNode(node_id=node_id, x=float(row.geometry.x), y=float(row.geometry.y))

    links: list[MatsimLink] = []
    used_link_ids: set[str] = set()
    for index, row in edges_projected.iterrows():
        from_node = _edge_node_value(row, ("u", "from", "from_node"))
        to_node = _edge_node_value(row, ("v", "to", "to_node"))
        if from_node not in nodes_by_id or to_node not in nodes_by_id or row.geometry is None or row.geometry.is_empty:
            continue

        highway = normalize_highway(row.get("highway"))
        lanes = parse_lanes(row.get("lanes"), highway)
        freespeed = parse_maxspeed_mps(row.get("maxspeed"), highway)
        capacity = HIGHWAY_DEFAULTS[highway]["capacity"] * lanes
        base_id = _edge_base_id(row, index)
        length = float(row.geometry.length)

        direction = oneway_direction(row.get("oneway"))
        if direction in {"forward", "both"}:
            forward_id = _unique_link_id(f"{base_id}_f", used_link_ids)
            links.append(
                MatsimLink(
                    link_id=forward_id,
                    from_node=from_node,
                    to_node=to_node,
                    length=length,
                    freespeed=freespeed,
                    capacity=capacity,
                    permlanes=lanes,
                    modes="car",
                    geometry=row.geometry,
                )
            )

        if direction in {"reverse", "both"}:
            reverse_id = _unique_link_id(f"{base_id}_r", used_link_ids)
            links.append(
                MatsimLink(
                    link_id=reverse_id,
                    from_node=to_node,
                    to_node=from_node,
                    length=length,
                    freespeed=freespeed,
                    capacity=capacity,
                    permlanes=lanes,
                    modes="car",
                    geometry=_reverse_geometry(row.geometry),
                )
            )

    used_nodes = {node_id for link in links for node_id in (link.from_node, link.to_node)}
    return [node for node_id, node in nodes_by_id.items() if node_id in used_nodes], links


def _unique_link_id(base_id: str, used_link_ids: set[str]) -> str:
    candidate = base_id
    suffix = 1
    while candidate in used_link_ids:
        suffix += 1
        candidate = f"{base_id}_{suffix}"
    used_link_ids.add(candidate)
    return candidate


def _keep_largest_strong_component(
    nodes_by_id: dict[str, MatsimNode],
    links: list[MatsimLink],
    nx_module: Any,
) -> tuple[list[MatsimNode], list[MatsimLink]]:
    # MATSim car routing requires a STRONGLY-connected network: every node must be
    # reachable from every other following link directions. A weakly-connected
    # filter leaves one-way traps that crash routing ("No route found ... by car"),
    # so keep the largest strongly-connected component (same as NetworkCleaner).
    graph = nx_module.DiGraph()
    graph.add_nodes_from(nodes_by_id)
    graph.add_edges_from((link.from_node, link.to_node) for link in links)
    components = list(nx_module.strongly_connected_components(graph))
    if not components:
        return [], []
    largest = max(components, key=len)
    kept_links = [link for link in links if link.from_node in largest and link.to_node in largest]
    used_nodes = {node_id for link in kept_links for node_id in (link.from_node, link.to_node)}
    kept_nodes = [node for node_id, node in nodes_by_id.items() if node_id in used_nodes]
    return kept_nodes, kept_links


def run(cfg) -> None:
    """INPUT: Geofabrik Illinois OSM PBF plus Logan Square buffered boundary. OUTPUT: MATSim network.xml.gz and data/interim/network_links.gpkg."""
    from pyrosm import OSM
    import networkx as nx

    network_path = cfg.project_root / "scenarios" / "logan_square" / NETWORK_OUTPUT
    links_path = cfg.data_interim / LINKS_OUTPUT
    if network_path.exists() and network_path.stat().st_size > 0 and links_path.exists() and links_path.stat().st_size > 0:
        print(f"s1 outputs already exist: {network_path}, {links_path}")
        return

    osm_cfg = cfg.sources["osm"]
    dataset = osm_cfg.get("pyrosm_dataset")
    if dataset:
        from pyrosm import get_data

        osm_path = get_data(dataset, directory=str(cfg.data_raw))
    else:
        osm_path = download_to_raw(osm_cfg["url"], _osm_download_path(cfg))
    bounding_polygon = _buffered_boundary_wgs84(cfg)
    osm = OSM(str(osm_path), bounding_box=bounding_polygon)
    nodes_gdf, edges_gdf = osm.get_network(network_type="driving", nodes=True)

    nodes, links = build_matsim_network_from_gdfs(nodes_gdf, edges_gdf, cfg.crs)
    raw_link_count = len(links)
    raw_median_length = _median_link_length(links)
    nodes, links = simplify_network(nodes, links)
    simplified_link_count = len(links)
    simplified_median_length = _median_link_length(links)
    nodes, links = _keep_largest_strong_component({node.node_id: node for node in nodes}, links, nx)
    if not nodes or not links:
        raise ValueError("No routable driving network links remained after largest-component filtering.")

    write_matsim_network_xml(nodes, links, network_path)
    write_links_gpkg(links, links_path, cfg.crs)

    total_km = sum(link.length for link in links) / 1000
    minx, miny, maxx, maxy = links_path_bounds(links)
    print(
        "s1 complete: "
        f"nodes={len(nodes)}; links={len(links)}; total_km={total_km:.2f}; "
        f"simplified_links={raw_link_count}->{simplified_link_count}; "
        f"median_link_m={raw_median_length:.1f}->{simplified_median_length:.1f}; "
        f"bbox=({_format_float(minx)}, {_format_float(miny)}, {_format_float(maxx)}, {_format_float(maxy)})"
    )


def links_path_bounds(links: list[MatsimLink]) -> tuple[float, float, float, float]:
    from shapely.ops import unary_union

    bounds = unary_union([link.geometry for link in links]).bounds
    return (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
