# CitySim - Logan Square Traffic Cost-Benefit Simulator

CitySim is an agent-based traffic simulation and scenario builder for Logan Square, Chicago. It is built from public data and is meant to answer questions like:

> If we fix potholes or add a bike lane, what are the benefits and drawbacks?

It produces before/after MATSim outputs, benefit-cost results, model-health diagnostics, and a deck.gl web visualization. The core model is auto-focused, and transit now runs in a separate auto+pt config lineage.

![intervention footprint](scenarios/logan_square/output/pothole_map.png)

## What It Does

1. Builds a Logan Square road network from OpenStreetMap.
2. Generates sampled car trips from CMAP regional travel model data and Census LODES, including scaled cordon/through traffic.
3. Runs sampled full-day MATSim traffic simulations.
4. Validates traffic volumes against Chicago traffic counts.
5. Builds intervention scenarios such as pothole repair and a Milwaukee Ave bike-lane test.
6. Compares before/after outputs with model-health warnings.
7. Lets users draw a custom corridor in a local browser UI, preview affected links, and run the scenario.
8. Renders scenarios as an animated web map.

## What It Uses

| Layer | Tooling |
|---|---|
| Pipeline / data | Python 3.11, geopandas, pyrosm, shapely, pyproj, pandas, lxml, networkx, requests |
| Traffic engine | MATSim 2024.0 with Java 17 |
| Scenario UI | FastAPI, Uvicorn, deck.gl |
| Visualization | deck.gl HTML, matplotlib static maps |
| Data sources | OpenStreetMap, CMAP c24q4, Census LODES/TIGERweb, Chicago Open Data |

## Setup

Python 3.11 is required. Do not use Python 3.14 because geospatial wheels often lag for this stack. From the repo root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you prefer conda and need `osmium-tool`, use `environment.yml` instead:

```powershell
conda env create -f environment.yml
conda activate citysim
```

A JDK 17 is required for MATSim. This repo is set up for a local portable Temurin JDK at `tools/jdk-17.0.19+10`, but any Java 17 install works if `JAVA_HOME` points to it.

```powershell
$env:JAVA_HOME = "D:\Projects\CitySim\tools\jdk-17.0.19+10"
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
```

Build the MATSim runner:

```powershell
cd matsim
.\gradlew.bat --no-daemon installDist
cd ..
```

## Quick Start: Scenario Builder

Easiest: **double-click `run.bat`** in the repo root (or run `.\run.bat`). It prepares the
Python environment on first run, points Java at the bundled JDK, starts the app, and opens
your browser automatically. Any `cli.py serve` flags pass straight through, e.g. `run.bat --port 8010`.

Prefer to run it by hand? If the network and base scenario files already exist:

```powershell
.\.venv\Scripts\python.exe cli.py serve
```

This opens `http://127.0.0.1:8000` in your browser on startup (use `--no-open` to disable,
`--port` to change the port).

The app is a full-screen, one-step-at-a-time wizard. It opens on a Welcome screen, then asks you to choose a path:

- **Run the standard model** — pick a workflow (one-click full workflow, or a single phase like run/diagnostics/benefit-cost/rebuild map) and watch it run.
- **Design a street change** — name it, choose a change type (bike lane, road diet, traffic-flow improvement, add potholes), draw the corridor on the map, tune car capacity/speed and preview the affected links, then run it.

Each step gates the Next button until it is satisfied. Running a scenario requires the MATSim Java runner to be built first. Runs use a single local worker; a second run is rejected while one is already in progress.

User-drawn scenarios are saved under:

```text
data/interim/user_scenarios/<scenario_id>/
```

Completed user scenario outputs are written under:

```text
scenarios/logan_square/output_<scenario_id>/
```

## Run The Pipeline

To run the default Python stage order:

```powershell
.\.venv\Scripts\python.exe cli.py run
```

The default order is `s0`, `s1`, `s2`, `s2c`, `s2d`, `s3`, `s4`, `s5`, `s6`. Stage `s3` converts CTA GTFS into MATSim transit when `sources.gtfs.enabled=true`; the switch is default-off so the full pipeline stays green offline.

For the current full-demand auto workflow, the usual manual stage sequence is:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage s0
.\.venv\Scripts\python.exe cli.py run --stage s1
.\.venv\Scripts\python.exe cli.py run --stage s2c
.\.venv\Scripts\python.exe cli.py run --stage s2d
```

Run the clean-network simulation:

```powershell
cd scenarios\logan_square
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config.xml
cd ..\..
```

Validate against traffic counts:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage s4
```

## Transit Stage

`s3` downloads CTA GTFS, filters it to Logan Square, and writes `scenarios/logan_square/transitSchedule.xml.gz` plus `transitVehicles.xml.gz`. `sources.gtfs.enabled` is `false` by default; set it to `true` to fetch GTFS. Metra and Pace feeds are present but disabled by default.

Build transit demand with `s2pt`, which writes `scenarios/logan_square/plans_pt.xml.gz` as unchanged car `plans.xml.gz` plus CMAP transit-mode pt riders from inferred `transit_modes=[4,5,6]`. Transit runs through `scenarios/logan_square/config_pt.xml` into `output_pt`; `RunCitySim.java` creates a pseudo-network for reserved `tr_` links, so transit does not congest the car network.

## Intervention Runs

Generate intervention networks and configs:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage s5
```

This writes:

- `network_potholes.xml.gz`
- `network_bike_lane.xml.gz`
- `config_baseline.xml`
- `config_fixed.xml`
- `config_bike_lane.xml`

Run the pothole baseline, fixed network, and bike-lane scenario:

```powershell
cd scenarios\logan_square
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config_baseline.xml
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config_fixed.xml
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config_bike_lane.xml
cd ..\..
```

Compute benefit-cost and scenario-comparison outputs:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage s6
```

This also writes:

- `data/processed/pothole_bca.json`
- `data/processed/bike_lane_bca.json`
- `data/processed/scenario_comparison.csv`
- `data/processed/scenario_comparison.json`

## Diagnostics

Default diagnostics target `scenarios/logan_square/output`:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage diag
```

To diagnose another MATSim output folder:

```powershell
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_fixed"
$env:CITYSIM_DIAGNOSTICS_SUFFIX = "fixed"
.\.venv\Scripts\python.exe cli.py run --stage diag
```

Use `output_baseline`, `output_fixed`, `output_bike_lane`, or another output directory as needed.

Diagnostics also writes `sim_tuning_recommendations*.csv`, a reviewable list of capacity/speed edit candidates for links with high stuck counts.

## Live Visualization

```powershell
.\.venv\Scripts\python.exe viz\build_live_viz.py
Invoke-Item scenarios\logan_square\output\live_traffic.html
```

The HTML includes an intervention selector, scenario selector, speed-color legend, info panel, live counters, before/after summary metrics, and user scenario views when completed outputs exist. The scenario builder and viewer use deck.gl/CARTO browser assets, so the browser needs network access for map tiles and deck.gl CDN assets unless those assets are vendored locally.

## Live demo (Vercel)

The `public/` folder is a self-contained static site (landing page + needs-priority map + traffic-sim map). Regenerate it, then deploy:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage s7   # builds the needs index (needs network the first time)
.\.venv\Scripts\python.exe viz\build_live_viz.py   # traffic animation
.\.venv\Scripts\python.exe viz\build_site.py       # assembles public/
```

Push to GitHub and import the repo in Vercel; it serves the committed `public/` folder (`vercel.json` sets `outputDirectory: public`, no build step). The bundled JDK, raw data, and MATSim run outputs stay git-ignored.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ --basetemp=.pytest_tmp
```

## Current Status

Latest validated full-demand setup:

- Cordon multiplier: `sources.cmap.cordon.sample_fraction_multiplier: 0.05`
- Total agents: `36,085`
- Pothole baseline: `35,413 / 36,085` arrivals, `98.1%` completion
- Fixed network: `35,226 / 36,085` arrivals, `97.6%` completion
- Bike-lane scenario: `35,282 / 36,085` arrivals, `97.8%` completion
- Health tuning changed `qsim.stuckTime` from `30` seconds to `600` seconds so normal urban spillback is not treated as stuck too aggressively.

Pothole model:

- Uses open 311 reports plus reports created within 365 days as of `2026-06-30`
- Keeps 312 pothole records from 28,690 pothole-type records
- Snaps 287 potholes to 239 network links
- Latest BCR: marked unreliable after health tuning because paired baseline/fixed completion is not balanced closely enough for travel-time monetization
- Vehicle-damage-only annual benefit remains about `$0.76M` from modeled pothole-link VMT under the current run
- Repair cost: `$8.7k` (`287` potholes x `$30`)
- Travel-time savings remain absent; the benefit currently comes from avoided vehicle damage.

Bike-lane model:

- Tests a Milwaukee Ave corridor from `params.yaml`
- Edits 154 car links
- Reduces car capacity and speed on those links
- Capital cost is now `$80k`/lane-mile, updated from `$50k`
- Monetizes sketch-level cycling facility quality, induced active-trip health, accessibility, avoided auto VMT, crash-safety benefit, and car-network disbenefits
- Crash-safety benefit added: about `$90k`/yr
- Writes `data/processed/bike_lane_bca.json`
- Latest bike-lane BCA remains net-negative; after health tuning, `s6` still shows a very large car-network time disbenefit, so the scenario edit/rerouting behavior remains the next thing to investigate before treating the BCA as meaningful

Edit layers:

- `interventions.generic_edit_layers` can apply link-edit CSVs or corridor edits to generate new MATSim networks/configs
- A reviewed edit-layer test on top stuck links did not improve completion (`95.2%`->`95.1%`) or stuck count (`1741`->`1782`), so the layer remains disabled
- `.\.venv\Scripts\python.exe cli.py serve` provides a local map UI for drawing and running user corridor edits without manually editing `params.yaml`

Completed recent steps: bike-lane coefficient refresh, pothole coefficient refresh, rejected capacity/freespeed edit-layer test, CTA GTFS auto+pt transit implementation, and stuck-time model-health tuning. Remaining work is paired-scenario BCA stability, bike-lane car-network disbenefit investigation, transit calibration/BCA, and replacing remaining sketch coefficients with policy-grade Chicago evidence. See `CLAUDE.md` for the detailed handoff.

Known limitations:

- Transit (`s3` / `s2pt`) is implemented as a separate auto+pt lineage with CTA GTFS; CMAP transit mode codes still need confirmation, ridership/transfers are not calibrated, and transit BCA is not modeled.
- Pothole and bike-lane benefit-cost values are planning/sketch estimates, not policy-grade results.
- Bike-lane demand, health, accessibility, and capital-cost assumptions are placeholders until calibrated with local counts, crash history, and Chicago bid tabs.
- Absolute traffic volumes are less reliable than paired before/after deltas.

## Model-Health Tuning Result

The latest accepted tuning keeps `storageCapacityFactor=1.0` and changes `stuckTime` from `30` to `600` seconds in the MATSim configs. This improved completion without changing link speeds or capacities:

- Baseline: `95.0%` -> `98.1%`
- Fixed: `95.2%` -> `97.6%`
- Bike lane: `95.1%` -> `97.8%`

The previous reviewed capacity/freespeed edit-layer test did not help and remains rejected.

1. Run diagnostics for every relevant MATSim output:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage diag
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_baseline"; $env:CITYSIM_DIAGNOSTICS_SUFFIX = "baseline"; .\.venv\Scripts\python.exe cli.py run --stage diag
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_fixed"; $env:CITYSIM_DIAGNOSTICS_SUFFIX = "fixed"; .\.venv\Scripts\python.exe cli.py run --stage diag
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_bike_lane"; $env:CITYSIM_DIAGNOSTICS_SUFFIX = "bike_lane"; .\.venv\Scripts\python.exe cli.py run --stage diag
```

2. Review `data/processed/sim_tuning_recommendations*.csv`. Treat these as diagnostics evidence, not automatic capacity fixes.
3. Leave `interventions.generic_edit_layers` disabled unless a future edit addresses a defensible network-coding issue rather than broad capacity increases.
4. Re-run MATSim for the edited config, then run `diag` and `s6`.
5. Accept future tuning only if completion improves and VHT/stuck counts improve without unrealistic speed/capacity changes or worse scenario deltas.

## Repo Layout

```text
pipeline/                    Python stages s0..s6 plus diagnostics
matsim/                      Gradle Java MATSim runner
scenarios/logan_square/      MATSim configs and generated outputs
viz/                         deck.gl live visualization builder
data/raw/                    downloaded source data
data/interim/                generated intermediate data
data/processed/              diagnostics, validation, BCA outputs
params.yaml                  central configuration
CLAUDE.md                    project context and handoff notes
```
