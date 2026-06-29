# CitySim — Logan Square Traffic Cost-Benefit Simulator

CitySim is an **agent-based traffic simulation of Logan Square, Chicago**, built
entirely from public data, used to answer questions like:

> *"If we fix the potholes (or add a bike lane), how much travel time and vehicle
> damage does it save — and is that worth the cost?"*

It produces a **dollar benefit-cost ratio (BCR)** for an infrastructure
intervention, and a **live 3-D web visualization** of the simulated traffic.

![intervention footprint](scenarios/logan_square/output/pothole_map.png)

## What it does

1. Builds a road network for Logan Square from **OpenStreetMap**.
2. Generates ~118,000 daily car trips from **CMAP's regional travel model** +
   **Census LODES**, including through-traffic via boundary "gateways".
3. Simulates a full day of traffic with **MATSim** (agents choose routes,
   congestion emerges).
4. Validates simulated volumes against real **Chicago traffic counts**.
5. Applies an **intervention** (e.g. fix potholes from 311 data), re-simulates,
   and monetizes the difference → **benefit-cost ratio**.
6. Renders the result as an animated, zoomable **deck.gl web map**.

## What it's built with

| Layer | Tooling |
|-------|---------|
| Pipeline / data | **Python 3.11** — geopandas, pyrosm, shapely, pyproj, pandas, lxml, networkx, requests |
| Traffic engine | **MATSim 2024.0** (Java 17) via Gradle |
| Visualization | **deck.gl** (self-contained HTML), matplotlib (static maps) |
| Data sources | OSM (BBBike Chicago), CMAP c24q4 travel model, Census LODES + TIGERweb, Chicago Open Data (community areas, ADT counts, 311 potholes) |

## Setup

**Python** (uses Python 3.11 — not 3.14; geospatial wheels lag on 3.14):

```powershell
# create venv from a 3.11 interpreter, then:
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Java** — a JDK 17 is required for MATSim. A portable Temurin 17 is expected at
`tools/jdk-17.0.19+10` (gitignored). Set it before any gradle/java command:

```powershell
$env:JAVA_HOME = "D:\Projects\CitySim\tools\jdk-17.0.19+10"
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
```

Build the MATSim runner:

```powershell
cd matsim; .\gradlew.bat --no-daemon installDist; cd ..
```

## Run it end-to-end

All pipeline stages are run via `cli.py` (see `python cli.py run --help`).
Stages download real data into `data/` on first run and cache it.

```powershell
# 1. Boundary + TAZ crosswalk
python cli.py run --stage s0
# 2. Road network (OSM -> network.xml.gz)
python cli.py run --stage s1
# 3. Demand: all-purpose internal trips, then cordon/through trips
python cli.py run --stage s2c
python cli.py run --stage s2d        # -> scenarios/logan_square/plans.xml.gz (~118k agents)
```

Run the simulation (one simulated day, 10 % population sample):

```powershell
cd scenarios\logan_square
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config.xml
cd ..\..
```

Validate against real traffic counts:

```powershell
python cli.py run --stage s4         # -> data/processed/calibration_validation.csv
```

Pothole intervention + benefit-cost:

```powershell
python cli.py run --stage s5         # builds network_potholes.xml.gz + config_baseline/fixed.xml
# run BOTH scenarios (fixed == the clean-network run above; reuse or re-run):
cd scenarios\logan_square
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config_baseline.xml
cd ..\..
python cli.py run --stage s6         # -> data/processed/pothole_bca.json (the BCR)
```

## See the simulation (live web viz)

```powershell
python viz\build_live_viz.py                 # -> scenarios/logan_square/output/live_traffic.html
Invoke-Item scenarios\logan_square\output\live_traffic.html
```

Opens a dark 3-D map with glowing vehicles moving in real time. Drag to pan,
scroll to zoom, Ctrl-drag to tilt/rotate; play/pause, time scrubber, speed
slider, and a **scenario switcher** (potholes-fixed vs with-potholes).

Static maps are also written to `scenarios/logan_square/output/`:
`volume_map.png`, `pothole_map.png`, `peak_frame.png`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ --basetemp=.pytest_tmp
```

## Status & caveats

The simulation is **gridlock-free and healthy** running on **internal demand
only** (s2c): latest run had 96% of trips complete, median trip 7.8 min, ~27
km/h. The live viz works.

The **cordon/through-traffic stage (s2d) is currently paused** — its gateway
model concentrated ~60k veh/day onto a single boundary link and caused a
cascading deadlock; the fix (distribute entries across arterial boundary links)
is the top open item. Note the **departure-time smoothing** is currently a
manual post-process on `plans.xml.gz`, not a pipeline stage (re-apply after
regenerating demand). The earlier pothole **BCR ≈ 10.8** is a pre-fix prototype
(placeholder coefficients, historical potholes) and should be re-run on the
current network.

See **`CLAUDE.md` → "WHERE WE ARE NOW"** for the full state, the gridlock
post-mortem, active manual hacks, and the prioritized next steps.

## Repo layout

```
pipeline/        Python stages s0..s6 + crosswalk, demand, IO helpers
matsim/          Gradle Java project (MATSim runner)
scenarios/logan_square/   config*.xml + generated MATSim inputs/outputs (gitignored)
viz/             deck.gl live-visualization builder
data/{raw,interim,processed}/   downloaded + generated data (gitignored)
params.yaml      central config (CRS, sources, coefficients, scenario knobs)
CLAUDE.md        full project context for contributors / AI assistants
```
