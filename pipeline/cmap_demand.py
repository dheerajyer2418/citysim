"""Stage s2c: CMAP all-purpose trip-roster demand synthesis."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import Iterable, Iterator

from pipeline.crosswalk import (
    build_taz_link_crosswalk,
    detect_taz_id_column,
    load_links_from_gpkg,
    sample_activity_coord,
)
from pipeline.plans_io import PersonPlan, stochastic_count, write_population


PLANS_OUTPUT = "plans.xml.gz"
ROSTER_ZIP = "c24q4_100.zip"
ROSTER_CACHE = "cmap_internal_trips.csv"
ROSTER_FIELDS = ("purpose", "mode", "o_zone", "d_zone", "a_zone", "timeperiod", "trips")
RNG_SEED = 42

PURPOSE_ACTIVITY_TYPES = {
    "HBWH": "work",
    "HBWL": "work",
    "HBO": "other",
    "HBS": "shop",
    "NHB": "other",
    "VISIT": "visit",
    "DEAD": "other",
}
NO_HOME_ANCHOR_PURPOSES = {"NHB", "DEAD"}


@dataclass(frozen=True)
class RosterBuildSummary:
    rows_scanned: int | str
    internal_auto_trips: int
    agents_written: int
    purpose_trips: dict[str, int]
    timeperiod_trips: dict[str, int]


def is_internal_auto_trip(row: dict[str, str], taz_ids: set[str], auto_modes: set[str]) -> bool:
    return (
        str(row.get("mode", "")) in auto_modes
        and str(row.get("o_zone", "")) in taz_ids
        and str(row.get("d_zone", "")) in taz_ids
    )


def iter_internal_auto_rows(
    rows: Iterable[dict[str, str]],
    taz_ids: set[str],
    auto_modes: set[str],
) -> Iterator[dict[str, str]]:
    for row in rows:
        if is_internal_auto_trip(row, taz_ids, auto_modes):
            yield {field: str(row.get(field, "")) for field in ROSTER_FIELDS}


def sample_departure_seconds(timeperiod: str, tod_windows: dict[str, list[int]], rng) -> float:
    if timeperiod not in tod_windows:
        raise KeyError(f"Unknown CMAP timeperiod {timeperiod!r}")
    start, end = tod_windows[timeperiod]
    sampled = float(rng.uniform(float(start), float(end)))
    return sampled % 86400.0


def activity_types_for_trip(purpose: str, o_zone: str, d_zone: str, a_zone: str) -> tuple[str, str]:
    purpose = str(purpose)
    if purpose in NO_HOME_ANCHOR_PURPOSES:
        return ("other", "other")

    non_home_type = PURPOSE_ACTIVITY_TYPES.get(purpose, "other")
    origin_type = "home" if str(o_zone) == str(a_zone) else non_home_type
    dest_type = "home" if str(d_zone) == str(a_zone) else non_home_type
    return (origin_type, dest_type)


def _parse_positive_trips(value: str) -> int:
    try:
        trips = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, trips)


def generate_person_plans(
    rows: Iterable[dict[str, str]],
    sample_fraction: float,
    tod_windows: dict[str, list[int]],
    rng,
) -> Iterator[PersonPlan]:
    person_number = 0
    for row in rows:
        trips = _parse_positive_trips(row.get("trips", "0"))
        if trips <= 0:
            continue
        purpose = str(row["purpose"])
        timeperiod = str(row["timeperiod"])
        count = stochastic_count(trips * sample_fraction, rng)
        for _ in range(count):
            departure = sample_departure_seconds(timeperiod, tod_windows, rng)
            origin_type, dest_type = activity_types_for_trip(
                purpose,
                str(row["o_zone"]),
                str(row["d_zone"]),
                str(row.get("a_zone", "")),
            )
            o_x, o_y = sample_activity_coord(str(row["o_zone"]))
            d_x, d_y = sample_activity_coord(str(row["d_zone"]))
            yield (
                f"cmap_{person_number:08d}",
                [
                    (origin_type, o_x, o_y, departure),
                    (dest_type, d_x, d_y, None),
                ],
            )
            person_number += 1


def _open_roster_reader(zip_path: Path, member: str) -> Iterator[dict[str, str]]:
    import zipfile

    import zipfile_deflate64  # noqa: F401  # registers DEFLATE64 support with zipfile

    with zipfile.ZipFile(zip_path) as archive:
        with archive.open(member) as binary:
            text = TextIOWrapper(binary, encoding="utf-8", newline="")
            yield from csv.DictReader(text)


def _write_filtered_cache(
    source_rows: Iterable[dict[str, str]],
    cache_path: Path,
    taz_ids: set[str],
    auto_modes: set[str],
) -> tuple[int, int, dict[str, int], dict[str, int]]:
    rows_scanned = 0
    internal_auto_trips = 0
    purpose_counter: Counter[str] = Counter()
    timeperiod_counter: Counter[str] = Counter()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROSTER_FIELDS)
        writer.writeheader()
        for row in source_rows:
            rows_scanned += 1
            if not is_internal_auto_trip(row, taz_ids, auto_modes):
                continue
            slim = {field: str(row.get(field, "")) for field in ROSTER_FIELDS}
            trips = _parse_positive_trips(slim["trips"])
            if trips <= 0:
                continue
            writer.writerow(slim)
            internal_auto_trips += trips
            purpose_counter[slim["purpose"]] += trips
            timeperiod_counter[slim["timeperiod"]] += trips
    temp_path.replace(cache_path)
    return rows_scanned, internal_auto_trips, dict(purpose_counter), dict(timeperiod_counter)


def _read_filtered_cache(cache_path: Path) -> Iterator[dict[str, str]]:
    with cache_path.open("r", encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def _taz_ids_from_table(taz_gdf) -> set[str]:
    column = "taz_id" if "taz_id" in getattr(taz_gdf, "columns", []) else detect_taz_id_column(taz_gdf)
    return {str(value) for value in taz_gdf[column].dropna()}


def _make_summary_from_cache(rows: Iterable[dict[str, str]], agents_written: int) -> RosterBuildSummary:
    purpose_counter: Counter[str] = Counter()
    timeperiod_counter: Counter[str] = Counter()
    internal_auto_trips = 0
    for row in rows:
        trips = _parse_positive_trips(row.get("trips", "0"))
        internal_auto_trips += trips
        purpose_counter[str(row.get("purpose", ""))] += trips
        timeperiod_counter[str(row.get("timeperiod", ""))] += trips
    return RosterBuildSummary(
        rows_scanned="cache",
        internal_auto_trips=internal_auto_trips,
        agents_written=agents_written,
        purpose_trips=dict(purpose_counter),
        timeperiod_trips=dict(timeperiod_counter),
    )


def run(cfg) -> None:
    """INPUT: CMAP c24q4 trip roster, Logan Square TAZ, and S1 links. OUTPUT: MATSim plans.xml.gz."""
    import geopandas as gpd
    import numpy as np

    taz_path = cfg.data_interim / "logan_square_taz.gpkg"
    links_path = cfg.data_interim / "network_links.gpkg"
    for path in (taz_path, links_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing required prior-stage artifact: {path}")

    taz_gdf = gpd.read_file(taz_path).to_crs(cfg.crs)
    links = load_links_from_gpkg(links_path)
    build_taz_link_crosswalk(taz_gdf, links)
    taz_ids = _taz_ids_from_table(taz_gdf)

    roster_cfg = cfg.sources["cmap"]["roster"]
    member = str(roster_cfg["member"])
    auto_modes = {str(mode) for mode in roster_cfg.get("auto_modes", [1, 2, 3])}
    tod_windows = roster_cfg["tod_windows"]
    zip_path = cfg.data_raw / ROSTER_ZIP
    cache_path = cfg.data_interim / ROSTER_CACHE
    plans_path = cfg.project_root / "scenarios" / "logan_square" / PLANS_OUTPUT

    # TODO: One-end-internal cordon/through trips (~869k auto) not modeled yet;
    # needs boundary gateway zones - future refinement.
    rows_scanned: int | str = "cache"
    cache_trip_summary: tuple[int, dict[str, int], dict[str, int]] | None = None
    if not cache_path.exists():
        if not zip_path.exists():
            raise FileNotFoundError(f"Missing CMAP roster zip: {zip_path}")
        scanned, trips, purpose_trips, timeperiod_trips = _write_filtered_cache(
            _open_roster_reader(zip_path, member),
            cache_path,
            taz_ids,
            auto_modes,
        )
        rows_scanned = scanned
        cache_trip_summary = (trips, purpose_trips, timeperiod_trips)

    rng = np.random.default_rng(RNG_SEED)
    plans = generate_person_plans(
        _read_filtered_cache(cache_path),
        cfg.scenario.sample_fraction,
        tod_windows,
        rng,
    )
    agents_written = 0

    def counted_plans() -> Iterator[PersonPlan]:
        nonlocal agents_written
        for plan in plans:
            agents_written += 1
            yield plan

    write_population(counted_plans(), plans_path)

    if cache_trip_summary is None:
        summary = _make_summary_from_cache(_read_filtered_cache(cache_path), agents_written)
    else:
        trips, purpose_trips, timeperiod_trips = cache_trip_summary
        summary = RosterBuildSummary(
            rows_scanned=rows_scanned,
            internal_auto_trips=trips,
            agents_written=agents_written,
            purpose_trips=purpose_trips,
            timeperiod_trips=timeperiod_trips,
        )

    print(
        "s2c complete: "
        f"rows_scanned={summary.rows_scanned}; "
        f"internal_internal_auto_trips={summary.internal_auto_trips}; "
        f"agents_written={summary.agents_written}; "
        f"purpose_trips={summary.purpose_trips}; "
        f"timeperiod_trips={summary.timeperiod_trips}"
    )
