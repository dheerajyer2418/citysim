"""Generate missing community-area entries for params.yaml without editing it."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.io_socrata import fetch_geojson


KNOWN_AREAS = {
    "logan_square": 22,
    "lake_view": 6,
    "avondale": 21,
    "humboldt_park": 23,
    "west_town": 24,
    "lincoln_park": 7,
    "near_north_side": 8,
    "belmont_cragin": 19,
    "irving_park": 16,
    "lincoln_square": 4,
}


def make_slug(name: str) -> str:
    """Normalize community names to the slugs used in params.yaml."""
    name = name.lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


def main() -> int:
    with (ROOT / "params.yaml").open(encoding="utf-8") as handle:
        params = yaml.safe_load(handle)
    socrata = params["sources"]["socrata"]
    source = socrata["community_areas"]
    domain = source.get("domain") or socrata["domain"]
    frame = fetch_geojson(domain, source["dataset_id"])
    rows = sorted(
        (int(row.area_numbe), make_slug(row.community), row.community.title())
        for row in frame.itertuples(index=False)
    )
    configured = params.get("areas", {})
    configured_ids = {
        int(area["boundary"]["community_area_id"])
        for area in configured.values()
    }
    pending = [
        row for row in rows
        if row[1] not in configured and row[0] not in configured_ids
    ]
    slug_counts = Counter(slug for _, slug, _ in rows)
    collisions = sum(count - 1 for count in slug_counts.values())
    errors = []
    for slug, expected_id in KNOWN_AREAS.items():
        actual_ids = [area_id for area_id, actual_slug, _ in rows if actual_slug == slug]
        if actual_ids != [expected_id]:
            errors.append(f"Self-check failed: {slug} expected {expected_id}, got {actual_ids}")
    if collisions:
        errors.append(f"Slug collisions: {collisions}")
    if len(rows) != 77 or {area_id for area_id, _, _ in rows} != set(range(1, 78)):
        errors.append("Expected exactly one community area for each ID from 1 through 77")
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(
            f"Summary: fetched={len(rows)} configured={len(configured)} "
            f"generated=0 collisions={collisions}",
            file=sys.stderr,
        )
        return 1

    # Two-space base indent makes this block ready to paste under `areas:`.
    # JSON string quoting is also valid YAML and safely escapes display names.
    block = "".join(
        f"  {slug}:\n"
        f'    scenario_slug: "{slug}"\n'
        f"    boundary:\n"
        f"      community_area_id: {area_id}\n"
        f"      name: {json.dumps(name, ensure_ascii=False)}\n"
        f"      buffer_m: 1500\n"
        for area_id, slug, name in pending
    )
    output = ROOT / "data" / "interim" / "areas_generated.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(block, encoding="utf-8")
    print(block, end="")
    print(
        f"Summary: fetched={len(rows)} configured={len(configured)} "
        f"generated={len(pending)} collisions={collisions}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
