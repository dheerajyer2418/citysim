from __future__ import annotations

import gzip

import geopandas as gpd
import pandas as pd
from lxml import etree
from pyproj import Transformer
from shapely.geometry import box

from pipeline.cmap_demand import is_internal_transit_trip, iter_internal_transit_rows
from pipeline.gtfs_filter import active_service_ids, filter_feed, pick_service_date, stops_within
from pipeline.gtfs_matsim import build_transit_schedule_xml, build_transit_vehicles_xml, gtfs_time_to_seconds
from pipeline.plans_io import write_population


def test_active_service_ids_applies_weekday_and_exceptions() -> None:
    calendar = pd.DataFrame(
        [
            {
                "service_id": "weekday",
                "monday": "1",
                "tuesday": "1",
                "wednesday": "1",
                "thursday": "1",
                "friday": "1",
                "saturday": "0",
                "sunday": "0",
                "start_date": "20260701",
                "end_date": "20260731",
            },
            {
                "service_id": "weekend",
                "monday": "0",
                "tuesday": "0",
                "wednesday": "0",
                "thursday": "0",
                "friday": "0",
                "saturday": "1",
                "sunday": "1",
                "start_date": "20260701",
                "end_date": "20260731",
            },
        ]
    )
    calendar_dates = pd.DataFrame(
        [
            {"service_id": "weekday", "date": "20260708", "exception_type": "2"},
            {"service_id": "special", "date": "20260708", "exception_type": "1"},
        ]
    )

    assert active_service_ids(calendar, calendar_dates, "2026-07-08") == {"special"}


def test_pick_service_date_falls_back_to_most_active_trips() -> None:
    tables = {
        "calendar": pd.DataFrame(
            [
                {
                    "service_id": "few",
                    "monday": "1",
                    "tuesday": "0",
                    "wednesday": "0",
                    "thursday": "0",
                    "friday": "0",
                    "saturday": "0",
                    "sunday": "0",
                    "start_date": "20260706",
                    "end_date": "20260706",
                },
                {
                    "service_id": "many",
                    "monday": "0",
                    "tuesday": "1",
                    "wednesday": "0",
                    "thursday": "0",
                    "friday": "0",
                    "saturday": "0",
                    "sunday": "0",
                    "start_date": "20260707",
                    "end_date": "20260707",
                },
            ]
        ),
        "calendar_dates": pd.DataFrame(),
        "trips": pd.DataFrame(
            [
                {"trip_id": "t1", "service_id": "few"},
                {"trip_id": "t2", "service_id": "many"},
                {"trip_id": "t3", "service_id": "many"},
            ]
        ),
    }

    assert pick_service_date(tables, "2026-08-01") == "2026-07-07"


def test_stops_within_uses_buffered_boundary(tmp_path) -> None:
    boundary_path = tmp_path / "boundary.gpkg"
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:26971", always_xy=True)
    x, y = transformer.transform(-87.632218, 41.878868)
    gpd.GeoDataFrame({"geometry": [box(x - 50, y - 50, x + 50, y + 50)]}, crs="EPSG:26971").to_file(boundary_path)
    stops = pd.DataFrame(
        [
            {"stop_id": "in", "stop_lat": "41.878868", "stop_lon": "-87.632218"},
            {"stop_id": "out", "stop_lat": "41.9", "stop_lon": "-87.7"},
        ]
    )

    kept = stops_within(stops, boundary_path, "EPSG:26971", 0)

    assert list(kept["stop_id"]) == ["in"]


def test_filter_feed_retains_whole_trip_for_area_stop() -> None:
    tables = {
        "stops": pd.DataFrame(
            [
                {"stop_id": "a"},
                {"stop_id": "b"},
                {"stop_id": "c"},
            ]
        ),
        "routes": pd.DataFrame(
            [
                {"route_id": "r1", "route_short_name": "1"},
                {"route_id": "r2", "route_short_name": "2"},
            ]
        ),
        "trips": pd.DataFrame(
            [
                {"route_id": "r1", "service_id": "svc", "trip_id": "t1"},
                {"route_id": "r2", "service_id": "svc", "trip_id": "t2"},
                {"route_id": "r1", "service_id": "inactive", "trip_id": "t3"},
            ]
        ),
        "stop_times": pd.DataFrame(
            [
                {"trip_id": "t1", "stop_id": "a", "stop_sequence": "1"},
                {"trip_id": "t1", "stop_id": "b", "stop_sequence": "2"},
                {"trip_id": "t2", "stop_id": "c", "stop_sequence": "1"},
                {"trip_id": "t3", "stop_id": "a", "stop_sequence": "1"},
            ]
        ),
        "calendar": pd.DataFrame([{"service_id": "svc"}]),
        "calendar_dates": pd.DataFrame(),
    }

    filtered = filter_feed(tables, {"a"}, {"svc"})

    assert list(filtered["trips"]["trip_id"]) == ["t1"]
    assert list(filtered["stop_times"]["stop_id"]) == ["a", "b"]
    assert list(filtered["stops"]["stop_id"]) == ["a", "b"]
    assert list(filtered["routes"]["route_id"]) == ["r1"]


def test_gtfs_time_to_seconds_allows_after_midnight_times() -> None:
    assert gtfs_time_to_seconds("25:10:00") == 90600


def test_build_transit_schedule_xml_omits_link_references_and_routes() -> None:
    feeds = [("tiny", _tiny_filtered_feed())]

    xml = gzip.decompress(build_transit_schedule_xml(feeds, "EPSG:26971"))
    assert b"transitSchedule_v2.dtd" in xml
    root = etree.fromstring(xml)

    assert root.tag == "transitSchedule"
    assert len(root.xpath("./transitStops/stopFacility")) == 3
    assert len(root.xpath("./transitLine")) == 2
    assert len(root.xpath("./transitLine/transitRoute")) == 2
    assert len(root.xpath(".//departure")) == 3
    assert root.xpath("count(.//@linkRefId)") == 0
    assert len(root.xpath(".//transitRoute/route")) == 0
    first_stop = root.xpath("./transitLine[@id='tiny_bus']/transitRoute/routeProfile/stop")[0]
    assert first_stop.get("arrivalOffset") == "00:00:00"
    assert first_stop.get("departureOffset") == "00:00:00"
    assert root.xpath(".//departure[@id='bus_late']")[0].get("departureTime") == "25:10:00"


def test_build_transit_vehicles_xml_emits_types_and_trip_vehicles() -> None:
    feeds = [("tiny", _tiny_filtered_feed())]

    xml = gzip.decompress(build_transit_vehicles_xml(feeds, {}))
    assert b"http://www.matsim.org/files/dtd" in xml
    root = etree.fromstring(xml)
    ns = {"m": "http://www.matsim.org/files/dtd"}

    assert root.tag == "{http://www.matsim.org/files/dtd}vehicleDefinitions"
    assert {node.get("id") for node in root.xpath("./m:vehicleType", namespaces=ns)} == {"bus", "rail"}
    assert b'<capacity seats="38" standingRoomInPersons="40"/>' in xml
    assert b"<seats" not in xml
    vehicles = root.xpath("./m:vehicle", namespaces=ns)
    assert len(vehicles) == 3
    assert {vehicle.get("type") for vehicle in vehicles} == {"bus", "rail"}


def test_write_population_defaults_two_tuple_legs_to_car(tmp_path) -> None:
    path = tmp_path / "plans.xml.gz"

    write_population([("car_1", [("home", 1.0, 2.0, 3600.0), ("work", 3.0, 4.0, None)])], path)

    xml = gzip.decompress(path.read_bytes())
    assert b'<!DOCTYPE population SYSTEM "http://www.matsim.org/files/dtd/population_v6.dtd">' in xml
    root = etree.fromstring(xml)
    assert root.xpath("./person/plan/leg")[0].get("mode") == "car"


def test_write_population_uses_three_tuple_leg_mode(tmp_path) -> None:
    path = tmp_path / "plans_pt.xml.gz"

    write_population([("pt_1", [("home", 1.0, 2.0, 3600.0), ("work", 3.0, 4.0, None)], "pt")], path)

    root = etree.fromstring(gzip.decompress(path.read_bytes()))
    assert root.xpath("./person/plan/leg")[0].get("mode") == "pt"


def test_internal_transit_filter_uses_modes_and_internal_taz() -> None:
    rows = [
        {"mode": "4", "o_zone": "101", "d_zone": "102", "purpose": "HBWH", "a_zone": "101", "timeperiod": "AM1", "trips": "1"},
        {"mode": "5", "o_zone": "101", "d_zone": "999", "purpose": "HBWH", "a_zone": "101", "timeperiod": "AM1", "trips": "1"},
        {"mode": "1", "o_zone": "101", "d_zone": "102", "purpose": "HBWH", "a_zone": "101", "timeperiod": "AM1", "trips": "1"},
    ]

    assert is_internal_transit_trip(rows[0], {"101", "102"}, {"4", "5", "6"})
    kept = list(iter_internal_transit_rows(rows, {"101", "102"}, {"4", "5", "6"}))

    assert kept == [rows[0]]


def _tiny_filtered_feed() -> dict[str, pd.DataFrame]:
    return {
        "stops": pd.DataFrame(
            [
                {"stop_id": "s1", "stop_lat": "41.9000", "stop_lon": "-87.7000", "stop_name": "One"},
                {"stop_id": "s2", "stop_lat": "41.9010", "stop_lon": "-87.7010", "stop_name": "Two"},
                {"stop_id": "s3", "stop_lat": "41.9020", "stop_lon": "-87.7020", "stop_name": "Three"},
            ]
        ),
        "routes": pd.DataFrame(
            [
                {"route_id": "bus", "route_short_name": "B", "route_long_name": "Bus", "route_type": "3"},
                {"route_id": "rail", "route_short_name": "R", "route_long_name": "Rail", "route_type": "1"},
            ]
        ),
        "trips": pd.DataFrame(
            [
                {"trip_id": "bus_early", "route_id": "bus", "service_id": "svc"},
                {"trip_id": "bus_late", "route_id": "bus", "service_id": "svc"},
                {"trip_id": "rail_one", "route_id": "rail", "service_id": "svc"},
            ]
        ),
        "stop_times": pd.DataFrame(
            [
                {
                    "trip_id": "bus_early",
                    "stop_id": "s1",
                    "stop_sequence": "1",
                    "arrival_time": "08:00:00",
                    "departure_time": "08:00:00",
                },
                {
                    "trip_id": "bus_early",
                    "stop_id": "s2",
                    "stop_sequence": "2",
                    "arrival_time": "08:05:00",
                    "departure_time": "08:05:30",
                },
                {
                    "trip_id": "bus_late",
                    "stop_id": "s1",
                    "stop_sequence": "1",
                    "arrival_time": "25:10:00",
                    "departure_time": "25:10:00",
                },
                {
                    "trip_id": "bus_late",
                    "stop_id": "s2",
                    "stop_sequence": "2",
                    "arrival_time": "25:15:00",
                    "departure_time": "25:15:30",
                },
                {
                    "trip_id": "rail_one",
                    "stop_id": "s2",
                    "stop_sequence": "1",
                    "arrival_time": "09:00:00",
                    "departure_time": "09:00:00",
                },
                {
                    "trip_id": "rail_one",
                    "stop_id": "s3",
                    "stop_sequence": "2",
                    "arrival_time": "09:04:00",
                    "departure_time": "09:04:00",
                },
            ]
        ),
    }
