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
    s7_needs_index,
    sim_diagnostics,
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
    "s2pt": Stage("s2pt", "CMAP internal transit rider plans export", cmap_demand.run_pt),
    "s3": Stage("s3", "GTFS transit schedule and vehicle export", s3_transit.run),
    "s4": Stage("s4", "Counts calibration and GEH validation", s4_calibrate.run),
    "s5": Stage("s5", "Intervention edit-layer generation", s5_interventions.run),
    "s6": Stage("s6", "Cost-benefit monetization", s6_monetize.run),
    "s7": Stage("s7", "Needs-priority index (safety/pavement/congestion)", s7_needs_index.run),
    "diag": Stage("diag", "MATSim output diagnostics", sim_diagnostics.run),
}
DEFAULT_STAGE_ORDER = ("s0", "s1", "s2", "s2c", "s2d", "s3", "s4", "s5", "s6")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="citysim")
    parser.add_argument("--area", help="configured area slug to use, e.g. logan_square or lake_view")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one stage or all stages")
    run_parser.add_argument("--area", help="configured area slug to use, e.g. logan_square or lake_view")
    run_parser.add_argument(
        "--stage",
        choices=tuple(STAGES.keys()),
        help="pipeline stage to run; omit to run s0 through s6",
    )
    serve_parser = subparsers.add_parser("serve", help="start the local scenario builder")
    serve_parser.add_argument("--area", help="configured area slug to use, e.g. logan_square or lake_view")
    serve_parser.add_argument("--host", default="127.0.0.1", help="host interface to bind")
    serve_parser.add_argument("--port", type=int, default=8000, help="port to bind")
    serve_parser.add_argument(
        "--open",
        dest="open_browser",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="open the app in your browser on startup (default: on; use --no-open to disable)",
    )
    return parser


def run_command(args: argparse.Namespace) -> None:
    cfg = load_config(area=args.area)
    selected = [args.stage] if args.stage else list(DEFAULT_STAGE_ORDER)
    for stage_name in selected:
        stage = STAGES[stage_name]
        print(f"[{cfg.area_slug}:{stage.name}] {stage.description}")
        stage.runner(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        run_command(args)
        return 0
    if args.command == "serve":
        from pipeline.scenario_server import run_server

        run_server(host=args.host, port=args.port, open_browser=args.open_browser, area=args.area)
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
