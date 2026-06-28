"""Typed configuration loader for params.yaml."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class FhwaCoefficients:
    value_of_time_usd_per_hr: float
    emissions_usd_per_ton: dict[str, float]
    crash_cost_usd: dict[str, float]
    vehicle_damage_usd_per_vmt: float


@dataclass(frozen=True)
class CitySimConfig:
    project_root: Path
    crs: str
    boundary: BoundaryConfig
    sources: dict[str, Any]
    scenario: ScenarioConfig
    fhwa_coefficients: FhwaCoefficients

    @property
    def data_raw(self) -> Path:
        return self.project_root / "data" / "raw"

    @property
    def data_interim(self) -> Path:
        return self.project_root / "data" / "interim"

    @property
    def data_processed(self) -> Path:
        return self.project_root / "data" / "processed"


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


def load_config(path: str | Path = DEFAULT_PARAMS_PATH) -> CitySimConfig:
    params_path = Path(path)
    data = _read_yaml(params_path)

    boundary = data["boundary"]
    scenario = data["scenario"]
    fhwa = data["fhwa_coefficients"]

    return CitySimConfig(
        project_root=params_path.resolve().parent,
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
        ),
        fhwa_coefficients=FhwaCoefficients(
            value_of_time_usd_per_hr=float(fhwa["value_of_time_usd_per_hr"]),
            emissions_usd_per_ton=dict(fhwa["emissions_usd_per_ton"]),
            crash_cost_usd=dict(fhwa["crash_cost_usd"]),
            vehicle_damage_usd_per_vmt=float(fhwa["vehicle_damage_usd_per_vmt"]),
        ),
    )
