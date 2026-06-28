# CitySim

CitySim is a greenfield traffic cost-benefit simulator scaffold for a Logan Square, Chicago MATSim scenario. The project is intentionally stubbed: it defines the pipeline structure, configuration, command-line entry points, and Java MATSim shell without fetching or processing real data.

## Layout

- `params.yaml` centralizes scenario, source, CRS, and monetization placeholders.
- `pipeline/` contains Python stages `s0` through `s6`.
- `matsim/` contains a Gradle Java project for MATSim runs.
- `scenarios/logan_square/` contains placeholder MATSim config wiring.
- `data/raw`, `data/interim`, and `data/processed` are local working directories.

## Python Environment

Create the intended conda environment:

```powershell
conda env create -f environment.yml
conda activate citysim
```

No real data is downloaded by this scaffold.

## Run Pipeline Stages

List stages:

```powershell
python cli.py run --help
```

Run all stages:

```powershell
python cli.py run
```

Run one stage:

```powershell
python cli.py run --stage s0
python cli.py run --stage s6
```

Each current stage prints a TODO stub and returns without producing real artifacts.

## Java MATSim

From the `matsim` directory:

```powershell
gradle build
```

Run classes after building and replacing placeholder MATSim inputs:

```powershell
gradle runCitySim
gradle calibrateWithCadyts
```

The Java classes reference `../scenarios/logan_square/config.xml`. Real `network.xml.gz`, `plans.xml.gz`, transit files, and calibration inputs are TODOs for the pipeline stages.

## Tests

```powershell
python -m pytest tests/
```

The scaffold tests verify Python imports and CLI stage registration.
