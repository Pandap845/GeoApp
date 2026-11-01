"""Shared models and GTFS constants.

Anything that crosses modules or goes into a report is a pydantic model.
LoadedFeed is a dataclass because it wraps a live partridge object.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field


def numeric_stop_id(stop_uid: str) -> int:
    """Stable numeric id for a stop: the first 48 bits of sha256(stop_uid).

    Small enough to survive JSON/JavaScript numbers; collisions are negligible.
    """
    return int.from_bytes(hashlib.sha256(stop_uid.encode("utf-8")).digest()[:6], "big")


#: Files every feed must have (calendar files are checked separately).
REQUIRED_FILES: tuple[str, ...] = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
)

#: At least one of these must exist.
CALENDAR_FILES: tuple[str, ...] = ("calendar.txt", "calendar_dates.txt")

#: Optional files listed per feed in the report.
OPTIONAL_FILES: tuple[str, ...] = (
    "calendar.txt",
    "calendar_dates.txt",
    "shapes.txt",
    "transfers.txt",
    "frequencies.txt",
    "feed_info.txt",
    "fare_attributes.txt",
    "fare_rules.txt",
    "pathways.txt",
    "levels.txt",
    "attributions.txt",
)

#: Columns each file needs. stop_lat/stop_lon must exist as columns because
#: geometry is built from them, even though GTFS makes them conditional.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "agency.txt": ("agency_name", "agency_url", "agency_timezone"),
    "stops.txt": ("stop_id", "stop_lat", "stop_lon"),
    "routes.txt": ("route_id", "route_type"),
    "trips.txt": ("route_id", "service_id", "trip_id"),
    "stop_times.txt": ("trip_id", "stop_id", "stop_sequence"),
    "calendar.txt": (
        "service_id",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "start_date",
        "end_date",
    ),
    "calendar_dates.txt": ("service_id", "date", "exception_type"),
}


class Severity(str, enum.Enum):
    """ERROR: data is lost or the spec is broken. WARNING: recoverable. INFO: informational."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class Finding(BaseModel):
    """One validation result for a feed; *code* is stable for filtering."""

    severity: Severity
    code: str
    message: str
    file: str | None = None
    count: int | None = None
    sample: list[str] | None = None  # a few example offending values


class FeedSource(BaseModel):
    """A feed found on disk, before loading."""

    feed_id: str
    path: Path
    kind: Literal["zip", "dir", "csv"]

    @property
    def is_stops_only(self) -> bool:
        """True for a stops-only CSV: geometry, but no routes, trips or calendar."""
        return self.kind == "csv"


@dataclass
class LoadedFeed:
    """An opened feed. table() never raises; parse errors go to table_errors.

    feed is None when the container could not be opened (see load_error) or
    for a stops-only CSV, whose stops.txt is pre-seeded in _tables.
    load_findings holds problems found while opening (e.g. CSV header issues).
    """

    source: FeedSource
    feed: Any
    files_present: set[str]
    load_error: str | None = None
    table_errors: dict[str, str] = field(default_factory=dict)
    load_findings: list[Finding] = field(default_factory=list)
    _tables: dict[str, pd.DataFrame | None] = field(default_factory=dict, repr=False)

    @property
    def feed_id(self) -> str:
        return self.source.feed_id

    @property
    def is_stops_only(self) -> bool:
        return self.source.is_stops_only

    def table(self, filename: str) -> pd.DataFrame | None:
        """The raw table (all strings), or None if absent or unreadable."""
        if filename in self._tables:
            return self._tables[filename]
        df: pd.DataFrame | None = None
        if self.feed is not None and filename in self.files_present:
            try:
                df = self.feed.get(filename)
            except Exception as exc:  # noqa: BLE001 - any parse failure is a finding, not a crash
                self.table_errors[filename] = f"{type(exc).__name__}: {exc}"
        self._tables[filename] = df
        return df

    def row_counts(self) -> dict[str, int]:
        """Row count of every readable file in the feed."""
        counts: dict[str, int] = {}
        for name in sorted(self.files_present):
            df = self.table(name)
            if df is not None:
                counts[name] = len(df)
        return counts


class StopRecord(BaseModel):
    """One normalized stop; also the contract of the PostGIS stops table."""

    stop_uid: str  # "{feed_id}:{stop_id}"
    stop_num_id: int
    feed_id: str
    stop_id: str
    stop_name: str | None = None
    stop_lat: float = Field(ge=-90.0, le=90.0)
    stop_lon: float = Field(ge=-180.0, le=180.0)
    location_type: int = 0
    parent_station: str | None = None


#: Column order of the normalized stops frame (without geometry).
STOP_COLUMNS: tuple[str, ...] = tuple(StopRecord.model_fields)


class FeedReport(BaseModel):
    """Everything the report records about one feed."""

    feed_id: str
    source_path: str
    status: Literal["loaded", "failed"]
    source_kind: str = "dir"  # "zip", "dir" or "csv"
    row_counts: dict[str, int] = Field(default_factory=dict)
    optional_files: dict[str, bool] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    stops_normalized: int = 0
    stops_loaded: int = 0

    def count(self, severity: Severity) -> int:
        """Number of findings with this severity."""
        return sum(1 for f in self.findings if f.severity is severity)


class RunReport(BaseModel):
    """Phase 1 run report; analysis_date is the reference date for expiry checks."""

    generated_at: str
    dry_run: bool
    analysis_date: str | None = None
    feeds: list[FeedReport] = Field(default_factory=list)
