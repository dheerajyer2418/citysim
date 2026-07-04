"""Offline tests for S2C CMAP roster demand helpers."""

from __future__ import annotations

import gzip

from lxml import etree

from pipeline.cmap_demand import (
    activity_types_for_trip,
    iter_internal_auto_rows,
    sample_departure_seconds,
    smooth_departure_seconds,
)
from pipeline.plans_io import write_population


TOD_WINDOWS = {
    "NA": [72000, 108000],
    "MD": [36000, 50400],
}


class MidpointRng:
    def uniform(self, low: float, high: float) -> float:
        return (low + high) / 2.0


class FixedNormalRng:
    def normal(self, mean: float, std: float) -> float:
        return mean + std


def test_filter_keeps_only_internal_internal_auto_rows() -> None:
    rows = [
        {"purpose": "HBWH", "mode": "1", "o_zone": "101", "d_zone": "102", "a_zone": "101", "timeperiod": "AM1", "trips": "3"},
        {"purpose": "HBO", "mode": "4", "o_zone": "101", "d_zone": "102", "a_zone": "101", "timeperiod": "MD", "trips": "2"},
        {"purpose": "HBS", "mode": "2", "o_zone": "999", "d_zone": "102", "a_zone": "999", "timeperiod": "MD", "trips": "2"},
        {"purpose": "NHB", "mode": "3", "o_zone": "101", "d_zone": "888", "a_zone": "101", "timeperiod": "PM1", "trips": "2"},
    ]

    kept = list(iter_internal_auto_rows(rows, {"101", "102"}, {"1", "2", "3"}))

    assert kept == [
        {"purpose": "HBWH", "mode": "1", "o_zone": "101", "d_zone": "102", "a_zone": "101", "timeperiod": "AM1", "trips": "3"}
    ]


def test_tod_sampler_uses_windows_and_wraps_na() -> None:
    md_departure = sample_departure_seconds("MD", TOD_WINDOWS, MidpointRng())
    na_departure = sample_departure_seconds("NA", TOD_WINDOWS, MidpointRng())

    assert 36000 <= md_departure < 50400
    assert 0 <= na_departure < 86400
    assert na_departure == 3600


def test_departure_smoothing_applies_jitter_and_wraps_day() -> None:
    assert smooth_departure_seconds(1000.0, 0.0, FixedNormalRng()) == 1000.0
    assert smooth_departure_seconds(1000.0, 300.0, FixedNormalRng()) == 1300.0
    assert smooth_departure_seconds(86300.0, 300.0, FixedNormalRng()) == 200.0


def test_activity_type_mapping_uses_home_anchor() -> None:
    assert activity_types_for_trip("HBWH", "101", "102", "101") == ("home", "work")
    assert activity_types_for_trip("HBWL", "101", "102", "102") == ("work", "home")
    assert activity_types_for_trip("HBO", "101", "102", "101") == ("home", "other")
    assert activity_types_for_trip("HBS", "101", "102", "101") == ("home", "shop")
    assert activity_types_for_trip("VISIT", "101", "102", "101") == ("home", "visit")
    assert activity_types_for_trip("NHB", "101", "102", "101") == ("other", "other")
    assert activity_types_for_trip("DEAD", "101", "102", "101") == ("other", "other")


def test_write_population_round_trips(tmp_path) -> None:
    output = tmp_path / "plans.xml.gz"
    persons = [
        (
            "cmap_00000000",
            [
                ("home", 100.0, 200.0, 7.5 * 3600),
                ("work", 300.0, 400.0, None),
            ],
        ),
        (
            "cmap_00000001",
            [
                ("other", 110.0, 210.0, 12.0 * 3600),
                ("shop", 310.0, 410.0, None),
            ],
        ),
    ]

    write_population(persons, output)

    with gzip.open(output, "rb") as handle:
        root = etree.parse(handle).getroot()

    assert len(root.xpath("/population/person")) == 2
    assert len(root.xpath("/population/person/plan/activity")) == 4
    assert len(root.xpath("/population/person/plan/leg")) == 2
    assert root.xpath("/population/person")[0].attrib["id"] == "cmap_00000000"
    assert root.xpath("/population/person/plan/activity")[0].attrib["end_time"] == "07:30:00"
    assert all(leg.attrib["mode"] == "car" for leg in root.xpath("/population/person/plan/leg"))
