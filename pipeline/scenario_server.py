"""Local FastAPI server for the user-drawn scenario builder."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pipeline.config import CitySimConfig, load_config
from pipeline.scenario_builder import (
    JobQueue,
    _java_path,
    _python_path,
    _run_command,
    make_spec,
    map_data,
    preview_scenario,
    save_scenario,
    user_scenarios,
)


CONTROL_ACTIONS: dict[str, dict[str, Any]] = {
    "prepare_interventions": {
        "label": "Prepare networks",
        "description": "Fetches/updates intervention inputs and writes baseline, fixed, and bike-lane configs.",
        "steps": [{"kind": "python_stage", "stage": "s5"}],
    },
    "run_baseline": {
        "label": "Run pothole baseline",
        "description": "Runs MATSim using the pothole-damaged network.",
        "steps": [{"kind": "matsim", "config": "config_baseline.xml"}],
    },
    "run_fixed": {
        "label": "Run fixed network",
        "description": "Runs MATSim using the repaired/current network.",
        "steps": [{"kind": "matsim", "config": "config_fixed.xml"}],
    },
    "run_bike_lane": {
        "label": "Run bike lane",
        "description": "Runs MATSim using the configured bike-lane network.",
        "steps": [{"kind": "matsim", "config": "config_bike_lane.xml"}],
    },
    "run_all_scenarios": {
        "label": "Run all simulations",
        "description": "Runs baseline, fixed, and bike-lane MATSim simulations.",
        "steps": [
            {"kind": "matsim", "config": "config_baseline.xml"},
            {"kind": "matsim", "config": "config_fixed.xml"},
            {"kind": "matsim", "config": "config_bike_lane.xml"},
        ],
    },
    "diagnostics_all": {
        "label": "Run diagnostics",
        "description": "Refreshes model-health diagnostics for baseline, fixed, and bike-lane outputs.",
        "steps": [
            {"kind": "diagnostics", "output": "output_baseline", "suffix": "baseline"},
            {"kind": "diagnostics", "output": "output_fixed", "suffix": "fixed"},
            {"kind": "diagnostics", "output": "output_bike_lane", "suffix": "bike_lane"},
        ],
    },
    "monetize": {
        "label": "Run benefit-cost",
        "description": "Refreshes pothole and bike-lane BCA plus scenario comparison files.",
        "steps": [{"kind": "python_stage", "stage": "s6"}],
    },
    "build_viz": {
        "label": "Rebuild live map",
        "description": "Rebuilds the deck.gl live visualization from current outputs.",
        "steps": [{"kind": "script", "script": "viz\\build_live_viz.py"}],
    },
    "full_model_health": {
        "label": "Run full workflow",
        "description": "Runs simulations, diagnostics, BCA, and live-map rebuild in the recommended order.",
        "steps": [
            {"kind": "matsim", "config": "config_baseline.xml"},
            {"kind": "matsim", "config": "config_fixed.xml"},
            {"kind": "matsim", "config": "config_bike_lane.xml"},
            {"kind": "diagnostics", "output": "output_baseline", "suffix": "baseline"},
            {"kind": "diagnostics", "output": "output_fixed", "suffix": "fixed"},
            {"kind": "diagnostics", "output": "output_bike_lane", "suffix": "bike_lane"},
            {"kind": "python_stage", "stage": "s6"},
            {"kind": "script", "script": "viz\\build_live_viz.py"},
        ],
    },
}


@dataclass
class ControlJob:
    job_id: str
    action: str
    status: str = "queued"
    stage: str = "queued"
    log: list[str] = field(default_factory=list)
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "status": self.status,
            "stage": self.stage,
            "log_tail": self.log[-120:],
            "error": self.error,
        }


class ControlJobQueue:
    """Single-worker queue for predefined local model operations."""

    def __init__(self, cfg: CitySimConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._jobs: dict[str, ControlJob] = {}
        self._active_job_id: str | None = None

    def start(self, action: str) -> ControlJob:
        if action not in CONTROL_ACTIONS:
            raise ValueError(f"Unknown action: {action}")
        with self._lock:
            if self._active_job_id is not None:
                job = ControlJob(job_id=uuid4().hex, action=action, status="failed", stage="busy")
                job.error = f"Another control job is already running: {self._active_job_id}"
                self._jobs[job.job_id] = job
                return job
            job = ControlJob(job_id=uuid4().hex, action=action)
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id

        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> ControlJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: ControlJob) -> None:
        try:
            job.status = "running"
            for step in CONTROL_ACTIONS[job.action]["steps"]:
                job.stage = _step_label(step)
                _run_control_step(self.cfg, step, job.log)
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


def _step_label(step: dict[str, str]) -> str:
    kind = step["kind"]
    if kind == "matsim":
        return f"matsim {step['config']}"
    if kind == "diagnostics":
        return f"diagnostics {step['output']}"
    if kind == "python_stage":
        return f"stage {step['stage']}"
    if kind == "script":
        return str(step["script"])
    return kind


def _run_control_step(cfg: CitySimConfig, step: dict[str, str], log: list[str]) -> None:
    scenario_dir = cfg.scenario_dir
    kind = step["kind"]
    if kind == "matsim":
        _run_command(
            [
                str(_java_path(cfg)),
                "-Xmx8g",
                "-cp",
                str(cfg.project_root / "matsim" / "build" / "install" / "citysim-matsim" / "lib" / "*"),
                "citysim.RunCitySim",
                step["config"],
            ],
            cwd=scenario_dir,
            log=log,
        )
        return
    if kind == "diagnostics":
        env = os.environ.copy()
        env["CITYSIM_AREA"] = cfg.area_slug
        env["CITYSIM_MATSIM_OUTPUT_DIR"] = step["output"]
        env["CITYSIM_DIAGNOSTICS_SUFFIX"] = step["suffix"]
        _run_command([str(_python_path(cfg)), "cli.py", "run", "--stage", "diag"], cwd=cfg.project_root, log=log, env=env)
        return
    if kind == "python_stage":
        _run_command([str(_python_path(cfg)), "cli.py", "run", "--stage", step["stage"], "--area", cfg.area_slug], cwd=cfg.project_root, log=log)
        return
    if kind == "script":
        command = [str(_python_path(cfg)), step["script"]]
        if str(step["script"]).startswith("viz\\"):
            command.extend(["--area", cfg.area_slug])
        _run_command(command, cwd=cfg.project_root, log=log)
        return
    raise ValueError(f"Unsupported control step kind: {kind}")


def _output_roots(cfg: CitySimConfig) -> dict[str, Path]:
    scenario_dir = getattr(cfg, "scenario_dir", Path(cfg.project_root) / "scenarios" / "logan_square")
    return {
        "processed": cfg.data_processed,
        "interim": cfg.data_interim,
        "scenario": scenario_dir,
    }


def _output_file_rows(cfg: CitySimConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    specs = [
        ("Live map", "scenario", Path("output/live_traffic.html"), "Interactive traffic visualization"),
        ("Scenario comparison CSV", "processed", Path("scenario_comparison.csv"), "Flat scenario metrics table"),
        ("Scenario comparison JSON", "processed", Path("scenario_comparison.json"), "Scenario metrics as JSON"),
        ("Pothole BCA", "processed", Path("pothole_bca.json"), "Pothole benefit-cost details"),
        ("Bike-lane BCA", "processed", Path("bike_lane_bca.json"), "Bike-lane benefit-cost details"),
        ("Baseline diagnostics", "processed", Path("sim_diagnostics_baseline.json"), "Baseline model-health summary"),
        ("Fixed diagnostics", "processed", Path("sim_diagnostics_fixed.json"), "Fixed model-health summary"),
        ("Bike-lane diagnostics", "processed", Path("sim_diagnostics_bike_lane.json"), "Bike-lane model-health summary"),
        ("Baseline stuck links", "processed", Path("sim_top_stuck_links_baseline.csv"), "Baseline top stuck links"),
        ("Fixed stuck links", "processed", Path("sim_top_stuck_links_fixed.csv"), "Fixed top stuck links"),
        ("Bike-lane stuck links", "processed", Path("sim_top_stuck_links_bike_lane.csv"), "Bike-lane top stuck links"),
        ("Pothole links", "interim", Path("pothole_links.csv"), "Potholes snapped to MATSim links"),
        ("Bike-lane links", "interim", Path("bike_lane_links.csv"), "MATSim links edited for bike-lane scenario"),
        ("Baseline config", "scenario", Path("config_baseline.xml"), "MATSim baseline run config"),
        ("Fixed config", "scenario", Path("config_fixed.xml"), "MATSim fixed-network run config"),
        ("Bike-lane config", "scenario", Path("config_bike_lane.xml"), "MATSim bike-lane run config"),
    ]
    roots = _output_roots(cfg)
    for label, root_key, relative, description in specs:
        path = roots[root_key] / relative
        rows.append(
            {
                "label": label,
                "description": description,
                "available": path.exists(),
                "href": f"/outputs/{root_key}/{relative.as_posix()}",
                "size_bytes": path.stat().st_size if path.exists() else None,
                "updated": path.stat().st_mtime if path.exists() else None,
            }
        )
    return rows


def _safe_output_path(cfg: CitySimConfig, root_key: str, relative_path: str) -> Path:
    roots = _output_roots(cfg)
    if root_key not in roots:
        raise FileNotFoundError(f"Unknown output root: {root_key}")
    root = roots[root_key].resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("Refusing to serve a path outside CitySim outputs.")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"Output file not found: {relative_path}")
    return target


EDITOR_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>CitySim</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://unpkg.com/deck.gl@9.0.0/dist.min.js"></script>
<style>
  html,body,#map{margin:0;width:100%;height:100%;background:#0a0e14;overflow:hidden;font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#e7f1ff;}
  *{box-sizing:border-box;}
  #map{position:fixed;inset:0;}
  body.card-mode #map{filter:blur(3px) brightness(.42);}
  body.draw-mode #map{filter:none;}
  body.draw-mode #wizard{display:none;}
  h1{font-size:42px;line-height:1.02;margin:0 0 10px 0;letter-spacing:0;color:#f4f9ff;}
  h2{font-size:28px;line-height:1.12;margin:0 0 8px 0;color:#f4f9ff;letter-spacing:0;}
  h3{font-size:15px;margin:0;color:#f4f9ff;}
  p{font-size:15px;line-height:1.5;color:#b8cce0;margin:6px 0;}
  .sub{font-size:16px;color:#a8bfd5;margin-bottom:22px;}
  button,input,select{font:inherit;}
  button{cursor:pointer;}
  button:disabled,.disabled{opacity:.42;cursor:not-allowed;filter:grayscale(.25);}
  input,select{width:100%;background:#102235;color:#e7f1ff;border:1px solid #315673;border-radius:8px;padding:13px 14px;font-size:16px;}
  input:focus,select:focus{outline:2px solid #74d7ff;outline-offset:2px;}
  label{display:block;font-size:12px;color:#9fb7cc;text-transform:uppercase;letter-spacing:.04em;margin:18px 0 6px 0;}
  small.readout{display:block;font-size:13px;color:#74d7ff;margin-top:6px;}
  #wizard{position:fixed;inset:0;z-index:5;display:flex;align-items:center;justify-content:center;padding:28px;background:rgba(2,6,10,.28);}
  .card{width:min(560px,calc(100vw - 28px));max-height:calc(100vh - 56px);overflow:auto;background:rgba(10,18,29,.94);border:1px solid rgba(116,215,255,.34);border-radius:14px;padding:30px;box-shadow:0 24px 80px rgba(0,0,0,.52);backdrop-filter:blur(14px);}
  .screen{display:none;}
  .screen.active{display:block;animation:fade .16s ease;}
  @keyframes fade{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:none;}}
  #progressHead{display:none;margin-bottom:22px;}
  #progressMeta{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#9fb7cc;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;}
  #bar{height:5px;border-radius:999px;background:#183048;overflow:hidden;}
  #fill{height:100%;width:0;background:linear-gradient(90deg,#39c0ff,#74d7ff);transition:width .18s ease;}
  .nav{display:none;align-items:center;justify-content:space-between;gap:14px;margin-top:26px;}
  .btn{border-radius:9px;border:1px solid #3c6f8c;background:#12283c;color:#dff;min-height:46px;padding:0 18px;font-weight:650;}
  .btn:hover{border-color:#74d7ff;}
  .primary{background:#1f7fbf;border-color:#39c0ff;color:#fff;}
  .primary:hover{background:#2a94d8;}
  .ghost{background:transparent;color:#c9d9e8;}
  .danger{border-color:#7c4257;color:#f0a9c0;}
  .full{width:100%;margin-top:10px;}
  .bigstart{min-height:56px;font-size:18px;margin-top:12px;}
  .choices{display:grid;gap:12px;margin-top:16px;}
  .choice{display:block;width:100%;text-align:left;background:#102235;border:1px solid rgba(116,215,255,.25);border-radius:10px;padding:18px;color:#e7f1ff;}
  .choice:hover,.choice.selected{border-color:#74d7ff;background:#143555;}
  .choice b{display:block;font-size:17px;margin-bottom:6px;}
  .choice span{display:block;font-size:13.5px;color:#a9bed2;line-height:1.4;}
  .badge{display:inline-flex;align-items:center;min-height:22px;border:1px solid rgba(116,215,255,.35);border-radius:999px;padding:0 9px;font-size:11px;color:#8fd6ff;margin-left:8px;}
  details{margin-top:12px;border:1px solid rgba(120,160,210,.18);border-radius:10px;padding:12px;background:rgba(8,17,27,.55);}
  summary{cursor:pointer;color:#bcd4e8;font-weight:650;}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  .jobbox{background:#050d16;border:1px solid rgba(120,160,210,.22);border-radius:10px;padding:14px;margin-top:14px;}
  .spinner{width:28px;height:28px;border:3px solid rgba(116,215,255,.2);border-top-color:#39c0ff;border-radius:50%;animation:spin 1s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .jobhead{display:flex;gap:12px;align-items:center;margin-bottom:10px;}
  .stage{font-size:14px;color:#d9ecff;}
  pre{margin:0;white-space:pre-wrap;max-height:230px;overflow:auto;color:#c7d8ea;font-size:12px;line-height:1.45;}
  #outputsPanel{display:none;margin-top:18px;border-top:1px solid rgba(120,160,210,.2);padding-top:14px;}
  #outputsLink{display:inline-block;margin-top:16px;background:none;border:none;color:#8fd6ff;text-decoration:underline;padding:0;font-size:13px;}
  .group-head{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8fb4d6;margin:13px 0 6px 0;}
  .output-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
  .output-card{display:block;text-decoration:none;color:#e7f1ff;background:rgba(18,42,64,.72);border:1px solid rgba(120,160,210,.18);border-radius:8px;padding:10px;}
  .output-card:hover{border-color:#74d7ff;}
  .output-card.disabled{pointer-events:none;}
  .output-card b{display:block;font-size:13px;margin-bottom:3px;}
  .output-card span{display:block;font-size:11px;color:#a9bed2;line-height:1.35;}
  #drawHud{display:none;position:fixed;inset:0;z-index:8;pointer-events:none;}
  body.draw-mode #drawHud{display:block;}
  #drawBanner{position:absolute;top:18px;left:50%;transform:translateX(-50%);width:min(720px,calc(100vw - 28px));background:rgba(6,14,22,.9);border:1px solid rgba(116,215,255,.4);border-radius:12px;padding:14px 18px;text-align:center;box-shadow:0 12px 36px rgba(0,0,0,.42);}
  #drawBanner b{font-size:16px;}
  #drawBanner span{display:block;color:#a9bed2;font-size:13px;margin-top:3px;}
  #drawControls{position:absolute;left:50%;bottom:22px;transform:translateX(-50%);display:flex;gap:10px;pointer-events:auto;}
  #drawCounter{position:absolute;top:105px;left:50%;transform:translateX(-50%);background:rgba(6,14,22,.86);border:1px solid rgba(120,160,210,.24);border-radius:999px;padding:8px 13px;color:#dff;font-size:14px;}
  #points{font-size:14px;color:#bfd4e8;margin-top:12px;}
  #metrics{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px;}
  .metric{background:rgba(18,42,64,.7);border-radius:9px;padding:12px;}
  .metric span{display:block;font-size:11px;color:#91a9be;text-transform:uppercase;letter-spacing:.04em;}
  .metric b{font-size:24px;}
  .help{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;margin-left:5px;border:1px solid #6090b2;border-radius:50%;font-size:11px;color:#cfe8ff;background:transparent;padding:0;vertical-align:middle;}
  #tooltip{display:none;position:absolute;z-index:30;max-width:300px;background:rgba(5,14,23,.97);border:1px solid rgba(115,190,235,.65);border-radius:7px;padding:9px 10px;color:#e8f4ff;font-size:12px;line-height:1.35;box-shadow:0 12px 30px rgba(0,0,0,.4);}
  #hiddenLegacy{display:none;}
  @media(max-width:620px){
    #wizard{padding:14px;align-items:flex-start;}
    .card{width:calc(100vw - 28px);max-height:calc(100vh - 28px);padding:22px;}
    h1{font-size:34px;} h2{font-size:23px;}
    .grid2,.output-grid{grid-template-columns:1fr;}
    #drawControls{width:calc(100vw - 24px);display:grid;grid-template-columns:1fr 1fr;}
  }
</style>
</head>
<body class="card-mode">
<div id="map"></div>

<main id="wizard">
  <section class="card">
    <div id="progressHead">
      <div id="progressMeta"><span id="stepText">Step 1 of 6</span><span id="branchText">Design</span></div>
      <div id="bar"><div id="fill"></div></div>
    </div>

    <section class="screen" data-screen="welcome">
      <h1>CitySim</h1>
      <p class="sub">Run the standard Logan Square simulations or design a street change directly on the map.</p>
      <button type="button" class="btn primary full bigstart" id="startBtn">Get started</button>
    </section>

    <section class="screen" data-screen="choose">
      <h2>What do you want to do?</h2>
      <p class="sub">Choose the guided path that matches the work you want to run.</p>
      <div class="choices">
        <button type="button" class="choice" id="chooseRun"><b>Run the standard simulations</b><span id="runNote">Run baseline, fixed-road, bike-lane, diagnostics, benefit-cost, and live map outputs.</span></button>
        <button type="button" class="choice" id="chooseDesign"><b>Design a street change</b><span id="designNote">Draw a corridor, tune capacity and speed, preview affected roads, then run a custom scenario.</span></button>
      </div>
    </section>

    <section class="screen" data-screen="a_pick">
      <h2>Run full workflow</h2>
      <p class="sub">The recommended run refreshes simulations, diagnostics, benefit-cost files, and the live traffic map.</p>
      <button type="button" class="choice selected" id="fullRunChoice"><b>Run full workflow <span class="badge">Recommended</span></b><span id="fullRunDesc">full_model_health</span></button>
      <details>
        <summary>Run a single phase</summary>
        <div class="choices" id="singleActions"></div>
      </details>
    </section>

    <section class="screen" data-screen="a_run">
      <h2>Running model</h2>
      <p class="sub">This can take a while. You can go back while the server job continues.</p>
      <div class="jobbox"><div class="jobhead"><div class="spinner" id="aSpin"></div><div><h3 id="aStatus">Starting</h3><div class="stage" id="aStage">queued</div></div></div><pre id="aLog">Waiting for logs...</pre></div>
    </section>

    <section class="screen" data-screen="a_done">
      <h2>Standard simulations complete</h2>
      <p class="sub">The generated live map and output files are ready to inspect.</p>
      <a class="btn primary full" href="/live" target="_blank" rel="noopener" style="display:flex;align-items:center;justify-content:center;text-decoration:none;">Open live traffic map</a>
      <button type="button" class="btn full" id="aStartOver">Start over</button>
    </section>

    <section class="screen" data-screen="b_name">
      <h2>Name your scenario</h2>
      <p class="sub">Use a short name that will still make sense when you review generated files later.</p>
      <label>Scenario name <button type="button" class="help" data-help="scenario_name" aria-label="Scenario name help">?</button></label>
      <input id="name" value="My street change"/>
    </section>

    <section class="screen" data-screen="b_type">
      <h2>Choose the street change</h2>
      <p class="sub">Pick the closest starting point. You can tune the details after drawing.</p>
      <div class="choices" id="presetCards"></div>
    </section>

    <section class="screen" data-screen="b_draw">
      <h2>Draw the corridor</h2>
      <p class="sub">The map will expand full-screen. Click at least two points along the street you want to change.</p>
    </section>

    <section class="screen" data-screen="b_tune">
      <h2>Tune and preview</h2>
      <p class="sub">Adjust the change, preview the affected roads, then continue once the preview succeeds.</p>
      <div class="grid2">
        <div><label>Car capacity change <button type="button" class="help" data-help="capacity_factor" aria-label="Capacity change help">?</button></label><input id="capacity" type="number" min="0.1" max="3" step="0.05"/><small id="capReadout" class="readout"></small></div>
        <div><label>Car speed change <button type="button" class="help" data-help="freespeed_factor" aria-label="Speed change help">?</button></label><input id="speed" type="number" min="0.1" max="3" step="0.05"/><small id="spdReadout" class="readout"></small></div>
      </div>
      <details>
        <summary>Advanced</summary>
        <div class="grid2">
          <div><label>Search width (m) <button type="button" class="help" data-help="buffer_m" aria-label="Search width help">?</button></label><input id="buffer" type="number" min="5" max="200" step="5"/></div>
          <div><label>Ignore roads below (veh/hr) <button type="button" class="help" data-help="min_link_capacity" aria-label="Minimum capacity help">?</button></label><input id="mincap" type="number" min="0" step="100"/></div>
        </div>
      </details>
      <div id="points">0 points drawn.</div>
      <button type="button" class="btn primary full" id="previewBtn">Preview affected roads</button>
      <div id="metrics">
        <div class="metric"><span>Selected links</span><b id="linkCount">0</b></div>
        <div class="metric"><span>Facility miles</span><b id="miles">0.00</b></div>
      </div>
    </section>

    <section class="screen" data-screen="b_run">
      <h2>Running custom scenario</h2>
      <p class="sub">CitySim is saving your scenario and rebuilding the live map outputs.</p>
      <div class="jobbox"><div class="jobhead"><div class="spinner" id="bSpin"></div><div><h3 id="bStatus">Starting</h3><div class="stage" id="bStage">queued</div></div></div><pre id="bLog">Waiting for logs...</pre></div>
    </section>

    <section class="screen" data-screen="b_done">
      <h2>Scenario complete</h2>
      <p class="sub">Your custom run is ready. The summary below comes from the successful preview.</p>
      <div id="metrics">
        <div class="metric"><span>Selected links</span><b id="doneLinks">0</b></div>
        <div class="metric"><span>Facility miles</span><b id="doneMiles">0.00</b></div>
      </div>
      <a class="btn primary full" href="/live" target="_blank" rel="noopener" style="display:flex;align-items:center;justify-content:center;text-decoration:none;">Open live traffic map</a>
      <button type="button" class="btn full" id="designAnother">Design another</button>
      <button type="button" class="btn ghost full" id="bStartOver">Start over</button>
    </section>

    <nav class="nav" id="nav"><button type="button" class="btn ghost" id="backBtn">Back</button><button type="button" class="btn primary" id="nextBtn">Next</button></nav>
    <button type="button" id="outputsLink">Browse all output files</button>
    <div id="outputsPanel"><div id="outputGroups"></div></div>
  </section>
</main>

<div id="drawHud">
  <div id="drawBanner"><b>Draw a street corridor</b><span>Click the map to add points. Use Done when you have at least two.</span></div>
  <div id="drawCounter">0 points</div>
  <div id="drawControls">
    <button type="button" class="btn" id="drawUndo">Undo</button>
    <button type="button" class="btn danger" id="drawClear">Clear</button>
    <button type="button" class="btn primary" id="drawDone" disabled>Done</button>
  </div>
</div>

<div id="tooltip"></div>
<div id="hiddenLegacy">
  <select id="preset"></select><div id="presetDesc"></div><button id="run"></button><button id="preview"></button><button id="undo"></button><button id="export"></button><button id="clear"></button><a id="liveLink" href="/live" target="_blank"></a><span id="controlStatus">Idle</span><span id="consoleState">Idle</span><span id="cJob">Idle</span><pre id="log">Ready.</pre>
</div>

<script>
const {DeckGL, TileLayer, BitmapLayer, PathLayer, ScatterplotLayer} = deck;
let DATA=null, points=[], highlights=[], savedScenario=null, currentJob=null, currentControlJob=null;
let currentStep=0, jobRunning=false, deckgl=null;
let screen='welcome', selectedAction='full_model_health', chosenPreset='', lastPreview=null, statusData=null, actionsData={};
let startedControlJob=null, startedScenarioJob=null;
const el=id=>document.getElementById(id);
const SCREENS={
  welcome:{branch:null,step:0,total:0,next:'choose'},
  choose:{branch:null,step:0,total:0},
  a_pick:{branch:'Run',step:1,total:3,next:'a_run',back:'choose'},
  a_run:{branch:'Run',step:2,total:3,back:'a_pick'},
  a_done:{branch:'Run',step:3,total:3,back:'choose'},
  b_name:{branch:'Design',step:1,total:6,next:'b_type',back:'choose'},
  b_type:{branch:'Design',step:2,total:6,next:'b_draw',back:'b_name'},
  b_draw:{branch:'Design',step:3,total:6,next:'b_tune',back:'b_type'},
  b_tune:{branch:'Design',step:4,total:6,next:'b_run',back:'b_draw'},
  b_run:{branch:'Design',step:5,total:6,back:'b_tune'},
  b_done:{branch:'Design',step:6,total:6,back:'choose'}
};
const CATEGORY_ORDER=['Maps','Benefit-cost','Diagnostics','Configs'];

function log(msg){el('log').textContent=msg; const target=screen==='a_run'?el('aLog'):(screen==='b_run'?el('bLog'):null); if(target)target.textContent=msg;}
function setJobRunning(on){
  jobRunning=on;
  const s=el('consoleState'); s.textContent=on?'Running':'Idle'; s.classList.toggle('run',on);
  chip('cJob',on?'Running':'Idle',!on);
}
function chip(id,text,ok){
  const n=el(id); if(!n)return; n.textContent=text;
  const card=n.closest('.chip'); if(card){card.classList.toggle('ok',!!ok);card.classList.toggle('warn',!ok);}
}

const basemap = new TileLayer({
  id:'carto-dark', data:'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png', minZoom:0, maxZoom:19, tileSize:256,
  renderSubLayers: props => new BitmapLayer(props,{data:null,image:props.data,bounds:[props.tile.boundingBox[0][0],props.tile.boundingBox[0][1],props.tile.boundingBox[1][0],props.tile.boundingBox[1][1]]})
});
function layers(){
  return [
    basemap,
    new PathLayer({id:'roads',data:DATA.roads,getPath:d=>d.path,getColor:d=>d.capacity>=1500?[132,154,172,125]:[70,86,102,85],getWidth:d=>d.capacity>=1500?2.5:1.2,widthMinPixels:.7,widthMaxPixels:4,rounded:true}),
    new PathLayer({id:'drawn',data:points.length>1?[{path:points}]:[],getPath:d=>d.path,getColor:[70,210,255,230],getWidth:5,widthUnits:'pixels',rounded:true}),
    new ScatterplotLayer({id:'vertices',data:points,getPosition:d=>d,getFillColor:[70,210,255,240],getRadius:6,radiusUnits:'pixels'}),
    new PathLayer({id:'highlights',data:highlights,getPath:d=>d.path,getColor:[255,190,65,235],getWidth:5,widthUnits:'pixels',rounded:true})
  ];
}

async function init(){
  DATA=await (await fetch('/api/map-data')).json();
  buildPresetSelect();
  buildPresetCards();
  wireHelp();
  applyPreset();
  await loadActions();
  await refreshOutputs();
  await refreshStatus();
  deckgl=new DeckGL({container:'map',initialViewState:{longitude:DATA.center[0],latitude:DATA.center[1],zoom:13,pitch:45,bearing:-15},controller:true,layers:layers(),onClick:info=>{if(currentStep!==3||screen!=='b_draw')return; if(info.coordinate){points.push([Number(info.coordinate[0].toFixed(6)),Number(info.coordinate[1].toFixed(6))]);refresh();}}});
  refresh();
  go('welcome');
}
function buildPresetSelect(){
  const select=el('preset'); select.innerHTML='';
  Object.entries(DATA.presets).forEach(([key,p])=>{const o=document.createElement('option');o.value=key;o.textContent=p.label;select.appendChild(o);});
}
function presetOrder(){
  const preferred=['bike_lane','road_diet','flow_improvement','add_potholes'];
  const keys=preferred.filter(k=>DATA.presets[k]);
  Object.keys(DATA.presets).forEach(k=>{if(keys.indexOf(k)<0)keys.push(k);});
  return keys;
}
function buildPresetCards(){
  const host=el('presetCards'); host.innerHTML='';
  presetOrder().forEach(key=>{
    const p=DATA.presets[key];
    const btn=document.createElement('button'); btn.type='button'; btn.className='choice'; btn.dataset.preset=key;
    btn.innerHTML='<b></b><span></span>';
    btn.querySelector('b').textContent=p.label||key;
    btn.querySelector('span').textContent=p.description||'Use this scenario preset.';
    btn.onclick=()=>{chosenPreset=key; el('preset').value=key; applyPreset(); lastPreview=null; highlights=[]; refresh(); updatePresetCards(); updateNav();};
    host.appendChild(btn);
  });
}
function updatePresetCards(){
  document.querySelectorAll('[data-preset]').forEach(b=>b.classList.toggle('selected',b.dataset.preset===chosenPreset));
}
async function loadActions(){
  const payload=await (await fetch('/api/control/actions')).json();
  actionsData=payload.actions||{};
  if(actionsData.full_model_health) el('fullRunDesc').textContent=actionsData.full_model_health.description || 'full_model_health';
  const host=el('singleActions'); host.innerHTML='';
  Object.entries(actionsData).forEach(([key,a])=>{
    if(key==='full_model_health')return;
    const btn=document.createElement('button'); btn.type='button'; btn.className='choice'; btn.dataset.action=key;
    btn.innerHTML='<b></b><span></span>';
    btn.querySelector('b').textContent=a.label||key;
    const steps=a.step_count?(' - '+a.step_count+' step'+(a.step_count>1?'s':'')):'';
    btn.querySelector('span').textContent=(a.description||'Run this phase.')+steps;
    btn.onclick=()=>{selectedAction=key; document.querySelectorAll('[data-action]').forEach(n=>n.classList.toggle('selected',n.dataset.action===key)); el('fullRunChoice').classList.remove('selected'); updateNav();};
    host.appendChild(btn);
  });
  el('fullRunChoice').onclick=()=>{selectedAction='full_model_health'; el('fullRunChoice').classList.add('selected'); document.querySelectorAll('[data-action]').forEach(n=>n.classList.remove('selected')); updateNav();};
}
async function refreshStatus(){
  try{ statusData=await (await fetch('/api/status')).json(); }catch(err){ return; }
  if(!statusData.base_network_ready) el('runNote').textContent='Base network files are missing. Build the base network before running the standard simulations.';
  el('chooseDesign').disabled=!statusData.network_ready;
  if(!statusData.network_ready) el('designNote').textContent='Design is unavailable until network_links.gpkg exists.';
}
function categoryOf(item){
  const l=item.label.toLowerCase();
  if(l.indexOf('map')>=0 || item.href.endsWith('.html')) return 'Maps';
  if(l.indexOf('bca')>=0 || l.indexOf('benefit')>=0 || l.indexOf('comparison')>=0) return 'Benefit-cost';
  if(l.indexOf('diagnostic')>=0 || l.indexOf('stuck')>=0) return 'Diagnostics';
  return 'Configs';
}
async function refreshOutputs(){
  const data=await (await fetch('/api/outputs')).json();
  const items=data.outputs||[];
  const host=el('outputGroups'); host.innerHTML='';
  const groups={}; CATEGORY_ORDER.forEach(c=>groups[c]=[]);
  items.forEach(i=>{const c=categoryOf(i); (groups[c]||(groups[c]=[])).push(i);});
  CATEGORY_ORDER.forEach(cat=>{
    const list=groups[cat]; if(!list||!list.length)return;
    const head=document.createElement('div'); head.className='group-head'; head.textContent=cat; host.appendChild(head);
    const grid=document.createElement('div'); grid.className='output-grid';
    list.forEach(item=>{
      const a=document.createElement('a'); a.className='output-card'+(item.available?'':' disabled'); a.href=item.href; a.target='_blank'; a.rel='noopener';
      const t=document.createElement('b'); t.textContent=item.label;
      const d=document.createElement('span'); d.textContent=(item.available?'Open':'Not created yet')+' - '+item.description;
      a.appendChild(t); a.appendChild(d); grid.appendChild(a);
    });
    host.appendChild(grid);
  });
}
function go(id){
  screen=id;
  const meta=SCREENS[id];
  currentStep=meta.step||0;
  document.querySelectorAll('.screen').forEach(s=>s.classList.toggle('active',s.dataset.screen===id));
  const framed=id==='welcome'||id==='choose';
  el('progressHead').style.display=framed?'none':'block';
  el('nav').style.display=(id==='welcome'||id==='choose'||id==='a_done'||id==='b_done')?'none':'flex';
  document.body.classList.toggle('draw-mode',id==='b_draw');
  document.body.classList.toggle('card-mode',id!=='b_draw');
  if(meta.total){
    el('stepText').textContent='Step '+meta.step+' of '+meta.total;
    el('branchText').textContent=meta.branch;
    el('fill').style.width=(meta.step/meta.total*100)+'%';
  }
  if(id==='a_run') enterARun();
  if(id==='b_run') enterBRun();
  if(id==='b_done'){el('doneLinks').textContent=lastPreview?lastPreview.selected_link_count:0; el('doneMiles').textContent=lastPreview?Number(lastPreview.facility_miles).toFixed(2):'0.00';}
  refresh();
  updateNav();
}
function requirementMet(){
  if(screen==='a_pick')return !!selectedAction;
  if(screen==='a_run')return !jobRunning && startedControlJob===null;
  if(screen==='b_name')return el('name').value.trim().length>0;
  if(screen==='b_type')return !!chosenPreset;
  if(screen==='b_draw')return points.length>=2;
  if(screen==='b_tune')return !!lastPreview;
  if(screen==='b_run')return !jobRunning && startedScenarioJob===null;
  return true;
}
function updateNav(){
  const meta=SCREENS[screen];
  el('backBtn').disabled=!meta.back;
  el('nextBtn').disabled=!requirementMet();
  let text='Next';
  if(screen==='a_pick')text='Run';
  if(screen==='a_run'||screen==='b_run')text='See results';
  if(screen==='b_draw')text='Done';
  if(screen==='b_tune')text='Run scenario';
  el('nextBtn').textContent=text;
  el('drawCounter').textContent=points.length+' point'+(points.length===1?'':'s');
  el('drawDone').disabled=points.length<2;
}
function next(){
  const meta=SCREENS[screen];
  if(!requirementMet())return;
  if(screen==='a_run')return go('a_done');
  if(screen==='b_run')return go('b_done');
  go(meta.next);
}
function back(){
  const meta=SCREENS[screen];
  if(meta.back)go(meta.back);
}
async function enterARun(){
  el('aStatus').textContent=jobRunning?'Running':'Starting';
  el('aStage').textContent='queued';
  if(startedControlJob || currentControlJob || jobRunning){updateNav(); return;}
  startedControlJob='starting';
  await runControlAction(selectedAction);
  startedControlJob=currentControlJob;
  monitorControlDone(startedControlJob);
}
async function monitorControlDone(jobId){
  if(!jobId)return;
  const res=await fetch('/api/control/jobs/'+jobId);
  const job=await res.json();
  el('aStatus').textContent=job.status;
  el('aStage').textContent=job.stage||'running';
  el('aLog').textContent=(job.log_tail||[]).slice(-15).join('\\n')+(job.error?'\\nERROR: '+job.error:'');
  if(job.status==='succeeded'){startedControlJob=null; el('aSpin').style.display='none'; await refreshOutputs(); updateNav(); return;}
  if(job.status==='failed'){startedControlJob=null; el('aSpin').style.display='none'; updateNav(); return;}
  setTimeout(()=>monitorControlDone(jobId),3000);
}
async function enterBRun(){
  el('bStatus').textContent=jobRunning?'Running':'Starting';
  el('bStage').textContent='queued';
  if(startedScenarioJob || currentJob || jobRunning){updateNav(); return;}
  startedScenarioJob='starting';
  await runScenario();
  startedScenarioJob=currentJob;
  monitorScenarioDone(startedScenarioJob);
}
async function monitorScenarioDone(jobId){
  if(!jobId)return;
  const res=await fetch('/api/jobs/'+jobId);
  const job=await res.json();
  el('bStatus').textContent=job.status;
  el('bStage').textContent=job.stage||'running';
  el('bLog').textContent=(job.log_tail||[]).slice(-15).join('\\n')+(job.error?'\\nERROR: '+job.error:'');
  if(job.status==='succeeded'){startedScenarioJob=null; el('bSpin').style.display='none'; await refreshOutputs(); updateNav(); return;}
  if(job.status==='failed'){startedScenarioJob=null; el('bSpin').style.display='none'; updateNav(); return;}
  setTimeout(()=>monitorScenarioDone(jobId),3000);
}
async function runControlAction(action){
  if(jobRunning){log('A job is already running. Wait for it to finish.');return;}
  el('controlStatus').textContent='Running';
  const res=await fetch('/api/control/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
  const job=await res.json();
  if(job.status==='failed'){el('controlStatus').textContent='Idle';log('Could not start: '+(job.error||'unknown error'));return;}
  currentControlJob=job.job_id; setJobRunning(true); await refreshStatus(); pollControl();
}
async function pollControl(){
  if(!currentControlJob)return;
  const res=await fetch('/api/control/jobs/'+currentControlJob);
  const job=await res.json();
  log('Model job: '+job.status+' \\u00b7 '+job.stage+'\\n'+(job.log_tail||[]).join('\\n')+(job.error?'\\nERROR: '+job.error:''));
  if(job.status==='succeeded'){currentControlJob=null;el('controlStatus').textContent='Complete';setJobRunning(false);await refreshStatus();await refreshOutputs();return;}
  if(job.status==='failed'){currentControlJob=null;el('controlStatus').textContent='Failed';setJobRunning(false);await refreshStatus();await refreshOutputs();return;}
  setTimeout(pollControl,3000);
}
function wireHelp(){
  const tip=el('tooltip');
  function show(node){
    const text=DATA.variable_help[node.dataset.help]||'';
    if(!text)return;
    tip.textContent=text; tip.style.display='block';
    const rect=node.getBoundingClientRect();
    const left=Math.min(window.innerWidth-320, Math.max(10, rect.left));
    const top=Math.min(window.innerHeight-90, rect.bottom+8);
    tip.style.left=left+'px'; tip.style.top=top+'px';
  }
  function hide(){tip.style.display='none';}
  document.querySelectorAll('.help').forEach(node=>{
    node.addEventListener('mouseenter',()=>show(node));
    node.addEventListener('focus',()=>show(node));
    node.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();tip.style.display==='block'?hide():show(node);});
    node.addEventListener('mouseleave',hide);
    node.addEventListener('blur',hide);
  });
  document.addEventListener('click',event=>{if(!event.target.classList.contains('help'))hide();});
}
function pctCap(f){f=Number(f);const d=Math.round(Math.abs(1-f)*100);if(!isFinite(d)||d===0)return 'no change';return d+'% '+(f<1?'less':'more')+' capacity';}
function pctSpeed(f){f=Number(f);const d=Math.round(Math.abs(1-f)*100);if(!isFinite(d)||d===0)return 'no change';return d+'% '+(f<1?'slower':'faster');}
function updateFactorReadouts(){
  el('capReadout').textContent=el('capacity').value+' \\u2192 '+pctCap(el('capacity').value);
  el('spdReadout').textContent=el('speed').value+' \\u2192 '+pctSpeed(el('speed').value);
}
function applyPreset(){const p=DATA.presets[el('preset').value];el('buffer').value=p.buffer_m;el('mincap').value=p.min_link_capacity;el('capacity').value=p.capacity_factor;el('speed').value=p.freespeed_factor;el('presetDesc').textContent=p.description||'';updateFactorReadouts();}
function spec(){return {name:el('name').value,preset:el('preset').value,corridor_lonlat:points,buffer_m:Number(el('buffer').value),min_link_capacity:Number(el('mincap').value),capacity_factor:Number(el('capacity').value),freespeed_factor:Number(el('speed').value)};}
function refresh(){el('points').textContent=points.length+' point'+(points.length===1?'':'s')+' drawn.'; if(deckgl) deckgl.setProps({layers:layers()}); updateNav();}
async function preview(){
  if(points.length<2){log('Draw at least two corridor points on the map first.');return null;}
  const res=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(spec())});
  const data=await res.json(); if(!res.ok){log(data.detail||'Preview failed.');return null;}
  highlights=data.highlight_paths; el('linkCount').textContent=data.selected_link_count; el('miles').textContent=Number(data.facility_miles).toFixed(2);
  log((data.warnings||[]).join('\\n') || 'Preview ready. '+data.selected_link_count+' links selected.'); refresh(); return data;
}
async function save(){
  const res=await fetch('/api/scenarios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(spec())});
  const data=await res.json(); if(!res.ok){log(data.detail||'Save failed.');return null;}
  savedScenario=data; log('Saved '+data.scenario_id+' at '+data.path); await refreshStatus(); return data;
}
function downloadJSON(data){const blob=new Blob([JSON.stringify(data.spec,null,2)+'\\n'],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=data.scenario_id+'.json';a.click();URL.revokeObjectURL(a.href);}
async function runScenario(){
  if(jobRunning){log('A job is already running. Wait for it to finish.');return;}
  await preview(); if(points.length<2)return;
  const saved=await save(); if(!saved)return;
  const res=await fetch('/api/scenarios/'+saved.scenario_id+'/run',{method:'POST'});
  const job=await res.json();
  if(job.status==='failed'){log('Could not start: '+(job.error||'unknown error'));return;}
  currentJob=job.job_id; setJobRunning(true); await refreshStatus(); poll();
}
async function poll(){
  if(!currentJob)return;
  const res=await fetch('/api/jobs/'+currentJob); const job=await res.json();
  log('Scenario job: '+job.status+' \\u00b7 '+job.stage+'\\n'+(job.log_tail||[]).join('\\n')+(job.error?'\\nERROR: '+job.error:''));
  if(job.status==='succeeded'){el('liveLink').style.display='block';currentJob=null;setJobRunning(false);await refreshStatus();await refreshOutputs();return;}
  if(job.status==='failed'){currentJob=null;setJobRunning(false);await refreshStatus();return;}
  setTimeout(poll,3000);
}

el('startBtn').onclick=()=>go('choose');
el('chooseRun').onclick=()=>go('a_pick');
el('chooseDesign').onclick=()=>{if(!el('chooseDesign').disabled)go('b_name');};
el('backBtn').onclick=back;
el('nextBtn').onclick=next;
el('aStartOver').onclick=()=>go('choose');
el('bStartOver').onclick=()=>go('choose');
el('designAnother').onclick=()=>{points=[];highlights=[];savedScenario=null;lastPreview=null;go('b_name');};
el('outputsLink').onclick=()=>{const p=el('outputsPanel'); p.style.display=p.style.display==='block'?'none':'block'; if(p.style.display==='block')refreshOutputs();};
el('name').oninput=updateNav;
el('capacity').oninput=()=>{lastPreview=null; updateFactorReadouts(); updateNav();};
el('speed').oninput=()=>{lastPreview=null; updateFactorReadouts(); updateNav();};
el('buffer').oninput=()=>{lastPreview=null; updateNav();};
el('mincap').oninput=()=>{lastPreview=null; updateNav();};
el('previewBtn').onclick=async()=>{lastPreview=await preview(); updateNav();};
el('drawUndo').onclick=()=>{points.pop();highlights=[];lastPreview=null;refresh();};
el('drawClear').onclick=()=>{points=[];highlights=[];lastPreview=null;refresh();};
el('drawDone').onclick=()=>{if(points.length>=2)go('b_tune');};
el('undo').onclick=()=>{points.pop();highlights=[];refresh();};
el('clear').onclick=()=>{points=[];highlights=[];savedScenario=null;refresh();log('Drawing cleared.');};
el('export').onclick=async()=>{await preview(); const saved=await save(); if(saved)downloadJSON(saved);};
el('run').onclick=runScenario;
el('preview').onclick=preview;
init();
</script>
</body>
</html>
"""


def create_app(cfg: CitySimConfig | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    cfg = cfg or load_config()
    app = FastAPI(title="CitySim Scenario Builder")
    queue = JobQueue(cfg)
    control_queue = ControlJobQueue(cfg)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return EDITOR_HTML.replace("Logan Square", cfg.area_name)

    def _area_cfg(area_slug: str) -> CitySimConfig:
        return load_config(area=area_slug.replace("-", "_"))

    def _static_area_file(area_cfg: CitySimConfig, filename: str):
        path = area_cfg.scenario_dir / "output" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{area_cfg.area_name} {filename} has not been built yet.")
        return FileResponse(path)

    @app.get("/needs_map.html")
    def needs_map():
        return _static_area_file(cfg, "needs_map.html")

    @app.get("/live_traffic.html")
    def live_traffic():
        return _static_area_file(cfg, "live_traffic.html")

    @app.get("/{area_slug}/needs_map.html")
    def area_needs_map(area_slug: str):
        return _static_area_file(_area_cfg(area_slug), "needs_map.html")

    @app.get("/{area_slug}/live_traffic.html")
    def area_live_traffic(area_slug: str):
        return _static_area_file(_area_cfg(area_slug), "live_traffic.html")

    @app.get("/live")
    def live():
        path = cfg.scenario_dir / "output" / "live_traffic.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Live visualization has not been built yet.")
        return FileResponse(path)

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        scenario_dir = getattr(cfg, "scenario_dir", Path(cfg.project_root) / "scenarios" / "logan_square")
        network_links = getattr(cfg, "network_links_path", cfg.data_interim / "network_links.gpkg")
        return {
            "network_ready": network_links.exists(),
            "area": getattr(cfg, "area_slug", "logan_square"),
            "area_name": getattr(cfg, "area_name", "Logan Square"),
            "base_network_ready": (scenario_dir / "network.xml.gz").exists(),
            "live_viz_ready": (scenario_dir / "output" / "live_traffic.html").exists(),
            "user_scenario_count": len(user_scenarios(cfg)),
        }

    @app.get("/api/control/actions")
    def api_control_actions() -> dict[str, Any]:
        return {
            "actions": {
                key: {
                    "label": value["label"],
                    "description": value["description"],
                    "step_count": len(value["steps"]),
                }
                for key, value in CONTROL_ACTIONS.items()
            }
        }

    @app.post("/api/control/run")
    def api_control_run(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            job = control_queue.start(str(payload.get("action", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()

    @app.get("/api/control/jobs/{job_id}")
    def api_control_job(job_id: str) -> dict[str, Any]:
        job = control_queue.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        return job.snapshot()

    @app.get("/api/outputs")
    def api_outputs() -> dict[str, Any]:
        return {"outputs": _output_file_rows(cfg)}

    @app.get("/outputs/{root_key}/{relative_path:path}")
    def output_file(root_key: str, relative_path: str):
        try:
            return FileResponse(_safe_output_path(cfg, root_key, relative_path))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/map-data")
    def api_map_data() -> dict[str, Any]:
        return map_data(cfg)

    @app.post("/api/preview")
    def api_preview(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return preview_scenario(cfg, make_spec(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scenarios")
    def api_save(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return save_scenario(cfg, make_spec(payload))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scenarios/{scenario_id}/run")
    def api_run(scenario_id: str) -> dict[str, Any]:
        job = queue.start(scenario_id)
        return job.snapshot()

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict[str, Any]:
        job = queue.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        return job.snapshot()

    return app


def run_server(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False, area: str | None = None) -> None:
    import uvicorn

    display_host = "localhost" if host in ("0.0.0.0", "") else host
    url = f"http://{display_host}:{port}"
    print(f"CitySim scenario builder is starting at {url}")
    if open_browser:
        import threading
        import webbrowser

        # Open the browser shortly after uvicorn.run() below starts serving.
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(load_config(area=area)), host=host, port=port)

