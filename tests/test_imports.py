"""Import and CLI registration smoke tests for the scaffold."""

from __future__ import annotations

import importlib


PIPELINE_MODULES = [
    "pipeline.config",
    "pipeline.download",
    "pipeline.io_socrata",
    "pipeline.crosswalk",
    "pipeline.plans_io",
    "pipeline.gtfs_matsim",
    "pipeline.s0_boundary",
    "pipeline.s1_network",
    "pipeline.s2_demand",
    "pipeline.cmap_demand",
    "pipeline.cmap_cordon_demand",
    "pipeline.s3_transit",
    "pipeline.s4_calibrate",
    "pipeline.s5_interventions",
    "pipeline.s6_monetize",
    "pipeline.sim_diagnostics",
    "pipeline.scenario_builder",
    "pipeline.scenario_server",
]


def test_pipeline_modules_import() -> None:
    for module_name in PIPELINE_MODULES:
        importlib.import_module(module_name)


def test_cli_stages_registered() -> None:
    import cli

    assert list(cli.STAGES) == ["s0", "s1", "s2", "s2c", "s2d", "s2pt", "s3", "s4", "s5", "s6", "s7", "diag"]
    assert list(cli.DEFAULT_STAGE_ORDER) == ["s0", "s1", "s2", "s2c", "s2d", "s3", "s4", "s5", "s6"]
    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--port", "9000"])
    assert args.command == "serve"
    assert args.port == 9000


def test_default_config_has_required_coefficient_sources() -> None:
    from pipeline.config import load_config

    cfg = load_config()

    assert cfg.coefficient_sources["required"] is True
    assert cfg.coefficient_sources["values"]["value_of_time_usd_per_hr"]["url"].startswith("https://")
