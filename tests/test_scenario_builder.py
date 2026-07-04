"""Offline tests for user-drawn scenario builder helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

from pipeline.scenario_builder import PRESETS, VARIABLE_HELP, JobQueue, make_spec, preview_scenario, save_scenario
from pipeline.scenario_server import CONTROL_ACTIONS, _output_file_rows, _safe_output_path
from pipeline.s5_interventions import NetworkLink


def test_make_spec_uses_preset_defaults_and_validates_corridor() -> None:
    spec = make_spec(
        {
            "id": "test_drawn",
            "name": "Test drawn",
            "preset": "road_diet",
            "corridor_lonlat": [[-87.0, 41.0], [-86.999, 41.0]],
        }
    )

    assert spec.id == "test_drawn"
    assert spec.capacity_factor == 0.60
    assert spec.freespeed_factor == 0.90
    assert spec.buffer_m == 35.0

    with pytest.raises(ValueError, match="Draw at least two"):
        make_spec({"id": "bad", "preset": "bike_lane", "corridor_lonlat": [[-87.0, 41.0]]})


def test_pothole_preset_and_variable_help_are_user_facing() -> None:
    spec = make_spec(
        {
            "id": "pothole_test",
            "preset": "add_potholes",
            "corridor_lonlat": [[-87.0, 41.0], [-86.999, 41.0]],
        }
    )

    assert PRESETS["add_potholes"]["label"] == "Add pothole damage"
    assert "rough pavement" in PRESETS["add_potholes"]["description"]
    assert spec.capacity_factor == 1.0
    assert spec.freespeed_factor == 0.75
    assert "Multiplier applied to car capacity" in VARIABLE_HELP["capacity_factor"]


def test_preview_scenario_returns_selected_links(monkeypatch) -> None:
    from pyproj import Transformer
    import pipeline.scenario_builder as builder

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:26971", always_xy=True)
    x1, y1 = transformer.transform(-87.0, 41.0)
    x2, y2 = transformer.transform(-86.999, 41.0)
    links = [
        NetworkLink("near", LineString([(x1, y1), (x2, y2)]), length_m=1609.344, capacity=1000.0),
        NetworkLink("far", LineString([(x1, y1 + 1000), (x2, y2 + 1000)]), length_m=100.0, capacity=1000.0),
    ]
    monkeypatch.setattr(builder, "_scenario_links", lambda cfg: links)

    cfg = SimpleNamespace(crs="EPSG:26971")
    spec = make_spec(
        {
            "id": "preview_test",
            "preset": "bike_lane",
            "corridor_lonlat": [[-87.0, 41.0], [-86.999, 41.0]],
            "buffer_m": 20.0,
        }
    )

    preview = preview_scenario(cfg, spec)

    assert preview["selected_link_count"] == 1
    assert preview["selected_links"] == ["near"]
    assert preview["facility_miles"] == 0.5
    assert preview["highlight_paths"][0]["link_id"] == "near"


def test_save_scenario_writes_export_and_rejects_duplicate(tmp_path) -> None:
    cfg = SimpleNamespace(data_interim=tmp_path)
    spec = make_spec(
        {
            "id": "save_test",
            "preset": "flow_improvement",
            "corridor_lonlat": [[-87.0, 41.0], [-86.999, 41.0]],
        }
    )

    saved = save_scenario(cfg, spec)

    assert saved["scenario_id"] == "save_test"
    assert (tmp_path / "user_scenarios" / "save_test" / "scenario.json").exists()
    with pytest.raises(FileExistsError):
        save_scenario(cfg, spec)


def test_job_queue_reports_busy_without_starting_second_job() -> None:
    queue = JobQueue(SimpleNamespace())
    queue._active_job_id = "running"

    job = queue.start("scenario_a")

    assert job.status == "failed"
    assert job.stage == "busy"
    assert "Another job" in str(job.error)


def test_status_endpoint_reports_generated_asset_state(tmp_path) -> None:
    from pipeline.scenario_server import create_app

    scenario_dir = tmp_path / "scenarios" / "logan_square" / "output"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "live_traffic.html").write_text("<html></html>", encoding="utf-8")
    data_interim = tmp_path / "data" / "interim"
    data_interim.mkdir(parents=True)
    (data_interim / "network_links.gpkg").write_text("", encoding="utf-8")
    cfg = SimpleNamespace(project_root=tmp_path, data_interim=data_interim)

    app = create_app(cfg)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/status")
    payload = endpoint()

    assert payload["network_ready"] is True
    assert payload["live_viz_ready"] is True


def test_control_actions_include_no_terminal_workflow_buttons() -> None:
    assert "full_model_health" in CONTROL_ACTIONS
    assert "diagnostics_all" in CONTROL_ACTIONS
    assert CONTROL_ACTIONS["full_model_health"]["label"] == "Run full workflow"
    assert any(step["kind"] == "matsim" for step in CONTROL_ACTIONS["full_model_health"]["steps"])


def test_output_rows_report_generated_files_and_safe_paths(tmp_path) -> None:
    data_processed = tmp_path / "data" / "processed"
    data_interim = tmp_path / "data" / "interim"
    scenario_dir = tmp_path / "scenarios" / "logan_square" / "output"
    data_processed.mkdir(parents=True)
    data_interim.mkdir(parents=True)
    scenario_dir.mkdir(parents=True)
    (data_processed / "scenario_comparison.csv").write_text("scenario\nfixed\n", encoding="utf-8")
    (scenario_dir / "live_traffic.html").write_text("<html></html>", encoding="utf-8")
    cfg = SimpleNamespace(project_root=tmp_path, data_processed=data_processed, data_interim=data_interim)

    rows = _output_file_rows(cfg)
    comparison = next(row for row in rows if row["label"] == "Scenario comparison CSV")
    live = next(row for row in rows if row["label"] == "Live map")

    assert comparison["available"] is True
    assert live["available"] is True
    assert _safe_output_path(cfg, "processed", "scenario_comparison.csv") == data_processed / "scenario_comparison.csv"
    with pytest.raises(PermissionError):
        _safe_output_path(cfg, "processed", "../interim/secret.csv")
