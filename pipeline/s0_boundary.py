"""Stage s0: boundary and TAZ preprocessing."""

from __future__ import annotations


def run(cfg) -> None:
    """INPUT: Chicago community-area boundary and CMAP c24q4 TAZ polygons. OUTPUT: EPSG:26971 Logan Square boundary and clipped TAZ polygons in data/interim."""
    print(
        "TODO s0: select community area #"
        f"{cfg.boundary.community_area_id} ({cfg.boundary.name}), buffer "
        f"{cfg.boundary.buffer_m}m, reproject to {cfg.crs}, and write interim polygons."
    )
