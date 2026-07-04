"""Headway math + service-date filtering tests (incl. post-24:00 times)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from gtfs_ingest.analysis.headways import (
    DEFAULT_WINDOWS,
    TimeWindow,
    compute_stop_headways,
    parse_hhmm,
    route_rollup,
    stop_departures,
)
from gtfs_ingest.analysis.service_filter import load_service_feed, resolve_service
from gtfs_ingest.loader import discover_feeds

from conftest import feed_files, write_feed_dir

# A feed with weekday (WK) and Saturday (SA) services, a calendar_dates
# exception removing Monday 2026-01-05, and a post-24:00 departure.
ANALYSIS_FEED = feed_files(
    replace={
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WK,1,1,1,1,1,0,0,20260101,20261231\n"
            "SA,0,0,0,0,0,1,0,20260101,20261231\n"
        ),
        "calendar_dates.txt": "service_id,date,exception_type\nWK,20260105,2\n",
        "routes.txt": (
            "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            "R1,A1,10,Main Line,3\n"
            "R2,A1,20,Cross Line,3\n"
        ),
        "trips.txt": (
            "route_id,service_id,trip_id,direction_id\n"
            "R1,WK,T1,0\n"
            "R2,SA,T2,\n"
            "R2,WK,T3,0\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,S1,1\n"
            "T1,25:10:00,25:10:00,S3,2\n"
            "T2,10:00:00,10:00:00,S1,1\n"
            "T3,09:30:00,09:30:00,S1,1\n"
            "T3,09:40:00,09:40:00,S3,2\n"
        ),
    }
)


def analysis_source(tmp_path: Path):
    path = write_feed_dir(tmp_path, "analysis_feed", ANALYSIS_FEED)
    (source,) = discover_feeds([path])
    return source


def departures(*rows: tuple) -> pd.DataFrame:
    """Rows of (route_id, direction_id, stop_id, departure_s)."""
    return pd.DataFrame(
        list(rows), columns=["route_id", "direction_id", "stop_id", "departure_s"]
    )


# --------------------------------------------------------------------------
# Pure headway math
# --------------------------------------------------------------------------

def test_headways_basic_window() -> None:
    dep = departures(
        ("R1", 0, "S1", parse_hhmm("07:00")),
        ("R1", 0, "S1", parse_hhmm("07:10")),
        ("R1", 0, "S1", parse_hhmm("07:30")),
    )
    result = compute_stop_headways(dep, DEFAULT_WINDOWS)

    (row,) = result[result["time_window"] == "am_peak"].to_dict("records")
    assert row["trip_count"] == 3
    assert row["mean_headway_s"] == 900.0    # gaps 600 and 1200
    assert row["median_headway_s"] == 900.0
    assert row["min_headway_s"] == 600.0
    assert row["max_headway_s"] == 1200.0


def test_headways_across_midnight_post_24h() -> None:
    # GTFS times past 24:00 stay on the service day; a window may span them.
    late = TimeWindow.parse("late", "23:00-25:00")
    dep = departures(
        ("R1", 0, "S1", parse_hhmm("23:40")),
        ("R1", 0, "S1", parse_hhmm("24:00")),   # 86400 s
        ("R1", 0, "S1", parse_hhmm("24:20")),   # 87600 s
    )
    result = compute_stop_headways(dep, [late])

    (row,) = result.to_dict("records")
    assert row["trip_count"] == 3
    assert row["mean_headway_s"] == 1200.0  # 20 min, computed across 24:00


def test_single_departure_window_marked_not_crashed() -> None:
    dep = departures(("R1", 0, "S1", parse_hhmm("07:00")))
    result = compute_stop_headways(dep, DEFAULT_WINDOWS)

    (row,) = result.to_dict("records")
    assert row["time_window"] == "am_peak"
    assert row["trip_count"] == 1
    assert pd.isna(row["mean_headway_s"])   # headway undefined, row kept
    assert pd.isna(row["median_headway_s"])


def test_zero_departure_windows_are_omitted() -> None:
    dep = departures(("R1", 0, "S1", parse_hhmm("07:00")))
    result = compute_stop_headways(dep, DEFAULT_WINDOWS)
    assert set(result["time_window"]) == {"am_peak"}


def test_route_rollup_averages_stop_means() -> None:
    dep = departures(
        # S1: 10-min headway; S2: 20-min headway; S3: single departure.
        ("R1", 0, "S1", parse_hhmm("07:00")),
        ("R1", 0, "S1", parse_hhmm("07:10")),
        ("R1", 0, "S2", parse_hhmm("07:00")),
        ("R1", 0, "S2", parse_hhmm("07:20")),
        ("R1", 0, "S3", parse_hhmm("07:05")),
    )
    rollup = route_rollup(compute_stop_headways(dep, DEFAULT_WINDOWS))

    (row,) = rollup.to_dict("records")
    assert row["mean_headway_s"] == 900.0     # mean of 600 and 1200
    assert row["stop_count"] == 3
    assert row["stops_with_headway"] == 2
    assert row["trip_count"] == 2             # max departures at any stop


def test_window_parsing_and_validation() -> None:
    w = TimeWindow.parse("night", "23:00-27:30")
    assert (w.start_s, w.end_s) == (82800, 99000)
    assert w.label == "23:00-27:30"
    with pytest.raises(ValueError):
        TimeWindow.parse("bad", "09:00-06:00")   # end before start
    with pytest.raises(ValueError):
        TimeWindow.parse("bad", "banana")


# --------------------------------------------------------------------------
# Service-date filtering through partridge (calendar + calendar_dates)
# --------------------------------------------------------------------------

def test_weekday_service_selected(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    feed, selection = load_service_feed(source, date(2026, 1, 6))  # Tuesday

    assert selection.source == "explicit"
    assert set(feed.trips["trip_id"]) == {"T1", "T3"}   # WK trips only


def test_saturday_service_selected(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    feed, _ = load_service_feed(source, date(2026, 1, 3))  # Saturday
    assert set(feed.trips["trip_id"]) == {"T2"}


def test_calendar_dates_exception_removes_service(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    # 2026-01-05 is a Monday, but calendar_dates removes WK that day.
    selection = resolve_service(str(source.path), date(2026, 1, 5))
    assert selection.service_ids == frozenset()


def test_busiest_date_fallback(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    _, selection = load_service_feed(source, None)
    assert selection.source == "busiest"
    assert selection.service_ids     # non-empty


def test_post_24h_times_parsed_as_seconds(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    feed, _ = load_service_feed(source, date(2026, 1, 6))
    dep = stop_departures(feed)

    t1_s3 = dep[(dep["stop_id"] == "S3") & (dep["route_id"] == "R1")]
    assert t1_s3["departure_s"].tolist() == [25 * 3600 + 10 * 60]  # 90600


def test_direction_id_blank_becomes_sentinel(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    feed, _ = load_service_feed(source, date(2026, 1, 3))  # T2, blank direction
    dep = stop_departures(feed)
    assert set(dep["direction_id"]) == {-1}
