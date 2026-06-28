"""Offline tests for s5 pothole intervention helpers."""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point

from pipeline.s5_interventions import NetworkLink, degraded_freespeed, snap_potholes_to_links, speed_penalty


def test_snap_potholes_aggregates_to_nearest_links() -> None:
    links = [
        NetworkLink("a", LineString([(0, 0), (10, 0)]), length_m=10.0),
        NetworkLink("b", LineString([(0, 10), (10, 10)]), length_m=10.0),
    ]
    points = [Point(1, 1), Point(2, 1), Point(4, 9), Point(100, 100)]

    counts = snap_potholes_to_links(points, links, max_distance_m=3.0)

    assert counts["a"] == 2
    assert counts["b"] == 1
    assert sum(counts.values()) == 3


def test_degraded_freespeed_uses_capped_linear_penalty() -> None:
    assert math.isclose(speed_penalty(3, 0.05, 0.50), 0.15)
    assert math.isclose(degraded_freespeed(20.0, 3, 0.05, 0.50), 17.0)
    assert math.isclose(speed_penalty(20, 0.05, 0.50), 0.50)
    assert math.isclose(degraded_freespeed(20.0, 20, 0.05, 0.50), 10.0)
