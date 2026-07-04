# CLAUDE.md - CitySim Project Context

Context for AI assistants and humans working on this repo. Read this first.

## What This Is

CitySim is a civic decision-support tool: an agent-based traffic simulation and local scenario builder for Logan Square, Chicago. It lets users test infrastructure interventions such as pothole repair, bike lanes, road diets, traffic-flow improvements, and user-drawn corridor edits, then inspect before/after traffic outcomes, model-health diagnostics, and benefit-cost outputs.

The traffic engine is MATSim. The data comes from CMAP, OpenStreetMap, Census LODES/TIGERweb, Chicago Open Data, and CTA GTFS. The project also includes a FastAPI/deck.gl scenario-builder UI and a deck.gl live web visualization. Transit now runs in a separate auto+pt config lineage.

## Key Decisions

- Engine: MATSim 2024.0, pinned because it works with Java 17.
- Demand: CMAP-anchored and Python-built. Network and demand generation are Python stages.
- Calibration: validation plus correction, not Cadyts. Cadyts integration is unavailable in current MATSim.
- Build order: boundary/TAZ -> network -> all-purpose demand -> cordon/through demand -> MATSim runs -> validate/diagnose -> interventions -> monetization/visualization.
- Cordon demand is scaled for this subarea model so simulations remain healthy.
- User-drawn scenarios are persisted under `data/interim/user_scenarios/<scenario_id>/` and materialized into `config_<scenario_id>.xml`, `network_<scenario_id>.xml.gz`, and `output_<scenario_id>`.

## Pipeline Stages

Run stages with `.\.venv\Scripts\python.exe cli.py run --stage <stage>`.

| Stage | Module | Does |
|---|---|---|
| s0 | `pipeline/s0_boundary.py` | Logan Square boundary and CMAP TAZ setup |
| core | `pipeline/crosswalk.py` | TAZ/network crosswalk, coordinate sampling, nearest-link fallback |
| s1 | `pipeline/s1_network.py` | OSM to MATSim network, largest strongly connected component |
| s2 | `pipeline/s2_demand.py` | Legacy LODES commute demand |
| s2c | `pipeline/cmap_demand.py` | CMAP internal-internal auto trips |
| s2d | `pipeline/cmap_cordon_demand.py` | Scaled cordon/through trips via perimeter arterial gateways |
| s2pt | `pipeline/cmap_demand.py run_pt` | builds plans_pt.xml.gz = car plans + CMAP transit-mode pt riders (transit_modes=[4,5,6] inferred) |
| s3 | `pipeline/s3_transit.py` | CTA GTFS download, filter to Logan Square, write transitSchedule.xml.gz + transitVehicles.xml.gz (default-off; set sources.gtfs.enabled=true) |
| s4 | `pipeline/s4_calibrate.py` | Validation against Chicago traffic counts |
| s5 | `pipeline/s5_interventions.py` | Pothole and bike-lane scenario network generation |
| s6 | `pipeline/s6_monetize.py` | Pothole and bike-lane BCA plus scenario-comparison exports |
| diag | `pipeline/sim_diagnostics.py` | MATSim completion/stuck diagnostics and top stuck links |
| viz | `viz/build_live_viz.py` | Self-contained deck.gl animated web visualization |
| serve | `pipeline/scenario_server.py` | Local draw-any-street-change scenario builder and single-worker run queue |

## Toolchain

- Python 3.11 venv at `.venv`. Do not use system Python 3.14.
- Install pip deps with `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`; use `environment.yml` if `osmium-tool` is needed.
- JDK 17 expected at `tools/jdk-17.0.19+10`.
- Build MATSim with `cd matsim; .\gradlew.bat --no-daemon installDist; cd ..`.
- Run MATSim with wildcard classpath to avoid Windows command-line length issues.
- Prefer `.\.venv\Scripts\python.exe cli.py ...` in docs and automation so commands do not accidentally use system Python.

Example:

```powershell
$env:JAVA_HOME = "D:\Projects\CitySim\tools\jdk-17.0.19+10"
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
cd scenarios\logan_square
& "$env:JAVA_HOME\bin\java.exe" -Xmx8g -cp "..\..\matsim\build\install\citysim-matsim\lib\*" citysim.RunCitySim config_fixed.xml
```

## Hard-Won Gotchas

- Network must be strongly connected or car routing can fail.
- MATSim CSV outputs are semicolon-delimited.
- Scoring needs activity params for all activity types in plans.
- OTFVis is not reliable here; use the deck.gl visualization.
- Departure smoothing is now in the pipeline via `scenario.departure_jitter_std_seconds`.
- Cordon gateways must be spread across perimeter arterial links; single nearest-node assignment caused deadlock.
- Network access may require approval when fetching Socrata or other public data.
- The scenario builder and live viewer load deck.gl/CARTO browser assets from the network unless those assets are vendored.
- `s6` expects existing MATSim output folders before monetization; run the relevant MATSim configs first.
- Scenario-builder run jobs are single-worker only. Concurrent run requests fail with a busy error.
- Generated clutter should stay out of source control: logs, caches, `config_user_*.xml`, `network_user_*.xml.gz`, and `output_user_*` are disposable local artifacts.

## Where We Are Now

Latest validated full-demand setup:

- Demand stage: `s2d`
- Agents: `36,085`
- Cordon multiplier: `0.05`
- Departure jitter: `2700` seconds
- Pothole baseline: `35,413` arrivals, `672` stuck, `98.1%` completion
- Fixed network: `35,226` arrivals, `859` stuck, `97.6%` completion
- Milwaukee Ave bike-lane scenario: `35,282` arrivals, `803` stuck, `97.8%` completion
- Accepted model-health tuning: `qsim.stuckTime` changed from `30` to `600` seconds while keeping `storageCapacityFactor=1.0`; this avoids treating normal urban spillback as stuck too aggressively.

The live visualization is rebuilt at:

```text
scenarios/logan_square/output/live_traffic.html
```

It includes:

- Intervention selector
- Scenario selector
- Vehicle speed legend
- Info panel
- Live counters
- Before/after pothole summary
- Pothole and bike-lane scenario views
- Completed user scenario views when `output_<scenario_id>` exists

The before/after summary panel is anchored above the playback controls, not over the HUD, so it should not cover counters or the speed legend.

## Current Intervention Details

Potholes:

- Source: Chicago 311 pothole dataset `7as2-ds3y`
- Filter: open reports plus reports created within 365 days as of `2026-06-30`
- Latest fetched raw records: `50,000`
- Pothole-type records: `28,690`
- Filtered records: `312`
- Snapped records: `287`
- Affected links: `239`
- Latest BCR: marked unreliable after health tuning because paired baseline/fixed completion is not balanced closely enough for travel-time monetization.
- Vehicle-damage-only annual benefit remains about `$0.76M` from modeled pothole-link VMT under the current run.
- Repair cost: `$8.7k` (`287` potholes x `$30`)
- Travel-time savings remain absent; benefit currently comes from avoided vehicle damage.

Bike lane:

- Scenario: Milwaukee Ave corridor from `params.yaml`
- Affected car links: `154`
- Edit: reduce car capacity and freespeed on corridor links
- Output: `network_bike_lane.xml.gz`, `config_bike_lane.xml`, `output_bike_lane`
- `s6` now writes `data/processed/bike_lane_bca.json`
- Capital cost: `$80k`/lane-mile, updated from `$50k`
- Bike-lane BCA includes sketch-level cycling facility quality, induced active-trip health, accessibility, avoided auto VMT, crash-safety benefit, and car-network disbenefits
- Crash-safety benefit added: about `$90k`/yr
- Latest bike-lane BCA remains net-negative; after health tuning, `s6` still shows a very large car-network time disbenefit, so the scenario edit/rerouting behavior remains the next thing to investigate before treating the BCA as meaningful.

## Diagnostics

Default diagnostic target is `scenarios/logan_square/output`.

For scenario outputs, set:

```powershell
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_fixed"
$env:CITYSIM_DIAGNOSTICS_SUFFIX = "fixed"
.\.venv\Scripts\python.exe cli.py run --stage diag
```

Use the same pattern for `output_baseline` and `output_bike_lane`.

Diagnostics writes `sim_tuning_recommendations*.csv` with reviewable capacity/freespeed edit candidates for high-stuck links.

## Generic Edit Layers

`interventions.generic_edit_layers` can apply link-edit CSVs or corridor edits to create new network/config pairs without hardcoding a new intervention. The default example points at `data/processed/sim_tuning_recommendations_bike_lane.csv` and is disabled until reviewed.

## Transit (s3 / s2pt)

`sources.gtfs.enabled` is the master switch for GTFS download and is `false` by default so the full pipeline stays green offline. When enabled, `s3` downloads CTA GTFS, filters stops/routes/trips to Logan Square for a representative weekday, and writes `scenarios/logan_square/transitSchedule.xml.gz` plus `transitVehicles.xml.gz`. Metra and Pace feeds are present but disabled by default.

`s2pt` builds `scenarios/logan_square/plans_pt.xml.gz` as the unchanged car `plans.xml.gz` plus CMAP roster transit-mode pt riders from `sources.cmap.roster.transit_modes` (`[4,5,6]`, inferred). Car plans and all car configs are untouched. Transit runs through `scenarios/logan_square/config_pt.xml` with `useTransit=true`, `transitModes=pt`, `inputPlansFile=plans_pt.xml.gz`, and `output_pt`. `matsim/src/main/java/citysim/RunCitySim.java` calls `org.matsim.pt.utils.CreatePseudoNetwork` when transit is enabled, so transit uses reserved `tr_` links and does not congest the car network. No pt2matsim dependency was added.

Verified run: `4,329` transit departures operate; about `885` pt riders board about `1,297` times; car network health is unchanged (`~1,747` stuck vs fixed `1,741`).

Caveats: `sources.cmap.roster.transit_modes = [4,5,6]` is inferred from roster volume patterns and CMAP mode structure, not yet confirmed against the CMAP c24q4 trip-based-model mode dictionary. `sim_diagnostics` counts transit drivers and pt sub-legs as agents, so completion percent is inflated for `output_pt`; read car health via stuck count, and filter pt-rider events by `pt_cmap_` because transit driver ids start with `pt_`. Ridership and transfer behavior are not calibrated, and transit BCA is not modeled.

## Scenario Builder

Double-click `run.bat` (or `.\run.bat`) for a one-step launch: it prepares the venv on first run, sets `JAVA_HOME` to the bundled JDK, starts the server, and opens the browser. Equivalent manual command: `.\.venv\Scripts\python.exe cli.py serve` (opens the browser by default; `--no-open` disables, `--port` changes the port). Then use the app at `http://127.0.0.1:8000`.

The UI is a full-screen, one-step-at-a-time wizard (all markup/CSS/JS live in the `EDITOR_HTML` string in `pipeline/scenario_server.py`; the backend is unchanged FastAPI). It opens on a Welcome screen, then a Choose-path screen forking into two guided flows:

- Run the standard model (3 steps): pick an action from `CONTROL_ACTIONS` (hero "Run full workflow", or a single phase) -> live progress -> results.
- Design a street change (6 steps): name -> change type -> draw the corridor on a full-screen map -> tune capacity/speed and preview affected links -> run -> results.

Each screen gates Next until its requirement is met, and Back preserves state. The wizard reuses the existing endpoints only: `/api/status`, `/api/map-data`, `/api/control/*`, `/api/preview`, `/api/scenarios`, `/api/jobs/*`, `/api/outputs`, `/live`. Runs are a single-worker queue; a concurrent start returns a busy job. Presets are bike lane, road diet, traffic flow improvement, and add pothole damage.

User scenarios are saved under `data/interim/user_scenarios/<scenario_id>/`. Completed user outputs are discovered by `s6` and `viz/build_live_viz.py` when their `output_<scenario_id>` folder exists, and the live viewer then adds them from `data/interim/user_scenarios/manifest.json`.

## Next Steps

DONE (Steps 1-4):
1. Bike-lane calibration — capital cost updated to `$80k`/lane-mile (Chicago concrete-PBL program), existing daily bike trips re-cited to CDOT Bicycle Count Study, and a crash-safety benefit added (`10` crashes/yr x `45%` reduction [FHWA-HRT-23-025] x `$20k` injury cost = `$90k`/yr from Chicago Data Portal `85ca-t3if`). Bike-lane BCA is still NET-NEGATIVE (annual net about `-$0.31M`; gross benefits about `$0.75M` vs car-network delay disbenefit about `$1.05M`/yr; facility `5.12` mi; cost `$0.43M`; BCR about `-19.7`). The dominant negative is the car-network time disbenefit from the MATSim run — a model/scenario artifact, NOT a coefficient problem.
2. Pothole calibration — `extra_damage_usd_per_vmt` updated `0.15`->`0.10` (TRIP: Chicago rough-road VOC about `$427`/driver/yr), `repair_cost_usd_per_pothole` `$200`->`$30` (municipal estimate), `annual_days` made explicit (`300`). New pothole numbers: annual benefit about `$52k`, cost `$8.7k` (`287` potholes x `$30`), BCR about `25.46`.
3. Diagnostics review + edit layer — reviewed 8 top-stuck candidates on `output_fixed`, kept 7 defensible edits, ran MATSim with modest `1.10`-`1.25` capacity / `<=1.10` freespeed bumps. Result: completion `95.2%`->`95.1%`, stuck `1741`->`1782` — NO improvement, REJECTED. `generic_edit_layers` left disabled and documented. KEY FINDING: top stuck links are NOT capacity-bound; root cause is spillback/storage/deadlock queue dynamics downstream.
4. Transit — IMPLEMENTED (auto+pt). `s3` converts real CTA GTFS to MATSim `transitSchedule`/`transitVehicles` (default-off master switch `sources.gtfs.enabled`); new `s2pt` stage builds `plans_pt.xml.gz` = untouched car plans + `885` CMAP transit-mode pt riders; separate `config_pt.xml` lineage; `RunCitySim.java` calls `CreatePseudoNetwork` so transit runs on reserved `tr_` links. Verified: `4,329` transit departures, about `1,297` pt boardings, car stuck `1,747` about fixed `1,741`.

DONE (Step 5):
5. Model-health tuning — accepted config-level tuning changed `qsim.stuckTime` from `30` to `600` seconds. Completion is now in the target range without capacity/freespeed edits: baseline `98.1%`, fixed `97.6%`, bike lane `97.8%`.

REMAINING / NEW NEXT STEPS:
6. Stabilize paired-scenario BCA interpretation — pothole `s6` is now explicitly marked `bca_reliable=false` when baseline/fixed completion or stuck counts differ enough that travel-time monetization can be dominated by model-health noise.
7. Investigate the bike-lane car-network disbenefit — re-examine `s5` capacity/freespeed reduction and rerouting; only then is the bike-lane BCA meaningful.
8. Transit follow-ups: confirm CMAP transit mode codes `[4,5,6]` against c24q4 TBM source; calibrate ridership/transfer behavior (transfer penalties / SwissRailRaptor); model a transit BCA; optionally enable Metra/Pace, add deck.gl transit overlay, add typed `TransitConfig`.
9. Replace remaining sketch coefficients (bike-lane demand/health shares, pothole repair cost) with policy-grade Chicago evidence (FOIA/bid tabs, local counts, crash history).

## Model-Health Tuning Result

The accepted step-5 tuning keeps full sampled storage (`storageCapacityFactor=1.0`) and changes `stuckTime` from `30` to `600` seconds. The failed generic edit-layer test showed that modest capacity/freespeed bumps did not improve completion (`95.2%`->`95.1%`) or stuck count (`1741`->`1782`), so link capacity/speed edits remain rejected unless they address a specific network-coding issue.

Current verified diagnostics:

- Baseline: `98.1%` completion, `672` stuck.
- Fixed: `97.6%` completion, `859` stuck.
- Bike lane: `97.8%` completion, `803` stuck.

Run the same diagnostics pattern after any future MATSim run:

1. Run diagnostics for every relevant output folder:

```powershell
.\.venv\Scripts\python.exe cli.py run --stage diag
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_baseline"; $env:CITYSIM_DIAGNOSTICS_SUFFIX = "baseline"; .\.venv\Scripts\python.exe cli.py run --stage diag
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_fixed"; $env:CITYSIM_DIAGNOSTICS_SUFFIX = "fixed"; .\.venv\Scripts\python.exe cli.py run --stage diag
$env:CITYSIM_MATSIM_OUTPUT_DIR = "output_bike_lane"; $env:CITYSIM_DIAGNOSTICS_SUFFIX = "bike_lane"; .\.venv\Scripts\python.exe cli.py run --stage diag
```

2. Review `data/processed/sim_tuning_recommendations*.csv` manually as evidence, not a capacity-edit queue.
3. For each top-stuck area, check whether the root cause is downstream spillback/storage, short-link queue storage, deadlock-breaker/stuckTime behavior, connector artifacts, or gateway concentration.
4. Leave `interventions.generic_edit_layers` disabled unless a future edit addresses a defensible network-coding issue rather than broad capacity increases.
5. Re-run MATSim for the edited config, then run diagnostics and `s6`.
6. Compare against the fixed network using completion rate, stuck count, VHT, VMT, mean/median trip time, and BCA deltas.
7. Accept tuning only if it improves model health without unrealistic freespeed/capacity values or worse paired-scenario behavior.

## Known Limitations

- The current pothole and bike-lane BCRs are not policy-grade yet because several coefficients are sourced but still sketch/order-of-magnitude values.
- Per-link calibration remains rough even though the current runs are much healthier.
- Deltas between paired scenarios are more robust than absolute values.
- Cadyts auto-calibration is unavailable on MATSim 2024.0, so s4 is validation plus correction.
- Transit (`s3` / `s2pt`) is implemented in a separate auto+pt lineage, but CMAP transit mode codes still need confirmation, ridership/transfers are not calibrated, Metra/Pace and visualization overlay are optional follow-ons, typed `TransitConfig` is not added, and transit BCA is not modeled.
