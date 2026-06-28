"""Offline tests for S2D CMAP cordon demand helpers."""

from __future__ import annotations

from pipeline.cmap_cordon_demand import (
    NetworkNode,
    generate_cordon_person_plans,
    iter_cordon_auto_rows,
    nearest_node,
)


TOD_WINDOWS = {
    "AM1": [25200, 28800],
    "MD": [36000, 50400],
}


class FloorRng:
    def random(self) -> float:
        return 0.0

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
