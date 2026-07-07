"""Typed configuration loader for params.yaml."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARAMS_PATH = PROJECT_ROOT / "params.yaml"


@dataclass(frozen=True)
class BoundaryConfig:
    community_area_id: int
    name: str
    buffer_m: float


@dataclass(frozen=True)
class ScenarioConfig:
    sample_fraction: float
    iterations: int
    departure_jitter_std_seconds: float = 0.0


@dataclass(frozen=True)
class FhwaCoefficients:
    value_of_time_usd_per_hr: float
    emissions_usd_per_ton: dict[str, float]
    crash_cost_usd: dict[str, float]
    vehicle_damage_usd_per_vmt: float
    vehicle_operating_cost_usd_per_mile: float = 0.0
    external_highway_use_cost_usd_per_vmt: float = 0.0


@dataclass(frozen=True)
class CitySimConfig:
    project_root: Path
    area_slug: str
    area_name: str
    crs: str
    boundary: BoundaryConfig
    sources: dict[str, Any]
    scenario: ScenarioConfig
    fhwa_coefficients: FhwaCoefficients
    coefficient_sources: dict[str, Any]
    interventions: dict[str, Any]

    @property
    def data_raw(self) -> Path:
        return self.project_root / "data" / "raw"

    @property
    def data_interim(self) -> Path:
        if self.area_slug != "logan_square":
            return self.project_root / "data" / "interim" / self.area_slug
        return self.project_root / "data" / "interim"

    @property
    def data_processed(self) -> Path:
        if self.area_slug != "logan_square":
            return self.project_root / "data" / "processed" / self.area_slug
        return self.project_root / "data" / "processed"

    @property
    def scenario_dir(self) -> Path:
        return self.project_root / "scenarios" / self.area_slug

    @property
    def boundary_path(self) -> Path:
        return self.data_interim / f"{self.area_slug}_boundary.gpkg"

    @property
    def taz_path(self) -> Path:
        return self.data_interim / f"{self.area_slug}_taz.gpkg"

    @property
    def network_links_path(self) -> Path:
        return self.data_interim / "network_links.gpkg"


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load params.yaml. Use environment.yml.") from exc

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected top-level mapping in {path}")
    return data


def _validate_coefficient_sources(data: dict[str, Any]) -> None:
    sources = data.get("coefficient_sources", {})
    if not sources or not sources.get("required", False):
        return
    required = [
        "value_of_time_usd_per_hr",
        "vehicle_operating_cost_usd_per_mile",
        "external_highway_use_cost_usd_per_vmt",
        "vehicle_damage_usd_per_vmt",
        "emissions_usd_per_ton.co2",
        "emissions_usd_per_ton.nox",
        "emissions_usd_per_ton.pm25",
        "crash_cost_usd.fatal",
        "crash_cost_usd.injury",
        "crash_cost_usd.pdo",
        "interventions.pothole.repair_cost_usd_per_pothole",
        "interventions.pothole.extra_damage_usd_per_vmt",
        "interventions.bike_lane.benefits.facility_value_usd_per_cycling_mile",
        "interventions.bike_lane.benefits.health_benefit_usd_per_induced_trip",
        "interventions.bike_lane.benefits.capital_cost_usd_per_lane_mile",
    ]
    values = sources.get("values", {})
    missing = [
        key
        for key in required
        if not isinstance(values.get(key), dict) or not values[key].get("source") or not values[key].get("url")
    ]
    if missing:
        raise ValueError(f"Missing coefficient source metadata for: {', '.join(missing)}")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _area_data(data: dict[str, Any], area: str | None) -> tuple[str, dict[str, Any]]:
    selected = area or os.getenv("CITYSIM_AREA") or str(data.get("active_area", "logan_square"))
    areas = data.get("areas", {})
    if not areas:
        return selected, data
    if selected not in areas:
        raise ValueError(f"Unknown CitySim area {selected!r}. Available areas: {', '.join(sorted(areas))}")

    area_cfg = dict(areas[selected])
    merged = _deep_merge(data, area_cfg)
    merged.pop("areas", None)
    merged["active_area"] = selected
    return selected, merged


def load_config(path: str | Path = DEFAULT_PARAMS_PATH, *, area: str | None = None) -> CitySimConfig:
    params_path = Path(path)
    raw = _read_yaml(params_path)
    selected_area, data = _area_data(raw, area)
    _validate_coefficient_sources(data)

    boundary = data["boundary"]
    scenario = data["scenario"]
    fhwa = data["fhwa_coefficients"]

    return CitySimConfig(
        project_root=params_path.resolve().parent,
        area_slug=str(data.get("scenario_slug", selected_area)),
        area_name=str(boundary["name"]),
        crs=str(data["crs"]),
        boundary=BoundaryConfig(
            community_area_id=int(boundary["community_area_id"]),
            name=str(boundary["name"]),
            buffer_m=float(boundary["buffer_m"]),
        ),
        sources=dict(data["sources"]),
        scenario=ScenarioConfig(
            sample_fraction=float(scenario["sample_fraction"]),
            iterations=int(scenario["iterations"]),
            departure_jitter_std_seconds=float(scenario.get("departure_jitter_std_seconds", 0.0)),
        ),
        fhwa_coefficients=FhwaCoefficients(
            value_of_time_usd_per_hr=float(fhwa["value_of_time_usd_per_hr"]),
            emissions_usd_per_ton=dict(fhwa["emissions_usd_per_ton"]),
            crash_cost_usd=dict(fhwa["crash_cost_usd"]),
            vehicle_damage_usd_per_vmt=float(fhwa["vehicle_damage_usd_per_vmt"]),
            vehicle_operating_cost_usd_per_mile=float(fhwa.get("vehicle_operating_cost_usd_per_mile", 0.0)),
            external_highway_use_cost_usd_per_vmt=float(fhwa.get("external_highway_use_cost_usd_per_vmt", 0.0)),
        ),
        coefficient_sources=dict(data.get("coefficient_sources", {})),
        interventions=dict(data.get("interventions", {})),
    )
