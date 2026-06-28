"""Offline tests for S2 LODES demand helpers."""

from __future__ import annotations

import gzip

import geopandas as gpd
import numpy as np
from lxml import etree
from shapely.geometry import Polygon

from pipeline.s2_demand import (
    AgentPlan,
    build_block_taz_map,
    sample_departure_times,
    write_plans_xml,
)


def test_build_block_taz_map_with_synthetic_points() -> None:
    blocks = gpd.GeoDataFrame(
        [
            {
                "GEOID": "170310101001001",
                "CENTLON": 5.0,
                "CENTLAT": 5.0,
                "geometry": Polygon([(4, 4), (6, 4), (6, 6), (4, 6)]),
            },
            {
                "GEOID": "170310101001002",
                "CENTLON": 25.0,
                "CENTLAT": 5.0,
                "geometry": Polygon([(24, 4), (26, 4), (26, 6), (24, 6)]),
            },
            {
                "GEOID": "170310101001003",
                "CENTLON": 100.0,
                "CENTLAT": 100.0,
                "geometry": Polygon([(99, 99), (101, 99), (101, 101), (99, 101)]),
            },
        ],
        crs="EPSG:4326",
    )
    taz = gpd.GeoDataFrame(
        [
            {"taz_id": "a", "geometry": Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])},
            {"taz_id": "b", "geometry": Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])},
        ],
        crs="EPSG:4326",
    )

    mapping = build_block_taz_map(blocks, taz, "EPSG:4326")

    assert mapping == {
        "170310101001001": "a",
        "170310101001002": "b",
    }


def test_departure_time_sampler_bounds_and_reproducibility() -> None:
    rng_a = np.random.default_rng(123)
    rng_b = np.random.default_rng(123)

    sequence_a = [sample_departure_times(rng_a) for _ in range(200)]
    sequence_b = [sample_departure_times(rng_b) for _ in range(200)]

    assert sequence_a == sequence_b
    for home_end, work_end in sequence_a:
        assert 5 * 3600 <= home_end <= 10 * 3600
        assert 14 * 3600 <= work_end <= 21 * 3600
        assert work_end >= home_end + 4 * 3600


def test_write_plans_xml_round_trips(tmp_path) -> None:
    output = tmp_path / "plans.xml.gz"
    agents = [
        AgentPlan(
            person_id="p1",
            home_x=100.0,
            home_y=200.0,
            work_x=300.0,
            work_y=400.0,
            home_end_time=7.5 * 3600,
            work_end_time=17.0 * 3600,
        ),
        AgentPlan(
            person_id="p2",
            home_x=110.0,
            home_y=210.0,
            work_x=310.0,
            work_y=410.0,
            home_end_time=8.0 * 3600,
            work_end_time=18.0 * 3600,
        ),
    ]

    write_plans_xml(agents, output)

    with gzip.open(output, "rb") as handle:
        tree = etree.parse(handle)
    root = tree.getroot()
    persons = root.xpath("/population/person")
    activities = root.xpath("/population/person/plan/activity")
    legs = root.xpath("/population/person/plan/leg")

    assert len(persons) == 2
    assert len(activities) == 6
    assert len(legs) == 4
    assert persons[0].attrib["id"] == "p1"
    assert all(leg.attrib["mode"] == "car" for leg in legs)
    for activity in activities:
        assert "type" in activity.attrib
        assert "x" in activity.attrib
        assert "y" in activity.attrib
