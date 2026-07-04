"""Offline tests for s5 pothole intervention helpers."""

from __future__ import annotations

from datetime import date
import math

from shapely.geometry import LineString, Point

from pipeline.s5_interventions import (
    NetworkLink,
    _apply_link_edits,
    _valid_potholes,
    degraded_freespeed,
    read_link_edit_csv,
    select_links_near_corridor,
    snap_potholes_to_links,
    speed_penalty,
)


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


def test_valid_potholes_keeps_open_or_recent_records() -> None:
    records = [
        {
            "type_of_service_request": "Pothole in Street",
            "latitude": "41.1",
            "longitude": "-87.1",
            "status": "Open",
            "creation_date": "2020-01-01T00:00:00.000",
        },
        {
            "type_of_service_request": "Pothole in Street",
            "latitude": "41.2",
            "longitude": "-87.2",
            "status": "Completed",
            "creation_date": "2026-01-01T00:00:00.000",
        },
        {
            "type_of_service_request": "Pothole in Street",
            "latitude": "41.3",
            "longitude": "-87.3",
            "status": "Completed",
            "creation_date": "2020-01-01T00:00:00.000",
        },
    ]

    potholes = _valid_potholes(
        records,
        recent_days=365,
        active_statuses={"Open"},
        as_of=date(2026, 6, 30),
    )

    assert [(p.status, p.creation_date) for p in potholes] == [
        ("Open", date(2020, 1, 1)),
        ("Completed", date(2026, 1, 1)),
    ]


def test_select_links_near_corridor_filters_by_distance_capacity_and_connector() -> None:
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:26971", always_xy=True)
    x1, y1 = transformer.transform(-87.0, 41.0)
    x2, y2 = transformer.transform(-86.999, 41.0)
    links = [
        NetworkLink("near", LineString([(x1, y1), (x2, y2)]), length_m=100.0, capacity=1000.0),
        NetworkLink("low_capacity", LineString([(x1, y1 + 2), (x2, y2 + 2)]), length_m=100.0, capacity=100.0),
        NetworkLink("connector", LineString([(x1, y1 + 4), (x2, y2 + 4)]), length_m=100.0, capacity=1000.0, is_connector=True),
        NetworkLink("far", LineString([(x1, y1 + 1000), (x2, y2 + 1000)]), length_m=100.0, capacity=1000.0),
    ]

    selected = select_links_near_corridor(
        links,
        [[-87.0, 41.0], [-86.999, 41.0]],
        "EPSG:26971",
        buffer_m=20.0,
        min_capacity=600.0,
    )

    assert [link.link_id for link in selected] == ["near"]


def test_read_link_edit_csv_and_apply_link_edits(tmp_path) -> None:
    import gzip
    from lxml import etree

    edits_path = tmp_path / "edits.csv"
    edits_path.write_text(
        "link_id,capacity_factor,freespeed_factor\n"
        "a,1.25,1.1\n",
        encoding="utf-8",
    )
    input_network = tmp_path / "network.xml.gz"
    output_network = tmp_path / "network_edited.xml.gz"
    with gzip.open(input_network, "wt", encoding="utf-8") as handle:
        handle.write(
            '<network><links>'
            '<link id="a" from="1" to="2" length="10" freespeed="10" capacity="100"/>'
            '<link id="b" from="2" to="3" length="10" freespeed="10" capacity="100"/>'
            "</links></network>"
        )

    edits = read_link_edit_csv(edits_path)
    changed = _apply_link_edits(input_network, output_network, edits)

    assert changed == 1
    with gzip.open(output_network, "rb") as handle:
        tree = etree.parse(handle)
    link_a = tree.find(".//link[@id='a']")
    link_b = tree.find(".//link[@id='b']")
    assert link_a is not None
    assert link_a.get("capacity") == "125"
    assert link_a.get("freespeed") == "11"
    assert link_b is not None
    assert link_b.get("capacity") == "100"
