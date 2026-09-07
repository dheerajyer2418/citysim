import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


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


def maybe_run_step(step_number, name, argv, cwd, should_skip, skip_existing, env=None):
    if skip_existing and should_skip:
        print(f"SKIP step {step_number}: {name}", flush=True)
        return True
    return run_step(f"step {step_number}: {name}", argv, cwd, env=env, retries=2)


def run_area(area, skip_existing):
    # Data directories are namespaced per-area except for logan_square.
    data_interim = ROOT / "data" / "interim" if area == "logan_square" else ROOT / "data" / "interim" / area
    data_processed = ROOT / "data" / "processed" if area == "logan_square" else ROOT / "data" / "processed" / area

    steps = [
        (
            1,
            "boundary s0",
            python_argv("cli.py", "run", "--area", area, "--stage", "s0"),
            ROOT,
            data_interim / f"{area}_boundary.gpkg",
            None,
        ),
        (
            2,
            "network s1",
            python_argv("cli.py", "run", "--area", area, "--stage", "s1"),
            ROOT,
            data_interim / "network_links.gpkg",
            None,
        ),
        (
            3,
            "needs index s7",
            python_argv("cli.py", "run", "--area", area, "--stage", "s7"),
            ROOT,
            data_processed / "needs_index_summary.json",
            None,
        ),
    ]

    for step_number, name, argv, cwd, skip_signal, env in steps:
        should_skip = skip_signal.exists()
        if not maybe_run_step(step_number, name, argv, cwd, should_skip, skip_existing, env=env):
            print(f"FAILED {area} at step {step_number}: {name}", flush=True)
            return False, step_number, name

    return True, None, None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build one or more CitySim area needs maps using s0, s1, and s7."
    )
    parser.add_argument(
        "--areas",
        help="Comma-separated community area slugs. Defaults to all areas in params.yaml.",
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
    if args.areas is None:
        try:
            with (ROOT / "params.yaml").open("r", encoding="utf-8") as handle:
                params = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            print(f"Could not read areas from params.yaml: {exc}", file=sys.stderr)
            return 2
        configured_areas = params.get("areas", {}) if isinstance(params, dict) else {}
        if not isinstance(configured_areas, dict):
            print("Expected an areas mapping in params.yaml", file=sys.stderr)
            return 2
        areas = [area for area in configured_areas if isinstance(area, str) and area.strip()]
    else:
        areas = [area.strip() for area in args.areas.split(",") if area.strip()]
    if not areas:
        print("No areas provided", file=sys.stderr)
        return 2

    results = []
    for area in areas:
        print(f"\n=== AREA {area} (needs) ===", flush=True)
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
