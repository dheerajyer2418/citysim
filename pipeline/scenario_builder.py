"""User-drawn scenario builder and local run jobs."""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pipeline.config import CitySimConfig, load_config
from pipeline.s5_interventions import (
    NetworkLink,
    _apply_link_edits,
    _format_float,
    _read_network_links,
    _write_run_config,
    select_links_near_corridor,
)


PRESETS: dict[str, dict[str, Any]] = {
    "bike_lane": {
        "label": "Bike lane",
        "description": "Reallocates road space to a bike lane by lowering car capacity and slightly lowering car free-flow speed.",
        "capacity_factor": 0.75,
        "freespeed_factor": 0.95,
        "buffer_m": 45.0,
        "min_link_capacity": 600.0,
    },
    "road_diet": {
        "label": "Road diet",
        "description": "Models a lane reduction or calming project by substantially lowering car capacity and speed on the selected links.",
        "capacity_factor": 0.60,
        "freespeed_factor": 0.90,
        "buffer_m": 35.0,
        "min_link_capacity": 0.0,
    },
    "flow_improvement": {
        "label": "Traffic flow improvement",
        "description": "Models signal timing, turn-lane, or operational improvements by increasing car capacity and free-flow speed.",
        "capacity_factor": 1.20,
        "freespeed_factor": 1.05,
        "buffer_m": 35.0,
        "min_link_capacity": 0.0,
    },
    "add_potholes": {
        "label": "Add pothole damage",
        "description": "Models rough pavement or pothole damage by slowing cars on the selected links without changing road capacity.",
        "capacity_factor": 1.00,
        "freespeed_factor": 0.75,
        "buffer_m": 30.0,
        "min_link_capacity": 0.0,
    },
}

VARIABLE_HELP: dict[str, str] = {
    "scenario_name": "A readable name for this run. It appears later in the rebuilt visualization and comparison table.",
    "preset": "A starting template for how the selected street links should change.",
    "buffer_m": "How far from your drawn line the tool searches for road links. Use a larger buffer if the yellow highlight misses a street.",
    "min_link_capacity": "Ignores very small local links below this capacity. Use 0 to allow any road link.",
    "capacity_factor": "Multiplier applied to car capacity. 0.75 means cars get 25% less capacity; 1.20 means 20% more capacity.",
    "freespeed_factor": "Multiplier applied to free-flow speed. 0.75 means cars are 25% slower when uncongested.",
}

SCENARIO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    name: str
    type: str
    preset: str
    corridor_lonlat: list[list[float]]
    buffer_m: float
    min_link_capacity: float
    capacity_factor: float
    freespeed_factor: float
    baseline_output: str = "output_fixed"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class Job:
    job_id: str
    scenario_id: str
    status: str = "queued"
    stage: str = "queued"
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    config_path: str | None = None
    live_viz_path: str | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "stage": self.stage,
            "log_tail": self.log[-80:],
            "output_dir": self.output_dir,
            "config_path": self.config_path,
            "live_viz_path": self.live_viz_path,
            "error": self.error,
        }


class JobQueue:
    """Single-worker local job queue for long MATSim scenario runs."""

    def __init__(self, cfg: CitySimConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._active_job_id: str | None = None

    def start(self, scenario_id: str) -> Job:
        with self._lock:
            if self._active_job_id is not None:
                job = Job(job_id=uuid4().hex, scenario_id=scenario_id, status="failed", stage="busy")
                job.error = f"Another job is already running: {self._active_job_id}"
                self._jobs[job.job_id] = job
                return job
            job = Job(job_id=uuid4().hex, scenario_id=scenario_id)
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id

        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: Job) -> None:
        try:
            job.status = "running"
            spec = load_scenario(self.cfg, job.scenario_id)
            applied = apply_scenario(self.cfg, spec)
            job.output_dir = str(applied["output_dir"])
            job.config_path = str(applied["config_path"])

            job.stage = "matsim"
            _run_command(
                [
                    str(_java_path(self.cfg)),
                    "-Xmx8g",
                    "-cp",
                    str(self.cfg.project_root / "matsim" / "build" / "install" / "citysim-matsim" / "lib" / "*"),
                    "citysim.RunCitySim",
                    applied["config_path"].name,
                ],
                cwd=self.cfg.project_root / "scenarios" / "logan_square",
                log=job.log,
            )

            job.stage = "diagnostics"
            env = os.environ.copy()
            env["CITYSIM_MATSIM_OUTPUT_DIR"] = applied["output_dir"].name
            env["CITYSIM_DIAGNOSTICS_SUFFIX"] = spec.id
            _run_command([str(_python_path(self.cfg)), "cli.py", "run", "--stage", "diag"], cwd=self.cfg.project_root, log=job.log, env=env)

            job.stage = "monetization"
            _run_command([str(_python_path(self.cfg)), "cli.py", "run", "--stage", "s6"], cwd=self.cfg.project_root, log=job.log)

            job.stage = "visualization"
            _run_command([str(_python_path(self.cfg)), "viz\\build_live_viz.py"], cwd=self.cfg.project_root, log=job.log)
            job.live_viz_path = str(self.cfg.project_root / "scenarios" / "logan_square" / "output" / "live_traffic.html")
            job.stage = "complete"
            job.status = "succeeded"
        except Exception as exc:  # pragma: no cover - exercised by manual long runs
            job.status = "failed"
            job.error = str(exc)
            job.log.append(f"ERROR: {exc}")
        finally:
            with self._lock:
                if self._active_job_id == job.job_id:
                    self._active_job_id = None


def make_spec(payload: dict[str, Any]) -> ScenarioSpec:
    preset_name = str(payload.get("preset", "bike_lane"))
    if preset_name not in PRESETS:
        raise ValueError(f"Unknown preset: {preset_name}")
    preset = PRESETS[preset_name]
    scenario_id = str(payload.get("id") or _default_scenario_id()).strip()
    if not SCENARIO_ID_PATTERN.fullmatch(scenario_id):
        raise ValueError("Scenario id must be 3-64 letters, numbers, underscores, or hyphens.")

    corridor = payload.get("corridor_lonlat")
    if not isinstance(corridor, list) or len(corridor) < 2:
        raise ValueError("Draw at least two corridor points.")
    corridor_lonlat = []
    for point in corridor:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("Each corridor point must be [lon, lat].")
        lon = float(point[0])
        lat = float(point[1])
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise ValueError("Corridor coordinates are outside lon/lat bounds.")
        corridor_lonlat.append([lon, lat])

    return ScenarioSpec(
        id=scenario_id,
        name=str(payload.get("name") or preset["label"]),
        type="corridor",
        preset=preset_name,
        corridor_lonlat=corridor_lonlat,
        buffer_m=float(payload.get("buffer_m", preset["buffer_m"])),
        min_link_capacity=float(payload.get("min_link_capacity", preset["min_link_capacity"])),
        capacity_factor=float(payload.get("capacity_factor", preset["capacity_factor"])),
        freespeed_factor=float(payload.get("freespeed_factor", preset["freespeed_factor"])),
        baseline_output=str(payload.get("baseline_output", "output_fixed")),
        created_at=str(payload.get("created_at") or datetime.now().isoformat(timespec="seconds")),
    )


def preview_scenario(cfg: CitySimConfig, spec: ScenarioSpec) -> dict[str, Any]:
    links = _scenario_links(cfg)
    selected = select_links_near_corridor(links, spec.corridor_lonlat, cfg.crs, spec.buffer_m, spec.min_link_capacity)
    return {
        "scenario_id": spec.id,
        "selected_link_count": len(selected),
        "facility_miles": _facility_miles(selected),
        "capacity_factor": spec.capacity_factor,
        "freespeed_factor": spec.freespeed_factor,
        "selected_links": [link.link_id for link in selected],
        "highlight_paths": _highlight_paths(selected, cfg.crs),
        "warnings": [] if selected else ["No links matched this corridor. Try a wider buffer or draw closer to a street."],
    }


def map_data(cfg: CitySimConfig) -> dict[str, Any]:
    links = _scenario_links(cfg)
    paths = _highlight_paths(links, cfg.crs)
    roads = []
    all_points = []
    for link, path_row in zip(links, paths):
        path = path_row["path"]
        roads.append({"link_id": link.link_id, "path": path, "capacity": link.capacity or 0.0})
        all_points.extend(path)
    center = [-87.7105, 41.9172]
    if all_points:
        center = [
            round(sum(point[0] for point in all_points) / len(all_points), 6),
            round(sum(point[1] for point in all_points) / len(all_points), 6),
        ]
    return {
        "center": center,
        "roads": roads,
        "presets": PRESETS,
        "variable_help": VARIABLE_HELP,
        "user_scenarios": [asdict(spec) for spec in user_scenarios(cfg)],
    }


def save_scenario(cfg: CitySimConfig, spec: ScenarioSpec) -> dict[str, Any]:
    scenario_dir = _user_scenario_dir(cfg, spec.id)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / "scenario.json"
    if path.exists():
        raise FileExistsError(f"Scenario already exists: {spec.id}")
    path.write_text(json.dumps(asdict(spec), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"scenario_id": spec.id, "path": str(path), "spec": asdict(spec)}


def load_scenario(cfg: CitySimConfig, scenario_id: str) -> ScenarioSpec:
    path = _user_scenario_dir(cfg, scenario_id) / "scenario.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown user scenario: {scenario_id}")
    return make_spec(json.loads(path.read_text(encoding="utf-8")))


def apply_scenario(cfg: CitySimConfig, spec: ScenarioSpec) -> dict[str, Path | int]:
    links = _scenario_links(cfg)
    selected = select_links_near_corridor(links, spec.corridor_lonlat, cfg.crs, spec.buffer_m, spec.min_link_capacity)
    if not selected:
        raise ValueError("No links matched this scenario corridor.")
    scenario_dir = cfg.project_root / "scenarios" / "logan_square"
    user_dir = _user_scenario_dir(cfg, spec.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    selected_csv = user_dir / "selected_links.csv"
    _write_selected_links(selected_csv, selected, spec)

    network_name = f"network_{spec.id}.xml.gz"
    output_name = f"output_{spec.id}"
    config_path = scenario_dir / f"config_{spec.id}.xml"
    edits = {link.link_id: {"capacity_factor": spec.capacity_factor, "freespeed_factor": spec.freespeed_factor} for link in selected}
    changed = _apply_link_edits(scenario_dir / "network.xml.gz", scenario_dir / network_name, edits)
    _write_run_config(scenario_dir / "config.xml", config_path, network_name, output_name)
    _write_manifest(cfg)
    return {
        "changed_links": changed,
        "selected_csv": selected_csv,
        "network_path": scenario_dir / network_name,
        "config_path": config_path,
        "output_dir": scenario_dir / output_name,
    }


def user_scenarios(cfg: CitySimConfig) -> list[ScenarioSpec]:
    root = cfg.data_interim / "user_scenarios"
    specs: list[ScenarioSpec] = []
    if not root.exists():
        return specs
    for path in sorted(root.glob("*/scenario.json")):
        try:
            specs.append(make_spec(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return specs


def _write_manifest(cfg: CitySimConfig) -> None:
    manifest = [
        {
            "id": spec.id,
            "name": spec.name,
            "preset": spec.preset,
            "output_dir": f"output_{spec.id}",
            "config": f"config_{spec.id}.xml",
        }
        for spec in user_scenarios(cfg)
    ]
    path = cfg.data_interim / "user_scenarios" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scenarios": manifest}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _scenario_links(cfg: CitySimConfig) -> list[NetworkLink]:
    return _read_network_links(cfg.data_interim / "network_links.gpkg")


def _facility_miles(links: list[NetworkLink]) -> float:
    return sum(link.length_m for link in links) / 1609.344 / 2.0


def _highlight_paths(links: list[NetworkLink], projected_crs: str) -> list[dict[str, Any]]:
    from pyproj import Transformer

    transformer = Transformer.from_crs(projected_crs, "EPSG:4326", always_xy=True)
    paths: list[dict[str, Any]] = []
    for link in links:
        coords = list(link.geometry.coords)
        path = []
        for x, y, *_ in coords:
            lon, lat = transformer.transform(float(x), float(y))
            path.append([round(lon, 6), round(lat, 6)])
        paths.append({"link_id": link.link_id, "path": path})
    return paths


def _write_selected_links(path: Path, selected: list[NetworkLink], spec: ScenarioSpec) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["link_id", "length_m", "capacity_factor", "freespeed_factor"])
        writer.writeheader()
        for link in selected:
            writer.writerow(
                {
                    "link_id": link.link_id,
                    "length_m": _format_float(link.length_m),
                    "capacity_factor": _format_float(spec.capacity_factor),
                    "freespeed_factor": _format_float(spec.freespeed_factor),
                }
            )


def _user_scenario_dir(cfg: CitySimConfig, scenario_id: str) -> Path:
    return cfg.data_interim / "user_scenarios" / scenario_id


def _default_scenario_id() -> str:
    return "user_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]


def _python_path(cfg: CitySimConfig) -> Path:
    venv_python = cfg.project_root / ".venv" / "Scripts" / "python.exe"
    return venv_python if venv_python.exists() else Path("python")


def _java_path(cfg: CitySimConfig) -> Path:
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        return Path(java_home) / "bin" / "java.exe"
    bundled = cfg.project_root / "tools" / "jdk-17.0.19+10" / "bin" / "java.exe"
    return bundled if bundled.exists() else Path("java")


def _run_command(command: list[str], *, cwd: Path, log: list[str], env: dict[str, str] | None = None) -> None:
    log.append(f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        log.append(line.rstrip())
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def default_job_queue() -> JobQueue:
    return JobQueue(load_config())
