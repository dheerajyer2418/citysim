"""Stage s3: GTFS sourcing and Logan Square transit feed filtering."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from pipeline.download import download_and_extract_zip
from pipeline.gtfs_filter import active_service_ids, filter_feed, load_gtfs_tables, pick_service_date, stops_within
from pipeline.gtfs_matsim import summarize_transit, write_transit_files


def run(cfg) -> None:
    """INPUT: CTA, Metra, and Pace GTFS feeds. OUTPUT: filtered GTFS tables."""
    gtfs = cfg.sources.get("gtfs", {})
    if not gtfs.get("enabled", False):
        print("s3 transit: GTFS sourcing disabled (sources.gtfs.enabled=false); skipping.")
        return

    project_root = Path(getattr(cfg, "project_root", Path.cwd()))
    data_raw = Path(getattr(cfg, "data_raw", project_root / "data" / "raw"))
    data_interim = Path(getattr(cfg, "data_interim", project_root / "data" / "interim"))
    boundary_path = data_interim / "logan_square_boundary.gpkg"
    requested_date = str(gtfs.get("service_date", "2026-07-08"))
    access_buffer_m = float(gtfs.get("access_buffer_m", 1200))
    filtered_feeds: list[tuple[str, dict[str, pd.DataFrame]]] = []

    for feed in gtfs.get("feeds", []):
        if not feed.get("enabled", False):
            continue

        name = str(feed["name"])
        feed_dir = download_and_extract_zip(str(feed["url"]), data_raw / "gtfs" / name)
        tables = load_gtfs_tables(feed_dir)
        service_date = pick_service_date(tables, requested_date)
        active_services = active_service_ids(tables["calendar"], tables["calendar_dates"], service_date)
        kept_stops = stops_within(tables["stops"], boundary_path, cfg.crs, access_buffer_m)
        filtered = filter_feed(tables, set(kept_stops["stop_id"].astype(str)), active_services)
        filtered_feeds.append((name, filtered))

        output_dir = data_interim / "gtfs" / name
        output_dir.mkdir(parents=True, exist_ok=True)
        for table_name, frame in filtered.items():
            frame.to_csv(output_dir / f"{table_name}.txt", index=False)

        print(_summary(name, service_date, kept_stops, filtered))

    if filtered_feeds:
        scenario_dir = project_root / "scenarios" / "logan_square"
        schedule_path = scenario_dir / "transitSchedule.xml.gz"
        vehicles_path = scenario_dir / "transitVehicles.xml.gz"
        write_transit_files(
            filtered_feeds,
            cfg.crs,
            schedule_path,
            vehicles_path,
            gtfs.get("vehicle", {}),
        )
        counts = summarize_transit(filtered_feeds)
        print(
            "s3 transit MATSim: "
            f"stopFacilities={counts['stopFacilities']}; "
            f"transitLines={counts['transitLines']}; "
            f"transitRoutes={counts['transitRoutes']}; "
            f"departures={counts['departures']}; "
            f"vehicles={counts['vehicles']}; "
            f"schedule={schedule_path}; vehicles_file={vehicles_path}"
        )


def _summary(name: str, service_date: str, kept_stops: pd.DataFrame, filtered: dict[str, pd.DataFrame]) -> str:
    routes = filtered["routes"]
    stop_times = filtered["stop_times"]
    if routes.empty:
        route_names: list[str] = []
    else:
        route_column = "route_short_name" if "route_short_name" in routes.columns else "route_id"
        route_names = sorted(routes[route_column].astype(str).unique())

    departures = 0
    if not stop_times.empty:
        stop_sequence = pd.to_numeric(stop_times["stop_sequence"], errors="coerce")
        min_sequence = stop_sequence.groupby(stop_times["trip_id"]).transform("min")
        departures = int((stop_sequence == min_sequence).sum())

    return (
        f"s3 transit {name}: service_date={service_date}; "
        f"area_stops={len(kept_stops)}; routes={len(routes)} {route_names}; "
        f"trips={len(filtered['trips'])}; departures={departures}; stop_time_rows={len(stop_times)}"
    )
