"""Small GTFS table filters for the Logan Square transit preprocessing stage."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd


GTFS_TABLES = ("stops", "routes", "trips", "stop_times", "calendar", "calendar_dates")
OPTIONAL_TABLES = {"calendar", "calendar_dates"}


def load_gtfs_tables(feed_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Read core GTFS .txt tables from an extracted feed directory."""
    root = Path(feed_dir)
    tables: dict[str, pd.DataFrame] = {}
    for table in GTFS_TABLES:
        path = root / f"{table}.txt"
        if not path.exists():
            if table in OPTIONAL_TABLES:
                tables[table] = pd.DataFrame()
                continue
            raise FileNotFoundError(f"Missing required GTFS table: {path}")
        tables[table] = pd.read_csv(path, dtype=str, keep_default_na=False)
    return tables


def active_service_ids(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, service_date: str) -> set[str]:
    """Return GTFS service_ids active on service_date using calendar exceptions."""
    target = _parse_service_date(service_date)
    target_yyyymmdd = target.strftime("%Y%m%d")
    active: set[str] = set()

    if not calendar.empty:
        weekday = target.strftime("%A").lower()
        start_dates = _text_column(calendar, "start_date")
        end_dates = _text_column(calendar, "end_date")
        weekdays = _text_column(calendar, weekday)
        rows = calendar[
            (start_dates <= target_yyyymmdd)
            & (end_dates >= target_yyyymmdd)
            & (weekdays == "1")
        ]
        active.update(rows["service_id"].astype(str))

    if not calendar_dates.empty:
        dated = calendar_dates[_text_column(calendar_dates, "date") == target_yyyymmdd]
        exception_types = _text_column(dated, "exception_type")
        removals = set(dated[exception_types == "2"]["service_id"].astype(str))
        additions = set(dated[exception_types == "1"]["service_id"].astype(str))
        active.difference_update(removals)
        active.update(additions)

    return active


def pick_service_date(tables: dict[str, pd.DataFrame], requested_date: str) -> str:
    """Pick requested_date if active; otherwise choose the date with the most active trips."""
    calendar = tables.get("calendar", pd.DataFrame())
    calendar_dates = tables.get("calendar_dates", pd.DataFrame())
    trips = tables.get("trips", pd.DataFrame())
    if active_service_ids(calendar, calendar_dates, requested_date):
        return requested_date

    best_date = requested_date
    best_trip_count = -1
    for candidate in _candidate_service_dates(calendar, calendar_dates, requested_date):
        services = active_service_ids(calendar, calendar_dates, candidate)
        if trips.empty or "service_id" not in trips:
            trip_count = len(services)
        else:
            trip_count = int(trips["service_id"].astype(str).isin(services).sum())
        if trip_count > best_trip_count:
            best_date = candidate
            best_trip_count = trip_count

    return best_date


def stops_within(stops_df: pd.DataFrame, boundary_gpkg_path: str | Path, crs_epsg: str | int, buffer_m: float) -> pd.DataFrame:
    """Keep GTFS stops inside the buffered Logan Square boundary."""
    if stops_df.empty:
        return stops_df.copy()

    stops = stops_df.copy()
    stop_points = gpd.GeoDataFrame(
        stops,
        geometry=gpd.points_from_xy(pd.to_numeric(stops["stop_lon"]), pd.to_numeric(stops["stop_lat"])),
        crs="EPSG:4326",
    ).to_crs(crs_epsg)
    boundary = gpd.read_file(boundary_gpkg_path).to_crs(crs_epsg)
    buffered_boundary = boundary.geometry.union_all().buffer(float(buffer_m))
    mask = stop_points.geometry.intersects(buffered_boundary)
    return stops.loc[mask.to_numpy()].reset_index(drop=True)


def filter_feed(tables: dict[str, pd.DataFrame], kept_stop_ids: set[str], active_services: set[str]) -> dict[str, pd.DataFrame]:
    """Filter GTFS tables to active trips serving at least one kept stop."""
    stops = tables["stops"]
    routes = tables["routes"]
    trips = tables["trips"]
    stop_times = tables["stop_times"]

    stop_time_stop_ids = stop_times["stop_id"].astype(str)
    area_trip_ids = set(stop_times.loc[stop_time_stop_ids.isin(set(map(str, kept_stop_ids))), "trip_id"].astype(str))
    active_trips = trips[
        trips["service_id"].astype(str).isin(set(map(str, active_services)))
        & trips["trip_id"].astype(str).isin(area_trip_ids)
    ].copy()

    kept_trip_ids = set(active_trips["trip_id"].astype(str))
    filtered_stop_times = stop_times[stop_times["trip_id"].astype(str).isin(kept_trip_ids)].copy()
    referenced_stop_ids = set(filtered_stop_times["stop_id"].astype(str))
    filtered_stops = stops[stops["stop_id"].astype(str).isin(referenced_stop_ids)].copy()
    route_ids = set(active_trips["route_id"].astype(str))
    filtered_routes = routes[routes["route_id"].astype(str).isin(route_ids)].copy()

    return {
        "stops": filtered_stops.reset_index(drop=True),
        "routes": filtered_routes.reset_index(drop=True),
        "trips": active_trips.reset_index(drop=True),
        "stop_times": filtered_stop_times.reset_index(drop=True),
        "calendar": tables.get("calendar", pd.DataFrame()).copy().reset_index(drop=True),
        "calendar_dates": tables.get("calendar_dates", pd.DataFrame()).copy().reset_index(drop=True),
    }


def _parse_service_date(service_date: str) -> date:
    return datetime.strptime(service_date, "%Y-%m-%d").date()


def _text_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series([""] * len(frame), index=frame.index, dtype=str)
    return frame[column].astype(str)


def _candidate_service_dates(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, requested_date: str) -> list[str]:
    candidates: set[date] = set()

    if not calendar.empty:
        for _, row in calendar.iterrows():
            try:
                start = datetime.strptime(str(row["start_date"]), "%Y%m%d").date()
                end = datetime.strptime(str(row["end_date"]), "%Y%m%d").date()
            except (KeyError, TypeError, ValueError):
                continue
            if end < start:
                continue
            for offset in range(min((end - start).days + 1, 370)):
                candidates.add(start + timedelta(days=offset))

    if not calendar_dates.empty and "date" in calendar_dates:
        for value in calendar_dates["date"].astype(str):
            try:
                candidates.add(datetime.strptime(value, "%Y%m%d").date())
            except ValueError:
                continue

    if not candidates:
        candidates.add(_parse_service_date(requested_date))

    return [candidate.strftime("%Y-%m-%d") for candidate in sorted(candidates)]
