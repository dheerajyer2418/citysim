"""Offline tests for S1 MATSim network generation helpers."""

from __future__ import annotations

import gzip

from lxml import etree
from shapely.geometry import LineString, Polygon

from pipeline.crosswalk import LinkRecord, build_taz_link_crosswalk
from pipeline.s1_network import (
    HIGHWAY_DEFAULTS,
    MatsimLink,
    MatsimNode,
    parse_maxspeed_mps,
    simplify_network,
    write_matsim_network_xml,
)


class _FakeTazRow:
    def __init__(self, taz_id: str, geometry) -> None:
        self.taz_id = taz_id
        self.geometry = geometry

    def __getitem__(self, key: str):
        return getattr(self, key)


class _FakeTazTable:
    columns = ["taz_id", "geometry"]

    def __init__(self, rows: list[_FakeTazRow]) -> None:
        self._rows = rows

    def iterrows(self):
        return enumerate(self._rows)


def _test_nodes() -> list[MatsimNode]:
    return [
        MatsimNode("a", 0.0, 0.0),
        MatsimNode("n", 10.0, 0.0),
        MatsimNode("b", 30.0, 0.0),
        MatsimNode("c", 10.0, 20.0),
    ]


def _node_map(nodes: list[MatsimNode]) -> dict[str, MatsimNode]:
    return {node.node_id: node for node in nodes}


def _link(
    link_id: str,
    from_node: str,
    to_node: str,
    nodes_by_id: dict[str, MatsimNode],
    *,
    length: float = 10.0,
    freespeed: float = 10.0,
    permlanes: float = 1.0,
    modes: str = "car",
) -> MatsimLink:
    start = nodes_by_id[from_node]
    end = nodes_by_id[to_node]
    return MatsimLink(
        link_id=link_id,
        from_node=from_node,
        to_node=to_node,
        length=length,
        freespeed=freespeed,
        capacity=600.0,
        permlanes=permlanes,
        modes=modes,
        geometry=LineString([(start.x, start.y), (end.x, end.y)]),
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


def test_build_taz_link_crosswalk_accepts_in_memory_links() -> None:
    links = [
        LinkRecord(
            link_id="car_link",
            from_node="1",
            to_node="2",
            geometry=LineString([(0, 5), (10, 5)]),
        )
    ]
    taz_gdf = _FakeTazTable([_FakeTazRow("a", Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]))])
    crosswalk = build_taz_link_crosswalk(taz_gdf, links)

    assert len(links) == 1
    assert links[0].link_id == "car_link"
    assert crosswalk.taz_to_links["a"][0].link_id == "car_link"


def test_simplify_network_merges_straight_chain() -> None:
    nodes = _test_nodes()
    nodes_by_id = _node_map(nodes)
    links = [
        _link("a_n", "a", "n", nodes_by_id, length=100.0, freespeed=10.0),
        _link("n_b", "n", "b", nodes_by_id, length=200.0, freespeed=20.0),
    ]

    simplified_nodes, simplified_links = simplify_network(nodes, links)

    assert {node.node_id for node in simplified_nodes} == {"a", "b"}
    assert len(simplified_links) == 1
    link = simplified_links[0]
    assert link.from_node == "a"
    assert link.to_node == "b"
    assert link.length == 300.0
    assert abs(link.freespeed - 15.0) < 0.001
    assert list(link.geometry.coords) == [(0.0, 0.0), (10.0, 0.0), (30.0, 0.0)]


def test_simplify_network_does_not_merge_real_junction() -> None:
    nodes = _test_nodes()
    nodes_by_id = _node_map(nodes)
    links = [
        _link("a_n", "a", "n", nodes_by_id),
        _link("n_b", "n", "b", nodes_by_id),
        _link("n_c", "n", "c", nodes_by_id),
    ]

    _, simplified_links = simplify_network(nodes, links)

    assert {link.link_id for link in simplified_links} == {"a_n", "n_b", "n_c"}


def test_simplify_network_does_not_merge_lane_change() -> None:
    nodes = _test_nodes()
    nodes_by_id = _node_map(nodes)
    links = [
        _link("a_n", "a", "n", nodes_by_id, permlanes=1.0),
        _link("n_b", "n", "b", nodes_by_id, permlanes=2.0),
    ]

    _, simplified_links = simplify_network(nodes, links)

    assert {link.link_id for link in simplified_links} == {"a_n", "n_b"}


def test_simplify_network_drops_zero_length_links() -> None:
    nodes = _test_nodes()
    nodes_by_id = _node_map(nodes)
    links = [
        _link("a_n", "a", "n", nodes_by_id, length=0.0),
        _link("n_b", "n", "b", nodes_by_id, length=50.0),
    ]

    simplified_nodes, simplified_links = simplify_network(nodes, links)

    assert [link.link_id for link in simplified_links] == ["n_b"]
    assert {node.node_id for node in simplified_nodes} == {"n", "b"}


def test_simplify_network_merges_two_way_chain_in_both_directions() -> None:
    nodes = _test_nodes()
    nodes_by_id = _node_map(nodes)
    links = [
        _link("a_n", "a", "n", nodes_by_id, length=100.0),
        _link("n_a", "n", "a", nodes_by_id, length=100.0),
        _link("n_b", "n", "b", nodes_by_id, length=200.0),
        _link("b_n", "b", "n", nodes_by_id, length=200.0),
    ]

    simplified_nodes, simplified_links = simplify_network(nodes, links)

    assert {node.node_id for node in simplified_nodes} == {"a", "b"}
    assert len(simplified_links) == 2
    by_direction = {(link.from_node, link.to_node): link for link in simplified_links}
    assert set(by_direction) == {("a", "b"), ("b", "a")}
    assert by_direction[("a", "b")].length == 300.0
    assert by_direction[("b", "a")].length == 300.0
