"""Integrity validation for loaded GTFS feeds.

``validate_feed`` runs *all* checks and returns the full list of findings -
it never stops at the first problem and never raises. Every individual check
is additionally wrapped so that an unexpected crash inside one check becomes
an ERROR finding (code ``validator_crash``) rather than aborting the feed.

Checks performed (see README for the full documented list):
  * required files present; calendar.txt OR calendar_dates.txt present
  * required files non-empty; all present files parseable
  * required columns present per file
  * stops.txt: non-null stop_id, coordinate parseability + WGS84 range,
    duplicate stop_id detection, parent_station referential integrity
  * cross-file referential integrity: trips.route_id -> routes.route_id,
    stop_times.trip_id -> trips.trip_id, stop_times.stop_id -> stops.stop_id
  * inventory of optional files (INFO)

Severity policy:
  * ERROR   - spec violation or data loss (missing/empty/unreadable required
    file, missing required column, bad ids, bad coordinates, dangling
    trip/stop_time references).
  * WARNING - recoverable oddity; loading proceeds (dangling parent_station,
    since a stop is still a valid point without its station).
  * INFO    - inventory facts (optional files present/absent).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

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

#: Max number of example offending values attached to a finding.
_SAMPLE_SIZE = 5


def _sample(values: pd.Series) -> list[str]:
    """First few unique values of a series, as plain strings."""
    return [str(v) for v in values.dropna().unique()[:_SAMPLE_SIZE]]


def _clean_ids(series: pd.Series) -> pd.Series:
    """Whitespace-stripped string view of an id column ('' stays '')."""
    return series.astype("string").str.strip()


def validate_feed(loaded: LoadedFeed) -> list[Finding]:
    """Run every integrity check against one feed.

    Args:
        loaded: feed as returned by ``loader.load_feed``.

    Returns:
        All findings, in check order. Empty list means a fully clean feed.
        Never raises: unexpected exceptions inside a check are converted to
        an ERROR finding with code ``validator_crash``.
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

    checks: list[Callable[[LoadedFeed, list[Finding]], None]] = [
        _check_required_files,
        _check_table_readability,
        _check_required_columns,
        _check_stops,
        _check_referential_integrity,
        _report_optional_files,
    ]
    for check in checks:
        try:
            check(loaded, findings)
        except Exception as exc:  # noqa: BLE001 - a crashing check must not kill the run
            log.exception("Feed %s: check %s crashed", loaded.feed_id, check.__name__)
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code="validator_crash",
                    message=f"Internal error in {check.__name__}: {type(exc).__name__}: {exc}",
                )
            )
    return findings


# --------------------------------------------------------------------------
# Individual checks (each appends to `findings`, never raises intentionally)
# --------------------------------------------------------------------------

def _check_required_files(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """ERROR for each missing required file; calendar handled as either/or."""
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
    """Force-read every present file; ERROR on parse failure or empty required file."""
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
    """stop_id nullness/duplication, coordinate sanity, parent_station links."""
    stops = loaded.table("stops.txt")
    if stops is None or stops.empty:
        return

    # --- stop_id present and non-blank -----------------------------------
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

    # --- duplicate stop_id -------------------------------------------------
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

    # --- coordinates --------------------------------------------------------
    # location_type 3 (generic node) and 4 (boarding area) may legally omit
    # coordinates; every other row must have parseable, in-range values.
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

    # --- parent_station referential integrity ------------------------------
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
                        f"stops.txt has {int(len(dangling))} row(s) whose parent_station "
                        "does not match any stop_id; the stops are kept, the link is unusable"
                    ),
                    file="stops.txt",
                    count=int(len(dangling)),
                    sample=_sample(dangling),
                )
            )


def _ref_check(
    findings: list[Finding],
    child: Optional[pd.DataFrame],
    child_file: str,
    child_col: str,
    parent: Optional[pd.DataFrame],
    parent_file: str,
    parent_col: str,
) -> None:
    """ERROR if child[child_col] contains values absent from parent[parent_col]."""
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
                count=int(len(dangling)),
                sample=_sample(dangling),
            )
        )


def _check_referential_integrity(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """trips->routes, stop_times->trips, stop_times->stops link checks."""
    routes = loaded.table("routes.txt")
    trips = loaded.table("trips.txt")
    stops = loaded.table("stops.txt")
    stop_times = loaded.table("stop_times.txt")

    _ref_check(findings, trips, "trips.txt", "route_id", routes, "routes.txt", "route_id")
    _ref_check(findings, stop_times, "stop_times.txt", "trip_id", trips, "trips.txt", "trip_id")
    _ref_check(findings, stop_times, "stop_times.txt", "stop_id", stops, "stops.txt", "stop_id")


def _report_optional_files(loaded: LoadedFeed, findings: list[Finding]) -> None:
    """INFO finding summarizing which optional files the feed ships."""
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
    """Presence map of every tracked optional file for this feed.

    Args:
        loaded: feed to inspect.

    Returns:
        ``{optional_filename: is_present}`` for all of ``OPTIONAL_FILES``;
        feeds that failed to open report every file as absent.
    """
    return {name: name in loaded.files_present for name in OPTIONAL_FILES}
