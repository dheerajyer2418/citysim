"""Offline tests for S2D CMAP cordon demand helpers."""

from __future__ import annotations

from shapely.geometry import LineString, Point

from pipeline.cmap_cordon_demand import (
    GatewayChoice,
    NetworkNode,
    gateway_candidate_links,
    gateway_choices_for_zone,
    generate_cordon_person_plans,
    iter_cordon_auto_rows,
    nearest_node,
    select_gateway,
)
from pipeline.crosswalk import LinkRecord


TOD_WINDOWS = {
    "AM1": [25200, 28800],
    "MD": [36000, 50400],
}


class FloorRng:
    def random(self) -> float:
        return 0.0

    def uniform(self, low: float, high: float) -> float:
        return low


class HighRandomRng:
    def random(self) -> float:
        return 0.99

    def uniform(self, low: float, high: float) -> float:
        return low


def test_nearest_gateway_selects_closest_node() -> None:
    nodes = [
        ("west", 0.0, 0.0),
        ("center", 10.0, 10.0),
        ("east", 30.0, 0.0),
    ]

    assert nearest_node(nodes, 27.0, 2.0) == NetworkNode("east", 30.0, 0.0)
    assert nearest_node(nodes, 9.0, 12.0) == NetworkNode("center", 10.0, 10.0)


def test_filter_keeps_only_one_end_internal_auto_rows() -> None:
    rows = [
        {"purpose": "HBWH", "mode": "1", "o_zone": "101", "d_zone": "102", "a_zone": "101", "timeperiod": "AM1", "trips": "3"},
        {"purpose": "HBO", "mode": "2", "o_zone": "101", "d_zone": "900", "a_zone": "101", "timeperiod": "MD", "trips": "2"},
        {"purpose": "HBS", "mode": "3", "o_zone": "901", "d_zone": "102", "a_zone": "901", "timeperiod": "MD", "trips": "2"},
        {"purpose": "NHB", "mode": "1", "o_zone": "901", "d_zone": "902", "a_zone": "901", "timeperiod": "PM1", "trips": "2"},
        {"purpose": "HBWH", "mode": "4", "o_zone": "101", "d_zone": "903", "a_zone": "101", "timeperiod": "AM1", "trips": "3"},
    ]

    kept = list(iter_cordon_auto_rows(rows, {"101", "102"}, {"1", "2", "3"}))

    assert kept == [
        {"purpose": "HBO", "mode": "2", "o_zone": "101", "d_zone": "900", "a_zone": "101", "timeperiod": "MD", "trips": "2"},
        {"purpose": "HBS", "mode": "3", "o_zone": "901", "d_zone": "102", "a_zone": "901", "timeperiod": "MD", "trips": "2"},
    ]


def test_gateway_candidates_prefer_arterial_capacity_links() -> None:
    links = [
        LinkRecord("local", "a", "b", LineString([(0, 0), (10, 0)]), capacity=600.0),
        LinkRecord("arterial", "c", "d", LineString([(0, 10), (10, 10)]), capacity=1200.0),
        LinkRecord("connector", "e", "f", LineString([(0, 20), (10, 20)]), is_connector=True, capacity=2000.0),
    ]

    candidates = gateway_candidate_links(links, min_capacity=1000.0)

    assert [link.link_id for link in candidates] == ["arterial"]


def test_gateway_candidates_prefer_boundary_band_when_available() -> None:
    boundary = LineString([(0, 0), (0, 100)])
    links = [
        LinkRecord("interior", "a", "b", LineString([(500, 0), (500, 20)]), capacity=3600.0),
        LinkRecord("perimeter", "c", "d", LineString([(50, 0), (50, 20)]), capacity=1200.0),
    ]

    candidates = gateway_candidate_links(links, min_capacity=1000.0, boundary_geometry=boundary, boundary_band_m=100.0)

    assert [link.link_id for link in candidates] == ["perimeter"]


def test_gateway_choices_use_k_nearest_and_capacity_weights() -> None:
    links = [
        LinkRecord("near_low", "a", "b", LineString([(0, 0), (10, 0)]), capacity=1000.0),
        LinkRecord("near_high", "c", "d", LineString([(0, 10), (10, 10)]), capacity=3000.0),
        LinkRecord("far", "e", "f", LineString([(100, 0), (110, 0)]), capacity=5000.0),
    ]

    choices = gateway_choices_for_zone(Point(5, 4), links, k_nearest=2)

    assert [choice.link_id for choice in choices] == ["near_low", "near_high"]
    assert choices[0].weight == 0.25
    assert choices[1].weight == 0.75
    assert (choices[0].x, choices[0].y) == (5.0, 0.0)


def test_select_gateway_uses_weighted_choice_list() -> None:
    choices = [
        GatewayChoice("low", 1.0, 1.0, capacity=1000.0, weight=0.25),
        GatewayChoice("high", 2.0, 2.0, capacity=3000.0, weight=0.75),
    ]

    assert select_gateway(choices, FloorRng()).link_id == "low"
    assert select_gateway(choices, HighRandomRng()).link_id == "high"


def test_cordon_plan_structure_and_gateway_activity_type() -> None:
    rows = [
        {"purpose": "HBWH", "mode": "1", "o_zone": "101", "d_zone": "900", "a_zone": "101", "timeperiod": "AM1", "trips": "1"},
        {"purpose": "HBWH", "mode": "1", "o_zone": "901", "d_zone": "102", "a_zone": "102", "timeperiod": "MD", "trips": "1"},
    ]
    gateways = {
        "900": NetworkNode("g900", 900.0, 90.0),
        "901": NetworkNode("g901", 901.0, 91.0),
    }
    coords = {
        "101": (101.0, 1.0),
        "102": (102.0, 2.0),
    }

    plans = list(
        generate_cordon_person_plans(
            rows,
            {"101", "102"},
            gateways,
            sample_fraction=1.0,
            tod_windows=TOD_WINDOWS,
            rng=FloorRng(),
            coord_sampler=lambda taz_id: coords[taz_id],
        )
    )

    assert plans[0] == (
        (
            "cordon_00000000",
            [
                ("home", 101.0, 1.0, 25200.0),
                ("gateway", 900.0, 90.0, None),
            ],
        ),
        "out",
    )
    assert plans[1] == (
        (
            "cordon_00000001",
            [
                ("gateway", 901.0, 91.0, 36000.0),
                ("home", 102.0, 2.0, None),
            ],
        ),
        "in",
    )


def test_cordon_plan_can_sample_gateway_choice_lists() -> None:
    rows = [
        {"purpose": "HBWH", "mode": "1", "o_zone": "101", "d_zone": "900", "a_zone": "101", "timeperiod": "AM1", "trips": "1"},
    ]
    gateways = {
        "900": [
            GatewayChoice("g900a", 900.0, 90.0, capacity=1000.0, weight=0.25),
            GatewayChoice("g900b", 901.0, 91.0, capacity=3000.0, weight=0.75),
        ]
    }

    plans = list(
        generate_cordon_person_plans(
            rows,
            {"101"},
            gateways,
            sample_fraction=1.0,
            tod_windows=TOD_WINDOWS,
            rng=HighRandomRng(),
            coord_sampler=lambda taz_id: (101.0, 1.0),
        )
    )

    assert plans[0][0][1][1] == ("gateway", 901.0, 91.0, None)


def test_cordon_generation_uses_supplied_sample_fraction() -> None:
    rows = [
        {"purpose": "HBWH", "mode": "1", "o_zone": "101", "d_zone": "900", "a_zone": "101", "timeperiod": "AM1", "trips": "4"},
    ]
    gateways = {"900": NetworkNode("g900", 900.0, 90.0)}

    plans = list(
        generate_cordon_person_plans(
            rows,
            {"101"},
            gateways,
            sample_fraction=0.25,
            tod_windows=TOD_WINDOWS,
            rng=FloorRng(),
            coord_sampler=lambda taz_id: (101.0, 1.0),
        )
    )

    assert len(plans) == 1
