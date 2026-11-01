"""Route dedupe: one source per physical line in the routing inputs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from gtfs_ingest import cli
from gtfs_ingest.analysis.pipeline_phase2 import JSON_NAME, AnalysisConfig
from gtfs_ingest.analysis.route_dedupe import (
    ROUTE_DEDUPE,
    duplicate_trips,
    match_routes,
)

from conftest import feed_files, write_feed_dir

RANK = {"old": ("2025-12-31", "2025-06-03"), "new": ("2026-12-31", "2026-01-06")}


def _route_stops(*rows: tuple[str, str, str]) -> pd.DataFrame:
    """("old", "R1", "ABC") = route old:R1 serving stops old:A, old:B, old:C."""
    records = [
        {"feed_id": feed, "route_uid": f"{feed}:{route}", "stop_uid": f"{feed}:{stop}"}
        for feed, route, stops in rows
        for stop in stops
    ]
    return pd.DataFrame(records)


def _canonical(*stops: str) -> pd.DataFrame:
    """Each stop letter is one station across both feeds (old:A = new:A)."""
    rows = [
        {"stop_uid": f"{feed}:{stop}", "canonical_uid": f"old:{stop}", "group_id": i}
        for i, stop in enumerate(stops)
        for feed in ("old", "new")
    ]
    return pd.DataFrame(rows)


def _routes(*rows: tuple[str, str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"route_uid": f"{f}:{r}", "route_short_name": name} for f, r, name in rows]
    )


def test_same_line_under_other_ids_keeps_the_newer_feed() -> None:
    route_stops = _route_stops(("old", "2215-C98", "ABCDE"), ("new", "C98", "ABCDE"))
    routes = _routes(("old", "2215-C98", "C98"), ("new", "C98", "C98"))
    matches = match_routes(route_stops, _canonical(*"ABCDE"), routes, RANK)
    assert matches.to_dict("records") == [
        {
            "kept_route_uid": "new:C98",
            "dropped_route_uid": "old:2215-C98",
            "stop_jaccard": 1.0,
            "same_short_name": True,
            "rule": "newer_calendar",
        }
    ]


def test_shared_short_name_on_other_stops_is_not_a_match() -> None:
    # "L1" is a light-rail line in one feed and a bus line in the other.
    route_stops = _route_stops(("old", "MT_L1", "ABCD"), ("new", "ST_L1", "WXYZ"))
    routes = _routes(("old", "MT_L1", "L1"), ("new", "ST_L1", "L1"))
    assert match_routes(route_stops, _canonical(*"ABCDWXYZ"), routes, RANK).empty


def test_express_variant_inside_a_line_is_not_a_match() -> None:
    route_stops = _route_stops(("old", "MC-L1", "ABCDEFGH"), ("new", "MC-L1E", "ADH"))
    assert match_routes(route_stops, _canonical(*"ABCDEFGH"), _routes(), RANK).empty


def test_matches_are_one_to_one_best_first() -> None:
    # old:R matches new:R1 exactly and new:R2 almost; only the best pair is kept.
    route_stops = _route_stops(
        ("old", "R", "ABCDEFGHIJ"), ("new", "R1", "ABCDEFGHIJ"), ("new", "R2", "ABCDEFGHI")
    )
    matches = match_routes(route_stops, _canonical(*"ABCDEFGHIJ"), _routes(), RANK)
    assert matches[["kept_route_uid", "dropped_route_uid"]].values.tolist() == [["new:R1", "old:R"]]


def _feed(second_trip_lat: float) -> SimpleNamespace:
    """Route R1 with trips T1 and T2 at the same times; T2 over stops X1/X2."""
    stops = pd.DataFrame(
        {
            "stop_id": ["A", "B", "X1", "X2"],
            "stop_lat": [20.0, 20.01, 20.0, second_trip_lat],
            "stop_lon": [-103.0, -103.0, -103.0, -103.0],
        }
    )
    stop_times = pd.DataFrame(
        {
            "trip_id": ["T1", "T1", "T2", "T2"],
            "stop_id": ["A", "B", "X1", "X2"],
            "stop_sequence": [1, 2, 1, 2],
            "arrival_time": [0.0, 300.0, 0.0, 300.0],
            "departure_time": [0.0, 300.0, 0.0, 300.0],
        }
    )
    trips = pd.DataFrame({"trip_id": ["T1", "T2"], "route_id": ["R1", "R1"]})
    return SimpleNamespace(stops=stops, stop_times=stop_times, trips=trips)


def test_trip_listed_twice_over_alias_stops_is_dropped() -> None:
    dropped = duplicate_trips(_feed(second_trip_lat=20.01), "f", radius_m=5.0)
    assert dropped.to_dict("records") == [
        {"feed_id": "f", "route_uid": "f:R1", "kept_trip_id": "T1", "dropped_trip_id": "T2"}
    ]
    # X2 50 m away from B: a different trip, kept.
    assert duplicate_trips(_feed(second_trip_lat=20.01045), "f", radius_m=5.0).empty


def _line_feed(calendar_end: str, route_id: str) -> dict[str, str]:
    """The same three-stop line, published under *route_id*, valid until *calendar_end*."""
    return feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "A,Alpha,40.0,-75.0,0,\nB,Beta,40.01,-75.0,0,\nC,Gamma,40.02,-75.0,0,\n"
            ),
            "routes.txt": (
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                f"{route_id},A1,10,Main Line,3\n"
            ),
            "trips.txt": f"route_id,service_id,trip_id,direction_id\n{route_id},WK,T1,0\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,A,1\nT1,08:05:00,08:05:00,B,2\nT1,08:10:00,08:10:00,C,3\n"
            ),
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                f"start_date,end_date\nWK,1,1,1,1,1,0,0,20260101,{calendar_end}\n"
            ),
        }
    )


def _run(tmp_path: Path, out: str, *extra: str) -> Path:
    feeds = tmp_path / "feeds"
    if not feeds.exists():
        write_feed_dir(feeds, "older", _line_feed("20260630", "2215-R1"))
        write_feed_dir(feeds, "newer", _line_feed("20261231", "R1"))
    args = ["analyze-frequency", "--feeds", str(feeds), "--report", str(tmp_path / out)]
    assert cli.main([*args, "--service-date", "2026-01-06", "--dry-run", *extra]) == 0
    return tmp_path / out


def test_pipeline_keeps_one_copy_in_routing_and_both_in_routes_csv(tmp_path: Path) -> None:
    out = _run(tmp_path, "dedupe")
    matches = pd.read_csv(out / "route_equivalences.csv")
    assert matches[["kept_route_uid", "dropped_route_uid"]].values.tolist() == [
        ["newer:R1", "older:2215-R1"]
    ]
    edges = pd.read_csv(out / "station_network_edges.csv")
    rides = edges[edges["edge_type"] == "in_vehicle"]
    assert set(rides["route_uid"]) == {"newer:R1"}
    routes = pd.read_csv(out / "routes.csv").set_index("route_uid")
    assert routes.loc["older:2215-R1", "duplicate_of"] == "newer:R1"
    assert pd.isna(routes.loc["newer:R1", "duplicate_of"])
    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    assert report["graph"]["n_routes"] == 1
    assert report["route_dedupe"]["dropped_routes_by_feed"] == {"older": 1}

    out = _run(tmp_path, "both", "--no-route-dedupe")
    rides = pd.read_csv(out / "station_network_edges.csv").query("edge_type == 'in_vehicle'")
    assert set(rides["route_uid"]) == {"newer:R1", "older:2215-R1"}
    assert pd.read_csv(out / "route_equivalences.csv").empty
    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    assert report["graph"]["n_routes"] == 2
    assert report["route_dedupe"] == {"enabled": False}


def test_route_dedupe_default_is_the_same_in_cli_and_config() -> None:
    args = cli.build_parser().parse_args(["analyze-frequency", "--feeds", "x", "--dry-run"])
    assert args.route_dedupe is ROUTE_DEDUPE
    assert AnalysisConfig(feed_paths=[]).route_dedupe is ROUTE_DEDUPE
