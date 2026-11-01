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
    expand_frequencies,
    parse_hhmm,
    route_rollup,
    stop_departures,
)
from gtfs_ingest.analysis.service_filter import (
    common_service_date,
    load_service_feed,
    resolve_service,
    service_ids_in_range,
)
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
            "route_id,service_id,trip_id,direction_id\nR1,WK,T1,0\nR2,SA,T2,\nR2,WK,T3,0\n"
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
    return pd.DataFrame(list(rows), columns=["route_id", "direction_id", "stop_id", "departure_s"])


# Pure headway math
def test_headways_basic_window() -> None:
    dep = departures(
        ("R1", 0, "S1", parse_hhmm("07:00")),
        ("R1", 0, "S1", parse_hhmm("07:10")),
        ("R1", 0, "S1", parse_hhmm("07:30")),
    )
    result = compute_stop_headways(dep, DEFAULT_WINDOWS)

    (row,) = result[result["time_window"] == "am_peak"].to_dict("records")
    assert row["trip_count"] == 3
    assert row["mean_headway_s"] == 900.0  # gaps 600 and 1200
    assert row["median_headway_s"] == 900.0
    assert row["min_headway_s"] == 600.0
    assert row["max_headway_s"] == 1200.0


def test_headways_across_midnight_post_24h() -> None:
    # GTFS times past 24:00 stay on the service day; a window may span them.
    late = TimeWindow.parse("late", "23:00-25:00")
    dep = departures(
        ("R1", 0, "S1", parse_hhmm("23:40")),
        ("R1", 0, "S1", parse_hhmm("24:00")),  # 86400 s
        ("R1", 0, "S1", parse_hhmm("24:20")),  # 87600 s
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
    assert pd.isna(row["mean_headway_s"])  # headway undefined, row kept
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
    assert row["mean_headway_s"] == 900.0  # mean of 600 and 1200
    assert row["stop_count"] == 3
    assert row["stops_with_headway"] == 2
    assert row["trip_count"] == 2  # max departures at any stop


def test_window_parsing_and_validation() -> None:
    w = TimeWindow.parse("night", "23:00-27:30")
    assert (w.start_s, w.end_s) == (82800, 99000)
    assert w.label == "23:00-27:30"
    with pytest.raises(ValueError):
        TimeWindow.parse("bad", "09:00-06:00")  # end before start
    with pytest.raises(ValueError):
        TimeWindow.parse("bad", "banana")


# Service-date filtering through partridge (calendar + calendar_dates)
def test_weekday_service_selected(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    feed, selection = load_service_feed(source, date(2026, 1, 6))  # Tuesday

    assert selection.source == "explicit"
    assert set(feed.trips["trip_id"]) == {"T1", "T3"}  # WK trips only


def test_saturday_service_selected(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    feed, _ = load_service_feed(source, date(2026, 1, 3))  # Saturday
    assert set(feed.trips["trip_id"]) == {"T2"}


def test_calendar_dates_exception_removes_service(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    # 2026-01-05 is a Monday, but calendar_dates removes WK that day.
    selection = resolve_service(str(source.path), date(2026, 1, 5))
    assert selection.service_ids == frozenset()


def test_typical_weekday_is_the_default(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    _, selection = load_service_feed(source, None)
    assert selection.source == "typical_weekday"
    assert selection.service_ids == frozenset({"WK"})
    assert selection.service_date.weekday() in (1, 2, 3)  # Tuesday-Thursday
    assert selection.service_date != date(2026, 1, 5)  # the calendar_dates exception
    assert "typical weekday" in selection.note


def test_busiest_rule_is_still_available(tmp_path: Path) -> None:
    source = analysis_source(tmp_path)
    _, selection = load_service_feed(source, None, rule="busiest")
    assert selection.source == "busiest"
    assert selection.service_ids  # non-empty


#: Like the Guadalajara feeds: on a holiday, calendar_dates adds Sunday service
#: D on top of Monday-Saturday LS and removes weekday-only LV.
HOLIDAY_FEED = feed_files(
    replace={
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "LS,1,1,1,1,1,1,0,20260101,20261231\n"
            "LV,1,1,1,1,1,0,0,20260101,20261231\n"
            "D,0,0,0,0,0,0,1,20260101,20261231\n"
        ),
        "calendar_dates.txt": ("service_id,date,exception_type\nD,20260317,1\nLV,20260317,2\n"),
        "trips.txt": (
            "route_id,service_id,trip_id\nR1,LS,T1\nR1,LV,T2\nR1,D,T3\nR1,D,T4\nR1,D,T5\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            + "".join(f"T{i},08:00:00,08:00:00,S1,1\n" for i in range(1, 6))
        ),
    }
)


def test_holiday_is_busiest_but_not_the_typical_weekday(tmp_path: Path) -> None:
    from gtfs_ingest.analysis.service_filter import typical_weekday

    path = str(write_feed_dir(tmp_path, "holiday", HOLIDAY_FEED))

    busiest = resolve_service(path, None, rule="busiest")
    typical = typical_weekday(path)

    assert busiest.service_date == date(2026, 3, 17)  # LS + D = 4 trips
    assert typical is not None
    assert typical.service_ids == frozenset({"LS", "LV"})
    assert typical.service_date != date(2026, 3, 17)


def test_typical_weekday_falls_back_to_busiest_without_weekdays(tmp_path: Path) -> None:
    weekend = feed_files(
        replace={
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "SA,0,0,0,0,0,1,0,20260101,20261231\n"
            ),
            "trips.txt": "route_id,service_id,trip_id\nR1,SA,T1\n",
        }
    )
    path = str(write_feed_dir(tmp_path, "weekend", weekend))

    selection = resolve_service(path, None)

    assert selection.source == "busiest"
    assert "no regular weekday" in selection.note


def test_most_common_pattern_breaks_ties_by_trips() -> None:
    from collections import Counter

    from gtfs_ingest.analysis.service_filter import _most_common_pattern

    candidates = {
        date(2026, 1, 6): frozenset({"A"}),
        date(2026, 1, 7): frozenset({"B"}),
    }
    assert _most_common_pattern(candidates, Counter({"A": 1, "B": 5})) == frozenset({"B"})
    assert _most_common_pattern(candidates, Counter()) == frozenset({"A"})  # then by ids


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


# Common service date + date ranges
def test_common_service_date_overlapping_feeds(tmp_path: Path) -> None:
    # Both feeds share 2026 weekdays: the latest common served date wins.
    a = write_feed_dir(tmp_path, "feed_a", ANALYSIS_FEED)
    b = write_feed_dir(tmp_path, "feed_b", feed_files())  # VALID_FEED: WK 2026
    common = common_service_date([str(a), str(b)])
    assert common is not None
    assert common == date(2026, 12, 31)  # Thursday, both serve it


def test_common_service_date_disjoint_feeds_is_none(tmp_path: Path) -> None:
    old = feed_files(
        replace={
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20200101,20201231\n"
            )
        }
    )
    a = write_feed_dir(tmp_path, "old_feed", old)
    b = write_feed_dir(tmp_path, "new_feed", ANALYSIS_FEED)  # 2026
    assert common_service_date([str(a), str(b)]) is None


def test_service_ids_in_range_skips_unserved_days(tmp_path: Path) -> None:
    path = write_feed_dir(tmp_path, "range_feed", ANALYSIS_FEED)
    # Mon 2026-01-05 (removed by calendar_dates) .. Sun 2026-01-11.
    served = service_ids_in_range(str(path), date(2026, 1, 5), date(2026, 1, 11))
    assert sorted(d.isoformat() for d in served) == [
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",  # Tue-Fri (WK)
        "2026-01-10",  # Sat (SA)
    ]
    assert all(ids for ids in served.values())


# frequencies.txt expansion
def _template(*rows: tuple[str, str, float]) -> pd.DataFrame:
    """stop_times rows of (trip_id, stop_id, departure_s)."""
    return pd.DataFrame(list(rows), columns=["trip_id", "stop_id", "departure_time"])


def test_expand_frequencies_basic_runs_and_offsets() -> None:
    st = _template(("T1", "A", 28800.0), ("T1", "B", 29400.0))  # 08:00, 08:10
    freq = pd.DataFrame(
        {
            "trip_id": ["T1"],
            "start_time": [28800.0],
            "end_time": [32400.0],
            "headway_secs": [1200.0],
        }  # 08:00-09:00 @ 20 min
    )
    out = expand_frequencies(st, freq)

    assert len(out) == 6  # 3 runs x 2 stops
    a = out[out["stop_id"] == "A"]["departure_time"].tolist()
    b = out[out["stop_id"] == "B"]["departure_time"].tolist()
    assert a == [28800.0, 30000.0, 31200.0]  # per-run starts
    assert b == [29400.0, 30600.0, 31800.0]  # +10 min offset


def test_expand_frequencies_end_time_is_exclusive() -> None:
    # end - start divisible by headway: no run departs AT end_time.
    st = _template(("T1", "A", 0.0))
    freq = pd.DataFrame(
        {"trip_id": ["T1"], "start_time": [0.0], "end_time": [3600.0], "headway_secs": [1800.0]}
    )
    out = expand_frequencies(st, freq)
    assert out["departure_time"].tolist() == [0.0, 1800.0]  # not 3600.0


def test_expand_frequencies_skips_bad_rows() -> None:
    st = _template(("T1", "A", 0.0))
    freq = pd.DataFrame(
        {
            "trip_id": ["T1", "T1", "T1", "T1"],
            "start_time": [0.0, 0.0, None, 3600.0],
            "end_time": [3600.0, 3600.0, 3600.0, 3600.0],  # last: end == start
            "headway_secs": [0.0, None, 600.0, 600.0],
        }
    )
    assert expand_frequencies(st, freq).empty


def test_expand_frequencies_caps_runaway_rows() -> None:
    st = _template(("T1", "A", 0.0))
    freq = pd.DataFrame(
        {
            "trip_id": ["T1"],
            "start_time": [0.0],
            "end_time": [86400.0],
            "headway_secs": [1.0],
        }  # 86400 runs!
    )
    out = expand_frequencies(st, freq, max_runs=10)
    assert len(out) == 10


def test_expand_frequencies_deterministic() -> None:
    st = _template(("T1", "A", 0.0), ("T1", "B", 60.0), ("T2", "A", 0.0))
    freq = pd.DataFrame(
        {
            "trip_id": ["T1", "T2"],
            "start_time": [0.0, 0.0],
            "end_time": [1800.0, 1800.0],
            "headway_secs": [600.0, 900.0],
        }
    )
    pd.testing.assert_frame_equal(expand_frequencies(st, freq), expand_frequencies(st, freq))


#: ANALYSIS_FEED plus a frequencies.txt turning T1 into 3 runs 08:00-09:00.
FREQUENCY_FEED = {
    **ANALYSIS_FEED,
    "frequencies.txt": ("trip_id,start_time,end_time,headway_secs\nT1,08:00:00,09:00:00,1200\n"),
}


def test_stop_departures_replaces_template_for_frequency_trips(tmp_path: Path) -> None:
    path = write_feed_dir(tmp_path, "freq_feed", FREQUENCY_FEED)
    (source,) = discover_feeds([path])
    feed, _ = load_service_feed(source, date(2026, 1, 6))  # Tuesday: T1 + T3
    dep = stop_departures(feed)

    # T1's template departure at S1 (08:00) is replaced by the 3 runs.
    s1_r1 = sorted(dep[(dep["stop_id"] == "S1") & (dep["route_id"] == "R1")]["departure_s"])
    assert s1_r1 == [28800.0, 30000.0, 31200.0]
    # Non-frequency trip T3 (route R2) keeps its single real departure.
    s1_r2 = dep[(dep["stop_id"] == "S1") & (dep["route_id"] == "R2")]["departure_s"].tolist()
    assert s1_r2 == [9.5 * 3600]  # 09:30


def test_frequency_expansion_raises_window_trip_count(tmp_path: Path) -> None:
    path = write_feed_dir(tmp_path, "freq_feed", FREQUENCY_FEED)
    (source,) = discover_feeds([path])
    feed, _ = load_service_feed(source, date(2026, 1, 6))
    stats = compute_stop_headways(stop_departures(feed), DEFAULT_WINDOWS)

    (row,) = stats[
        (stats["route_id"] == "R1")
        & (stats["stop_id"] == "S1")
        & (stats["time_window"] == "am_peak")
    ].to_dict("records")
    assert row["trip_count"] == 3  # was 1 pre-expansion
    assert row["mean_headway_s"] == 1200.0  # the defined headway
