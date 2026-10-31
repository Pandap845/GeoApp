"""Typed models and GTFS schema constants shared across the pipeline.

Everything that crosses a module boundary (findings, feed metadata, report
payloads) is a pydantic model so it can be validated once and serialized
directly into the JSON report. The one exception is :class:`LoadedFeed`,
which wraps a live partridge feed object and is therefore a plain dataclass.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field


def numeric_stop_id(stop_uid: str) -> int:
    """Deterministic numeric ID for a stop, derived from its ``stop_uid``.

    First 48 bits of ``sha256(stop_uid)`` as an unsigned integer: numeric,
    globally unique in practice (collision odds for even 100k stops are
    ~1e-5), stable across runs and feed sets, and small enough to survive
    a round-trip through JSON/JavaScript numbers. ``stop_uid`` remains the
    durable human-readable key; this is its machine-friendly alias.
    """
    return int.from_bytes(hashlib.sha256(stop_uid.encode("utf-8")).digest()[:6], "big")

# --------------------------------------------------------------------------
# GTFS schema constants
# --------------------------------------------------------------------------

#: Files that must be present in every feed. calendar.txt/calendar_dates.txt
#: are conditionally required (at least one of the two) and checked separately.
REQUIRED_FILES: tuple[str, ...] = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
)

#: At least one of these must be present (GTFS conditional requirement).
CALENDAR_FILES: tuple[str, ...] = ("calendar.txt", "calendar_dates.txt")

#: Optional files whose presence we report per feed (the feed x optional-file
#: matrix in the report). calendar_dates.txt appears here too because it is
#: optional whenever calendar.txt exists.
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

#: Minimum columns each file must carry for this pipeline to work with it.
#: Note: per the GTFS spec stop_lat/stop_lon are only conditionally required
#: (location_type 0/1/2), but this pipeline requires the *columns* to exist
#: in stops.txt because geometry is built from them; per-row blanks on
#: location_type 3/4 rows are tolerated (see validators/normalizer).
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


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

class Severity(str, enum.Enum):
    """Severity classification for a validation finding.

    Levels::

        ERROR   - the feed violates the GTFS spec or is unusable in a way that
                  loses data (missing required file, unparseable table, bad ids).
        WARNING - suspicious but recoverable; affected rows are skipped or the
                  condition does not block loading (e.g. dangling parent_station).
        INFO    - purely informational (e.g. which optional files are present).
    """

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class Finding(BaseModel):
    """A single validation finding for one feed.

    Attributes:
        severity: ERROR / WARNING / INFO classification.
        code: stable machine-readable identifier (e.g. ``duplicate_stop_ids``)
            so downstream tooling can filter without parsing messages.
        message: human-readable description of the problem.
        file: GTFS filename the finding relates to, if any.
        count: number of affected rows/values, if countable.
        sample: up to a handful of example offending values, for debugging.
    """

    severity: Severity
    code: str
    message: str
    file: Optional[str] = None
    count: Optional[int] = None
    sample: Optional[list[str]] = None


# --------------------------------------------------------------------------
# Feed metadata
# --------------------------------------------------------------------------

class FeedSource(BaseModel):
    """A discovered feed on disk, before any loading is attempted.

    Attributes:
        feed_id: deterministic identifier derived from the file/directory
            name (see ``loader.derive_feed_id`` for the exact rules).
        path: absolute path to the .zip file, feed directory, or stops CSV.
        kind: ``"zip"``, ``"dir"``, or ``"csv"`` for a stops-only CSV
            (see :mod:`gtfs_ingest.stops_csv`).
    """

    feed_id: str
    path: Path
    kind: Literal["zip", "dir", "csv"]

    @property
    def is_stops_only(self) -> bool:
        """True for a source that carries stop geometry and nothing else.

        Such a source has no routes, trips, stop_times or calendar, so it
        contributes stops to the inventory, proximity, deduplication and the
        spatial exports, but no headways, route connectivity or same-line
        topology. Every phase branches on this one predicate.
        """
        return self.kind == "csv"


@dataclass
class LoadedFeed:
    """A feed opened with partridge, plus per-table read bookkeeping.

    Table access goes through :meth:`table`, which never raises: an
    unreadable table (bad encoding, malformed CSV, ...) is recorded in
    ``table_errors`` and reported as ``None``. This is what lets the
    validators run every check on a broken feed instead of dying on the
    first bad file.

    A stops-only CSV source is represented here with ``feed=None`` and a
    **pre-seeded** ``_tables["stops.txt"]``: :meth:`table` consults that cache
    before it ever looks at ``feed``, so every consumer that reads GTFS tables
    through :meth:`table` works on such a source unchanged, with no partridge
    object in play.

    Attributes:
        source: the discovered feed this was loaded from.
        feed: the partridge raw feed object, or ``None`` if the container
            itself could not be opened (see ``load_error``) or the source is
            a stops-only CSV.
        files_present: basenames of the ``.txt`` members found in the zip/dir;
            ``{"stops.txt"}`` for a stops-only CSV, which presents exactly one
            logical table.
        load_error: message if the feed container could not be opened.
        table_errors: filename -> error message for tables that failed to parse.
        load_findings: findings produced while opening the source itself
            (encoding, delimiter and header resolution for a CSV), replayed by
            ``validators.validate_feed`` so they reach the report.
    """

    source: FeedSource
    feed: Any
    files_present: set[str]
    load_error: Optional[str] = None
    table_errors: dict[str, str] = field(default_factory=dict)
    load_findings: list[Finding] = field(default_factory=list)
    _tables: dict[str, Optional[pd.DataFrame]] = field(default_factory=dict, repr=False)

    @property
    def feed_id(self) -> str:
        """Shortcut for ``source.feed_id``."""
        return self.source.feed_id

    @property
    def is_stops_only(self) -> bool:
        """Shortcut for ``source.is_stops_only``."""
        return self.source.is_stops_only

    def table(self, filename: str) -> Optional[pd.DataFrame]:
        """Return a GTFS table as a DataFrame, or ``None`` if unavailable.

        Args:
            filename: GTFS filename, e.g. ``"stops.txt"``.

        Returns:
            The table as read by partridge (raw, all-string dtypes), or
            ``None`` when the file is absent, the feed failed to open, or
            the file could not be parsed (the parse error is stored in
            ``table_errors``). Never raises.
        """
        if filename in self._tables:
            return self._tables[filename]
        df: Optional[pd.DataFrame] = None
        if self.feed is not None and filename in self.files_present:
            try:
                df = self.feed.get(filename)
            except Exception as exc:  # noqa: BLE001 - any parse failure is a finding, not a crash
                self.table_errors[filename] = f"{type(exc).__name__}: {exc}"
        self._tables[filename] = df
        return df

    def row_counts(self) -> dict[str, int]:
        """Row counts for every readable file present in the feed."""
        counts: dict[str, int] = {}
        for name in sorted(self.files_present):
            df = self.table(name)
            if df is not None:
                counts[name] = int(len(df))
        return counts


# --------------------------------------------------------------------------
# Normalized stop schema
# --------------------------------------------------------------------------

class StopRecord(BaseModel):
    """One row of the normalized, cross-feed stops schema.

    This model documents (and in tests, enforces) the exact contract of the
    normalized stops DataFrame and of the PostGIS ``stops`` table.

    Attributes:
        stop_uid: globally unique key, ``"{feed_id}:{stop_id}"``.
        stop_num_id: numeric alias of ``stop_uid`` (see
            :func:`numeric_stop_id`) for consumers that need integer ids.
        feed_id: provenance - which feed the stop came from.
        stop_id: original GTFS stop_id (whitespace-stripped).
        stop_name: stop name or ``None`` when blank in the source.
        stop_lat: WGS84 latitude in [-90, 90].
        stop_lon: WGS84 longitude in [-180, 180].
        location_type: GTFS location_type; blanks/unparseable values
            normalize to 0 (= stop/platform, the GTFS default).
        parent_station: original parent_station id or ``None``.
    """

    stop_uid: str
    stop_num_id: int
    feed_id: str
    stop_id: str
    stop_name: Optional[str] = None
    stop_lat: float = Field(ge=-90.0, le=90.0)
    stop_lon: float = Field(ge=-180.0, le=180.0)
    location_type: int = 0
    parent_station: Optional[str] = None


#: Column order of the normalized stops frame (geometry column excluded).
STOP_COLUMNS: tuple[str, ...] = tuple(StopRecord.model_fields)


# --------------------------------------------------------------------------
# Report payloads
# --------------------------------------------------------------------------

class FeedReport(BaseModel):
    """Everything the report records about one feed."""

    feed_id: str
    source_path: str
    status: Literal["loaded", "failed"]
    #: Source layout this feed came from - "zip", "dir", or "csv" for a
    #: stops-only CSV. Defaulted so older reports deserialize unchanged.
    source_kind: str = "dir"
    row_counts: dict[str, int] = Field(default_factory=dict)
    optional_files: dict[str, bool] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    stops_normalized: int = 0
    stops_loaded: int = 0

    def count(self, severity: Severity) -> int:
        """Number of findings with the given severity."""
        return sum(1 for f in self.findings if f.severity is severity)


class RunReport(BaseModel):
    """Top-level report for one pipeline run (serialized to JSON as-is).

    ``generated_at`` is a wall-clock timestamp (run metadata);
    ``analysis_date`` is the run's *input* clock - the reference date the
    expiry judgments were made against (reproducible via --analysis-date).
    """

    generated_at: str
    dry_run: bool
    analysis_date: Optional[str] = None
    feeds: list[FeedReport] = Field(default_factory=list)
