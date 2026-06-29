# CLAUDE.md — CitySim project context

Context for AI assistants (and humans) working on this repo. Read this first.

## What this is

**CitySim** is a civic decision-support tool: a calibrated, agent-based traffic
**simulation of Logan Square, Chicago** (community area #22), on top of which you
can test infrastructure interventions (potholes, bike lanes, road diets) and get
a **dollar benefit-cost ratio** — Δdelay/ΔVMT/Δcrashes × cost coefficients ÷
intervention cost.

The traffic engine (MATSim) and the data (CMAP, OSM, Census, Chicago open data)
already exist; the novel work is the **sub-area model built from public data + a
cost-benefit reasoning layer**, plus a **deck.gl live web visualization**.

## Key decisions (why things are the way they are)

- **Engine: MATSim 2024.0** — agent-based, native mode choice. Pinned to 2024.0
  because it's the last release built for **Java 17** bytecode (2025.0=Java21,
  2026.0=Java25). MATSim uses year-based versions now (no "16.0").
- **Demand: CMAP-anchored, Python-built.** We do network + demand in Python
  (pyrosm), not pt2matsim, so most stages need no JVM.
- **Calibration: validation + correction, NOT Cadyts.** `cadytsIntegration` was
  dropped after MATSim ~13, so auto-calibration isn't available. Demand is
  already CMAP-regionally-calibrated; we validate against counts + correct.
- **Build order: network → all-purpose demand → cordon/through demand →
  validate → interventions.** Cordon trips were required before calibration
  because internal-only demand was ~3× short on arterials.

## Architecture / pipeline stages

Run via `python cli.py run --stage <sN>`. Pure-Python unless noted.

| Stage | Module | Does |
|-------|--------|------|
| s0 | `pipeline/s0_boundary.py` | Logan Sq boundary (Socrata `igwz-8jzy`) + CMAP TAZ (zones17 GeoJSON), reproject EPSG:26971, clip to +1500 m buffer |
| (core) | `pipeline/crosswalk.py` | **TAZ↔network crosswalk** (STRtree join, connector exclusion, in-polygon coord sampler that snaps to a link). Everything routes through this. |
| s1 | `pipeline/s1_network.py` | OSM (pyrosm BBBike **Chicago** extract) → MATSim `network.xml.gz`. Keeps largest **strongly-connected** component (car routing needs it). |
| s2 | `pipeline/s2_demand.py` | LODES commute demand → plans (legacy/baseline; superseded by s2c/s2d) |
| s2c | `pipeline/cmap_demand.py` | CMAP `trip_roster` all-purpose **internal-internal** auto trips → plans |
| s2d | `pipeline/cmap_cordon_demand.py` | + **cordon/through** trips via boundary gateway nodes → final combined `plans.xml.gz` (~118k agents) |
| s4 | `pipeline/s4_calibrate.py` | Validate sim vs ADT counts (`gc7y-n4xa`), directional GEH, global/per-direction correction factors |
| s5 | `pipeline/s5_interventions.py` | 311 potholes (`7as2-ds3y`) → degrade affected links → `network_potholes.xml.gz` + `config_baseline.xml`/`config_fixed.xml` |
| s6 | `pipeline/s6_monetize.py` | Baseline vs fixed runs → ΔVHT×VOT + pothole VMT×damage − repair cost → **BCR** (`data/processed/pothole_bca.json`) |
| viz | `viz/build_live_viz.py` | MATSim events → self-contained **deck.gl** animated web viz (multi-scenario) |

Shared: `pipeline/plans_io.py` (population writer + `stochastic_count`),
`io_socrata.py`, `io_arcgis.py`, `download.py`.

## Toolchain / environment

- **Python 3.11** venv at `.venv` (NOT system 3.14 — geospatial wheels lag).
  `pip install -r requirements.txt`. Conda not used.
- **JDK 17**: portable Temurin at `tools/jdk-17.0.19+10` (gitignored). Set
  `$env:JAVA_HOME` to it for any gradle/java command.
- **MATSim build**: `cd matsim; .\gradlew.bat --no-daemon installDist`. Repos in
  `matsim/settings.gradle`. Run the sim with a wildcard classpath (avoids
  Windows "input line too long"):
  ```
  java -Xmx8g -cp "matsim/build/install/citysim-matsim/lib/*" citysim.RunCitySim scenarios/logan_square/config.xml
  ```

## Hard-won gotchas (don't re-discover these)

- **jai_core**: Maven Central has its POM but not the JAR → exclude `javax.media`
  from Central so OSGeo resolves it (in `settings.gradle`).
- **Network must be strongly connected** or car routing crashes ("No route
  found … by car"). s1 keeps the largest SCC.
- **Stochastic rounding**: per-row `round(trips*0.1)` zeroed out disaggregate
  demand (most rows have trips=1). Use `plans_io.stochastic_count`.
- **MATSim CSV outputs are `;`-delimited** (legs/trips). s6 broke reading them
  as comma → VHT showed 0.
- **Config scoring** needs `activityParams` for every activity type in plans
  (home/work/shop/other/visit/gateway) AND standard `modeParams`
  (car/pt/walk/bike/ride) or the config writer NPEs.
- **OTFVis is broken here** (JOGL/OpenGL "GraphicsConfiguration" error on this
  GPU). Use the deck.gl web viz instead.
- **codex sandbox** blocks network + `.git` writes and **hangs on geopandas
  import** → have codex write code + offline tests only; the human runs the
  pipeline, downloads, and git.

## WHERE WE ARE NOW (read this first in a new session)

The simulation is **healthy and gridlock-free**, running on **INTERNAL demand
only** (s2c, ~32k agents). Latest verified run: 96% trips complete, median trip
7.8 min, mean 11 min, 27 km/h. The live deck.gl viz
(`scenarios/logan_square/output/live_traffic.html`) is built and works.

**How we got here (the gridlock saga):** the sim used to deadlock (mean 130-160
min). After much diagnosis the root cause was the **cordon (s2d) gateway
concentration** — thousands of external zones map to ~57 boundary nodes, so
~60k veh/day spawned on ONE link and cascaded into deadlock (NOT capacity:
network mean V/C ≈ 0.29). Permanent fixes made: (a) `s1` now simplifies the
network (merge degree-2 micro-links: median link 12m→51m); (b) crosswalk has a
nearest-link fallback for empty TAZ; (c) config uses storageCapacityFactor 1.0
(decoupled from the 0.1 flow sample) + queue model + removeStuckVehicles. To get
a clean run we **dropped cordon through-traffic** (s2d) for now.

**Active manual hacks not yet in the pipeline (make permanent or re-apply):**
- **Departure smoothing**: plan `end_time`s were jittered ~45min (numpy
  normal, σ=2700s) to break a 180k-trips-in-one-hour TOD spike. This is a manual
  post-process on plans.xml.gz — NOT a pipeline stage. Re-apply after any demand
  regeneration, or fold it into s2c/cmap_demand.
- `output_baseline`/`output_fixed` were deleted, so the pothole BCR (s6) needs a
  re-run on the current network.

## Open items / next steps (in priority order)

1. **Re-add through-traffic without gridlock** — fix s2d so each external zone's
   cordon trips are distributed across the K-nearest **arterial** boundary links
   (weighted by capacity), instead of all piling on the single nearest node.
   This restores realistic arterial volumes (the point of s2d) without the
   concentration deadlock. After this, re-run the full chain.
2. **Make departure-smoothing a real pipeline step** (in cmap_demand / s2d), then
   drop the manual jitter.
3. **Re-run the pothole BCR** (s5 → config_baseline run → s6) on the fixed
   network for a trustworthy number.
4. Real FHWA coefficients + filter 311 potholes to currently-open/recent.
5. Extend the edit layer to bike lanes / road diets (s5 already edits the network).

## Earlier known limitations (still true)

- The prototype pothole **BCR ≈ 10.8** (from the pre-gridlock-fix run) is not
  policy-grade: placeholder coefficients, cumulative-historical potholes, rough
  per-link calibration (S4 aggregate good ~factor 1.0, per-link GEH ~9%).
- For interventions, **deltas** (before/after same links) are more robust than
  absolute values.
- Cadyts auto-calibration unavailable on MATSim 2024.0 — S4 is validate+correct.
