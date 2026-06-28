"""TAZ-to-network crosswalk and activity coordinate sampling skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
from operator import index as operator_index
from pathlib import Path
from random import Random
from typing import Any


@dataclass
class LinkRecord:
    """Minimal network link representation used by the scaffold."""

    link_id: str
    from_node: str
    to_node: str
    geometry: Any
    is_connector: bool = False


@dataclass
class TazLinkCrosswalk:
    """Container for TAZ polygons and candidate non-connector MATSim links."""

    taz_to_links: dict[str, list[LinkRecord]] = field(default_factory=dict)
    taz_geometries: dict[str, Any] = field(default_factory=dict)
    rng_seed: int = 42
    rng: Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rng = Random(self.rng_seed)


_ACTIVE_CROSSWALK: TazLinkCrosswalk | None = None


TAZ_ID_CANDIDATES = (
    "zone17",
    "ZONE17",
    "taz",
    "TAZ",
    "taz_id",
    "TAZ_ID",
    "zone",
    "ZONE",
    "id",
    "ID",
)


def detect_taz_id_column(taz_gdf: Any) -> str:
    """Detect the most likely TAZ identifier column in a GeoDataFrame."""
    columns = [column for column in getattr(taz_gdf, "columns", []) if column != "geometry"]
    for candidate in TAZ_ID_CANDIDATES:
        if candidate in columns:
            return candidate

    integer_like_columns: list[str] = []
    for column in columns:
        series = taz_gdf[column].dropna()
        if series.empty:
            continue
        numeric = getattr(series, "dtype", None)
        if str(numeric).startswith(("int", "uint")) and series.is_unique:
            integer_like_columns.append(column)
            continue
        try:
            coerced = series.astype(int)
        except (TypeError, ValueError):
            continue
        if coerced.astype(str).equals(series.astype(str)) and series.is_unique:
            integer_like_columns.append(column)

    if integer_like_columns:
        return integer_like_columns[0]
    raise ValueError("Could not detect a TAZ id column. Expected zone17, taz, zone, id, or a unique integer-like field.")


def _coerce_links(network: Any) -> list[LinkRecord]:
    if isinstance(network, list) and all(isinstance(link, LinkRecord) for link in network):
        return network

    if hasattr(network, "iterrows") and "geometry" in getattr(network, "columns", []):
        links: list[LinkRecord] = []
        for index, row in network.iterrows():
            link_id = str(row.get("link_id", row.get("id", index)))
            from_node = str(row.get("from_node", row.get("from", "")))
            to_node = str(row.get("to_node", row.get("to", "")))
            is_connector = bool(row.get("is_connector", False))
            links.append(
                LinkRecord(
                    link_id=link_id,
                    from_node=from_node,
                    to_node=to_node,
                    geometry=row.geometry,
                    is_connector=is_connector,
                )
            )
        return links

    raise TypeError("network must be a list[LinkRecord] or a GeoDataFrame-like link table")


def load_links_from_gpkg(path: str | Path) -> list[LinkRecord]:
    """Load MATSim link records from an S1 network_links GeoPackage."""
    import geopandas as gpd

    links_gdf = gpd.read_file(path)
    required_columns = {"link_id", "from_node", "to_node", "geometry"}
    missing = required_columns.difference(links_gdf.columns)
    if missing:
        raise ValueError(f"Missing required link columns in {path}: {sorted(missing)}")

    links: list[LinkRecord] = []
    for _, row in links_gdf.iterrows():
        links.append(
            LinkRecord(
                link_id=str(row["link_id"]),
                from_node=str(row["from_node"]),
                to_node=str(row["to_node"]),
                geometry=row.geometry,
                is_connector=bool(row.get("is_connector", False)),
            )
        )
    return links


def build_taz_link_crosswalk(taz_gdf: Any, network: Any) -> TazLinkCrosswalk:
    """Build a spatial index from CMAP TAZ polygons to nearby non-connector links.

    Args:
        taz_gdf: GeoDataFrame-like object with TAZ identifiers and projected geometries.
        network: MATSim network-like object or parsed link table with geometries.

    Returns:
        TazLinkCrosswalk keyed by TAZ id.

    """
    from shapely.strtree import STRtree

    taz_id_column = detect_taz_id_column(taz_gdf)
    links = [link for link in _coerce_links(network) if not link.is_connector and not link.geometry.is_empty]
    geometries = [link.geometry for link in links]
    tree = STRtree(geometries) if geometries else None

    crosswalk = TazLinkCrosswalk()
    geometry_id_to_link = {id(geometry): link for geometry, link in zip(geometries, links)}

    for _, row in taz_gdf.iterrows():
        taz_id = str(row[taz_id_column])
        polygon = row.geometry
        crosswalk.taz_geometries[taz_id] = polygon
        if tree is None:
            crosswalk.taz_to_links[taz_id] = []
            continue

        query_result = tree.query(polygon)
        candidate_links: list[LinkRecord] = []
        for item in query_result:
            try:
                link_index = operator_index(item)
            except TypeError:
                link_index = None
            if link_index is not None:
                link = links[link_index]
                geometry = link.geometry
            else:
                geometry = item
                link = geometry_id_to_link[id(geometry)]
            if geometry.intersects(polygon):
                candidate_links.append(link)
        crosswalk.taz_to_links[taz_id] = candidate_links

    global _ACTIVE_CROSSWALK
    _ACTIVE_CROSSWALK = crosswalk
    return crosswalk


def _sample_point_in_polygon(polygon: Any, rng: Random, max_attempts: int = 10_000) -> Any:
    from shapely.geometry import Point

    minx, miny, maxx, maxy = polygon.bounds
    for _ in range(max_attempts):
        point = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if polygon.contains(point) or polygon.touches(point):
            return point
    return polygon.representative_point()


def sample_activity_coord(taz_id: str) -> tuple[float, float]:
    """Sample an in-polygon activity coordinate snapped to the nearest non-connector link.

    Args:
        taz_id: CMAP TAZ identifier.

    Returns:
        Projected `(x, y)` coordinate in the configured CRS.

    """
    if _ACTIVE_CROSSWALK is None:
        raise RuntimeError("No active crosswalk. Call build_taz_link_crosswalk first.")
    if taz_id not in _ACTIVE_CROSSWALK.taz_to_links:
        raise KeyError(f"TAZ {taz_id!r} is not present in the active crosswalk.")
    links = _ACTIVE_CROSSWALK.taz_to_links[taz_id]
    if not links:
        raise ValueError(f"TAZ {taz_id!r} has no candidate non-connector links.")

    polygon = _ACTIVE_CROSSWALK.taz_geometries[taz_id]
    point = _sample_point_in_polygon(polygon, _ACTIVE_CROSSWALK.rng)
    nearest_link = min(links, key=lambda link: link.geometry.distance(point))
    snapped = nearest_link.geometry.interpolate(nearest_link.geometry.project(point))
    return (float(snapped.x), float(snapped.y))
