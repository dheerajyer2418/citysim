"""Stage s6: pothole benefit-cost monetization."""

from __future__ import annotations

import csv
import gzip
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any


METERS_PER_MILE = 1609.344
MAX_BCA_COMPLETION_RATE_DELTA = 0.005
MAX_BCA_STUCK_SAMPLE_DELTA = 100.0


def parse_matsim_time_hours(value: str) -> float:
    """Parse MATSim HH:MM:SS duration strings to hours."""
    parts = str(value).split(":")
    if len(parts) != 3:
        raise ValueError(f"Expected HH:MM:SS time, got {value!r}")
    hours, minutes, seconds = parts
    return float(hours) + float(minutes) / 60.0 + float(seconds) / 3600.0


def discount_factor_sum(period_years: int, discount_rate: float) -> float:
    """Return the sum of annual discount factors for years 1..N."""
    years = max(0, int(period_years))
    rate = float(discount_rate)
    return sum(1.0 / ((1.0 + rate) ** year) for year in range(1, years + 1))


def compute_bca(
    vht_base: float,
    vht_fixed: float,
    vmt_pothole_base: float,
    total_potholes: int,
    coeffs: dict[str, float],
    period_years: int,
    discount_rate: float,
    annual_days: float = 300.0,
) -> dict[str, Any]:
    """Compute discounted pothole repair benefits, cost, BCR, and net benefit."""
    vht_delta = max(0.0, float(vht_base) - float(vht_fixed))
    daily_travel_time_benefit = vht_delta * float(coeffs["value_of_time_usd_per_hr"])
    daily_damage_benefit = float(vmt_pothole_base) * float(coeffs["extra_damage_usd_per_vmt"])
    daily_total_benefit = daily_travel_time_benefit + daily_damage_benefit
    annual_total_benefit = daily_total_benefit * float(annual_days)
    discount_sum = discount_factor_sum(period_years, discount_rate)
    discounted_benefit = annual_total_benefit * discount_sum
    cost = max(0, int(total_potholes)) * float(coeffs["repair_cost_usd_per_pothole"])

    return {
        "daily_benefits_usd": {
            "travel_time": daily_travel_time_benefit,
            "vehicle_damage": daily_damage_benefit,
            "total": daily_total_benefit,
        },
        "annual_benefits_usd": {
            "travel_time": daily_travel_time_benefit * float(annual_days),
            "vehicle_damage": daily_damage_benefit * float(annual_days),
            "total": annual_total_benefit,
        },
        "discounted_benefits_usd": {
            "total": discounted_benefit,
            "discount_factor_sum": discount_sum,
        },
        "cost_usd": cost,
        "benefit_cost_ratio": None if cost == 0.0 else discounted_benefit / cost,
        "net_benefit_usd": discounted_benefit - cost,
    }


def compute_bike_lane_bca(
    *,
    baseline_vht: float,
    bike_lane_vht: float,
    baseline_vmt: float,
    bike_lane_vmt: float,
    facility_miles: float,
    cfg: dict[str, Any],
    fhwa: dict[str, float],
    period_years: int,
    discount_rate: float,
    annual_days: float = 300.0,
) -> dict[str, Any]:
    """Compute a sketch-level BCA for a cycling facility scenario."""
    daily_existing_trips = float(cfg.get("existing_daily_bike_trips", 0.0))
    daily_induced_trips = float(cfg.get("induced_daily_bike_trips", 0.0))
    avg_trip_miles = min(float(cfg.get("average_bike_trip_miles", 2.38)), float(cfg.get("max_benefit_miles_per_trip", 2.38)))
    daily_cycling_miles = (daily_existing_trips + daily_induced_trips) * min(float(facility_miles), avg_trip_miles)

    facility_quality = daily_cycling_miles * float(cfg.get("facility_value_usd_per_cycling_mile", 0.0))
    eligible_health_trips = (
        daily_induced_trips
        * float(cfg.get("health_age_share", 1.0))
        * float(cfg.get("induced_from_inactive_share", 1.0))
    )
    health = eligible_health_trips * float(cfg.get("health_benefit_usd_per_induced_trip", 0.0))
    accessibility = daily_induced_trips * float(cfg.get("accessibility_benefit_usd_per_induced_trip", 0.0))

    occupancy = max(float(cfg.get("auto_vehicle_occupancy", 1.0)), 0.01)
    shifted_auto_trips = daily_induced_trips * float(cfg.get("auto_mode_shift_share", 0.0))
    avoided_auto_vmt = shifted_auto_trips * avg_trip_miles / occupancy
    avoided_auto = avoided_auto_vmt * (
        float(fhwa.get("vehicle_operating_cost_usd_per_mile", 0.0))
        + float(fhwa.get("external_highway_use_cost_usd_per_vmt", 0.0))
    )

    car_time_disbenefit = max(0.0, float(bike_lane_vht) - float(baseline_vht)) * float(fhwa["value_of_time_usd_per_hr"])
    car_vmt_disbenefit = max(0.0, float(bike_lane_vmt) - float(baseline_vmt)) * float(fhwa.get("vehicle_operating_cost_usd_per_mile", 0.0))
    daily_car_disbenefits = car_time_disbenefit + car_vmt_disbenefit

    daily_benefits = {
        "cycling_facility_quality": facility_quality,
        "health": health,
        "accessibility": accessibility,
        "avoided_auto_operating_external": avoided_auto,
        "total_gross": facility_quality + health + accessibility + avoided_auto,
        "car_network_disbenefits": daily_car_disbenefits,
        "total_net": facility_quality + health + accessibility + avoided_auto - daily_car_disbenefits,
    }
    annual_crash_baseline = float(cfg.get("annual_bicycle_crashes_baseline", 0.0))
    crash_reduction = float(cfg.get("separated_lane_crash_reduction_factor", 0.0))
    injury_cost = float(fhwa.get("crash_injury_usd", 0.0))
    annual_crash_safety_benefit = annual_crash_baseline * crash_reduction * injury_cost
    annual_net = daily_benefits["total_net"] * float(annual_days) + annual_crash_safety_benefit
    annual_gross = daily_benefits["total_gross"] * float(annual_days) + annual_crash_safety_benefit
    discount_sum = discount_factor_sum(period_years, discount_rate)
    cost = float(cfg.get("capital_cost_usd", 0.0))
    if cost <= 0.0:
        cost = float(facility_miles) * float(cfg.get("capital_cost_usd_per_lane_mile", 0.0))
    discounted_net = annual_net * discount_sum

    return {
        "daily_benefits_usd": daily_benefits,
        "annual_benefits_usd": {
            "gross": annual_gross,
            "net": annual_net,
            "crash_safety": annual_crash_safety_benefit,
        },
        "discounted_benefits_usd": {
            "net": discounted_net,
            "discount_factor_sum": discount_sum,
        },
        "cost_usd": cost,
        "benefit_cost_ratio": None if cost == 0.0 else discounted_net / cost,
        "net_benefit_usd": discounted_net - cost,
        "physical_inputs": {
            "facility_miles": facility_miles,
            "daily_existing_bike_trips": daily_existing_trips,
            "daily_induced_bike_trips": daily_induced_trips,
            "daily_cycling_miles_on_facility": daily_cycling_miles,
            "daily_avoided_auto_vmt": avoided_auto_vmt,
            "annual_bicycle_crashes_baseline": annual_crash_baseline,
            "separated_lane_crash_reduction_factor": crash_reduction,
            "annual_avoided_bicycle_crashes": annual_crash_baseline * crash_reduction,
        },
    }


def _find_last_iteration_dir(output_dir: Path) -> tuple[int, Path]:
    iters_dir = output_dir / "ITERS"
    candidates: list[tuple[int, Path]] = []
    if iters_dir.exists():
        for child in iters_dir.iterdir():
            match = re.fullmatch(r"it\.(\d+)", child.name)
            if child.is_dir() and match:
                candidates.append((int(match.group(1)), child))
    if candidates:
        return max(candidates, key=lambda item: item[0])
    raise FileNotFoundError(f"Could not find MATSim iteration outputs under {output_dir}")


def _read_legs_totals(output_dir: Path, sample_fraction: float) -> dict[str, float]:
    iteration, iteration_dir = _find_last_iteration_dir(output_dir)
    legs_path = iteration_dir / f"{iteration}.legs.csv.gz"
    if not legs_path.exists():
        raise FileNotFoundError(f"Missing MATSim legs file: {legs_path}")
    if sample_fraction <= 0:
        raise ValueError("scenario.sample_fraction must be positive.")

    vht = 0.0
    vmt = 0.0
    with gzip.open(legs_path, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=";")  # MATSim CSVs use ';'
        for row in reader:
            trav_time = row.get("trav_time")
            distance = row.get("distance")
            if trav_time:
                vht += parse_matsim_time_hours(trav_time)
            if distance not in (None, ""):
                vmt += float(distance) / METERS_PER_MILE
    scale = 1.0 / float(sample_fraction)
    return {"iteration": float(iteration), "vht": vht * scale, "vmt": vmt * scale}


def _read_trip_stats(output_dir: Path, sample_fraction: float) -> dict[str, float]:
    iteration, iteration_dir = _find_last_iteration_dir(output_dir)
    trips_path = iteration_dir / f"{iteration}.trips.csv.gz"
    if not trips_path.exists():
        raise FileNotFoundError(f"Missing MATSim trips file: {trips_path}")
    if sample_fraction <= 0:
        raise ValueError("scenario.sample_fraction must be positive.")

    travel_hours: list[float] = []
    distances_m = 0.0
    with gzip.open(trips_path, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            trav_time = row.get("trav_time")
            if trav_time:
                travel_hours.append(parse_matsim_time_hours(trav_time))
            distance = row.get("traveled_distance")
            if distance not in (None, ""):
                distances_m += float(distance)

    completed_sample = len(travel_hours)
    scale = 1.0 / float(sample_fraction)
    return {
        "completed_trips_sample": float(completed_sample),
        "completed_trips_scaled": completed_sample * scale,
        "mean_trip_minutes": mean(travel_hours) * 60.0 if travel_hours else 0.0,
        "median_trip_minutes": median(travel_hours) * 60.0 if travel_hours else 0.0,
        "completed_vmt_scaled": (distances_m / METERS_PER_MILE) * scale,
    }


def _read_event_health(output_dir: Path, sample_fraction: float) -> dict[str, float]:
    iteration, iteration_dir = _find_last_iteration_dir(output_dir)
    events_path = iteration_dir / f"{iteration}.events.xml.gz"
    if not events_path.exists():
        raise FileNotFoundError(f"Missing MATSim events file: {events_path}")
    if sample_fraction <= 0:
        raise ValueError("scenario.sample_fraction must be positive.")

    counts: Counter[str] = Counter()
    with gzip.open(events_path, "rb") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag == "event":
                counts[str(element.get("type"))] += 1
            element.clear()

    departures = counts["departure"]
    arrivals = counts["arrival"]
    stuck = counts["stuckAndAbort"]
    vehicle_aborts = counts["vehicle aborts"]
    denominator = departures if departures > 0 else arrivals + stuck
    completion_rate = arrivals / denominator if denominator else 0.0
    stuck_rate = stuck / denominator if denominator else 0.0
    scale = 1.0 / float(sample_fraction)
    return {
        "departures_sample": float(departures),
        "arrivals_sample": float(arrivals),
        "stuck_sample": float(stuck),
        "vehicle_aborts_sample": float(vehicle_aborts),
        "departures_scaled": departures * scale,
        "arrivals_scaled": arrivals * scale,
        "stuck_scaled": stuck * scale,
        "vehicle_aborts_scaled": vehicle_aborts * scale,
        "completion_rate": completion_rate,
        "stuck_rate": stuck_rate,
    }


def _scenario_summary(output_dir: Path, sample_fraction: float) -> dict[str, Any]:
    totals = _read_legs_totals(output_dir, sample_fraction)
    trips = _read_trip_stats(output_dir, sample_fraction)
    health = _read_event_health(output_dir, sample_fraction)
    return {
        "output_dir": str(output_dir),
        "iteration": int(totals["iteration"]),
        "vht": totals["vht"],
        "vmt": totals["vmt"],
        **trips,
        **health,
    }


def _comparison_warnings(baseline: dict[str, Any], fixed: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for label, summary in (("baseline", baseline), ("fixed", fixed)):
        if summary["completion_rate"] < 0.9:
            warnings.append(
                f"{label} completion rate is {summary['completion_rate']:.1%}; "
                "benefit-cost results are not policy-grade until the simulation is healthier."
            )
        if summary["stuck_sample"] > 0:
            warnings.append(f"{label} has {int(summary['stuck_sample'])} sampled stuck/lost trips.")
    if abs(baseline["departures_sample"] - fixed["departures_sample"]) > 0:
        warnings.append("baseline and fixed scenarios have different departure counts.")
    completion_delta = abs(baseline["completion_rate"] - fixed["completion_rate"])
    if completion_delta > MAX_BCA_COMPLETION_RATE_DELTA:
        warnings.append(
            "paired scenario completion rates differ by "
            f"{completion_delta:.1%}; travel-time BCA may be dominated by model-health noise."
        )
    stuck_delta = abs(baseline["stuck_sample"] - fixed["stuck_sample"])
    if stuck_delta > MAX_BCA_STUCK_SAMPLE_DELTA:
        warnings.append(
            "paired scenario stuck/lost trips differ by "
            f"{stuck_delta:.0f} sampled agents; compare travel-time monetization cautiously."
        )
    return warnings


def _bca_reliable(warnings: list[str]) -> bool:
    unstable_markers = (
        "completion rates differ",
        "stuck/lost trips differ",
        "different departure counts",
        "completion rate is",
    )
    return not any(any(marker in warning for marker in unstable_markers) for warning in warnings)


def _read_pothole_link_lengths(path: Path) -> dict[str, float]:
    lengths: dict[str, float] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            link_id = row.get("link_id")
            if not link_id:
                continue
            lengths[str(link_id)] = float(row.get("length_m") or 0.0)
    return lengths


def _read_total_potholes(path: Path) -> int:
    total = 0
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            total += int(float(row.get("n_potholes") or 0))
    return total


def _read_bike_lane_facility_miles(path: Path, direction_factor: float = 2.0) -> float:
    total_meters = 0.0
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            total_meters += float(row.get("length_m") or 0.0)
    return (total_meters / METERS_PER_MILE) / max(float(direction_factor), 1.0)


def _read_pothole_link_vmt(output_dir: Path, pothole_lengths_m: dict[str, float], sample_fraction: float) -> float:
    iteration, iteration_dir = _find_last_iteration_dir(output_dir)
    events_path = iteration_dir / f"{iteration}.events.xml.gz"
    if not events_path.exists():
        raise FileNotFoundError(f"Missing MATSim events file: {events_path}")
    if sample_fraction <= 0:
        raise ValueError("scenario.sample_fraction must be positive.")

    counts: Counter[str] = Counter()
    pothole_ids = set(pothole_lengths_m)
    with gzip.open(events_path, "rb") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag == "event" and element.get("type") == "entered link":
                link_id = element.get("link")
                if link_id in pothole_ids:
                    counts[str(link_id)] += 1
            element.clear()

    meters = sum(count * pothole_lengths_m[link_id] for link_id, count in counts.items())
    return (meters / METERS_PER_MILE) / float(sample_fraction)


def _coeff_dict(cfg: Any) -> dict[str, float]:
    pothole_cfg = cfg.interventions.get("pothole", {})
    return {
        "value_of_time_usd_per_hr": float(cfg.fhwa_coefficients.value_of_time_usd_per_hr),
        "extra_damage_usd_per_vmt": float(pothole_cfg.get("extra_damage_usd_per_vmt", 0.0)),
        "repair_cost_usd_per_pothole": float(pothole_cfg.get("repair_cost_usd_per_pothole", 0.0)),
    }


def _fhwa_dict(cfg: Any) -> dict[str, float]:
    values = {
        "value_of_time_usd_per_hr": float(cfg.fhwa_coefficients.value_of_time_usd_per_hr),
        "vehicle_damage_usd_per_vmt": float(cfg.fhwa_coefficients.vehicle_damage_usd_per_vmt),
    }
    for key, value in cfg.fhwa_coefficients.emissions_usd_per_ton.items():
        values[f"emissions_{key}_usd_per_ton"] = float(value)
    for key, value in cfg.fhwa_coefficients.crash_cost_usd.items():
        values[f"crash_{key}_usd"] = float(value)
    for key in ("vehicle_operating_cost_usd_per_mile", "external_highway_use_cost_usd_per_vmt"):
        if hasattr(cfg.fhwa_coefficients, key):
            values[key] = float(getattr(cfg.fhwa_coefficients, key))
    return values


def _write_bike_lane_bca(cfg: Any, fixed: dict[str, Any]) -> dict[str, Any] | None:
    bike_lane_cfg = cfg.interventions.get("bike_lane", {})
    bca_cfg = bike_lane_cfg.get("benefits", {})
    if not bike_lane_cfg.get("enabled", False) or not bca_cfg.get("enabled", False):
        return None

    scenario_dir = getattr(cfg, "scenario_dir", Path(cfg.project_root) / "scenarios" / "logan_square")
    bike_lane_dir = scenario_dir / "output_bike_lane"
    if not (bike_lane_dir / "output_events.xml.gz").exists():
        # No bike-lane MATSim run for this area (e.g. areas that only ran the
        # needs-driven road-diet scenarios); skip the bike-lane BCA gracefully.
        return None
    bike_lane_links_csv = cfg.data_interim / "bike_lane_links.csv"
    output_json = cfg.data_processed / "bike_lane_bca.json"

    sample_fraction = float(cfg.scenario.sample_fraction)
    bike_lane = _scenario_summary(bike_lane_dir, sample_fraction)
    facility_miles = _read_bike_lane_facility_miles(
        bike_lane_links_csv,
        float(bca_cfg.get("facility_direction_factor", 2.0)),
    )
    annual_days = float(bca_cfg.get("annual_days", 300.0))
    period_years = int(bca_cfg.get("analysis_period_years", 10))
    discount_rate = float(bca_cfg.get("discount_rate", 0.03))

    result = compute_bike_lane_bca(
        baseline_vht=fixed["vht"],
        bike_lane_vht=bike_lane["vht"],
        baseline_vmt=fixed["vmt"],
        bike_lane_vmt=bike_lane["vmt"],
        facility_miles=facility_miles,
        cfg=bca_cfg,
        fhwa=_fhwa_dict(cfg),
        period_years=period_years,
        discount_rate=discount_rate,
        annual_days=annual_days,
    )
    payload = {
        "inputs": {
            "baseline_output_dir": str(scenario_dir / "output_fixed"),
            "bike_lane_output_dir": str(bike_lane_dir),
            "sample_fraction": sample_fraction,
            "annual_days": annual_days,
            "analysis_period_years": period_years,
            "discount_rate": discount_rate,
            "facility_miles": facility_miles,
        },
        "scenario_health": {
            "baseline": fixed,
            "bike_lane": bike_lane,
            "warnings": _comparison_warnings(fixed, bike_lane),
        },
        "scenario_delta": {
            "vht_delta_hours": fixed["vht"] - bike_lane["vht"],
            "vmt_delta_miles": fixed["vmt"] - bike_lane["vmt"],
            "completed_trips_delta_scaled": fixed["completed_trips_scaled"] - bike_lane["completed_trips_scaled"],
            "stuck_trips_delta_scaled": fixed["stuck_scaled"] - bike_lane["stuck_scaled"],
            "mean_trip_minutes_delta": fixed["mean_trip_minutes"] - bike_lane["mean_trip_minutes"],
            "median_trip_minutes_delta": fixed["median_trip_minutes"] - bike_lane["median_trip_minutes"],
        },
        **result,
    }
    payload["bca_reliable"] = _bca_reliable(payload["scenario_health"]["warnings"])
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def scenario_comparison_rows(
    scenarios: dict[str, dict[str, Any]],
    *,
    reference_name: str,
    bca_by_scenario: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build flat comparison rows for scenario health, travel metrics, and BCA."""
    if reference_name not in scenarios:
        raise KeyError(f"Missing comparison reference scenario: {reference_name}")
    reference = scenarios[reference_name]
    bca_by_scenario = bca_by_scenario or {}
    rows: list[dict[str, Any]] = []
    for name, summary in scenarios.items():
        bca = bca_by_scenario.get(name, {})
        rows.append(
            {
                "scenario": name,
                "reference_scenario": reference_name,
                "output_dir": summary["output_dir"],
                "iteration": summary["iteration"],
                "departures_sample": summary["departures_sample"],
                "arrivals_sample": summary["arrivals_sample"],
                "completion_rate": summary["completion_rate"],
                "stuck_sample": summary["stuck_sample"],
                "vht": summary["vht"],
                "vmt": summary["vmt"],
                "mean_trip_minutes": summary["mean_trip_minutes"],
                "median_trip_minutes": summary["median_trip_minutes"],
                "vht_delta_vs_reference": summary["vht"] - reference["vht"],
                "vmt_delta_vs_reference": summary["vmt"] - reference["vmt"],
                "completed_trips_delta_vs_reference": summary["completed_trips_scaled"] - reference["completed_trips_scaled"],
                "stuck_trips_delta_vs_reference": summary["stuck_scaled"] - reference["stuck_scaled"],
                "annual_net_benefit_usd": bca.get("annual_benefits_usd", {}).get("net", bca.get("annual_benefits_usd", {}).get("total")),
                "discounted_net_benefit_usd": bca.get("net_benefit_usd"),
                "cost_usd": bca.get("cost_usd"),
                "benefit_cost_ratio": bca.get("benefit_cost_ratio"),
            }
        )
    return rows


def _write_scenario_comparison(cfg: Any, scenarios: dict[str, dict[str, Any]], bca_by_scenario: dict[str, dict[str, Any]]) -> None:
    rows = scenario_comparison_rows(scenarios, reference_name="fixed", bca_by_scenario=bca_by_scenario)
    csv_path = Path(cfg.data_processed) / "scenario_comparison.csv"
    json_path = Path(cfg.data_processed) / "scenario_comparison.json"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps({"reference_scenario": "fixed", "scenarios": rows}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(cfg: Any) -> None:
    """Read baseline/fixed MATSim outputs and write pothole BCA JSON."""
    scenario_dir = getattr(cfg, "scenario_dir", Path(cfg.project_root) / "scenarios" / "logan_square")
    baseline_dir = scenario_dir / "output_baseline"
    fixed_dir = scenario_dir / "output_fixed"
    pothole_links_csv = cfg.data_interim / "pothole_links.csv"
    output_json = cfg.data_processed / "pothole_bca.json"

    sample_fraction = float(cfg.scenario.sample_fraction)
    baseline = _scenario_summary(baseline_dir, sample_fraction)
    fixed = _scenario_summary(fixed_dir, sample_fraction)
    pothole_lengths = _read_pothole_link_lengths(pothole_links_csv)
    vmt_pothole_base = _read_pothole_link_vmt(baseline_dir, pothole_lengths, sample_fraction)
    total_potholes = _read_total_potholes(pothole_links_csv)

    pothole_cfg = cfg.interventions.get("pothole", {})
    annual_days = float(pothole_cfg.get("annual_days", 300.0))
    period_years = int(pothole_cfg.get("analysis_period_years", 1))
    discount_rate = float(pothole_cfg.get("discount_rate", 0.03))

    result = compute_bca(
        baseline["vht"],
        fixed["vht"],
        vmt_pothole_base,
        total_potholes,
        _coeff_dict(cfg),
        period_years,
        discount_rate,
        annual_days,
    )
    payload = {
        "inputs": {
            "baseline_output_dir": str(baseline_dir),
            "fixed_output_dir": str(fixed_dir),
            "sample_fraction": sample_fraction,
            "annual_days": annual_days,
            "analysis_period_years": period_years,
            "discount_rate": discount_rate,
            "total_potholes": total_potholes,
            "daily_vht_baseline": baseline["vht"],
            "daily_vht_fixed": fixed["vht"],
            "daily_vmt_baseline": baseline["vmt"],
            "daily_vmt_fixed": fixed["vmt"],
            "daily_vmt_pothole_links_baseline": vmt_pothole_base,
        },
        "scenario_health": {
            "baseline": baseline,
            "fixed": fixed,
            "warnings": _comparison_warnings(baseline, fixed),
        },
        "scenario_delta": {
            "vht_delta_hours": baseline["vht"] - fixed["vht"],
            "vmt_delta_miles": baseline["vmt"] - fixed["vmt"],
            "completed_trips_delta_scaled": baseline["completed_trips_scaled"] - fixed["completed_trips_scaled"],
            "stuck_trips_delta_scaled": baseline["stuck_scaled"] - fixed["stuck_scaled"],
            "mean_trip_minutes_delta": baseline["mean_trip_minutes"] - fixed["mean_trip_minutes"],
            "median_trip_minutes_delta": baseline["median_trip_minutes"] - fixed["median_trip_minutes"],
        },
        **result,
    }
    payload["bca_reliable"] = _bca_reliable(payload["scenario_health"]["warnings"])

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    bike_lane_payload = _write_bike_lane_bca(cfg, fixed)
    scenarios = {
        "pothole_baseline": baseline,
        "fixed": fixed,
    }
    default_output = scenario_dir / "output"
    if default_output.exists():
        try:
            scenarios["clean_output"] = _scenario_summary(default_output, sample_fraction)
        except FileNotFoundError:
            pass
    bike_lane_dir = scenario_dir / "output_bike_lane"
    if bike_lane_dir.exists():
        try:
            scenarios["bike_lane"] = _scenario_summary(bike_lane_dir, sample_fraction)
        except FileNotFoundError:
            pass
    try:
        from pipeline.scenario_builder import user_scenarios

        for spec in user_scenarios(cfg):
            output_dir = scenario_dir / f"output_{spec.id}"
            if output_dir.exists():
                try:
                    scenarios[spec.id] = _scenario_summary(output_dir, sample_fraction)
                except FileNotFoundError:
                    pass
    except ImportError:
        pass
    bca_by_scenario = {"fixed": payload}
    if bike_lane_payload is not None:
        bca_by_scenario["bike_lane"] = bike_lane_payload
    _write_scenario_comparison(cfg, scenarios, bca_by_scenario)

    bcr = payload["benefit_cost_ratio"]
    bcr_text = "undefined" if bcr is None else f"{bcr:.3f}"
    print(
        "s6 pothole BCA: "
        f"daily_VHT_delta={baseline['vht'] - fixed['vht']:.2f} hr, "
        f"baseline_completion={baseline['completion_rate']:.1%}, "
        f"fixed_completion={fixed['completion_rate']:.1%}, "
        f"stuck_delta={baseline['stuck_scaled'] - fixed['stuck_scaled']:.0f}, "
        f"pothole_link_VMT={vmt_pothole_base:.2f}, "
        f"annual_benefit=${payload['annual_benefits_usd']['total']:,.2f}, "
        f"discounted_benefit=${payload['discounted_benefits_usd']['total']:,.2f}, "
        f"cost=${payload['cost_usd']:,.2f}, BCR={bcr_text}, "
        f"net=${payload['net_benefit_usd']:,.2f}, "
        f"reliable={payload['bca_reliable']}"
    )
    if bike_lane_payload is not None:
        bike_bcr = bike_lane_payload["benefit_cost_ratio"]
        bike_bcr_text = "undefined" if bike_bcr is None else f"{bike_bcr:.3f}"
        print(
            "s6 bike-lane BCA: "
            f"facility_miles={bike_lane_payload['inputs']['facility_miles']:.2f}, "
            f"annual_net_benefit=${bike_lane_payload['annual_benefits_usd']['net']:,.2f}, "
            f"cost=${bike_lane_payload['cost_usd']:,.2f}, BCR={bike_bcr_text}, "
            f"net=${bike_lane_payload['net_benefit_usd']:,.2f}, "
            f"reliable={bike_lane_payload['bca_reliable']}"
        )

