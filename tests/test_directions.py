"""direction_id relabelled from stop order on routes that mix both senses."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from gtfs_ingest import cli
from gtfs_ingest.analysis.directions import FIX_DIRECTIONS, fix_directions
from gtfs_ingest.analysis.pipeline_phase2 import JSON_NAME, AnalysisConfig

from conftest import feed_files, write_feed_dir


def _stop_times(*trips: tuple[str, str]) -> pd.DataFrame:
    """One row per stop: ("t1", "ABC") is a trip visiting A, B, C in that order."""
    rows = [
        {"trip_id": trip, "stop_id": stop, "stop_sequence": i}
        for trip, stops in trips
        for i, stop in enumerate(stops)
    ]
    return pd.DataFrame(rows)


def _trips(*rows: tuple[str, str, int]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["trip_id", "route_id", "direction_id"])


def test_reversed_trip_under_the_same_direction_is_relabelled() -> None:
    stop_times = _stop_times(("t1", "ABCD"), ("t2", "DCBA"), ("t3", "ABC"))
    trips = _trips(("t1", "R1", 0), ("t2", "R1", 0), ("t3", "R1", 0))
    fixed, fixes = fix_directions(stop_times, trips)
    assert dict(zip(fixed["trip_id"], fixed["direction_id"])) == {"t1": 0, "t2": 1, "t3": 0}
    assert fixes.to_dict("records") == [
        {"route_id": "R1", "trip_id": "t2", "direction_id_feed": 0, "direction_id_used": 1}
    ]


def test_consistent_routes_and_unclear_trips_are_left_alone() -> None:
    # R1 is already labelled by sense; the second R2 trip shares one stop only.
    stop_times = _stop_times(("a", "ABC"), ("b", "CBA"), ("c", "XYZ"), ("d", "ZQ"))
    trips = _trips(("a", "R1", 0), ("b", "R1", 1), ("c", "R2", 0), ("d", "R2", 0))
    fixed, fixes = fix_directions(stop_times, trips)
    assert fixes.empty
    assert fixed is trips


def test_labels_follow_the_majority_of_same_sense_trips() -> None:
    # The longest trip (reference) carries 1, but most trips of its sense carry 0.
    stop_times = _stop_times(("ref", "ABCDE"), ("x", "ABCD"), ("y", "BCDE"), ("z", "EDCB"))
    trips = _trips(("ref", "R1", 1), ("x", "R1", 0), ("y", "R1", 0), ("z", "R1", 0))
    fixed, _ = fix_directions(stop_times, trips)
    assert dict(zip(fixed["trip_id"], fixed["direction_id"])) == {
        "ref": 0,
        "x": 0,
        "y": 0,
        "z": 1,
    }


# Both trips of R1 are labelled direction 0, one per sense, every 10 minutes.
MIXED_FEED = feed_files(
    replace={
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "S1,North,40.0,-75.0,0,\nS2,Middle,40.01,-75.0,0,\nS3,South,40.02,-75.0,0,\n"
        ),
        "trips.txt": "route_id,service_id,trip_id,direction_id\nR1,WK,T1,0\nR1,WK,T2,0\n",
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,07:00:00,07:00:00,S1,1\nT1,07:05:00,07:05:00,S2,2\nT1,07:10:00,07:10:00,S3,3\n"
            "T2,07:00:00,07:00:00,S3,1\nT2,07:07:00,07:07:00,S2,2\nT2,07:10:00,07:10:00,S1,3\n"
        ),
        "frequencies.txt": (
            "trip_id,start_time,end_time,headway_secs\n"
            "T1,06:00:00,09:00:00,600\nT2,06:00:00,09:00:00,600\n"
        ),
    }
)


def _run(tmp_path: Path, out: str, *extra: str) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    feeds = tmp_path / "feeds"
    if not feeds.exists():
        write_feed_dir(feeds, "metro", MIXED_FEED)
    args = ["analyze-frequency", "--feeds", str(feeds), "--report", str(tmp_path / out)]
    args += ["--service-date", "2026-01-06", "--dry-run", *extra]
    assert cli.main(args) == 0
    report = json.loads((tmp_path / out / JSON_NAME).read_text(encoding="utf-8"))
    headways = pd.read_csv(tmp_path / out / "route_stop_headways.csv")
    fixes = pd.read_csv(tmp_path / out / "direction_fixes.csv")
    return report, headways, fixes


def _middle_stop(headways: pd.DataFrame) -> pd.DataFrame:
    return headways[(headways["stop_id"] == "S2") & (headways["time_window"] == "am_peak")]


def test_fixed_directions_give_the_published_headway(tmp_path: Path) -> None:
    report, headways, fixes = _run(tmp_path, "fixed")
    middle = _middle_stop(headways)
    assert sorted(middle["direction_id"]) == [0, 1]
    assert set(middle["median_headway_s"]) == {600.0}
    assert fixes[["trip_id", "direction_id_used"]].values.tolist() == [["T2", 1]]
    (feed,) = report["feeds"]
    assert feed["directions_fixed"] == 1

    _, raw, raw_fixes = _run(tmp_path, "raw", "--no-fix-directions")
    middle = _middle_stop(raw)
    # Both senses counted as one direction: the median gap is not the published 600 s.
    assert middle["direction_id"].tolist() == [0]
    assert middle["median_headway_s"].iloc[0] < 600.0
    assert raw_fixes.empty


def test_fix_directions_default_is_the_same_in_cli_and_config() -> None:
    args = cli.build_parser().parse_args(["analyze-frequency", "--feeds", "x", "--dry-run"])
    assert args.fix_directions is FIX_DIRECTIONS
    assert AnalysisConfig(feed_paths=[]).fix_directions is FIX_DIRECTIONS
