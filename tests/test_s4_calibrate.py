"""Offline tests for s4 calibration helper logic."""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point

from pipeline.s4_calibrate import (
    bearing,
    dedupe_count_records,
    direction_matches,
    geh,
    select_directional_link,
)


@dataclass(frozen=True)
class _Link:
    link_id: str
    geometry: LineString
    bearing: float


def test_bearing_known_segments() -> None:
    assert bearing(LineString([(0, 0), (0, 10)])) == 0.0
    assert bearing(LineString([(0, 0), (10, 0)])) == 90.0
    assert bearing(LineString([(0, 0), (0, -10)])) == 180.0
    assert bearing(LineString([(0, 0), (-10, 0)])) == 270.0


def test_directional_snap_matches_northbound_and_rejects_eastbound() -> None:
    point = Point(0, 1)
    northbound = _Link("north", LineString([(0, 0), (0, 10)]), 0.0)
    eastbound = _Link("east", LineString([(-1, 1), (9, 1)]), 90.0)

    assert direction_matches(northbound.bearing, "NB")
    assert not direction_matches(eastbound.bearing, "NB")
    assert select_directional_link(point, [eastbound, northbound], "NB").link_id == "north"


def test_geh_formula() -> None:
    assert geh(1000, 1000) == 0.0
    assert math.isclose(geh(1000, 2000), 25.81988897471611)


def test_dedupe_count_records_averages_duplicate_stations() -> None:
    records = [
        {
            "midpointlat": "41.93001",
            "midpointlon": "-87.70001",
            "direction": "NB",
            "vehiclecount": "1000",
            "roadname": "Milwaukee",
        },
        {
            "midpointlat": "41.93004",
            "midpointlon": "-87.70004",
            "direction": "NB",
            "vehiclecount": "1400",
            "roadname": "Milwaukee",
        },
        {
            "midpointlat": "41.93004",
            "midpointlon": "-87.70004",
            "direction": "SB",
            "vehiclecount": "800",
            "roadname": "Milwaukee",
        },
    ]

    stations = dedupe_count_records(records)

    assert len(stations) == 2
    nb = next(station for station in stations if station.direction == "NB")
    assert nb.observed == 1200.0
