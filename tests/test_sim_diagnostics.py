"""Offline tests for MATSim output diagnostics."""

from __future__ import annotations

import gzip
import math

from pipeline.sim_diagnostics import event_diagnostics, read_link_table, tuning_recommendations


def test_event_diagnostics_counts_health_and_top_stuck_links(tmp_path) -> None:
    events_path = tmp_path / "events.xml.gz"
    with gzip.open(events_path, "wt", encoding="utf-8") as handle:
        handle.write("<events>")
        handle.write('<event time="0" type="departure" link="a"/>')
        handle.write('<event time="1" type="entered link" link="a"/>')
        handle.write('<event time="2" type="arrival" link="b"/>')
        handle.write('<event time="3" type="departure" link="a"/>')
        handle.write('<event time="4" type="entered link" link="b"/>')
        handle.write('<event time="5" type="stuckAndAbort" link="b"/>')
        handle.write('<event time="5" type="vehicle aborts" link="b"/>')
        handle.write("</events>")

    diagnostics = event_diagnostics(
        events_path,
        {"b": {"capacity": 100.0, "vol_car": 50.0, "volume_capacity_ratio": 0.5}},
        top_n=5,
    )

    assert diagnostics["summary"]["departures"] == 2
    assert diagnostics["summary"]["arrivals"] == 1
    assert diagnostics["summary"]["stuck"] == 1
    assert math.isclose(diagnostics["summary"]["completion_rate"], 0.5)
    assert diagnostics["top_stuck_links"][0]["link_id"] == "b"
    assert diagnostics["top_stuck_links"][0]["stuck"] == 1
    assert diagnostics["top_stuck_links"][0]["entered"] == 1
    assert diagnostics["top_stuck_links"][0]["capacity"] == 100.0


def test_read_link_table_handles_matsim_output_links(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    with gzip.open(output_dir / "output_links.csv.gz", "wt", newline="", encoding="utf-8") as handle:
        handle.write("link;length;freespeed;capacity;vol_car\n")
        handle.write("a;10;5;100;25\n")

    links = read_link_table(output_dir)

    assert links["a"]["length_m"] == 10.0
    assert links["a"]["freespeed_mps"] == 5.0
    assert links["a"]["capacity"] == 100.0
    assert links["a"]["vol_car"] == 25.0
    assert links["a"]["volume_capacity_ratio"] == 0.25


def test_tuning_recommendations_flag_high_stuck_links() -> None:
    rows = [
        {
            "link_id": "a",
            "stuck": 25,
            "entered": 100,
            "stuck_per_entered": 0.25,
            "volume_capacity_ratio": 1.2,
            "freespeed_mps": 6.0,
        },
        {
            "link_id": "b",
            "stuck": 5,
            "entered": 100,
            "stuck_per_entered": 0.05,
            "volume_capacity_ratio": 0.5,
            "freespeed_mps": 10.0,
        },
    ]

    recommendations = tuning_recommendations(rows)

    assert len(recommendations) == 1
    assert recommendations[0]["link_id"] == "a"
    assert recommendations[0]["capacity_factor"] == 1.25
    assert recommendations[0]["freespeed_factor"] == 1.10
