"""Build and run top needs-index road-diet scenarios."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import CitySimConfig, load_config
from pipeline.scenario_builder import _java_path, apply_scenario, make_spec, save_scenario


@dataclass(frozen=True)
class SelectedCorridor:
    rank: int
    link_id: str
    street: str
    need_score: float
    scenario_id: str
    scenario_name: str
    path: list[list[float]]


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return re.sub(r"-+", "-", text).strip("-") or "street"


def scenario_id_for(rank: int, street: str) -> str:
    prefix = f"needs{rank}_"
    max_slug_len = 64 - len(prefix)
    return prefix + slug(street)[:max_slug_len].strip("-")


def downsample_points(points: list[list[float]], target: int = 50) -> list[list[float]]:
    if len(points) <= 60:
        return points
    if target < 2:
        raise ValueError("target must be at least 2")
    selected: list[list[float]] = []
    last_index = len(points) - 1
    for i in range(target):
        index = round(i * last_index / (target - 1))
        selected.append(points[index])
    return selected


def load_need_scores(path: Path) -> dict[str, float]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {
            str(row["link_id"]): float(row["need_score"])
            for row in reader
            if row.get("link_id") and row.get("need_score") not in (None, "")
        }


def load_link_names(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(link_id): str(name) for link_id, name in data.items()}


def load_link_paths(path: Path) -> dict[str, list[list[float]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    paths: dict[str, list[list[float]]] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        link_id = props.get("link_id")
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if not link_id:
            continue
        if geom.get("type") == "MultiLineString":
            coords = coords[0] if coords else []
        if geom.get("type") not in {"LineString", "MultiLineString"} or len(coords) < 2:
            continue
        paths[str(link_id)] = [[float(point[0]), float(point[1])] for point in coords]
    return paths


def select_top_corridors(
    need_scores: dict[str, float],
    link_names: dict[str, str],
    link_paths: dict[str, list[list[float]]],
    count: int = 5,
) -> list[SelectedCorridor]:
    selected: list[SelectedCorridor] = []
    seen_streets: set[str] = set()
    ranked_links = sorted(need_scores.items(), key=lambda item: item[1], reverse=True)
    for link_id, need_score in ranked_links:
        street = link_names.get(link_id, "").strip()
        if not street or street == "Unnamed" or street in seen_streets:
            continue
        path = link_paths.get(link_id)
        if not path:
            continue
        rank = len(selected) + 1
        selected.append(
            SelectedCorridor(
                rank=rank,
                link_id=link_id,
                street=street,
                need_score=need_score,
                scenario_id=scenario_id_for(rank, street),
                scenario_name=f"{street} road diet",
                path=downsample_points(path),
            )
        )
        seen_streets.add(street)
        if len(selected) == count:
            break
    return selected


def write_selected_manifest(cfg: CitySimConfig, corridors: list[SelectedCorridor]) -> None:
    manifest = {
        "scenarios": [
            {
                "config": f"config_{corridor.scenario_id}.xml",
                "id": corridor.scenario_id,
                "name": corridor.scenario_name,
                "output_dir": f"output_{corridor.scenario_id}",
                "preset": "road_diet",
            }
            for corridor in corridors
        ]
    }
    path = cfg.data_interim / "user_scenarios" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_matsim(cfg: CitySimConfig, scenario_id: str) -> bool:
    command = [
        str(_java_path(cfg)),
        "-Xmx8g",
        "-cp",
        str(cfg.project_root / "matsim" / "build" / "install" / "citysim-matsim" / "lib" / "*"),
        "citysim.RunCitySim",
        f"config_{scenario_id}.xml",
    ]
    print(f"Running MATSim for {scenario_id}...")
    try:
        subprocess.run(
            command,
            cwd=str(cfg.scenario_dir),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"MATSim failed for {scenario_id}: exit code {exc.returncode}")
        return False
    print(f"Completed MATSim for {scenario_id}.")
    return True


def build_scenarios(cfg: CitySimConfig) -> list[SelectedCorridor]:
    need_scores = load_need_scores(cfg.data_processed / "needs_index.csv")
    link_names = load_link_names(cfg.data_interim / "link_names.json")
    link_paths = load_link_paths(cfg.data_processed / "needs_index.geojson")
    corridors = select_top_corridors(need_scores, link_names, link_paths)
    if len(corridors) < 5:
        raise RuntimeError(f"Only selected {len(corridors)} corridors; expected 5.")

    for corridor in corridors:
        payload = {
            "id": corridor.scenario_id,
            "name": corridor.scenario_name,
            "preset": "road_diet",
            "corridor_lonlat": corridor.path,
        }
        spec = make_spec(payload)
        try:
            save_scenario(cfg, spec)
            print(f"Saved scenario {spec.id}: {spec.name}")
        except FileExistsError:
            print(f"Scenario {spec.id} already exists; reusing scenario.json")
        applied = apply_scenario(cfg, spec)
        print(
            f"Applied {spec.id}: changed_links={applied['changed_links']} "
            f"config={applied['config_path']}"
        )

    write_selected_manifest(cfg, corridors)
    return corridors


def print_summary(cfg: CitySimConfig, corridors: list[SelectedCorridor], run_results: dict[str, bool]) -> None:
    print("\nFinal summary")
    for corridor in corridors:
        iters = cfg.scenario_dir / f"output_{corridor.scenario_id}" / "ITERS"
        status = "exists" if iters.exists() else "missing"
        run_status = "ok" if run_results.get(corridor.scenario_id) else "failed"
        print(
            f"{corridor.rank}. {corridor.street} | {corridor.scenario_id} | "
            f"need_score={corridor.need_score:.3f} | MATSim={run_status} | ITERS={status}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--area", help="configured area slug to use")
    args = parser.parse_args()
    cfg = load_config(area=args.area)
    corridors = build_scenarios(cfg)
    run_results: dict[str, bool] = {}
    for corridor in corridors:
        run_results[corridor.scenario_id] = run_matsim(cfg, corridor.scenario_id)
    write_selected_manifest(cfg, corridors)
    print_summary(cfg, corridors, run_results)
    if not all(run_results.values()):
        failed = [scenario_id for scenario_id, ok in run_results.items() if not ok]
        print(f"Failed MATSim scenarios: {', '.join(failed)}")


if __name__ == "__main__":
    main()

