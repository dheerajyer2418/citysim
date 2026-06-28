"""Command-line orchestrator for the CitySim scaffold."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipeline import (
    cmap_cordon_demand,
    cmap_demand,
    s0_boundary,
    s1_network,
    s2_demand,
    s3_transit,
    s4_calibrate,
    s5_interventions,
    s6_monetize,
)
from pipeline.config import load_config


@dataclass(frozen=True)
class Stage:
    name: str
    description: str
    runner: Callable[[Any], None]


STAGES: dict[str, Stage] = {
    "s0": Stage("s0", "Boundary and CMAP TAZ preprocessing", s0_boundary.run),
    "s1": Stage("s1", "OSM network clipping and MATSim network export", s1_network.run),
    "s2": Stage("s2", "Demand synthesis and MATSim plans export", s2_demand.run),
    "s2c": Stage("s2c", "CMAP all-purpose trip-roster demand export", cmap_demand.run),
    "s2d": Stage("s2d", "CMAP cordon demand and final plans export", cmap_cordon_demand.run),
    "s3": Stage("s3", "GTFS transit schedule and vehicle export", s3_transit.run),
    "s4": Stage("s4", "Counts calibration and GEH validation", s4_calibrate.run),
    "s5": Stage("s5", "Intervention edit-layer generation", s5_interventions.run),
    "s6": Stage("s6", "Cost-benefit monetization", s6_monetize.run),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="citysim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one stage or all stages")
    run_parser.add_argument(
        "--stage",
        choices=tuple(STAGES.keys()),
        help="pipeline stage to run; omit to run s0 through s6",
    )
    return parser


def run_command(args: argparse.Namespace) -> None:
    cfg = load_config()
    selected = [args.stage] if args.stage else list(STAGES.keys())
    for stage_name in selected:
        stage = STAGES[stage_name]
        print(f"[{stage.name}] {stage.description}")
        stage.runner(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        run_command(args)
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
