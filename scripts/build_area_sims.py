import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
JDK_HOME = ROOT / "tools" / "jdk-17.0.19+10"
JAVA_EXE = JDK_HOME / "bin" / "java.exe"
MATSIM_CLASSPATH = ROOT / "matsim" / "build" / "install" / "citysim-matsim" / "lib" / "*"


def run_step(name, argv, cwd, env=None, retries=1):
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        suffix = f" (attempt {attempt}/{attempts})" if attempts > 1 else ""
        print(f"\n[{timestamp}] {name}{suffix}", flush=True)

        started = time.monotonic()
        result = subprocess.run(argv, cwd=str(cwd), env=env)
        elapsed_minutes = (time.monotonic() - started) / 60.0
        print(f"{name} elapsed: {elapsed_minutes:.2f} minutes", flush=True)

        if result.returncode == 0:
            return True

        print(f"{name} failed with return code {result.returncode}", flush=True)
        if attempt < attempts:
            print(f"Retrying {name}", flush=True)

    return False


def python_argv(*args):
    return [str(VENV_PYTHON), *args]


def java_env():
    env = os.environ.copy()
    env["JAVA_HOME"] = str(JDK_HOME)
    env["PATH"] = str(JDK_HOME / "bin") + os.pathsep + env.get("PATH", "")
    return env


def skip_needs_scenarios(area):
    manifest = ROOT / "data" / "interim" / area / "user_scenarios" / "manifest.json"
    if not manifest.exists():
        return False

    with manifest.open("r", encoding="utf-8") as handle:
        manifest_data = json.load(handle)

    entries = manifest_data.get("scenarios", []) if isinstance(manifest_data, dict) else manifest_data
    if not entries:
        return False

    scenario_dir = ROOT / "scenarios" / area
    for entry in entries:
        output_dir = entry.get("output_dir")
        if not output_dir:
            return False
        if not (scenario_dir / output_dir / "output_events.xml.gz").exists():
            return False

    return True


def maybe_run_step(step_number, name, argv, cwd, should_skip, skip_existing, env=None):
    if skip_existing and should_skip:
        print(f"SKIP step {step_number}: {name}", flush=True)
        return True
    return run_step(f"step {step_number}: {name}", argv, cwd, env=env)


def run_area(area, skip_existing):
    scenario_dir = ROOT / "scenarios" / area
    # data/interim is namespaced per-area for every area except logan_square.
    data_interim = ROOT / "data" / "interim" if area == "logan_square" else ROOT / "data" / "interim" / area
    jenv = java_env()

    steps = [
        (
            1,
            "demand s2c",
            python_argv("cli.py", "run", "--area", area, "--stage", "s2c"),
            ROOT,
            # s2c writes the internal trip roster (and an internal-only plans.xml.gz);
            # key off its distinctive interim output, NOT plans.xml.gz.
            data_interim / "cmap_internal_trips.csv",
            None,
        ),
        (
            2,
            "demand s2d",
            python_argv("cli.py", "run", "--area", area, "--stage", "s2d"),
            ROOT,
            # s2d finalizes plans (internal + cordon) and creates config.xml, which
            # s5 depends on; config.xml is the reliable "s2d done" signal.
            scenario_dir / "config.xml",
            None,
        ),
        (
            3,
            "interventions s5",
            python_argv("cli.py", "run", "--area", area, "--stage", "s5"),
            ROOT,
            (scenario_dir / "config_fixed.xml", scenario_dir / "config_baseline.xml"),
            None,
        ),
        (
            4,
            "baseline FIXED",
            [
                str(JAVA_EXE),
                "-Xmx8g",
                "-cp",
                str(MATSIM_CLASSPATH),
                "citysim.RunCitySim",
                "config_fixed.xml",
            ],
            scenario_dir,
            scenario_dir / "output_fixed" / "output_events.xml.gz",
            jenv,
        ),
        (
            5,
            "baseline POTHOLES",
            [
                str(JAVA_EXE),
                "-Xmx8g",
                "-cp",
                str(MATSIM_CLASSPATH),
                "citysim.RunCitySim",
                "config_baseline.xml",
            ],
            scenario_dir,
            scenario_dir / "output_baseline" / "output_events.xml.gz",
            jenv,
        ),
        (
            6,
            "needs road diets",
            python_argv("pipeline/build_needs_scenarios.py", "--area", area),
            ROOT,
            None,
            None,
        ),
    ]

    for step_number, name, argv, cwd, skip_signal, env in steps:
        if step_number == 3:
            should_skip = all(path.exists() for path in skip_signal)
        elif step_number == 6:
            should_skip = skip_needs_scenarios(area)
        else:
            should_skip = skip_signal.exists()

        if not maybe_run_step(step_number, name, argv, cwd, should_skip, skip_existing, env=env):
            print(f"FAILED {area}: {name}", flush=True)
            return False, step_number, name

    optional_steps = [
        (
            7,
            "monetize s6",
            python_argv("cli.py", "run", "--area", area, "--stage", "s6"),
            ROOT,
        ),
        (
            8,
            "viz",
            python_argv("viz/build_live_viz.py", "--area", area),
            ROOT,
        ),
    ]

    for step_number, name, argv, cwd in optional_steps:
        if not run_step(f"step {step_number}: {name}", argv, cwd):
            print(f"FAILED optional step for {area}: {name}", flush=True)

    return True, None, None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build one or more CitySim area simulation artifacts."
    )
    parser.add_argument(
        "--areas",
        required=True,
        help="Comma-separated community area slugs.",
    )
    parser.add_argument(
        "--skip-existing",
        dest="skip_existing",
        action="store_true",
        default=True,
        help="Skip steps whose outputs already exist.",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Run steps even when skip outputs already exist.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    areas = [area.strip() for area in args.areas.split(",") if area.strip()]
    if not areas:
        print("No areas provided", file=sys.stderr)
        return 2

    results = []
    for area in areas:
        print(f"\n=== AREA {area} ===", flush=True)
        ok, step_number, step_name = run_area(area, args.skip_existing)
        results.append((area, ok, step_number, step_name))

    print("\nSummary", flush=True)
    for area, ok, step_number, step_name in results:
        if ok:
            print(f"{area}: OK", flush=True)
        else:
            print(f"{area}: FAILED at step {step_number}: {step_name}", flush=True)

    return 0 if all(ok for _, ok, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
