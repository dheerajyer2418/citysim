"""Stage s2: demand synthesis."""

from __future__ import annotations


def run(cfg) -> None:
    """INPUT: LODES OD records, CMAP c24q4 trip tables, TAZ polygons, and TNP sanity-check data. OUTPUT: MATSim plans.xml.gz for scenarios/logan_square."""
    print(
        "TODO s2: downscale LODES/CMAP demand at sample_fraction="
        f"{cfg.scenario.sample_fraction}, use crosswalk sampling, validate against TNP, and write plans.xml.gz."
    )
