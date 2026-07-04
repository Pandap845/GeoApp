"""Tests for the integrity validators (one scenario per required case)."""

from __future__ import annotations

from pathlib import Path

from gtfs_ingest.models import Finding, Severity
from gtfs_ingest.validators import optional_file_matrix, validate_feed

from conftest import feed_files, load_dir_feed


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
    assert len(by_code(findings, "missing_required_file")) == 5


def test_optional_file_matrix(valid_feed) -> None:
    matrix = optional_file_matrix(valid_feed)
    assert matrix["feed_info.txt"] is True
    assert matrix["calendar.txt"] is True
    assert matrix["shapes.txt"] is False
    assert matrix["transfers.txt"] is False
