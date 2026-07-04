"""MATSim output diagnostics for model-health and bottleneck triage."""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


TOP_N = 20
MIN_TUNING_STUCK = 20
MIN_TUNING_STUCK_PER_ENTERED = 0.10
HIGH_VOLUME_CAPACITY_RATIO = 1.0


def find_last_iteration_dir(output_dir: Path) -> tuple[int, Path]:
    iters_dir = output_dir / "ITERS"
    candidates: list[tuple[int, Path]] = []
    if iters_dir.exists():
        for child in iters_dir.iterdir():
            match = re.fullmatch(r"it\.(\d+)", child.name)
            if child.is_dir() and match:
                candidates.append((int(match.group(1)), child))
    if not candidates:
        raise FileNotFoundError(f"Could not find MATSim iteration outputs under {output_dir}")
    return max(candidates, key=lambda item: item[0])


def read_link_table(output_dir: Path) -> dict[str, dict[str, float]]:
    links_path = output_dir / "output_links.csv.gz"
    if not links_path.exists():
        return {}

    links: dict[str, dict[str, float]] = {}
    with gzip.open(links_path, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            link_id = str(row.get("link", ""))
            if not link_id:
                continue
            capacity = float(row.get("capacity") or 0.0)
            volume = float(row.get("vol_car") or 0.0)
            length = float(row.get("length") or 0.0)
            freespeed = float(row.get("freespeed") or 0.0)
            links[link_id] = {
                "capacity": capacity,
                "vol_car": volume,
                "length_m": length,
                "freespeed_mps": freespeed,
                "volume_capacity_ratio": volume / capacity if capacity > 0.0 else 0.0,
            }
    return links


def event_diagnostics(events_path: Path, link_stats: dict[str, dict[str, float]], top_n: int = TOP_N) -> dict[str, Any]:
    event_counts: Counter[str] = Counter()
    stuck_by_link: Counter[str] = Counter()
    entered_by_link: Counter[str] = Counter()
    abort_by_link: Counter[str] = Counter()

    with gzip.open(events_path, "rb") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag != "event":
                element.clear()
                continue
            event_type = str(element.get("type"))
            link_id = element.get("link")
            event_counts[event_type] += 1
            if event_type == "stuckAndAbort" and link_id:
                stuck_by_link[str(link_id)] += 1
            elif event_type == "entered link" and link_id:
                entered_by_link[str(link_id)] += 1
            elif event_type == "vehicle aborts" and link_id:
                abort_by_link[str(link_id)] += 1
            element.clear()

    departures = event_counts["departure"]
    arrivals = event_counts["arrival"]
    stuck = event_counts["stuckAndAbort"]
    denominator = departures if departures > 0 else arrivals + stuck

    return {
        "event_counts": dict(event_counts),
        "summary": {
            "departures": departures,
            "arrivals": arrivals,
            "stuck": stuck,
            "vehicle_aborts": event_counts["vehicle aborts"],
            "completion_rate": arrivals / denominator if denominator else 0.0,
            "stuck_rate": stuck / denominator if denominator else 0.0,
        },
        "top_stuck_links": _ranked_links(stuck_by_link, entered_by_link, abort_by_link, link_stats, top_n),
        "top_volume_links": _top_volume_links(link_stats, top_n),
    }


def _ranked_links(
    stuck_by_link: Counter[str],
    entered_by_link: Counter[str],
    abort_by_link: Counter[str],
    link_stats: dict[str, dict[str, float]],
    top_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for link_id, stuck_count in stuck_by_link.most_common(top_n):
        stats = link_stats.get(link_id, {})
        entered = entered_by_link.get(link_id, 0)
        row = {
            "link_id": link_id,
            "stuck": stuck_count,
            "entered": entered,
            "vehicle_aborts": abort_by_link.get(link_id, 0),
            "stuck_per_entered": stuck_count / entered if entered > 0 else None,
            **stats,
        }
        rows.append(row)
    return rows


def _top_volume_links(link_stats: dict[str, dict[str, float]], top_n: int) -> list[dict[str, Any]]:
    rows = [
        {"link_id": link_id, **stats}
        for link_id, stats in link_stats.items()
    ]
    rows.sort(key=lambda row: (float(row.get("vol_car", 0.0)), float(row.get("volume_capacity_ratio", 0.0))), reverse=True)
    return rows[:top_n]


def tuning_recommendations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert top stuck-link diagnostics into reviewable network-tuning candidates."""
    recommendations: list[dict[str, Any]] = []
    for row in rows:
        stuck = int(row.get("stuck") or 0)
        entered = int(row.get("entered") or 0)
        stuck_rate = row.get("stuck_per_entered")
        volume_capacity = float(row.get("volume_capacity_ratio") or 0.0)
        if stuck < MIN_TUNING_STUCK:
            continue
        if stuck_rate is None or float(stuck_rate) < MIN_TUNING_STUCK_PER_ENTERED:
            continue

        capacity_factor = 1.0
        freespeed_factor = 1.0
        reasons = []
        if volume_capacity >= HIGH_VOLUME_CAPACITY_RATIO:
            capacity_factor = 1.25
            reasons.append("volume/capacity >= 1.0")
        else:
            capacity_factor = 1.10
            reasons.append("high stuck share below nominal capacity")
        if float(row.get("freespeed_mps") or 0.0) < 8.0:
            freespeed_factor = 1.10
            reasons.append("low freespeed on stuck link")

        recommendations.append(
            {
                "link_id": row["link_id"],
                "stuck": stuck,
                "entered": entered,
                "stuck_per_entered": stuck_rate,
                "volume_capacity_ratio": volume_capacity,
                "capacity_factor": capacity_factor,
                "freespeed_factor": freespeed_factor,
                "reason": "; ".join(reasons),
            }
        )
    return recommendations


def write_top_stuck_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "link_id",
        "stuck",
        "entered",
        "vehicle_aborts",
        "stuck_per_entered",
        "vol_car",
        "capacity",
        "volume_capacity_ratio",
        "length_m",
        "freespeed_mps",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_tuning_recommendations_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "link_id",
        "stuck",
        "entered",
        "stuck_per_entered",
        "volume_capacity_ratio",
        "capacity_factor",
        "freespeed_factor",
        "reason",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(cfg: Any) -> None:
    scenario_dir = Path(cfg.project_root) / "scenarios" / "logan_square"
    output_name = os.environ.get("CITYSIM_MATSIM_OUTPUT_DIR", "output")
    output_dir = Path(output_name)
    if not output_dir.is_absolute():
        output_dir = scenario_dir / output_dir
    iteration, iteration_dir = find_last_iteration_dir(output_dir)
    events_path = iteration_dir / f"{iteration}.events.xml.gz"
    if not events_path.exists():
        raise FileNotFoundError(f"Missing MATSim events file: {events_path}")

    diagnostics = {
        "output_dir": str(output_dir),
        "iteration": iteration,
        **event_diagnostics(events_path, read_link_table(output_dir), TOP_N),
    }

    suffix = os.environ.get("CITYSIM_DIAGNOSTICS_SUFFIX", "").strip()
    if suffix and not suffix.startswith("_"):
        suffix = f"_{suffix}"
    output_json = Path(cfg.data_processed) / f"sim_diagnostics{suffix}.json"
    output_csv = Path(cfg.data_processed) / f"sim_top_stuck_links{suffix}.csv"
    tuning_csv = Path(cfg.data_processed) / f"sim_tuning_recommendations{suffix}.csv"
    output_json.parent.mkdir(parents=True, exist_ok=True)
    diagnostics["tuning_recommendations"] = tuning_recommendations(diagnostics["top_stuck_links"])
    output_json.write_text(json.dumps(diagnostics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_top_stuck_csv(diagnostics["top_stuck_links"], output_csv)
    write_tuning_recommendations_csv(diagnostics["tuning_recommendations"], tuning_csv)

    summary = diagnostics["summary"]
    top_link = diagnostics["top_stuck_links"][0]["link_id"] if diagnostics["top_stuck_links"] else "none"
    print(
        "diagnostics complete: "
        f"iteration={iteration}; "
        f"departures={summary['departures']}; "
        f"arrivals={summary['arrivals']}; "
        f"stuck={summary['stuck']}; "
        f"completion_rate={summary['completion_rate']:.1%}; "
        f"top_stuck_link={top_link}; "
        f"wrote={output_json}, {output_csv}, {tuning_csv}"
    )
