"""Offline tests for s6 pothole monetization math."""

from __future__ import annotations

import gzip
import json
import math
from types import SimpleNamespace

from pipeline.s6_monetize import (
    _bca_reliable,
    _comparison_warnings,
    _read_bike_lane_facility_miles,
    _scenario_summary,
    compute_bca,
    compute_bike_lane_bca,
    run,
    scenario_comparison_rows,
)


def test_compute_bca_known_values() -> None:
    result = compute_bca(
        vht_base=200.0,
        vht_fixed=100.0,
        vmt_pothole_base=50.0,
        total_potholes=10,
        coeffs={
            "value_of_time_usd_per_hr": 1.5,
            "extra_damage_usd_per_vmt": 2.0,
            "repair_cost_usd_per_pothole": 100.0,
        },
        period_years=1,
        discount_rate=0.0,
        annual_days=2.0,
    )

    assert math.isclose(result["daily_benefits_usd"]["travel_time"], 150.0)
    assert math.isclose(result["daily_benefits_usd"]["vehicle_damage"], 100.0)
    assert math.isclose(result["annual_benefits_usd"]["total"], 500.0)
    assert math.isclose(result["cost_usd"], 1000.0)
    assert math.isclose(result["benefit_cost_ratio"], 0.5)
    assert math.isclose(result["net_benefit_usd"], -500.0)


def test_compute_bca_handles_zero_cost() -> None:
    result = compute_bca(
        vht_base=10.0,
        vht_fixed=5.0,
        vmt_pothole_base=10.0,
        total_potholes=0,
        coeffs={
            "value_of_time_usd_per_hr": 10.0,
            "extra_damage_usd_per_vmt": 1.0,
            "repair_cost_usd_per_pothole": 100.0,
        },
        period_years=1,
        discount_rate=0.0,
        annual_days=1.0,
    )

    assert result["cost_usd"] == 0.0
    assert result["benefit_cost_ratio"] is None
    assert math.isclose(result["net_benefit_usd"], 60.0)


def _write_matsim_summary(output_dir, *, leg_hours: float, entered_link: str | None = None) -> None:
    iteration_dir = output_dir / "ITERS" / "it.1"
    iteration_dir.mkdir(parents=True)
    minutes = int(leg_hours * 60)
    with gzip.open(iteration_dir / "1.legs.csv.gz", "wt", newline="", encoding="utf-8") as handle:
        handle.write("person;trip_id;trav_time;distance\n")
        handle.write(f"a;a_1;{minutes // 60:02d}:{minutes % 60:02d}:00;1609.344\n")
    with gzip.open(iteration_dir / "1.trips.csv.gz", "wt", newline="", encoding="utf-8") as handle:
        handle.write("person;trip_id;trav_time;traveled_distance\n")
        handle.write(f"a;a_1;{minutes // 60:02d}:{minutes % 60:02d}:00;1609.344\n")
    with gzip.open(iteration_dir / "1.events.xml.gz", "wt", encoding="utf-8") as handle:
        handle.write("<events>")
        handle.write('<event time="0" type="departure"/>')
        if entered_link is not None:
            handle.write(f'<event time="1" type="entered link" link="{entered_link}"/>')
        handle.write('<event time="60" type="arrival"/>')
        handle.write("</events>")


def test_run_reads_pothole_annual_days_from_params(tmp_path) -> None:
    scenario_dir = tmp_path / "scenarios" / "logan_square"
    _write_matsim_summary(scenario_dir / "output_baseline", leg_hours=1.0, entered_link="p1")
    _write_matsim_summary(scenario_dir / "output_fixed", leg_hours=1.0)

    data_interim = tmp_path / "data" / "interim"
    data_processed = tmp_path / "data" / "processed"
    data_interim.mkdir(parents=True)
    with (data_interim / "pothole_links.csv").open("w", newline="", encoding="utf-8") as handle:
        handle.write("link_id,length_m,n_potholes\n")
        handle.write("p1,1609.344,1\n")

    cfg = SimpleNamespace(
        project_root=tmp_path,
        data_interim=data_interim,
        data_processed=data_processed,
        scenario=SimpleNamespace(sample_fraction=1.0),
        interventions={
            "pothole": {
                "extra_damage_usd_per_vmt": 0.10,
                "repair_cost_usd_per_pothole": 100.0,
                "analysis_period_years": 1,
                "discount_rate": 0.0,
                "annual_days": 200,
            },
        },
        fhwa_coefficients=SimpleNamespace(
            value_of_time_usd_per_hr=0.0,
            vehicle_damage_usd_per_vmt=0.07,
            emissions_usd_per_ton={},
            crash_cost_usd={},
        ),
    )

    run(cfg)

    payload = json.loads((data_processed / "pothole_bca.json").read_text(encoding="utf-8"))
    assert payload["inputs"]["annual_days"] == 200.0
    assert math.isclose(payload["annual_benefits_usd"]["total"], 20.0)
    assert math.isclose(payload["benefit_cost_ratio"], 0.2)


def test_scenario_summary_reads_completion_and_trip_stats(tmp_path) -> None:
    iteration_dir = tmp_path / "ITERS" / "it.3"
    iteration_dir.mkdir(parents=True)
    with gzip.open(iteration_dir / "3.legs.csv.gz", "wt", newline="", encoding="utf-8") as handle:
        handle.write("person;trip_id;trav_time;distance\n")
        handle.write("a;a_1;00:10:00;1609.344\n")
        handle.write("b;b_1;00:20:00;3218.688\n")
    with gzip.open(iteration_dir / "3.trips.csv.gz", "wt", newline="", encoding="utf-8") as handle:
        handle.write("person;trip_id;trav_time;traveled_distance\n")
        handle.write("a;a_1;00:10:00;1609.344\n")
        handle.write("b;b_1;00:20:00;3218.688\n")
    with gzip.open(iteration_dir / "3.events.xml.gz", "wt", encoding="utf-8") as handle:
        handle.write("<events>")
        handle.write('<event time="0" type="departure"/>')
        handle.write('<event time="600" type="arrival"/>')
        handle.write('<event time="0" type="departure"/>')
        handle.write('<event time="900" type="stuckAndAbort"/>')
        handle.write("</events>")

    summary = _scenario_summary(tmp_path, sample_fraction=0.5)

    assert summary["iteration"] == 3
    assert math.isclose(summary["vht"], 1.0)
    assert math.isclose(summary["vmt"], 6.0)
    assert summary["completed_trips_sample"] == 2
    assert summary["departures_sample"] == 2
    assert summary["arrivals_sample"] == 1
    assert summary["stuck_sample"] == 1
    assert math.isclose(summary["completion_rate"], 0.5)
    assert math.isclose(summary["median_trip_minutes"], 15.0)


def test_comparison_warnings_flag_unhealthy_runs() -> None:
    baseline = {"completion_rate": 0.5, "stuck_sample": 10, "departures_sample": 100}
    fixed = {"completion_rate": 0.95, "stuck_sample": 0, "departures_sample": 100}

    warnings = _comparison_warnings(baseline, fixed)

    assert any("baseline completion rate" in warning for warning in warnings)
    assert any("baseline has 10 sampled stuck/lost trips" in warning for warning in warnings)


def test_comparison_warnings_flag_unbalanced_paired_health() -> None:
    baseline = {
        "completion_rate": 0.981,
        "stuck_sample": 672,
        "departures_sample": 36085,
    }
    fixed = {
        "completion_rate": 0.976,
        "stuck_sample": 859,
        "departures_sample": 36085,
    }

    warnings = _comparison_warnings(baseline, fixed)

    assert any("completion rates differ" in warning for warning in warnings)
    assert any("stuck/lost trips differ" in warning for warning in warnings)
    assert not _bca_reliable(warnings)


def test_compute_bike_lane_bca_accounts_for_benefits_disbenefits_and_cost() -> None:
    result = compute_bike_lane_bca(
        baseline_vht=100.0,
        bike_lane_vht=110.0,
        baseline_vmt=1000.0,
        bike_lane_vmt=990.0,
        facility_miles=1.5,
        cfg={
            "existing_daily_bike_trips": 100.0,
            "induced_daily_bike_trips": 10.0,
            "average_bike_trip_miles": 2.0,
            "max_benefit_miles_per_trip": 2.0,
            "facility_value_usd_per_cycling_mile": 1.0,
            "health_age_share": 0.5,
            "induced_from_inactive_share": 0.8,
            "health_benefit_usd_per_induced_trip": 5.0,
            "accessibility_benefit_usd_per_induced_trip": 1.0,
            "auto_mode_shift_share": 0.5,
            "auto_vehicle_occupancy": 2.0,
            "capital_cost_usd_per_lane_mile": 1000.0,
        },
        fhwa={
            "value_of_time_usd_per_hr": 10.0,
            "vehicle_operating_cost_usd_per_mile": 1.0,
            "external_highway_use_cost_usd_per_vmt": 0.5,
        },
        period_years=1,
        discount_rate=0.0,
        annual_days=1.0,
    )

    assert math.isclose(result["daily_benefits_usd"]["cycling_facility_quality"], 165.0)
    assert math.isclose(result["daily_benefits_usd"]["health"], 20.0)
    assert math.isclose(result["daily_benefits_usd"]["accessibility"], 10.0)
    assert math.isclose(result["daily_benefits_usd"]["avoided_auto_operating_external"], 7.5)
    assert math.isclose(result["daily_benefits_usd"]["car_network_disbenefits"], 100.0)
    assert math.isclose(result["annual_benefits_usd"]["net"], 102.5)
    assert math.isclose(result["cost_usd"], 1500.0)


def test_compute_bike_lane_bca_includes_crash_safety_benefit() -> None:
    result = compute_bike_lane_bca(
        baseline_vht=100.0,
        bike_lane_vht=100.0,
        baseline_vmt=1000.0,
        bike_lane_vmt=1000.0,
        facility_miles=1.0,
        cfg={
            "annual_bicycle_crashes_baseline": 10.0,
            "separated_lane_crash_reduction_factor": 0.45,
        },
        fhwa={
            "value_of_time_usd_per_hr": 10.0,
            "crash_injury_usd": 220000.0,
        },
        period_years=1,
        discount_rate=0.0,
        annual_days=1.0,
    )

    expected_crash_safety = 10.0 * 0.45 * 220000.0
    assert math.isclose(result["annual_benefits_usd"]["crash_safety"], expected_crash_safety)
    assert result["annual_benefits_usd"]["net"] >= result["annual_benefits_usd"]["crash_safety"]


def test_compute_bike_lane_bca_defaults_to_zero_crash_safety_benefit() -> None:
    result = compute_bike_lane_bca(
        baseline_vht=100.0,
        bike_lane_vht=100.0,
        baseline_vmt=1000.0,
        bike_lane_vmt=1000.0,
        facility_miles=1.0,
        cfg={},
        fhwa={"value_of_time_usd_per_hr": 10.0},
        period_years=1,
        discount_rate=0.0,
        annual_days=1.0,
    )

    assert result["annual_benefits_usd"]["crash_safety"] == 0.0
    assert math.isfinite(result["annual_benefits_usd"]["net"])


def test_read_bike_lane_facility_miles_collapses_directional_links(tmp_path) -> None:
    path = tmp_path / "bike_lane_links.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        handle.write("link_id,length_m\n")
        handle.write("a,1609.344\n")
        handle.write("b,1609.344\n")

    assert math.isclose(_read_bike_lane_facility_miles(path, direction_factor=2.0), 1.0)


def test_scenario_comparison_rows_include_deltas_and_bca() -> None:
    scenarios = {
        "fixed": {
            "output_dir": "fixed",
            "iteration": 1,
            "departures_sample": 10,
            "arrivals_sample": 9,
            "completion_rate": 0.9,
            "stuck_sample": 1,
            "stuck_scaled": 10,
            "completed_trips_scaled": 90,
            "vht": 100.0,
            "vmt": 1000.0,
            "mean_trip_minutes": 10.0,
            "median_trip_minutes": 8.0,
        },
        "bike_lane": {
            "output_dir": "bike",
            "iteration": 1,
            "departures_sample": 10,
            "arrivals_sample": 8,
            "completion_rate": 0.8,
            "stuck_sample": 2,
            "stuck_scaled": 20,
            "completed_trips_scaled": 80,
            "vht": 110.0,
            "vmt": 990.0,
            "mean_trip_minutes": 11.0,
            "median_trip_minutes": 9.0,
        },
    }

    rows = scenario_comparison_rows(
        scenarios,
        reference_name="fixed",
        bca_by_scenario={"bike_lane": {"annual_benefits_usd": {"net": 50.0}, "cost_usd": 100.0, "benefit_cost_ratio": 0.5}},
    )

    bike_row = next(row for row in rows if row["scenario"] == "bike_lane")
    assert bike_row["vht_delta_vs_reference"] == 10.0
    assert bike_row["vmt_delta_vs_reference"] == -10.0
    assert bike_row["completed_trips_delta_vs_reference"] == -10
    assert bike_row["stuck_trips_delta_vs_reference"] == 10
    assert bike_row["annual_net_benefit_usd"] == 50.0
    assert bike_row["benefit_cost_ratio"] == 0.5
