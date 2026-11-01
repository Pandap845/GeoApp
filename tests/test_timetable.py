"""Timetable exports: connections (frequencies expanded) and footpaths."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from gtfs_ingest import cli
from gtfs_ingest.analysis.pipeline_phase2 import JSON_NAME, AnalysisConfig
from gtfs_ingest.analysis.timetable import (
    CONNECTIONS_NAME,
    FOOTPATHS_NAME,
    MIN_TRANSFER_S,
    feed_connections,
    footpaths,
)

from conftest import write_feed_dir
from test_connectivity import ANALYSIS_FEED, OTHER_FEED


def _feed() -> SimpleNamespace:
    """T1 A->B->C (dwell at B), T2 A->C every 10 min 08:00-08:30, T3 after midnight."""
    stop_times = pd.DataFrame(
        {
            "trip_id": ["T1", "T1", "T1", "T2", "T2", "T3", "T3"],
            "stop_id": ["A", "B", "C", "A", "C", "C", "A"],
            "stop_sequence": [1, 2, 3, 1, 2, 1, 2],
            "arrival_time": [28800.0, 29100.0, 29400.0, 0.0, 600.0, 90600.0, 91200.0],
            "departure_time": [28800.0, 29160.0, 29400.0, 0.0, 600.0, 90600.0, 91200.0],
        }
    )
    trips = pd.DataFrame(
        {
            "trip_id": ["T1", "T2", "T3"],
            "route_id": ["R1", "R2", "R1"],
            "direction_id": [0, 0, 1],
        }
    )
    frequencies = pd.DataFrame(
        {"trip_id": ["T2"], "start_time": [28800.0], "end_time": [30600.0], "headway_secs": [600]}
    )
    return SimpleNamespace(stop_times=stop_times, trips=trips, frequencies=frequencies)


def test_connections_expand_runs_and_keep_dwell_times() -> None:
    connections = feed_connections(_feed(), "f")
    rows = connections.values.tolist()
    assert rows == [
        ["f:T1", "f:R1", 0, "f:A", "f:B", 28800, 29100],
        ["f:T2@08:00:00", "f:R2", 0, "f:A", "f:C", 28800, 29400],
        ["f:T1", "f:R1", 0, "f:B", "f:C", 29160, 29400],  # departs after its dwell
        ["f:T2@08:10:00", "f:R2", 0, "f:A", "f:C", 29400, 30000],
        ["f:T2@08:20:00", "f:R2", 0, "f:A", "f:C", 30000, 30600],
        ["f:T3", "f:R1", 1, "f:C", "f:A", 90600, 91200],  # 25:10, never wrapped
    ]


def test_connections_ignore_input_order() -> None:
    feed = _feed()
    shuffled = SimpleNamespace(
        stop_times=feed.stop_times.iloc[::-1].reset_index(drop=True),
        trips=feed.trips.iloc[::-1].reset_index(drop=True),
        frequencies=feed.frequencies,
    )
    pd.testing.assert_frame_equal(feed_connections(shuffled, "f"), feed_connections(feed, "f"))


def test_footpaths_kinds_and_change_time() -> None:
    pairs = pd.DataFrame({"stop_uid_a": ["f:A", "f:B"], "stop_uid_b": ["f:B", "f:Z"]})
    pairs["distance_m"] = [160.0, 50.0]
    aliases = pd.DataFrame({"stop_uid_a": ["f:A"], "stop_uid_b": ["g:A"]})
    paths = footpaths(["f:A", "f:B", "g:A"], pairs, aliases, min_transfer_s=90)
    got = {(a, b, kind): s for a, b, s, kind in paths.values.tolist()}
    assert got == {
        ("f:A", "f:A", "self"): 90,
        ("f:B", "f:B", "self"): 90,
        ("g:A", "g:A", "self"): 90,
        ("f:A", "g:A", "same_station"): 90,
        ("g:A", "f:A", "same_station"): 90,
        # 160 m is 2 walking minutes; the f:B-f:Z pair has an unserved stop.
        ("f:A", "f:B", "walk"): 120,
        ("f:B", "f:A", "walk"): 120,
    }


def _run(tmp_path: Path, out: str, *extra: str) -> Path:
    feeds = tmp_path / "feeds"
    if not feeds.exists():
        write_feed_dir(feeds, "metro", ANALYSIS_FEED)
        write_feed_dir(feeds, "other", OTHER_FEED)
    args = ["analyze-frequency", "--feeds", str(feeds), "--report", str(tmp_path / out)]
    assert cli.main([*args, "--dry-run", *extra]) == 0
    return tmp_path / out


def test_analyze_writes_a_reproducible_timetable(tmp_path: Path) -> None:
    out = _run(tmp_path, "a", "--service-date", "2026-01-06")
    again = _run(tmp_path, "b", "--service-date", "2026-01-06")
    for name in (CONNECTIONS_NAME, FOOTPATHS_NAME):
        assert (out / name).read_bytes() == (again / name).read_bytes()

    connections = pd.read_csv(out / CONNECTIONS_NAME)
    assert connections["dep_s"].is_monotonic_increasing
    assert set(connections["time_quality"]) <= {"ok", "zero", "too_fast", "too_slow"}
    paths = pd.read_csv(out / FOOTPATHS_NAME)
    served = set(connections["from_stop"]) | set(connections["to_stop"])
    self_paths = paths[paths["kind"] == "self"]
    assert set(self_paths["from_stop"]) == served
    assert set(self_paths["walk_s"]) == {MIN_TRANSFER_S}

    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))["timetable"]
    assert report["written"] is True
    assert report["connections"] == len(connections)

    custom = _run(tmp_path, "c", "--service-date", "2026-01-06", "--min-transfer-s", "30")
    custom_paths = pd.read_csv(custom / FOOTPATHS_NAME)
    assert set(custom_paths.loc[custom_paths["kind"] == "self", "walk_s"]) == {30}


def test_date_ranges_write_no_timetable(tmp_path: Path) -> None:
    out = _run(tmp_path, "range", "--service-date-range", "2026-01-05..2026-01-07")
    assert not (out / CONNECTIONS_NAME).exists()
    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))["timetable"]
    assert report["written"] is False


def test_min_transfer_default_is_the_same_in_cli_and_config() -> None:
    args = cli.build_parser().parse_args(["analyze-frequency", "--feeds", "x", "--dry-run"])
    assert args.min_transfer_s == MIN_TRANSFER_S
    assert AnalysisConfig(feed_paths=[]).min_transfer_s == MIN_TRANSFER_S
