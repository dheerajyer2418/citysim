"""Import and CLI registration smoke tests for the scaffold."""

from __future__ import annotations

import importlib


PIPELINE_MODULES = [
    "pipeline.config",
    "pipeline.download",
    "pipeline.io_socrata",
    "pipeline.crosswalk",
    "pipeline.plans_io",
    "pipeline.s0_boundary",
    "pipeline.s1_network",
    "pipeline.s2_demand",
    "pipeline.cmap_demand",
    "pipeline.s3_transit",
    "pipeline.s4_calibrate",
    "pipeline.s5_interventions",
    "pipeline.s6_monetize",
]


def test_pipeline_modules_import() -> None:
    for module_name in PIPELINE_MODULES:
        importlib.import_module(module_name)


def test_cli_stages_registered() -> None:
    import cli

    assert list(cli.STAGES) == ["s0", "s1", "s2", "s2c", "s3", "s4", "s5", "s6"]
