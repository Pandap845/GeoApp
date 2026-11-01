"""Feed validation: run every check and return all findings, never raising.

Checks: required files and columns, readable tables, stop ids and
coordinates, parent_station links, trips/stop_times references, calendar
expiry, optional-file inventory. A check that crashes becomes a
validator_crash ERROR instead of stopping the feed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import pandas as pd

from .models import (
    CALENDAR_FILES,
    OPTIONAL_FILES,
    REQUIRED_COLUMNS,
    REQUIRED_FILES,
    Finding,
    LoadedFeed,
    Severity,
)

log = logging.getLogger(__name__)

#: Max example values per finding.
_SAMPLE_SIZE = 5


def _sample(values: pd.Series) -> list[str]:
    """First few unique values of a series, as plain strings."""
    return [str(v) for v in values.dropna().unique()[:_SAMPLE_SIZE]]


def _clean_ids(series: pd.Series) -> pd.Series:
    """Id column as stripped strings."""
    return series.astype("string").str.strip()


def validate_feed(loaded: LoadedFeed, analysis_date: date | None = None) -> list[Finding]:
    """All findings for one feed, in check order (empty = clean). Never raises.

    *analysis_date* is the reference date for the expiry check (default: today).
    """
    findings: list[Finding] = []

    if loaded.load_error is not None:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="feed_unreadable",
                message=f"Feed could not be opened: {loaded.load_error}",
            )
        )

    # Problems found while opening the source (e.g. CSV header).
    findings.extend(loaded.load_findings)

    def _calendar_expiry(inner: LoadedFeed, out: list[Finding]) -> None:
        _check_calendar_expiry(inner, out, analysis_date)

    _calendar_expiry.__name__ = "_check_calendar_expiry"  # keep crash logs readable

    checks: list[Callable[[LoadedFeed, list[Finding]], None]] = [
        _check_required_files,
        _check_table_readability,
        _check_required_columns,
        _check_stops,
        _check_referential_integrity,
        _calendar_expiry,
        _report_optional_files,
    ]
    for check in checks:
        try:
            check(loaded, findings)
        except Exception as exc:  # a crashing check must not kill the run
            log.exception("Feed %s: check %s crashed", loaded.feed_id, check.__name__)
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="validator_crash",
                    message=f"Internal error in {check.__name__}: {type(exc).__name__}: {exc}",
                )
            )
    return findings


def _check_required_files(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """ERROR per missing required file; at least one calendar file is required.

    Stops-only sources skip this check (they are not GTFS feeds) but still get
    every other check.
    """
    if loaded.is_stops_only:
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="stops_only_source",
                message=(
                    "Stops-only CSV source: the GTFS required files (agency, routes, "
                    "trips, stop_times, calendar) do not apply. This feed contributes "
                    "stop geometry only - no routes, trips, headways, connectivity "
                    "edges or same-line topology."
                ),
                file="stops.txt",
            )
        )
        return

    for name in REQUIRED_FILES:
        if name not in loaded.files_present:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="missing_required_file",
                    message=f"Required file {name} is missing",
                    file=name,
                )
            )
    if not any(name in loaded.files_present for name in CALENDAR_FILES):
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="missing_calendar",
                message="Neither calendar.txt nor calendar_dates.txt is present",
                file="calendar.txt",
            )
        )


def _check_table_readability(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """Read every file; ERROR if one can't be parsed or a required one is empty."""
    conditionally_required = set(REQUIRED_FILES) | set(CALENDAR_FILES)
    for name in sorted(loaded.files_present):
        df = loaded.table(name)  # records parse errors in table_errors
        if df is None and name in loaded.table_errors:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="unreadable_file",
                    message=f"{name} could not be parsed: {loaded.table_errors[name]}",
                    file=name,
                )
            )
        elif df is not None and df.empty and name in conditionally_required:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="empty_required_file",
                    message=f"{name} is present but contains no data rows",
                    file=name,
                )
            )


def _check_required_columns(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """ERROR for each required column missing from a readable file."""
    for name, required in REQUIRED_COLUMNS.items():
        df = loaded.table(name)
        if df is None or df.empty:
            continue  # absence/emptiness already reported
        missing = [col for col in required if col not in df.columns]
        if missing:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="missing_required_columns",
                    message=f"{name} is missing required column(s): {', '.join(missing)}",
                    file=name,
                    count=len(missing),
                    sample=missing[:_SAMPLE_SIZE],
                )
            )


def _check_stops(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """Blank or repeated stop_ids, bad coordinates, broken parent_station links."""
    stops = loaded.table("stops.txt")
    if stops is None or stops.empty:
        return

    if "stop_id" not in stops.columns:
        return  # missing_required_columns already covers this
    stop_ids = _clean_ids(stops["stop_id"])
    blank_mask = stop_ids.isna() | (stop_ids == "")
    if blank_mask.any():
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="null_stop_id",
                message="stops.txt has rows with a missing/blank stop_id",
                file="stops.txt",
                count=int(blank_mask.sum()),
            )
        )

    valid_ids = stop_ids[~blank_mask]
    dupes = valid_ids[valid_ids.duplicated()]
    if not dupes.empty:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="duplicate_stop_ids",
                message=f"stops.txt has {dupes.nunique()} stop_id value(s) that appear more than once",
                file="stops.txt",
                count=int(dupes.nunique()),
                sample=_sample(dupes),
            )
        )

    # location_type 3 and 4 may omit coordinates; all other rows need valid ones.
    if "location_type" in stops.columns:
        loc_type = pd.to_numeric(stops["location_type"], errors="coerce").fillna(0)
    else:
        loc_type = pd.Series(0, index=stops.index)
    coords_required = ~loc_type.isin([3, 4])

    for col, lo, hi in (("stop_lat", -90.0, 90.0), ("stop_lon", -180.0, 180.0)):
        if col not in stops.columns:
            continue  # missing_required_columns already covers this
        raw = stops[col].astype("string").str.strip()
        numeric = pd.to_numeric(raw, errors="coerce")
        missing = (raw.isna() | (raw == "")) & coords_required
        unparseable = numeric.isna() & ~raw.isna() & (raw != "")
        out_of_range = numeric.notna() & ((numeric < lo) | (numeric > hi))
        bad = missing | unparseable | out_of_range
        if bad.any():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="invalid_coordinates",
                    message=(
                        f"stops.txt has {int(bad.sum())} row(s) with missing, unparseable, "
                        f"or out-of-range {col} (valid range [{lo}, {hi}])"
                    ),
                    file="stops.txt",
                    count=int(bad.sum()),
                    sample=_sample(stop_ids[bad]),
                )
            )

    if "parent_station" in stops.columns:
        parents = _clean_ids(stops["parent_station"])
        has_parent = parents.notna() & (parents != "")
        known = set(valid_ids)
        dangling = parents[has_parent & ~parents.isin(known)]
        if not dangling.empty:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="dangling_parent_station",
                    message=(
                        f"stops.txt has {len(dangling)} row(s) whose parent_station "
                        "does not match any stop_id; the stops are kept, the link is unusable"
                    ),
                    file="stops.txt",
                    count=len(dangling),
                    sample=_sample(dangling),
                )
            )


def _ref_check(
    findings: list[Finding],
    child: pd.DataFrame | None,
    child_file: str,
    child_col: str,
    parent: pd.DataFrame | None,
    parent_file: str,
    parent_col: str,
) -> None:
    """ERROR if child[child_col] has values missing from parent[parent_col]."""
    if child is None or parent is None or child.empty:
        return
    if child_col not in child.columns or parent_col not in parent.columns:
        return  # column-level findings already emitted
    child_vals = _clean_ids(child[child_col])
    parent_vals = set(_clean_ids(parent[parent_col]).dropna())
    present = child_vals.notna() & (child_vals != "")
    dangling = child_vals[present & ~child_vals.isin(parent_vals)]
    if not dangling.empty:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="dangling_reference",
                message=(
                    f"{child_file}.{child_col} references {dangling.nunique()} value(s) "
                    f"missing from {parent_file}.{parent_col} ({len(dangling)} row(s) affected)"
                ),
                file=child_file,
                count=len(dangling),
                sample=_sample(dangling),
            )
        )


def _check_referential_integrity(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """Check trips -> routes, stop_times -> trips and stop_times -> stops."""
    routes = loaded.table("routes.txt")
    trips = loaded.table("trips.txt")
    stops = loaded.table("stops.txt")
    stop_times = loaded.table("stop_times.txt")

    _ref_check(findings, trips, "trips.txt", "route_id", routes, "routes.txt", "route_id")
    _ref_check(findings, stop_times, "stop_times.txt", "trip_id", trips, "trips.txt", "trip_id")
    _ref_check(findings, stop_times, "stop_times.txt", "stop_id", stops, "stops.txt", "stop_id")


def calendar_range(
    calendar: pd.DataFrame | None,
    calendar_dates: pd.DataFrame | None,
) -> tuple[date | None, date | None]:
    """(first, last) service date from calendar.txt plus added calendar_dates; Nones if none.

    Removed dates (exception_type 2) never extend the range. Used by Phase 2 too.
    """

    def _dates(frame: pd.DataFrame | None, column: str) -> pd.Series:
        if frame is None or frame.empty or column not in frame.columns:
            return pd.Series(dtype="datetime64[ns]")
        return pd.to_datetime(
            frame[column].astype("string").str.strip(), format="%Y%m%d", errors="coerce"
        ).dropna()

    starts = [_dates(calendar, "start_date")]
    ends = [_dates(calendar, "end_date")]
    added = calendar_dates
    if added is not None and not added.empty and "exception_type" in added.columns:
        kind = added["exception_type"].astype("string").str.strip()
        added = added[kind == "1"]
    starts.append(_dates(added, "date"))
    ends.append(_dates(added, "date"))

    all_starts = pd.concat(starts) if starts else pd.Series(dtype="datetime64[ns]")
    all_ends = pd.concat(ends) if ends else pd.Series(dtype="datetime64[ns]")
    start = all_starts.min().date() if not all_starts.empty else None
    end = all_ends.max().date() if not all_ends.empty else None
    return start, end


def _check_calendar_expiry(
    loaded: LoadedFeed, findings: list[Finding], today: date | None = None
) -> None:
    """WARNING when all of the feed's service ended before *today* (default: real today)."""
    _, end = calendar_range(loaded.table("calendar.txt"), loaded.table("calendar_dates.txt"))
    if end is None:
        return  # missing/unreadable calendars have their own findings
    today = today or date.today()
    if end < today:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="feed_expired",
                message=(
                    f"Feed's service ends {end.isoformat()} - "
                    f"{(today - end).days} day(s) before today; any analysis "
                    "runs on formally expired service"
                ),
                file="calendar.txt",
                sample=[end.isoformat()],
            )
        )


def _report_optional_files(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """INFO finding listing which optional files the feed has."""
    present = [f for f in OPTIONAL_FILES if f in loaded.files_present]
    absent = [f for f in OPTIONAL_FILES if f not in loaded.files_present]
    findings.append(
        Finding(
            severity=Severity.INFO,
            code="optional_files",
            message=(
                f"Optional files present: {', '.join(present) if present else '(none)'}; "
                f"absent: {', '.join(absent) if absent else '(none)'}"
            ),
            count=len(present),
        )
    )


def optional_file_matrix(loaded: LoadedFeed) -> dict[str, bool]:
    """{optional file: present?} for every tracked optional file."""
    return {name: name in loaded.files_present for name in OPTIONAL_FILES}
