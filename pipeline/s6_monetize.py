"""Stage s6: pothole benefit-cost monetization."""

from __future__ import annotations

import csv
import gzip
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


METERS_PER_MILE = 1609.344


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
        reader = csv.DictReader(handle)
        for row in reader:
            trav_time = row.get("trav_time")
            distance = row.get("distance")
            if trav_time:
                vht += parse_matsim_time_hours(trav_time)
            if distance not in (None, ""):
                vmt += float(distance) / METERS_PER_MILE
    scale = 1.0 / float(sample_fraction)
    return {"iteration": float(iteration), "vht": vht * scale, "vmt": vmt * scale}


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


def run(cfg: Any) -> None:
    """Read baseline/fixed MATSim outputs and write pothole BCA JSON."""
    project_root = Path(cfg.project_root)
    scenario_dir = project_root / "scenarios" / "logan_square"
    baseline_dir = scenario_dir / "output_baseline"
    fixed_dir = scenario_dir / "output_fixed"
    pothole_links_csv = cfg.data_interim / "pothole_links.csv"
    output_json = cfg.data_processed / "pothole_bca.json"

    sample_fraction = float(cfg.scenario.sample_fraction)
    baseline = _read_legs_totals(baseline_dir, sample_fraction)
    fixed = _read_legs_totals(fixed_dir, sample_fraction)
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
        **result,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    bcr = payload["benefit_cost_ratio"]
    bcr_text = "undefined" if bcr is None else f"{bcr:.3f}"
    print(
        "s6 pothole BCA: "
        f"daily_VHT_delta={baseline['vht'] - fixed['vht']:.2f} hr, "
        f"pothole_link_VMT={vmt_pothole_base:.2f}, "
        f"annual_benefit=${payload['annual_benefits_usd']['total']:,.2f}, "
        f"discounted_benefit=${payload['discounted_benefits_usd']['total']:,.2f}, "
        f"cost=${payload['cost_usd']:,.2f}, BCR={bcr_text}, "
        f"net=${payload['net_benefit_usd']:,.2f}"
    )
