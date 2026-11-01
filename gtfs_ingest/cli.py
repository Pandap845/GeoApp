"""Command line: python -m gtfs_ingest.cli <command> ...

Commands: ingest, analyze-frequency, detect-interchanges, conflate-osm,
google-qa, locate-stations, export-neo4j. Flags without a command run ingest.
See README.md for every flag and docs/guide/ for the full run order.

Exit codes: 0 = finished (feeds may still have findings), 1 = run-level
problem (no feeds, database, bad arguments), 2 = ingest found ERRORs with
--fail-on-error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings, setup_logging
from .models import Severity

log = logging.getLogger(__name__)

_SUBCOMMANDS = (
    "ingest",
    "analyze-frequency",
    "detect-interchanges",
    "conflate-osm",
    "google-qa",
    "locate-stations",
    "export-neo4j",
)

#: Longest allowed --service-date-range, to avoid accidental year-long runs.
MAX_RANGE_DAYS = 31


# Argument builders


def _add_db_url(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--db-url", default=None, help=help_text)


def _add_report(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--report", type=Path, default=None, metavar="DIR", help=help_text)


def _add_log_level(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO, or GTFS_LOG_LEVEL env var).",
    )


def _add_dry_run(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--dry-run", action="store_true", help=help_text)


def _add_analysis_date(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--analysis-date", default=None, metavar="YYYY-MM-DD", help=help_text)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Flags shared by the commands that read feeds."""
    parser.add_argument(
        "--feeds",
        nargs="+",
        required=True,
        type=Path,
        metavar="PATH",
        help="Feed .zip files, feed directories, directories containing multiple "
        "feeds, or stops-only .csv files (columns: id, name, latitude, longitude; "
        "named explicitly, never picked up by scanning a directory).",
    )
    _add_db_url(parser, "PostgreSQL/PostGIS SQLAlchemy URL (default: DATABASE_URL env var).")
    _add_report(parser, "Directory for reports/exports (default: ./out).")
    _add_log_level(parser)
    _add_dry_run(parser, "Validate/analyze and write reports only; no database writes.")


def _add_unserved_arg(parser: argparse.ArgumentParser) -> None:
    """--drop-unserved-stops (analyze-frequency and detect-interchanges)."""
    from .normalizer import DROP_UNSERVED_STOPS

    parser.add_argument(
        "--drop-unserved-stops",
        action=argparse.BooleanOptionalAction,
        default=DROP_UNSERVED_STOPS,
        help="Leave out stops that appear in no stop_times row (no trip can board "
        "them). They stay in the PostGIS stops table. On by default; "
        "--no-drop-unserved-stops keeps them.",
    )


def _add_dedupe_args(parser: argparse.ArgumentParser) -> None:
    """Station deduplication flags (analyze-frequency and detect-interchanges)."""
    from .analysis.dedupe import DEDUPE_DISTANCE_M, NAME_SIM_THRESHOLD, WITHIN_FEED_DISTANCE_M

    parser.add_argument(
        "--dedupe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Merge cross-feed duplicate stations (same physical stop under "
        "different stop_ids) before connectivity/near-stations analysis. "
        "On by default; --no-dedupe disables.",
    )
    parser.add_argument(
        "--dedupe-distance-m",
        type=float,
        default=DEDUPE_DISTANCE_M,
        metavar="METERS",
        help="Max separation for two stops of different feeds to be alias "
        "candidates (default: 30).",
    )
    parser.add_argument(
        "--dedupe-name-threshold",
        type=float,
        default=NAME_SIM_THRESHOLD,
        metavar="RATIO",
        help="Minimum normalized-name similarity (0-1) to accept a cross-feed "
        "alias pair on names alone; pairs sharing a route short name match "
        "regardless (default: 0.75).",
    )
    parser.add_argument(
        "--dedupe-within-feed-distance-m",
        type=float,
        default=WITHIN_FEED_DISTANCE_M,
        metavar="METERS",
        help="Max separation for two stops of the SAME feed to be alias "
        "candidates - one station published under two id schemes. Accepted "
        "ONLY when both stops share a route (never on names alone). Kept "
        "tight on purpose: duplicate records sit at ~0 m, while the same "
        "route's opposite-direction stop (20-40 m away) must stay distinct. "
        "Pass 0 to disable within-feed deduplication (default: 5).",
    )


def _add_ingest_parser(subparsers: Any) -> None:
    ingest = subparsers.add_parser(
        "ingest", help="Validate feeds, normalize stops, load them into PostGIS (Phase 1)."
    )
    _add_common_args(ingest)
    ingest.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with code 2 if any feed produced an ERROR finding.",
    )
    _add_analysis_date(
        ingest,
        "Reference date for the feed-expiry check (default: today; also "
        "GTFS_ANALYSIS_DATE). Makes expiry findings reproducible.",
    )


def _add_analyze_parser(subparsers: Any) -> None:
    analyze = subparsers.add_parser(
        "analyze-frequency",
        help="Compute headways per route/stop/window and the route connectivity graph (Phase 2).",
    )
    _add_common_args(analyze)
    analyze.add_argument(
        "--service-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Service date to analyze (calendar + calendar_dates honored). "
        "Default: each feed's busiest date.",
    )
    analyze.add_argument(
        "--service-date-range",
        default=None,
        metavar="START..END",
        help="Analyze every served day in the window (max 31 days), e.g. "
        "2026-01-05..2026-01-11. Headways get one row per day; connectivity "
        "and near-stations use the union of days. Mutually exclusive with "
        "--service-date/--common-date.",
    )
    analyze.add_argument(
        "--common-date",
        action="store_true",
        help="Resolve ONE service date shared by ALL feeds (the latest date "
        "every feed has active service) instead of per-feed busiest dates; "
        "exits 1 when the feeds' calendars share no such date.",
    )
    _add_analysis_date(
        analyze,
        "Reference date for expiry/staleness notes (default: today; also "
        "GTFS_ANALYSIS_DATE). Makes the report reproducible.",
    )
    analyze.add_argument(
        "--proximity-m",
        type=float,
        default=150.0,
        metavar="METERS",
        help="Stop proximity threshold for route connectivity (default: 150).",
    )
    analyze.add_argument(
        "--window",
        action="append",
        default=None,
        metavar="NAME=HH:MM-HH:MM",
        help="Override the default time windows (repeatable; hours may exceed "
        "23 for post-midnight service, e.g. night=23:00-27:00).",
    )
    analyze.add_argument(
        "--transport-mode",
        action="append",
        default=None,
        metavar="MODE",
        help="Restrict the analysis to these transport modes (repeatable or "
        "comma-separated). Accepts names (bus, tram, metro, rail, ferry, "
        "cable_tram, aerial_lift, funicular, trolleybus, monorail) or GTFS "
        "route_type codes, basic or extended (3, 700, ...). Default: all modes.",
    )
    analyze.add_argument(
        "--network-walk",
        action="store_true",
        help="Compute near-station walking times along the OSM street network "
        "(planet_osm_line in PostGIS) instead of straight lines. Slower; "
        "pairs without a street path keep the straight-line value.",
    )
    analyze.add_argument(
        "--walk-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Cache the OSM walk graph on disk keyed by feeds + OSM state "
        "(only used with --network-walk). --no-walk-cache forces a rebuild.",
    )
    analyze.add_argument(
        "--walk-cache-dir",
        type=Path,
        default=Path(".gtfs_cache"),
        metavar="DIR",
        help="Directory for the walk-graph cache (default: ./.gtfs_cache).",
    )
    analyze.add_argument(
        "--route",
        action="append",
        default=None,
        metavar="ROUTE",
        help="Restrict the analysis to these routes, matched by route_id or "
        "route_short_name (repeatable or comma-separated). Combines with "
        "--transport-mode: a route passes if it matches either. Default: all routes.",
    )
    _add_dedupe_args(analyze)
    _add_unserved_arg(analyze)


def _add_detect_parser(subparsers: Any) -> None:
    detect = subparsers.add_parser(
        "detect-interchanges",
        help="Cluster stops into (multimodal) interchanges and cross-check transfers.txt (Phase 3A).",
    )
    _add_common_args(detect)
    detect.add_argument(
        "--eps",
        type=float,
        default=120.0,
        metavar="METERS",
        help="DBSCAN neighborhood radius in meters (default: 120).",
    )
    detect.add_argument(
        "--min-samples",
        type=int,
        default=2,
        metavar="N",
        help="DBSCAN min_samples (default: 2).",
    )
    detect.add_argument(
        "--crs",
        default=None,
        metavar="EPSG:XXXXX",
        help="Metric CRS for distance math (default: auto-detect a UTM zone).",
    )
    _add_dedupe_args(detect)
    _add_unserved_arg(detect)


def _add_conflate_parser(subparsers: Any) -> None:
    conflate = subparsers.add_parser(
        "conflate-osm",
        help="Match stops to OSM transit nodes and compute side-of-street (Phase 3B+C).",
    )
    # No --feeds: reads stops and OSM from PostGIS.
    _add_db_url(
        conflate,
        "PostgreSQL/PostGIS SQLAlchemy URL (default: DATABASE_URL env var). "
        "Required even with --dry-run: OSM in the database is the reference source.",
    )
    _add_report(conflate, "Directory for the reports (default: ./out).")
    _add_log_level(conflate)
    _add_dry_run(conflate, "Compute and report only; skip the table writes.")
    conflate.add_argument(
        "--match-threshold-m",
        type=float,
        default=50.0,
        metavar="METERS",
        help="Max stop-to-OSM-node distance for a match (default: 50).",
    )
    conflate.add_argument(
        "--street-threshold-m",
        type=float,
        default=50.0,
        metavar="METERS",
        help="Max stop-to-centerline distance for side-of-street (default: 50).",
    )
    conflate.add_argument(
        "--osm-line-table",
        default=None,
        help="OSM highway line table (default: planet_osm_line / GTFS_OSM_LINE_TABLE).",
    )
    conflate.add_argument(
        "--osm-point-table",
        default=None,
        help="OSM point table (default: planet_osm_point / GTFS_OSM_POINT_TABLE).",
    )
    conflate.add_argument(
        "--osm-geom-column",
        default=None,
        help="Geometry column of the OSM tables (default: way / GTFS_OSM_GEOM_COLUMN).",
    )
    conflate.add_argument(
        "--crs",
        default=None,
        metavar="EPSG:XXXXX",
        help="Metric CRS for side math (default: auto-detect a UTM zone).",
    )
    conflate.add_argument(
        "--coverage-radius-m",
        type=float,
        default=100.0,
        metavar="METERS",
        help="Reverse coverage: an OSM transit node is a gap when no GTFS "
        "stop is within this distance (default: 100).",
    )
    conflate.add_argument(
        "--coverage-buffer-m",
        type=float,
        default=500.0,
        metavar="METERS",
        help="Reverse coverage: buffer around the stops' convex hull that "
        "defines the service area (default: 500).",
    )
    conflate.add_argument(
        "--skip-coverage",
        action="store_true",
        help="Skip the reverse OSM coverage check and its GeoPackage export.",
    )


def _add_qa_parser(subparsers: Any) -> None:
    qa = subparsers.add_parser(
        "google-qa",
        help="Ephemeral Google Places QA cross-check of Phase 3 results "
        "(OFF by default; needs --enable-google-qa AND GOOGLE_MAPS_API_KEY).",
    )
    _add_db_url(qa, "PostgreSQL SQLAlchemy URL (default: DATABASE_URL env var).")
    _add_report(qa, "Directory for the QA reports (default: ./out).")
    _add_log_level(qa)
    qa.add_argument(
        "--enable-google-qa",
        action="store_true",
        help="Explicit opt-in; without it the command no-ops. The API key "
        "must come from the GOOGLE_MAPS_API_KEY env var (no flag, by design).",
    )
    qa.add_argument(
        "--max-calls",
        type=int,
        default=200,
        metavar="N",
        help="Hard ceiling on Places API calls for this run (default: 200).",
    )
    qa.add_argument(
        "--radius-m",
        type=float,
        default=50.0,
        metavar="METERS",
        help="Stop-existence search radius (default: 50).",
    )
    qa.add_argument(
        "--interchange-radius-m",
        type=float,
        default=150.0,
        metavar="METERS",
        help="Interchange centroid search radius (default: 150).",
    )
    qa.add_argument(
        "--rate-per-s",
        type=float,
        default=5.0,
        metavar="RPS",
        help="Max Places API requests per second (default: 5).",
    )
    _add_dry_run(qa, "Report eligibility only; no API calls, no DB writes.")


def _add_locate_parser(subparsers: Any) -> None:
    locate = subparsers.add_parser(
        "locate-stations",
        help="Resolve a station-name list to coordinates and travel times via "
        "Google APIs and export the route to Excel (OFF by default; needs "
        "--enable-google-api AND GOOGLE_MAPS_API_KEY). Never writes to the database.",
    )
    locate.add_argument(
        "--stations",
        type=Path,
        required=True,
        metavar="FILE",
        help="TXT/CSV file with station names in route order (comma- and/or newline-separated).",
    )
    _add_report(locate, "Directory for the reports and the Excel export (default: ./out).")
    _add_log_level(locate)
    locate.add_argument(
        "--enable-google-api",
        action="store_true",
        help="Explicit opt-in; without it the command no-ops. The API key "
        "must come from the GOOGLE_MAPS_API_KEY env var (no flag, by design).",
    )
    locate.add_argument(
        "--transport-type",
        default="SITREN",
        metavar="TYPE",
        help="Transport type used in the query and mode mapping, e.g. "
        "SITREN, MiMacro, Troncal, Alimentador, Expreso (default: SITREN).",
    )
    locate.add_argument(
        "--line",
        default=None,
        metavar="LINE",
        help="Optional line identifier appended to queries ('... línea N').",
    )
    locate.add_argument(
        "--language",
        default="es",
        metavar="LANG",
        help="Response language for all Google calls (default: es).",
    )
    locate.add_argument(
        "--region",
        default="mx",
        metavar="REGION",
        help="Region bias for all Google calls (default: mx).",
    )
    locate.add_argument(
        "--search-radius-m",
        type=float,
        default=3000.0,
        metavar="METERS",
        help="Anchor bias radius around the previous station (default: 3000).",
    )
    locate.add_argument(
        "--no-distance-matrix",
        action="store_true",
        help="Skip the Distance Matrix API; estimate every travel time "
        "from the Haversine distance and --avg-speed-kmh.",
    )
    locate.add_argument(
        "--avg-speed-kmh",
        type=float,
        default=25.0,
        metavar="KMH",
        help="Average speed for estimated travel times (default: 25).",
    )
    locate.add_argument(
        "--max-route-minutes",
        type=float,
        default=60.0,
        metavar="MIN",
        help="Total route time used for the generic per-pair estimate when "
        "coordinates are missing (default: 60).",
    )
    locate.add_argument(
        "--max-calls",
        type=int,
        default=200,
        metavar="N",
        help="Hard ceiling on Google API calls for this run (default: 200).",
    )
    locate.add_argument(
        "--rate-per-s",
        type=float,
        default=5.0,
        metavar="RPS",
        help="Max Google API requests per second (default: 5).",
    )
    locate.add_argument(
        "--excel-name",
        default="stations_route.xlsx",
        metavar="NAME",
        help="File name of the Excel export inside the report directory "
        "(default: stations_route.xlsx).",
    )
    _add_dry_run(
        locate,
        "Report the plan (stations read, worst-case call count) only; no API calls, no export.",
    )


def _add_neo4j_parser(subparsers: Any) -> None:
    neo = subparsers.add_parser(
        "export-neo4j",
        help="Build an offline Neo4j load package (CSVs + Cypher) from a report directory.",
    )
    # No --feeds/--db-url: works on an existing report folder.
    _add_report(neo, "analyze-frequency report directory to read (default: ./out).")
    neo.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="DIR",
        help="Package destination (default: <report>/neo4j).",
    )
    _add_log_level(neo)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="gtfs-ingest",
        description="GTFS ingestion, validation, and service analysis on PostGIS.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for add in (
        _add_ingest_parser,
        _add_analyze_parser,
        _add_detect_parser,
        _add_conflate_parser,
        _add_qa_parser,
        _add_locate_parser,
        _add_neo4j_parser,
    ):
        add(subparsers)
    return parser


# Argument parsing helpers


def _parse_date_range(spec: str) -> tuple[date, date]:
    """Parse START..END (ISO dates); raises ValueError if invalid or too long."""
    start_s, sep, end_s = spec.partition("..")
    if not sep or not start_s or not end_s:
        raise ValueError(
            f"--service-date-range {spec!r}: expected START..END (YYYY-MM-DD..YYYY-MM-DD)"
        )
    start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    if end < start:
        raise ValueError(f"--service-date-range: END {end} is before START {start}")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(
            f"--service-date-range: {(end - start).days + 1} day(s) exceeds "
            f"the {MAX_RANGE_DAYS}-day cap"
        )
    return start, end


def _settings(args: argparse.Namespace) -> Settings:
    """Settings from CLI flags, falling back to the environment and .env."""
    overrides: dict = {
        "feed_paths": list(getattr(args, "feeds", None) or []),
        # export-neo4j has no --dry-run and never writes to the database.
        "dry_run": getattr(args, "dry_run", True),
    }
    for flag, field in (
        ("db_url", "database_url"),
        ("report", "report_dir"),
        ("log_level", "log_level"),
    ):
        value = getattr(args, flag, None)
        if value:
            overrides[field] = value
    if getattr(args, "analysis_date", None):
        overrides["analysis_date"] = date.fromisoformat(args.analysis_date)
    return Settings(**overrides)


def _stop_options(args: argparse.Namespace) -> dict[str, Any]:
    """Dedupe and unserved-stop flags as config fields; within-feed 0 turns that rule off."""
    return {
        **_dedupe_options(args),
        "drop_unserved_stops": args.drop_unserved_stops,
    }


def _dedupe_options(args: argparse.Namespace) -> dict[str, Any]:
    """Dedupe flags as config fields; a within-feed radius of 0 turns that rule off."""
    return {
        "dedupe": args.dedupe,
        "dedupe_distance_m": args.dedupe_distance_m,
        "dedupe_name_threshold": args.dedupe_name_threshold,
        "dedupe_within_feed_distance_m": args.dedupe_within_feed_distance_m or None,
    }


def _date_options(args: argparse.Namespace) -> dict[str, Any]:
    """The analyze-frequency date mode; raises ValueError for bad or clashing flags."""
    date_modes = [
        name
        for name, on in (
            ("--service-date", bool(args.service_date)),
            ("--service-date-range", bool(args.service_date_range)),
            ("--common-date", args.common_date),
        )
        if on
    ]
    if len(date_modes) > 1:
        raise ValueError(f"{' and '.join(date_modes)} are mutually exclusive")
    return {
        "service_date": date.fromisoformat(args.service_date) if args.service_date else None,
        "service_date_range": (
            _parse_date_range(args.service_date_range) if args.service_date_range else None
        ),
        "common_date": args.common_date,
    }


def _windows(specs: list[str] | None) -> list:
    """--window NAME=HH:MM-HH:MM values, or the default windows."""
    from .analysis.headways import DEFAULT_WINDOWS, TimeWindow

    if not specs:
        return list(DEFAULT_WINDOWS)
    windows = []
    for spec in specs:
        name, _, rng = spec.partition("=")
        if not name or not rng:
            raise ValueError(f"--window {spec!r}: expected NAME=HH:MM-HH:MM")
        windows.append(TimeWindow.parse(name.strip(), rng.strip()))
    return windows


# Run helpers


def _guarded(what: str, run: Callable[[], Any]) -> tuple[bool, Any]:
    """Call *run*; a fatal error is logged and returns (False, None) instead of raising."""
    try:
        return True, run()
    except Exception as exc:  # fatal run-level errors -> exit 1
        log.error("%s aborted: %s", what, exc)
        log.debug("Traceback:", exc_info=True)
        return False, None


def _feed_inputs(settings: Settings) -> list[Path]:
    """The feed files a run actually read, for the manifest."""
    from .loader import discover_feeds
    from .loader import log as loader_log

    # The run already logged the discovery; don't repeat it.
    level = loader_log.level
    loader_log.setLevel(logging.ERROR)
    try:
        return [source.path for source in discover_feeds(settings.feed_paths)]
    finally:
        loader_log.setLevel(level)


def _record_run(
    command: str, report_dir: Path, parameters: dict, inputs: Sequence[Path] = ()
) -> None:
    """Write the run manifest; a failure here never fails the run."""
    from .manifest import build_manifest, write_manifest

    try:
        write_manifest(Path(report_dir), build_manifest(command, parameters, inputs))
    except Exception as exc:  # noqa: BLE001 - the outputs are already written
        log.warning("Run manifest not written: %s", exc)


def _settings_or_none(args: argparse.Namespace) -> Settings | None:
    """Settings with logging set up; None (after logging why) for invalid flags."""
    try:
        settings = _settings(args)
    except ValueError as exc:
        setup_logging("INFO")
        log.error("Invalid arguments: %s", exc)
        return None
    setup_logging(settings.log_level)
    return settings


# Command handlers


def _run_ingest(args: argparse.Namespace) -> int:
    """Run Phase 1 and return the exit code."""
    from .pipeline_phase1 import run_pipeline

    settings = _settings_or_none(args)
    if settings is None:
        return 1
    ok, run_report = _guarded("Pipeline", lambda: run_pipeline(settings))
    if not ok:
        return 1
    if not run_report.feeds:
        log.error("No feeds were found to process")
        return 1
    _record_run("ingest", settings.report_dir, settings.model_dump(), _feed_inputs(settings))
    error_count = sum(f.count(Severity.ERROR) for f in run_report.feeds)
    if args.fail_on_error and error_count:
        log.warning("%d ERROR finding(s) across feeds; exiting 2 (--fail-on-error)", error_count)
        return 2
    return 0


def _analysis_config(args: argparse.Namespace, settings: Settings) -> Any:
    """AnalysisConfig from the flags; raises ValueError for invalid values."""
    from .analysis import route_filter
    from .analysis.pipeline_phase2 import AnalysisConfig

    return AnalysisConfig(
        feed_paths=settings.feed_paths,
        database_url=settings.database_url,
        report_dir=settings.report_dir,
        **_date_options(args),
        analysis_date=settings.analysis_date or date.today(),
        proximity_m=args.proximity_m,
        windows=_windows(args.window),
        mode_codes=route_filter.parse_modes(args.transport_mode) if args.transport_mode else None,
        route_specs=route_filter.parse_routes(args.route) if args.route else None,
        network_walk=args.network_walk,
        walk_cache=args.walk_cache,
        walk_cache_dir=args.walk_cache_dir,
        osm_line_table=settings.osm_line_table,
        osm_geom_column=settings.osm_geom_column,
        **_stop_options(args),
        dry_run=settings.dry_run,
    )


def _run_analyze(args: argparse.Namespace) -> int:
    """Run Phase 2 and return the exit code."""
    from .analysis.pipeline_phase2 import run_analysis

    settings = _settings_or_none(args)
    if settings is None:
        return 1
    try:
        config = _analysis_config(args, settings)
    except ValueError as exc:
        log.error("Invalid arguments: %s", exc)
        return 1
    ok, report = _guarded("Analysis", lambda: run_analysis(config))
    if not ok:
        return 1
    if not report.feeds:
        log.error("No feeds were found to analyze")
        return 1
    _record_run("analyze-frequency", config.report_dir, config.model_dump(), _feed_inputs(settings))
    return 0


def _run_detect_interchanges(args: argparse.Namespace) -> int:
    """Run interchange detection and return the exit code."""
    from .conflation.pipeline_phase3 import InterchangeConfig, run_interchanges

    settings = _settings(args)
    setup_logging(settings.log_level)
    config = InterchangeConfig(
        feed_paths=settings.feed_paths,
        database_url=settings.database_url,
        report_dir=settings.report_dir,
        eps_m=args.eps,
        min_samples=args.min_samples,
        metric_crs=args.crs or settings.metric_crs,
        **_stop_options(args),
        dry_run=settings.dry_run,
    )
    ok, report = _guarded("Interchange detection", lambda: run_interchanges(config))
    if not ok or not report.feeds:
        return 1
    _record_run(
        "detect-interchanges", config.report_dir, config.model_dump(), _feed_inputs(settings)
    )
    return 0


def _run_conflate_osm(args: argparse.Namespace) -> int:
    """Run OSM conflation and return the exit code."""
    from .conflation.pipeline_phase3 import ConflationConfig, run_conflation

    settings = _settings(args)
    setup_logging(settings.log_level)
    config = ConflationConfig(
        database_url=settings.database_url,
        report_dir=settings.report_dir,
        match_threshold_m=args.match_threshold_m,
        street_threshold_m=args.street_threshold_m,
        osm_line_table=args.osm_line_table or settings.osm_line_table,
        osm_point_table=args.osm_point_table or settings.osm_point_table,
        osm_geom_column=args.osm_geom_column or settings.osm_geom_column,
        metric_crs=args.crs or settings.metric_crs,
        coverage_radius_m=args.coverage_radius_m,
        coverage_buffer_m=args.coverage_buffer_m,
        skip_coverage=args.skip_coverage,
        dry_run=settings.dry_run,
    )
    ok, _ = _guarded("OSM conflation", lambda: run_conflation(config))
    if not ok:
        return 1
    _record_run("conflate-osm", config.report_dir, config.model_dump())
    return 0


def _run_google_qa(args: argparse.Namespace) -> int:
    """Run (or skip) Google QA and return the exit code."""
    from .qa.pipeline_phase4 import QaConfig, run_google_qa

    settings = _settings(args)
    setup_logging(settings.log_level)
    config = QaConfig(
        database_url=settings.database_url,
        report_dir=settings.report_dir,
        enabled=args.enable_google_qa,
        api_key=settings.google_maps_api_key,  # env only - no CLI flag exists
        max_calls=args.max_calls,
        radius_m=args.radius_m,
        interchange_radius_m=args.interchange_radius_m,
        rate_per_s=args.rate_per_s,
        dry_run=settings.dry_run,
    )
    ok, _ = _guarded("Google QA", lambda: run_google_qa(config))
    if not ok:
        return 1
    _record_run("google-qa", config.report_dir, config.model_dump())
    return 0


def _run_locate_stations(args: argparse.Namespace) -> int:
    """Run (or skip) the station locator and return the exit code."""
    from .qa.station_locator import LocatorConfig, run_station_locator

    settings = _settings(args)
    setup_logging(settings.log_level)
    config = LocatorConfig(
        stations_file=args.stations,
        report_dir=settings.report_dir,
        enabled=args.enable_google_api,
        api_key=settings.google_maps_api_key,  # env only - no CLI flag exists
        transport_type=args.transport_type,
        line=args.line,
        language=args.language,
        region=args.region,
        search_radius_m=args.search_radius_m,
        use_distance_matrix=not args.no_distance_matrix,
        avg_speed_kmh=args.avg_speed_kmh,
        max_route_minutes=args.max_route_minutes,
        max_calls=args.max_calls,
        rate_per_s=args.rate_per_s,
        excel_name=args.excel_name,
        dry_run=settings.dry_run,
    )
    ok, _ = _guarded("Station locator", lambda: run_station_locator(config))
    if not ok:
        return 1
    _record_run("locate-stations", config.report_dir, config.model_dump(), [args.stations])
    return 0


def _run_export_neo4j(args: argparse.Namespace) -> int:
    """Build the Neo4j package and return the exit code."""
    from .export.neo4j import run_export

    settings = _settings(args)
    setup_logging(settings.log_level)
    ok, _ = _guarded("Export", lambda: run_export(settings.report_dir, out_dir=args.out))
    if not ok:
        return 1
    _record_run("export-neo4j", settings.report_dir, {"out": args.out})
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "ingest": _run_ingest,
    "analyze-frequency": _run_analyze,
    "detect-interchanges": _run_detect_interchanges,
    "conflate-osm": _run_conflate_osm,
    "google-qa": _run_google_qa,
    "locate-stations": _run_locate_stations,
    "export-neo4j": _run_export_neo4j,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code. Expected failures are logged, not raised."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # Flags without a command mean ingest (backward compatibility).
    if argv and argv[0] not in _SUBCOMMANDS and argv[0].startswith("--") and argv[0] != "--help":
        argv = ["ingest", *argv]
    args = build_parser().parse_args(argv)
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
