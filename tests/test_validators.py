"""Tests for the integrity validators (one scenario per required case)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from gtfs_ingest.models import REQUIRED_FILES, Finding, Severity
from gtfs_ingest.validators import calendar_range, optional_file_matrix, validate_feed

from conftest import feed_files, load_dir_feed, load_stops_csv_feed


def by_code(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity is Severity.ERROR]


def test_valid_feed_has_no_errors(valid_feed) -> None:
    findings = validate_feed(valid_feed)
    assert errors(findings) == []
    # The optional-file inventory INFO finding is always present.
    assert by_code(findings, "optional_files")


def test_missing_required_file(tmp_path: Path) -> None:
    loaded = load_dir_feed(tmp_path, "no_stop_times", feed_files(omit=("stop_times.txt",)))
    findings = validate_feed(loaded)
    (finding,) = by_code(findings, "missing_required_file")
    assert finding.severity is Severity.ERROR
    assert finding.file == "stop_times.txt"


def test_missing_both_calendar_files(tmp_path: Path) -> None:
    loaded = load_dir_feed(tmp_path, "no_calendar", feed_files(omit=("calendar.txt",)))
    findings = validate_feed(loaded)
    assert by_code(findings, "missing_calendar")


def test_calendar_dates_satisfies_calendar_requirement(tmp_path: Path) -> None:
    files = feed_files(
        omit=("calendar.txt",),
        replace={
            "calendar_dates.txt": "service_id,date,exception_type\nWK,20260102,1\n"
        },
    )
    loaded = load_dir_feed(tmp_path, "cal_dates_only", files)
    assert not by_code(validate_feed(loaded), "missing_calendar")


def test_bad_coordinates_flagged(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "S1,Ok,40.0,-75.0\n"
                "S2,Lat out of range,95.0,-75.0\n"
                "S3,Lon not a number,40.0,abc\n"
                "S4,Missing lat,,-75.0\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,S1,1\n"
                "T1,08:10:00,08:10:00,S2,2\n"
            ),
        }
    )
    loaded = load_dir_feed(tmp_path, "bad_coords", files)
    coord_findings = by_code(validate_feed(loaded), "invalid_coordinates")
    assert coord_findings and all(f.severity is Severity.ERROR for f in coord_findings)
    # lat column: S2 (out of range) + S4 (missing); lon column: S3 (unparseable)
    flagged = {sid for f in coord_findings for sid in (f.sample or [])}
    assert flagged == {"S2", "S3", "S4"}


def test_duplicate_stop_ids_flagged(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "S1,First,40.0,-75.0\n"
                "S1,Duplicate,40.1,-75.1\n"
                "S2,Fine,40.2,-75.2\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,S1,1\n"
                "T1,08:10:00,08:10:00,S2,2\n"
            ),
        }
    )
    loaded = load_dir_feed(tmp_path, "dupes", files)
    (finding,) = by_code(validate_feed(loaded), "duplicate_stop_ids")
    assert finding.severity is Severity.ERROR
    assert finding.count == 1
    assert finding.sample == ["S1"]


def test_dangling_parent_station_is_warning(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "S1,Main St,40.0,-75.0,0,GHOST\n"
                "S3,Other,40.01,-75.01,0,\n"
            ),
        }
    )
    loaded = load_dir_feed(tmp_path, "ghost_parent", files)
    (finding,) = by_code(validate_feed(loaded), "dangling_parent_station")
    assert finding.severity is Severity.WARNING
    assert finding.sample == ["GHOST"]


def test_referential_integrity_across_files(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "trips.txt": "route_id,service_id,trip_id\nNO_SUCH_ROUTE,WK,T1\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,NO_SUCH_STOP,1\n"
                "NO_SUCH_TRIP,08:10:00,08:10:00,S1,1\n"
            ),
        }
    )
    loaded = load_dir_feed(tmp_path, "dangling_refs", files)
    findings = by_code(validate_feed(loaded), "dangling_reference")
    referenced_files = sorted(f.file for f in findings)
    assert referenced_files == ["stop_times.txt", "stop_times.txt", "trips.txt"]
    samples = {s for f in findings for s in (f.sample or [])}
    assert {"NO_SUCH_ROUTE", "NO_SUCH_STOP", "NO_SUCH_TRIP"} <= samples


def test_empty_required_file_flagged(tmp_path: Path) -> None:
    # Header-only routes.txt: parseable but zero data rows.
    files = feed_files(replace={"routes.txt": "route_id,route_type\n"})
    loaded = load_dir_feed(tmp_path, "empty_routes", files)
    (finding,) = by_code(validate_feed(loaded), "empty_required_file")
    assert finding.file == "routes.txt"


def test_missing_required_columns_flagged(tmp_path: Path) -> None:
    files = feed_files(
        replace={"stops.txt": "stop_id,stop_name\nS1,No coords here\nS3,Also none\n"}
    )
    loaded = load_dir_feed(tmp_path, "no_coord_cols", files)
    (finding,) = by_code(validate_feed(loaded), "missing_required_columns")
    assert finding.file == "stops.txt"
    assert set(finding.sample or []) == {"stop_lat", "stop_lon"}


def test_unreadable_feed_container_reported(tmp_path: Path) -> None:
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"garbage bytes, not a zip")
    from gtfs_ingest.loader import discover_feeds, load_feed

    (source,) = discover_feeds([bad])
    findings = validate_feed(load_feed(source))
    assert by_code(findings, "feed_unreadable")
    # All required files also reported missing - the report shows full impact.
    assert len(by_code(findings, "missing_required_file")) == len(REQUIRED_FILES)


def test_optional_file_matrix(valid_feed) -> None:
    matrix = optional_file_matrix(valid_feed)
    assert matrix["feed_info.txt"] is True
    assert matrix["calendar.txt"] is True
    assert matrix["shapes.txt"] is False
    assert matrix["transfers.txt"] is False


# --------------------------------------------------------------------------
# Calendar expiry (feed_expired) + calendar_range helper
# --------------------------------------------------------------------------

def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _calendar(start: date, end: date) -> str:
    return (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        f"WK,1,1,1,1,1,0,0,{_ymd(start)},{_ymd(end)}\n"
    )


def test_feed_expired_warning_for_past_calendar(tmp_path: Path) -> None:
    files = feed_files(replace={"calendar.txt": _calendar(date(2020, 1, 1), date(2022, 12, 31))})
    loaded = load_dir_feed(tmp_path, "expired", files)
    (finding,) = by_code(validate_feed(loaded), "feed_expired")
    assert finding.severity == Severity.WARNING
    assert "2022-12-31" in finding.message
    assert finding.sample == ["2022-12-31"]


def test_no_feed_expired_for_current_calendar(tmp_path: Path) -> None:
    # Today-relative dates: this test must never become a time bomb.
    today = date.today()
    files = feed_files(
        replace={"calendar.txt": _calendar(today - timedelta(days=30), today + timedelta(days=365))}
    )
    loaded = load_dir_feed(tmp_path, "current", files)
    assert not by_code(validate_feed(loaded), "feed_expired")


def test_added_calendar_date_extends_validity(tmp_path: Path) -> None:
    # calendar ends in the past, but a type-1 exception adds a future date.
    today = date.today()
    files = feed_files(
        replace={
            "calendar.txt": _calendar(date(2020, 1, 1), date(2022, 12, 31)),
            "calendar_dates.txt": (
                "service_id,date,exception_type\n"
                f"WK,{_ymd(today + timedelta(days=10))},1\n"
            ),
        }
    )
    loaded = load_dir_feed(tmp_path, "extended", files)
    assert not by_code(validate_feed(loaded), "feed_expired")


def test_removed_calendar_date_does_not_extend_validity(tmp_path: Path) -> None:
    today = date.today()
    files = feed_files(
        replace={
            "calendar.txt": _calendar(date(2020, 1, 1), date(2022, 12, 31)),
            "calendar_dates.txt": (
                "service_id,date,exception_type\n"
                f"WK,{_ymd(today + timedelta(days=10))},2\n"     # removal
            ),
        }
    )
    loaded = load_dir_feed(tmp_path, "removed_only", files)
    assert by_code(validate_feed(loaded), "feed_expired")


def test_junk_calendar_dates_tolerated(tmp_path: Path) -> None:
    files = feed_files(
        replace={"calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WK,1,1,1,1,1,0,0,not-a-date,also-junk\n"
        )}
    )
    loaded = load_dir_feed(tmp_path, "junk_dates", files)
    # Nothing parses -> no expiry verdict, and no validator crash either.
    findings = validate_feed(loaded)
    assert not by_code(findings, "feed_expired")
    assert not by_code(findings, "validator_crash")


def test_feed_expired_respects_injected_analysis_date(tmp_path: Path) -> None:
    # The same feed judged against two different clocks: deterministic counts.
    files = feed_files(replace={"calendar.txt": _calendar(date(2020, 1, 1), date(2022, 12, 31))})
    loaded = load_dir_feed(tmp_path, "clocked", files)

    (finding,) = by_code(validate_feed(loaded, analysis_date=date(2023, 1, 11)), "feed_expired")
    assert "11 day(s) before today" in finding.message           # exact, reproducible
    # Clock set BEFORE the calendar end: not expired at all.
    assert not by_code(validate_feed(loaded, analysis_date=date(2021, 6, 1)), "feed_expired")


def test_calendar_range_pure_helper() -> None:
    cal = pd.DataFrame({"start_date": ["20200101"], "end_date": ["20221231"]})
    added = pd.DataFrame({"date": ["20230115"], "exception_type": ["1"]})
    start, end = calendar_range(cal, added)
    assert (start, end) == (date(2020, 1, 1), date(2023, 1, 15))
    assert calendar_range(None, None) == (None, None)
    # Missing exception_type column: every date counts (tolerant).
    no_type = pd.DataFrame({"date": ["20240601"]})
    assert calendar_range(cal, no_type)[1] == date(2024, 6, 1)


# --------------------------------------------------------------------------
# Stops-only CSV sources
# --------------------------------------------------------------------------

def test_stops_csv_skips_the_required_file_check(tmp_path: Path) -> None:
    findings = validate_feed(load_stops_csv_feed(tmp_path, "extra_stops.csv"))

    assert not by_code(findings, "missing_required_file")
    assert not by_code(findings, "missing_calendar")
    assert not errors(findings)
    (info,) = by_code(findings, "stops_only_source")
    assert info.severity is Severity.INFO


def test_stops_csv_is_still_validated_for_coordinates_and_duplicates(tmp_path: Path) -> None:
    """The relaxation is scoped to required *files*; row checks still run."""
    content = (
        "id,name,latitude,longitude\n"
        "C1,Ok,40.0,-75.0\n"
        "C1,Duplicate,40.1,-75.1\n"
        "C2,Bad lat,999.0,-75.0\n"
    )
    findings = validate_feed(load_stops_csv_feed(tmp_path, "messy.csv", content))

    assert by_code(findings, "duplicate_stop_ids")
    assert by_code(findings, "invalid_coordinates")


def test_header_only_stops_csv_is_an_empty_required_file(tmp_path: Path) -> None:
    findings = validate_feed(
        load_stops_csv_feed(tmp_path, "empty.csv", "id,name,latitude,longitude\n")
    )

    assert by_code(findings, "empty_required_file")


def test_unresolvable_csv_header_reaches_the_report(tmp_path: Path) -> None:
    findings = validate_feed(
        load_stops_csv_feed(tmp_path, "bad.csv", "ID_PARADA,DESC,X,Y\n1,Centro,20.6,-103.3\n")
    )

    assert by_code(findings, "feed_unreadable")          # generic: the feed failed
    assert by_code(findings, "unresolved_csv_header")    # specific: which column to rename
