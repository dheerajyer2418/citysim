"""Offline tests for TAZ-to-link crosswalk behavior."""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

from pipeline.crosswalk import LinkRecord, build_taz_link_crosswalk, sample_activity_coord


def _synthetic_taz() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "zone17": [101, 102],
            "geometry": [
                Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
            ],
        },
        crs="EPSG:26971",
    )


def _synthetic_network() -> list[LinkRecord]:
    return [
        LinkRecord(
            link_id="l1",
            from_node="a",
            to_node="b",
            geometry=LineString([(0, 5), (10, 5)]),
            is_connector=False,
        ),
        LinkRecord(
            link_id="connector",
            from_node="x",
            to_node="y",
            geometry=LineString([(0, 0), (10, 10)]),
            is_connector=True,
        ),
        LinkRecord(
            link_id="l2",
            from_node="c",
            to_node="d",
            geometry=LineString([(25, 0), (25, 10)]),
            is_connector=False,
        ),
    ]


def test_every_taz_maps_to_non_connector_link() -> None:
    crosswalk = build_taz_link_crosswalk(_synthetic_taz(), _synthetic_network())

    assert set(crosswalk.taz_to_links) == {"101", "102"}
    assert all(len(links) >= 1 for links in crosswalk.taz_to_links.values())
    assert {
        link.link_id
        for links in crosswalk.taz_to_links.values()
        for link in links
    } == {"l1", "l2"}


def test_sample_activity_coord_is_inside_taz_and_on_non_connector_link() -> None:
    taz_gdf = _synthetic_taz()
    crosswalk = build_taz_link_crosswalk(taz_gdf, _synthetic_network())

    x, y = sample_activity_coord("101")
    point = Point(x, y)
    polygon = crosswalk.taz_geometries["101"]
    non_connector_links = [link.geometry for link in crosswalk.taz_to_links["101"]]

    assert polygon.buffer(1e-9).contains(point)
    assert min(link.distance(point) for link in non_connector_links) <= 1e-9


def test_sampling_advances_rng_and_is_reproducible_for_seed() -> None:
    taz_gdf = _synthetic_taz()
    network = _synthetic_network()

    build_taz_link_crosswalk(taz_gdf, network)
    first_sequence = [sample_activity_coord("101") for _ in range(5)]

    build_taz_link_crosswalk(taz_gdf, network)
    second_sequence = [sample_activity_coord("101") for _ in range(5)]

    assert len(set(first_sequence)) > 1
    assert first_sequence == second_sequence
