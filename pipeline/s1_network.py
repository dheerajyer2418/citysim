"""Stage s1: OSM network preparation."""

from __future__ import annotations


def run(cfg) -> None:
    """INPUT: Geofabrik Illinois OSM PBF plus Logan Square buffered boundary. OUTPUT: MATSim network.xml.gz for scenarios/logan_square."""
    osm_url = cfg.sources["osm"]["url"]
    print(f"TODO s1: cache {osm_url}, clip to Logan Square buffer, run pt2matsim, and write network.xml.gz.")
