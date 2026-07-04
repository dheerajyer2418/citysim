"""Stage s2c: CMAP all-purpose trip-roster demand synthesis."""

from __future__ import annotations

import csv
import gzip
from collections import Counter
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import Iterable, Iterator

from lxml import etree

from pipeline.crosswalk import (
    build_taz_link_crosswalk,
    detect_taz_id_column,
    load_links_from_gpkg,
    sample_activity_coord,
)
from pipeline.plans_io import PersonPlan, format_time, stochastic_count, write_population


PLANS_OUTPUT = "plans.xml.gz"
PT_PLANS_OUTPUT = "plans_pt.xml.gz"
ROSTER_ZIP = "c24q4_100.zip"
ROSTER_CACHE = "cmap_internal_trips.csv"
ROSTER_FIELDS = ("purpose", "mode", "o_zone", "d_zone", "a_zone", "timeperiod", "trips")
RNG_SEED = 42
DEFAULT_TRANSIT_MODES = {"4", "5", "6"}

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


@dataclass(frozen=True)
class PtRosterBuildSummary:
    rows_scanned: int
    internal_transit_trips: int
    car_persons: int
    pt_persons: int
    total_persons: int
    transit_modes: tuple[str, ...]


def is_internal_auto_trip(row: dict[str, str], taz_ids: set[str], auto_modes: set[str]) -> bool:
    return (
        str(row.get("mode", "")) in auto_modes
        and str(row.get("o_zone", "")) in taz_ids
        and str(row.get("d_zone", "")) in taz_ids
    )


def is_internal_transit_trip(row: dict[str, str], taz_ids: set[str], transit_modes: set[str]) -> bool:
    return (
        str(row.get("mode", "")) in transit_modes
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


def iter_internal_transit_rows(
    rows: Iterable[dict[str, str]],
    taz_ids: set[str],
    transit_modes: set[str],
) -> Iterator[dict[str, str]]:
    for row in rows:
        if is_internal_transit_trip(row, taz_ids, transit_modes):
            yield {field: str(row.get(field, "")) for field in ROSTER_FIELDS}


def sample_departure_seconds(timeperiod: str, tod_windows: dict[str, list[int]], rng) -> float:
    if timeperiod not in tod_windows:
        raise KeyError(f"Unknown CMAP timeperiod {timeperiod!r}")
    start, end = tod_windows[timeperiod]
    sampled = float(rng.uniform(float(start), float(end)))
    return sampled % 86400.0


def smooth_departure_seconds(departure: float, jitter_std_seconds: float, rng) -> float:
    """Apply deterministic zero-mean departure jitter and wrap within one day."""
    if jitter_std_seconds <= 0.0:
        return float(departure) % 86400.0
    if hasattr(rng, "normal"):
        jitter = float(rng.normal(0.0, float(jitter_std_seconds)))
    else:
        jitter = float(rng.gauss(0.0, float(jitter_std_seconds)))
    return (float(departure) + jitter) % 86400.0


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
    departure_jitter_std_seconds: float = 0.0,
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
            departure = smooth_departure_seconds(departure, departure_jitter_std_seconds, rng)
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


def generate_transit_person_plans(
    rows: Iterable[dict[str, str]],
    sample_fraction: float,
    tod_windows: dict[str, list[int]],
    rng,
    departure_jitter_std_seconds: float = 0.0,
) -> Iterator[PersonPlan]:
    for person_id, activities in generate_person_plans(
        rows,
        sample_fraction,
        tod_windows,
        rng,
        departure_jitter_std_seconds=departure_jitter_std_seconds,
    ):
        yield (f"pt_{person_id}", activities, "pt")


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


def _write_combined_pt_population(car_plans_path: Path, pt_persons: Iterable[PersonPlan], output_path: Path) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    car_persons = 0
    pt_persons_written = 0
    with gzip.open(output_path, "wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write(b'<!DOCTYPE population SYSTEM "http://www.matsim.org/files/dtd/population_v6.dtd">\n')
        with etree.xmlfile(handle, encoding="UTF-8") as xf:
            with xf.element("population"):
                with gzip.open(car_plans_path, "rb") as car_handle:
                    for _, element in etree.iterparse(car_handle, events=("end",), tag="person"):
                        xf.write(element)
                        car_persons += 1
                        element.clear()
                for person in pt_persons:
                    if len(person) == 2:
                        person_id, activities_iter = person
                        leg_mode = "car"
                    else:
                        person_id, activities_iter, leg_mode = person
                    activities = list(activities_iter)
                    with xf.element("person", id=str(person_id)):
                        with xf.element("plan", selected="yes"):
                            for index, (act_type, x, y, end_time) in enumerate(activities):
                                attributes = {
                                    "type": str(act_type),
                                    "x": f"{float(x):.3f}",
                                    "y": f"{float(y):.3f}",
                                }
                                if end_time is not None:
                                    attributes["end_time"] = format_time(float(end_time))
                                xf.write(etree.Element("activity", **attributes))
                                if index < len(activities) - 1:
                                    xf.write(etree.Element("leg", mode=str(leg_mode)))
                    pt_persons_written += 1
    return car_persons, pt_persons_written


def build_pt_plans(cfg) -> PtRosterBuildSummary:
    """Build transit-only scenario plans: existing car persons plus new PT riders."""
    import geopandas as gpd
    import numpy as np

    taz_path = cfg.data_interim / "logan_square_taz.gpkg"
    links_path = cfg.data_interim / "network_links.gpkg"
    for path in (taz_path, links_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing required prior-stage artifact: {path}")

    car_plans_path = cfg.project_root / "scenarios" / "logan_square" / PLANS_OUTPUT
    if not car_plans_path.exists():
        raise FileNotFoundError(f"Missing car-only plans file: {car_plans_path}")

    taz_gdf = gpd.read_file(taz_path).to_crs(cfg.crs)
    links = load_links_from_gpkg(links_path)
    build_taz_link_crosswalk(taz_gdf, links)
    taz_ids = _taz_ids_from_table(taz_gdf)

    roster_cfg = cfg.sources["cmap"]["roster"]
    member = str(roster_cfg["member"])
    transit_modes = {str(mode) for mode in roster_cfg.get("transit_modes", sorted(DEFAULT_TRANSIT_MODES))}
    tod_windows = roster_cfg["tod_windows"]
    zip_path = cfg.data_raw / ROSTER_ZIP
    if not zip_path.exists():
        raise FileNotFoundError(f"Missing CMAP roster zip: {zip_path}")

    rows_scanned = 0
    internal_transit_trips = 0

    def filtered_rows() -> Iterator[dict[str, str]]:
        nonlocal rows_scanned, internal_transit_trips
        for row in _open_roster_reader(zip_path, member):
            rows_scanned += 1
            if not is_internal_transit_trip(row, taz_ids, transit_modes):
                continue
            slim = {field: str(row.get(field, "")) for field in ROSTER_FIELDS}
            trips = _parse_positive_trips(slim["trips"])
            if trips <= 0:
                continue
            internal_transit_trips += trips
            yield slim

    rng = np.random.default_rng(RNG_SEED)
    pt_plans = generate_transit_person_plans(
        filtered_rows(),
        cfg.scenario.sample_fraction,
        tod_windows,
        rng,
        departure_jitter_std_seconds=cfg.scenario.departure_jitter_std_seconds,
    )
    output_path = cfg.project_root / "scenarios" / "logan_square" / PT_PLANS_OUTPUT
    car_persons, pt_persons = _write_combined_pt_population(car_plans_path, pt_plans, output_path)
    return PtRosterBuildSummary(
        rows_scanned=rows_scanned,
        internal_transit_trips=internal_transit_trips,
        car_persons=car_persons,
        pt_persons=pt_persons,
        total_persons=car_persons + pt_persons,
        transit_modes=tuple(sorted(transit_modes)),
    )


def run_pt(cfg) -> None:
    """INPUT: car-only plans and CMAP roster. OUTPUT: MATSim plans_pt.xml.gz for transit scenario only."""
    summary = build_pt_plans(cfg)
    print(
        "s2pt complete: "
        f"rows_scanned={summary.rows_scanned}; "
        f"transit_modes={list(summary.transit_modes)}; "
        f"internal_transit_trips={summary.internal_transit_trips}; "
        f"car_persons={summary.car_persons}; "
        f"pt_persons={summary.pt_persons}; "
        f"total_persons={summary.total_persons}"
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
        departure_jitter_std_seconds=cfg.scenario.departure_jitter_std_seconds,
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
        f"departure_jitter_std_seconds={cfg.scenario.departure_jitter_std_seconds:g}; "
        f"purpose_trips={summary.purpose_trips}; "
        f"timeperiod_trips={summary.timeperiod_trips}"
    )
