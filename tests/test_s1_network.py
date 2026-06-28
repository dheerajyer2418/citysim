"""Offline tests for S1 MATSim network generation helpers."""

from __future__ import annotations

import gzip

import geopandas as gpd
from lxml import etree
from shapely.geometry import LineString, Polygon

from pipeline.crosswalk import build_taz_link_crosswalk, load_links_from_gpkg
from pipeline.s1_network import (
    HIGHWAY_DEFAULTS,
    MatsimLink,
    MatsimNode,
    parse_maxspeed_mps,
    write_matsim_network_xml,
)


def test_parse_maxspeed_mph_and_highway_default() -> None:
    assert abs(parse_maxspeed_mps("30 mph", "residential") - 13.4112) < 0.01
    assert parse_maxspeed_mps(None, "primary") == HIGHWAY_DEFAULTS["primary"]["freespeed"]
    assert parse_maxspeed_mps("signals", "service") == HIGHWAY_DEFAULTS["service"]["freespeed"]


def test_write_matsim_network_xml_round_trips(tmp_path) -> None:
    output = tmp_path / "network.xml.gz"
    nodes = [
        MatsimNode("1", 100.0, 200.0),
        MatsimNode("2", 125.0, 200.0),
    ]
    links = [
        MatsimLink(
            link_id="l1",
            from_node="1",
            to_node="2",
            length=25.0,
            freespeed=13.4,
            capacity=600.0,
            permlanes=1.0,
            modes="car",
            geometry=LineString([(100, 200), (125, 200)]),
        )
    ]

    write_matsim_network_xml(nodes, links, output)

    with gzip.open(output, "rb") as handle:
        tree = etree.parse(handle)
    root = tree.getroot()
    xml_nodes = root.xpath("/network/nodes/node")
    xml_links = root.xpath("/network/links/link")

    assert len(xml_nodes) == 2
    assert len(xml_links) == 1
    assert xml_links[0].attrib["id"] == "l1"
    assert xml_links[0].attrib["from"] == "1"
    assert xml_links[0].attrib["to"] == "2"
    for attribute in ("length", "freespeed", "capacity", "permlanes", "modes"):
        assert attribute in xml_links[0].attrib


def test_load_links_from_gpkg_round_trips_into_crosswalk(tmp_path) -> None:
    gpkg_path = tmp_path / "network_links.gpkg"
    links_gdf = gpd.GeoDataFrame(
        [
            {
                "link_id": "car_link",
                "from_node": "1",
                "to_node": "2",
                "length": 10.0,
                "freespeed": 8.94,
                "capacity": 600.0,
                "permlanes": 1.0,
                "modes": "car",
                "is_connector": False,
                "geometry": LineString([(0, 5), (10, 5)]),
            }
        ],
        crs="EPSG:26971",
    )
    links_gdf.to_file(gpkg_path, driver="GPKG")

    links = load_links_from_gpkg(gpkg_path)
    taz_gdf = gpd.GeoDataFrame(
        [{"taz_id": "a", "geometry": Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])}],
        crs="EPSG:26971",
    )
    crosswalk = build_taz_link_crosswalk(taz_gdf, links)

    assert len(links) == 1
    assert links[0].link_id == "car_link"
    assert crosswalk.taz_to_links["a"][0].link_id == "car_link"
