"""Convert filtered GTFS tables into MATSim transit XML inputs."""

from __future__ import annotations

import gzip
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from lxml import etree
from pyproj import Transformer


RAIL_ROUTE_TYPES = {0, 1, 2}
DEFAULT_VEHICLES = {
    "bus": {"seats": 38, "standing_room": 40, "length_m": 18.0, "width_m": 2.5, "pce": 2.8},
    "rail": {"seats": 500, "standing_room": 500, "length_m": 150.0, "width_m": 3.0, "pce": 0.0},
}
VEHICLE_NS = "http://www.matsim.org/files/dtd"


def gtfs_time_to_seconds(hhmmss: str) -> int:
    """Parse a GTFS HH:MM:SS value, allowing hours greater than 23."""
    parts = str(hhmmss).split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid GTFS time: {hhmmss!r}")
    hours, minutes, seconds = (int(part) for part in parts)
    if minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60 or hours < 0:
        raise ValueError(f"Invalid GTFS time: {hhmmss!r}")
    return hours * 3600 + minutes * 60 + seconds


def build_transit_schedule_xml(feeds: list[tuple[str, dict[str, pd.DataFrame]]], crs_epsg: str | int) -> bytes:
    """Build gzipped MATSim transitSchedule_v2 XML without network link references."""
    root = etree.Element("transitSchedule")
    stops_el = etree.SubElement(root, "transitStops")
    transformer = Transformer.from_crs("EPSG:4326", crs_epsg, always_xy=True)

    for feed_name, tables in feeds:
        stops = tables["stops"].copy()
        if stops.empty:
            continue
        stops["_stop_id"] = stops["stop_id"].astype(str)
        stops = stops.drop_duplicates("_stop_id").sort_values("_stop_id")
        for _, stop in stops.iterrows():
            x, y = transformer.transform(float(stop["stop_lon"]), float(stop["stop_lat"]))
            etree.SubElement(
                stops_el,
                "stopFacility",
                id=_prefixed_id(feed_name, stop["_stop_id"]),
                x=_format_coord(x),
                y=_format_coord(y),
                name=str(stop.get("stop_name", "")),
            )

    for line in _grouped_lines(feeds):
        line_el = etree.SubElement(root, "transitLine", id=_prefixed_id(line.feed_name, line.route_id))
        for pattern_index, group in enumerate(line.route_groups, start=1):
            route_el = etree.SubElement(
                line_el,
                "transitRoute",
                id=f"{_prefixed_id(line.feed_name, line.route_id)}_{pattern_index}",
            )
            etree.SubElement(route_el, "transportMode").text = group.mode
            profile_el = etree.SubElement(route_el, "routeProfile")
            representative = group.trips[0]
            first_departure = representative.stop_times[0]["departure_seconds"]
            for stop_time in representative.stop_times:
                etree.SubElement(
                    profile_el,
                    "stop",
                    refId=_prefixed_id(line.feed_name, stop_time["stop_id"]),
                    arrivalOffset=_seconds_to_hhmmss(stop_time["arrival_seconds"] - first_departure),
                    departureOffset=_seconds_to_hhmmss(stop_time["departure_seconds"] - first_departure),
                )

            departures_el = etree.SubElement(route_el, "departures")
            for trip in group.trips:
                etree.SubElement(
                    departures_el,
                    "departure",
                    id=trip.trip_id,
                    departureTime=_seconds_to_hhmmss(trip.first_departure_seconds),
                    vehicleRefId=trip.trip_id,
                )

    return _gzip_xml(
        root,
        doctype='<!DOCTYPE transitSchedule SYSTEM "http://www.matsim.org/files/dtd/transitSchedule_v2.dtd">',
    )


def build_transit_vehicles_xml(
    feeds: list[tuple[str, dict[str, pd.DataFrame]]],
    vehicle_cfg: dict[str, Any] | None,
) -> bytes:
    """Build gzipped MATSim vehicleDefinitions_v2.0 XML for every filtered trip."""
    nsmap = {
        None: VEHICLE_NS,
        "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    }
    root = etree.Element(
        f"{{{VEHICLE_NS}}}vehicleDefinitions",
        nsmap=nsmap,
        attrib={
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation": (
                f"{VEHICLE_NS} http://www.matsim.org/files/dtd/vehicleDefinitions_v2.0.xsd"
            )
        },
    )

    merged_cfg = _vehicle_config(vehicle_cfg)
    for mode in ("bus", "rail"):
        vehicle_type = etree.SubElement(root, f"{{{VEHICLE_NS}}}vehicleType", id=mode)
        etree.SubElement(
            vehicle_type,
            f"{{{VEHICLE_NS}}}capacity",
            seats=str(int(merged_cfg[mode]["seats"])),
            standingRoomInPersons=str(int(merged_cfg[mode]["standing_room"])),
        )
        etree.SubElement(vehicle_type, f"{{{VEHICLE_NS}}}length", meter=str(float(merged_cfg[mode]["length_m"])))
        etree.SubElement(vehicle_type, f"{{{VEHICLE_NS}}}width", meter=str(float(merged_cfg[mode]["width_m"])))
        etree.SubElement(vehicle_type, f"{{{VEHICLE_NS}}}passengerCarEquivalents", pce=str(float(merged_cfg[mode]["pce"])))
        etree.SubElement(vehicle_type, f"{{{VEHICLE_NS}}}networkMode", networkMode="car")
        etree.SubElement(vehicle_type, f"{{{VEHICLE_NS}}}flowEfficiencyFactor", factor="1.0")

    for trip in _iter_trips_with_modes(feeds):
        etree.SubElement(root, f"{{{VEHICLE_NS}}}vehicle", id=trip["trip_id"], type=trip["mode"])

    return _gzip_xml(root)


def write_transit_files(
    feeds: list[tuple[str, dict[str, pd.DataFrame]]],
    crs_epsg: str | int,
    schedule_path: str | Path,
    vehicles_path: str | Path,
    vehicle_cfg: dict[str, Any] | None = None,
) -> None:
    """Write MATSim transitSchedule.xml.gz and transitVehicles.xml.gz."""
    schedule_path = Path(schedule_path)
    vehicles_path = Path(vehicles_path)
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    vehicles_path.parent.mkdir(parents=True, exist_ok=True)
    schedule_path.write_bytes(build_transit_schedule_xml(feeds, crs_epsg))
    vehicles_path.write_bytes(build_transit_vehicles_xml(feeds, vehicle_cfg))


def summarize_transit(feeds: list[tuple[str, dict[str, pd.DataFrame]]]) -> dict[str, int]:
    """Return high-level counts for generated MATSim transit inputs."""
    line_ids: set[tuple[str, str]] = set()
    route_count = 0
    departures = 0
    vehicles = 0
    stop_facilities = 0

    for feed_name, tables in feeds:
        stops = tables["stops"]
        stop_facilities += int(stops["stop_id"].astype(str).nunique()) if not stops.empty else 0
        line_ids.update((feed_name, route_id) for route_id in tables["trips"]["route_id"].astype(str).unique())
        departures += len(tables["trips"])
        vehicles += len(tables["trips"])

    for line in _grouped_lines(feeds):
        route_count += len(line.route_groups)

    return {
        "stopFacilities": stop_facilities,
        "transitLines": len(line_ids),
        "transitRoutes": route_count,
        "departures": departures,
        "vehicles": vehicles,
    }


class _Trip:
    def __init__(self, trip_id: str, stop_times: list[dict[str, Any]]) -> None:
        self.trip_id = trip_id
        self.stop_times = stop_times
        self.first_departure_seconds = int(stop_times[0]["departure_seconds"])


class _RouteGroup:
    def __init__(self, mode: str, stop_ids: tuple[str, ...], trips: list[_Trip]) -> None:
        self.mode = mode
        self.stop_ids = stop_ids
        self.trips = trips


class _Line:
    def __init__(self, feed_name: str, route_id: str, route_groups: list[_RouteGroup]) -> None:
        self.feed_name = feed_name
        self.route_id = route_id
        self.route_groups = route_groups


def _grouped_lines(feeds: list[tuple[str, dict[str, pd.DataFrame]]]) -> list[_Line]:
    lines: list[_Line] = []
    for feed_name, tables in feeds:
        routes = tables["routes"]
        trips = tables["trips"]
        stop_times = tables["stop_times"]
        if trips.empty or stop_times.empty:
            continue

        route_modes = _route_modes(routes)
        stop_times_by_trip = _stop_times_by_trip(stop_times)
        groups: dict[tuple[str, tuple[str, ...]], list[_Trip]] = defaultdict(list)
        for _, trip in trips.sort_values("trip_id").iterrows():
            trip_id = str(trip["trip_id"])
            route_id = str(trip["route_id"])
            ordered_stop_times = stop_times_by_trip.get(trip_id, [])
            if not ordered_stop_times:
                continue
            stop_ids = tuple(item["stop_id"] for item in ordered_stop_times)
            groups[(route_id, stop_ids)].append(_Trip(trip_id, ordered_stop_times))

        route_ids = sorted({route_id for route_id, _ in groups})
        for route_id in route_ids:
            route_groups = []
            matching = [(stop_ids, trip_list) for (group_route_id, stop_ids), trip_list in groups.items() if group_route_id == route_id]
            matching.sort(key=lambda item: (item[1][0].first_departure_seconds, item[0]))
            for stop_ids, trip_list in matching:
                trip_list.sort(key=lambda trip: (trip.first_departure_seconds, trip.trip_id))
                route_groups.append(_RouteGroup(route_modes.get(route_id, "bus"), stop_ids, trip_list))
            lines.append(_Line(feed_name, route_id, route_groups))

    return lines


def _iter_trips_with_modes(feeds: list[tuple[str, dict[str, pd.DataFrame]]]) -> list[dict[str, str]]:
    trips_with_modes: list[dict[str, str]] = []
    for _, tables in feeds:
        route_modes = _route_modes(tables["routes"])
        trips = tables["trips"]
        for _, trip in trips.sort_values("trip_id").iterrows():
            route_id = str(trip["route_id"])
            trips_with_modes.append({"trip_id": str(trip["trip_id"]), "mode": route_modes.get(route_id, "bus")})
    return trips_with_modes


def _stop_times_by_trip(stop_times: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    frame = stop_times.copy()
    frame["_trip_id"] = frame["trip_id"].astype(str)
    frame["_stop_id"] = frame["stop_id"].astype(str)
    frame["_stop_sequence"] = pd.to_numeric(frame["stop_sequence"], errors="coerce")
    frame = frame.dropna(subset=["_stop_sequence"]).sort_values(["_trip_id", "_stop_sequence"])

    grouped: dict[str, list[dict[str, Any]]] = {}
    for trip_id, rows in frame.groupby("_trip_id", sort=False):
        grouped[trip_id] = [
            {
                "stop_id": str(row["_stop_id"]),
                "arrival_seconds": gtfs_time_to_seconds(str(row["arrival_time"])),
                "departure_seconds": gtfs_time_to_seconds(str(row["departure_time"])),
            }
            for _, row in rows.iterrows()
        ]
    return grouped


def _route_modes(routes: pd.DataFrame) -> dict[str, str]:
    modes: dict[str, str] = {}
    if routes.empty:
        return modes
    for _, route in routes.iterrows():
        route_id = str(route["route_id"])
        try:
            route_type = int(str(route.get("route_type", "")))
        except ValueError:
            route_type = -1
        modes[route_id] = "rail" if route_type in RAIL_ROUTE_TYPES else "bus"
    return modes


def _vehicle_config(vehicle_cfg: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    cfg = vehicle_cfg or {}
    merged: dict[str, dict[str, float]] = {}
    for mode, defaults in DEFAULT_VEHICLES.items():
        values = dict(defaults)
        values.update(cfg.get(mode, {}) or {})
        merged[mode] = values
    return merged


def _seconds_to_hhmmss(seconds: int) -> str:
    if seconds < 0:
        raise ValueError(f"Negative MATSim time offset: {seconds}")
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _prefixed_id(feed_name: str, item_id: str) -> str:
    return f"{feed_name}_{item_id}"


def _format_coord(value: float) -> str:
    return f"{value:.3f}"


def _gzip_xml(root: etree._Element, doctype: str | None = None) -> bytes:
    xml_bytes = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
        doctype=doctype,
    )
    buffer = BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
        gz.write(xml_bytes)
    return buffer.getvalue()
