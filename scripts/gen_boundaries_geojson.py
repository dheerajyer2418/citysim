"""Regenerate viz/community_area_boundaries.geojson with all 77 Chicago community areas.

The on-map neighborhood selector in the unified needs/live maps is driven by this
file (viz/build_site.py reads it and keys polygons by properties.area_numbe). It
must contain every configured area or new areas are not selectable on the map.

Source: Socrata community-areas dataset igwz-8jzy (same as pipeline/s0_boundary.py).

Usage:
    .venv\\Scripts\\python.exe scripts\\gen_boundaries_geojson.py
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from shapely.geometry import mapping

from pipeline.io_socrata import fetch_geojson

OUT = ROOT / "viz" / "community_area_boundaries.geojson"


def main() -> int:
    params = yaml.safe_load((ROOT / "params.yaml").open(encoding="utf-8"))
    socrata = params["sources"]["socrata"]
    source = socrata["community_areas"]
    domain = source.get("domain") or socrata["domain"]
    frame = fetch_geojson(domain, source["dataset_id"])

    features = []
    for row in frame.itertuples(index=False):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        # build_site.py only needs properties.area_numbe + geometry.
        features.append(
            {
                "type": "Feature",
                "properties": {"area_numbe": str(int(row.area_numbe)),
                               "community": row.community},
                "geometry": mapping(geom),
            }
        )

    features.sort(key=lambda f: int(f["properties"]["area_numbe"]))
    fc = {"type": "FeatureCollection", "features": features}
    OUT.write_text(json.dumps(fc), encoding="utf-8")
    ids = [int(f["properties"]["area_numbe"]) for f in features]
    print(f"wrote {OUT} | features={len(features)} | ids {min(ids)}..{max(ids)} "
          f"| complete_1_77={ids == list(range(1, 78))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
