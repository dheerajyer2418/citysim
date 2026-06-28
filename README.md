# CitySim

CitySim is a greenfield traffic cost-benefit simulator scaffold for a Logan Square, Chicago MATSim scenario. The project is intentionally stubbed: it defines the pipeline structure, configuration, command-line entry points, and Java MATSim shell without fetching or processing real data.

## Layout

- `params.yaml` centralizes scenario, source, CRS, and monetization placeholders.
- `pipeline/` contains Python stages `s0` through `s6`.
- `matsim/` contains a Gradle Java project for MATSim runs.
- `scenarios/logan_square/` contains placeholder MATSim config wiring.
- `data/raw`, `data/interim`, and `data/processed` are local working directories.

## Toolchain / Setup

Use the local Python 3.11 virtual environment. Python 3.14 is intentionally avoided because Windows geospatial wheels for packages such as GeoPandas, Pyrosm, and Fiona are less reliable there.

```powershell
C:\Users\dheer\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`environment.yml` is retained as a conda-compatible reference, but conda is not required for this scaffold. `osmium-tool` is an optional external CLI and is not installed by `requirements.txt`.

A JDK 17+ must be installed manually before the `matsim/` build and MATSim-dependent stages work. This includes S1 `pt2matsim` conversion and S4 calibration runs. The Gradle wrapper scripts are scaffolded; if `matsim/gradle/wrapper/gradle-wrapper.jar` is absent, generate it with:

```powershell
cd matsim
gradle wrapper --gradle-version 8.10.2
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
.\gradlew.bat build
```

Run classes after building and replacing placeholder MATSim inputs:

```powershell
.\gradlew.bat runCitySim
.\gradlew.bat calibrateWithCadyts
```

The Java classes reference `../scenarios/logan_square/config.xml`. Real `network.xml.gz`, `plans.xml.gz`, transit files, and calibration inputs are TODOs for the pipeline stages.

## Tests

```powershell
python -m pytest tests/
```

The scaffold tests verify Python imports and CLI stage registration.
