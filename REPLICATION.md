# Replicating CitySim to All Chicago Community Areas

CitySim currently models **one** community area — Logan Square (community area **22**). This is the
plan to generalize it to **all 77 Chicago community areas**.

Live single-area demo: https://citysim-five.vercel.app/

---

## What already works citywide (no change needed — just filter/clip per area)

- **OpenStreetMap**: the repo already downloads the whole-Chicago extract (`data/raw/Chicago.osm.pbf`); `s1` just clips to a buffered boundary.
- **Chicago Open Data** (all citywide, queried by bbox/point): crashes `85ca-t3if`, 311 potholes `7as2-ds3y`, ADT counts `gc7y-n4xa`, community areas `igwz-8jzy`.
- **Census LODES/TIGER**, **CMAP c24q4** TAZ + trip roster, **CTA GTFS** — all regional.
- **CRS** `EPSG:26971` is valid for all of Chicago.

## What is hardcoded to Logan Square (must be parameterized)

- `params.yaml` → `boundary.community_area_id: 22` and `boundary.name: "Logan Square"`. The
  community-areas dataset `igwz-8jzy` exposes all 77 via the `area_numbe` field.
- The slug **`logan_square`** and `scenarios/logan_square/…` paths, hardcoded across:
  `pipeline/s0_boundary.py`, `crosswalk.py`, `s1_network.py`, `s2_demand.py`, `cmap_demand.py`,
  `cmap_cordon_demand.py`, `s3_transit.py`, `s4_calibrate.py`, `s5_interventions.py`,
  `s6_monetize.py`, `s7_needs_index.py`, `sim_diagnostics.py`, `scenario_server.py`, and
  `viz/build_live_viz.py`, `build_needs_map.py`, `build_site.py`.
- Tuned-for-LS knobs likely needing per-area values: cordon
  `sources.cmap.cordon.sample_fraction_multiplier` (0.05) and `qsim.stuckTime` in the MATSim configs.
- Map center/zoom and the hand-picked 5 "reddest street" scenarios are LS-specific.

---

## The big constraint: compute

- The **needs index (`s7`) requires NO simulation** — it is data fetch + snap + score. It can run for
  all 77 areas quickly and cheaply.
- **MATSim runs are the bottleneck**: network + demand + a baseline run + N intervention sims per area.
  77 areas × several minute-long sims = **hours-to-days** of compute, single-worker.

**Recommended sequencing:** ship the **needs map for all 77 areas first** (fast, honest, high value),
then add simulated road-diet scenarios area-by-area where they matter most.

---

## Generalization plan

### Step 1 — Make the pipeline area-aware (no behavior change for Logan Square)
- Add an `areas:` section to `params.yaml` (or a `data/areas.yaml`): a list of `{slug, name,
  community_area_id, center_lonlat}`. Keep Logan Square as the first entry so current behavior is
  preserved.
- Thread an **`area` argument** through `cli.py` (e.g. `cli.py run --area logan_square --stage s1`)
  and into each stage's `run(cfg)`. Replace the hardcoded `"logan_square"` / `scenarios/logan_square`
  with an `cfg.area_slug` / `cfg.scenario_dir(area)` helper on `CitySimConfig`.
- Update the three `viz/*.py` scripts and `scenario_server.py` to take the same area argument.
- **Acceptance:** `cli.py run --area logan_square …` reproduces today's outputs byte-for-similar.

### Step 2 — Batch the needs index for all 77 areas (no MATSim)
- Pull all 77 community areas from `igwz-8jzy`; for each, run `s0` (boundary) → `core` → `s1`
  (network) → `s7` (needs index). Cache the citywide crash/pothole/ADT pulls once and filter per
  area to avoid re-downloading.
- Output per area: `needs_index.geojson` + `_summary.json` + a `needs_map.html`.

### Step 3 — Multi-area site
- Decide: one site with an **area picker** (dropdown of 77 → loads that area's needs map) vs.
  per-area pages under `public/<slug>/`. A landing page + picker is the cleaner UX.
- `build_site.py` loops areas, writes `public/<slug>/needs_map.html`, and a top-level index with the
  picker. Still 100% static → same Vercel deploy.

### Step 4 — Simulations, incrementally
- Per area: run MATSim baseline + auto-select top-N needs streets + road-diet sims (`s5`/`s6`/`s7`
  + `build_needs_scenarios.py` generalized). Add each area's traffic map as its sims complete.
- Consider a job queue / overnight batch given the runtime, and per-area cordon/stuckTime calibration.

---

## Concrete checklist

- [ ] Add `areas` config + `area_slug`/`scenario_dir` helpers on `CitySimConfig`.
- [ ] Thread `--area` through `cli.py` and every `pipeline/*.py` `run()`.
- [ ] Parameterize the `viz/*.py` builders and `scenario_server.py`.
- [ ] Verify Logan Square still reproduces (regression).
- [ ] Fetch all 77 community areas; build boundary+network+needs index for each (batch, no sim).
- [ ] Multi-area static site (area picker) + Vercel deploy.
- [ ] Per-area MATSim scenarios, added incrementally.
- [ ] Per-area calibration of cordon scaling / stuckTime.

---

## Honest note

Keep the same integrity as the Logan Square version: the needs index is a **planning signal from
public data, not ground truth**, and simulated deltas are more reliable than absolute values. That
caveat should appear on every area's map.
