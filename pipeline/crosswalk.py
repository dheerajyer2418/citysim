"""TAZ-to-network crosswalk and activity coordinate sampling skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    rng_seed: int = 42


_ACTIVE_CROSSWALK: TazLinkCrosswalk | None = None


def build_taz_link_crosswalk(taz_gdf: Any, network: Any) -> TazLinkCrosswalk:
    """Build a spatial index from CMAP TAZ polygons to nearby non-connector links.

    Args:
        taz_gdf: GeoDataFrame-like object with TAZ identifiers and projected geometries.
        network: MATSim network-like object or parsed link table with geometries.

    Returns:
        TazLinkCrosswalk keyed by TAZ id.

    TODO:
        - Normalize TAZ id column names.
        - Convert MATSim links to projected LineString geometries.
        - Exclude connector links by mode, id convention, or explicit flag.
        - Spatially join TAZ polygons to candidate links.
        - Persist the crosswalk for demand generation.
    """
    _ = (taz_gdf, network)
    crosswalk = TazLinkCrosswalk()
    global _ACTIVE_CROSSWALK
    _ACTIVE_CROSSWALK = crosswalk
    return crosswalk


def sample_activity_coord(taz_id: str) -> tuple[float, float]:
    """Sample an in-polygon activity coordinate snapped to the nearest non-connector link.

    Args:
        taz_id: CMAP TAZ identifier.

    Returns:
        Projected `(x, y)` coordinate in the configured CRS.

    TODO:
        - Draw a random point inside the TAZ polygon.
        - Query nearest non-connector links from the active crosswalk.
        - Snap the point to the nearest link geometry.
        - Return the snapped coordinate for MATSim plans.
    """
    if _ACTIVE_CROSSWALK is None:
        raise RuntimeError("No active crosswalk. Call build_taz_link_crosswalk first.")
    if taz_id not in _ACTIVE_CROSSWALK.taz_to_links:
        raise KeyError(f"TAZ {taz_id!r} is not present in the active crosswalk.")

    rng = Random(_ACTIVE_CROSSWALK.rng_seed)
    _ = rng
    raise NotImplementedError("TODO: sample and snap coordinate to nearest non-connector link")
