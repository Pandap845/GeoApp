"""Command-line entry point with subcommands.

Usage:
    # Phase 1 - ingest & validate
    python -m gtfs_ingest.cli ingest --feeds ./data/feeds --report ./out [--dry-run]

    # Phase 2 - headways + route connectivity
    python -m gtfs_ingest.cli analyze-frequency --feeds ./data/feeds \\
        --db-url $DATABASE_URL --service-date 2025-06-10 --report ./out \\
        [--proximity-m 150] [--window am_peak=06:00-09:00 ...] [--dry-run]

    # Phase 3A - multimodal interchange detection (+ transfers.txt check)
    python -m gtfs_ingest.cli detect-interchanges --feeds ./data/feeds \\
        --db-url $DATABASE_URL --eps 120 --report ./out [--dry-run]

    # Phase 3B+C - OSM conflation + side-of-street (OSM already in PostGIS)
    python -m gtfs_ingest.cli conflate-osm --db-url $DATABASE_URL \\
        --match-threshold-m 50 --report ./out [--dry-run]

Backward compatibility: invoking without a subcommand but with flags
(``python -m gtfs_ingest.cli --feeds ...``) is treated as ``ingest``.

Exit codes (both subcommands):
    0 - run completed (individual feeds may still have findings/failures;
        consult the reports, or use ingest's --fail-on-error for a gate)
    1 - fatal run-level problem (no feeds found, DB unreachable/missing
        PostGIS, missing DB URL on a non-dry run, unwritable report dir,
        invalid --service-date/--window values)
    2 - ingest only: ERROR findings present and --fail-on-error was set
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from .config import Settings, setup_logging
from .models import Severity

log = logging.getLogger(__name__)

_SUBCOMMANDS = ("ingest", "analyze-frequency", "detect-interchanges", "conflate-osm")


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Flags shared by every subcommand."""
    parser.add_argument(
        "--feeds",
        nargs="+",
        required=True,
        type=Path,
        metavar="PATH",
        help="Feed .zip files, feed directories, or directories containing multiple feeds.",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="PostgreSQL/PostGIS SQLAlchemy URL (default: DATABASE_URL env var).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory for reports/exports (default: ./out).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO, or GTFS_LOG_LEVEL env var).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate/analyze and write reports only; no database writes.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(
        prog="gtfs-ingest",
        description="GTFS ingestion, validation, and service analysis on PostGIS.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser(
        "ingest", help="Validate feeds, normalize stops, load them into PostGIS (Phase 1)."
    )
    _add_common_args(ingest)
    ingest.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with code 2 if any feed produced an ERROR finding.",
    )

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

    conflate = subparsers.add_parser(
        "conflate-osm",
        help="Match stops to OSM transit nodes and compute side-of-street (Phase 3B+C).",
    )
    # No --feeds: conflation reads the stops table and OSM layers from PostGIS.
    conflate.add_argument(
        "--db-url",
        default=None,
        help="PostgreSQL/PostGIS SQLAlchemy URL (default: DATABASE_URL env var). "
        "Required even with --dry-run: OSM in the database is the reference source.",
    )
    conflate.add_argument("--report", type=Path, default=None, metavar="DIR",
                          help="Directory for the reports (default: ./out).")
    conflate.add_argument("--log-level", default=None,
                          choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                          help="Log verbosity (default: INFO, or GTFS_LOG_LEVEL env var).")
    conflate.add_argument("--dry-run", action="store_true",
                          help="Compute and report only; skip the table writes.")
    conflate.add_argument("--match-threshold-m", type=float, default=50.0, metavar="METERS",
                          help="Max stop-to-OSM-node distance for a match (default: 50).")
    conflate.add_argument("--street-threshold-m", type=float, default=50.0, metavar="METERS",
                          help="Max stop-to-centerline distance for side-of-street (default: 50).")
    conflate.add_argument("--osm-line-table", default=None,
                          help="OSM highway line table (default: planet_osm_line / GTFS_OSM_LINE_TABLE).")
    conflate.add_argument("--osm-point-table", default=None,
                          help="OSM point table (default: planet_osm_point / GTFS_OSM_POINT_TABLE).")
    conflate.add_argument("--osm-geom-column", default=None,
                          help="Geometry column of the OSM tables (default: way / GTFS_OSM_GEOM_COLUMN).")
    conflate.add_argument("--crs", default=None, metavar="EPSG:XXXXX",
                          help="Metric CRS for side math (default: auto-detect a UTM zone).")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Resolve Settings from explicit CLI flags with env/.env fallbacks."""
    overrides: dict = {"feed_paths": list(args.feeds), "dry_run": args.dry_run}
    if args.db_url:
        overrides["database_url"] = args.db_url
    if args.report:
        overrides["report_dir"] = args.report
    if args.log_level:
        overrides["log_level"] = args.log_level
    return Settings(**overrides)


def _run_ingest(args: argparse.Namespace) -> int:
    """Execute the Phase 1 ingest flow; returns the process exit code."""
    from .pipeline import run_pipeline

    settings = _settings_from_args(args)
    setup_logging(settings.log_level)
    try:
        run_report = run_pipeline(settings)
    except Exception as exc:  # noqa: BLE001 - fatal run-level errors -> exit 1
        log.error("Pipeline aborted: %s", exc)
        log.debug("Traceback:", exc_info=True)
        return 1

    if not run_report.feeds:
        log.error("No feeds were found to process")
        return 1
    error_count = sum(f.count(Severity.ERROR) for f in run_report.feeds)
    if args.fail_on_error and error_count:
        log.warning("%d ERROR finding(s) across feeds; exiting 2 (--fail-on-error)", error_count)
        return 2
    return 0


def _run_analyze(args: argparse.Namespace) -> int:
    """Execute the Phase 2 analysis flow; returns the process exit code."""
    from .analysis.headways import DEFAULT_WINDOWS, TimeWindow
    from .analysis.pipeline_day2 import AnalysisConfig, run_analysis

    settings = _settings_from_args(args)
    setup_logging(settings.log_level)

    try:
        service_date: Optional[date] = (
            date.fromisoformat(args.service_date) if args.service_date else None
        )
        if args.window:
            windows = []
            for spec in args.window:
                name, _, rng = spec.partition("=")
                if not name or not rng:
                    raise ValueError(f"--window {spec!r}: expected NAME=HH:MM-HH:MM")
                windows.append(TimeWindow.parse(name.strip(), rng.strip()))
        else:
            windows = list(DEFAULT_WINDOWS)
        config = AnalysisConfig(
            feed_paths=settings.feed_paths,
            database_url=settings.database_url,
            report_dir=settings.report_dir,
            service_date=service_date,
            proximity_m=args.proximity_m,
            windows=windows,
            dry_run=settings.dry_run,
        )
    except ValueError as exc:
        log.error("Invalid arguments: %s", exc)
        return 1

    try:
        report = run_analysis(config)
    except Exception as exc:  # noqa: BLE001 - fatal run-level errors -> exit 1
        log.error("Analysis aborted: %s", exc)
        log.debug("Traceback:", exc_info=True)
        return 1
    if not report.feeds:
        log.error("No feeds were found to analyze")
        return 1
    return 0


def _run_detect_interchanges(args: argparse.Namespace) -> int:
    """Execute the Phase 3A interchange flow; returns the process exit code."""
    from .conflation.pipeline_day3 import InterchangeConfig, run_interchanges

    settings = _settings_from_args(args)
    setup_logging(settings.log_level)
    config = InterchangeConfig(
        feed_paths=settings.feed_paths,
        database_url=settings.database_url,
        report_dir=settings.report_dir,
        eps_m=args.eps,
        min_samples=args.min_samples,
        metric_crs=args.crs or settings.metric_crs,
        dry_run=settings.dry_run,
    )
    try:
        report = run_interchanges(config)
    except Exception as exc:  # noqa: BLE001 - fatal run-level errors -> exit 1
        log.error("Interchange detection aborted: %s", exc)
        log.debug("Traceback:", exc_info=True)
        return 1
    return 0 if report.feeds else 1


def _run_conflate_osm(args: argparse.Namespace) -> int:
    """Execute the Phase 3B+C conflation flow; returns the process exit code."""
    from .conflation.pipeline_day3 import ConflationConfig, run_conflation

    overrides: dict = {"feed_paths": [], "dry_run": args.dry_run}
    if args.db_url:
        overrides["database_url"] = args.db_url
    if args.report:
        overrides["report_dir"] = args.report
    if args.log_level:
        overrides["log_level"] = args.log_level
    settings = Settings(**overrides)
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
        dry_run=settings.dry_run,
    )
    try:
        run_conflation(config)
    except Exception as exc:  # noqa: BLE001 - fatal run-level errors -> exit 1
        log.error("OSM conflation aborted: %s", exc)
        log.debug("Traceback:", exc_info=True)
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: argument vector override (for tests); defaults to sys.argv.

    Returns:
        Process exit code (see module docstring). Anticipated failures are
        logged and mapped to exit codes, never raised.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backward compatibility: `--feeds ...` without a subcommand means ingest.
    if argv and argv[0] not in _SUBCOMMANDS and argv[0].startswith("--") and argv[0] != "--help":
        argv = ["ingest", *argv]
    args = build_parser().parse_args(argv)

    if args.command == "ingest":
        return _run_ingest(args)
    if args.command == "analyze-frequency":
        return _run_analyze(args)
    if args.command == "detect-interchanges":
        return _run_detect_interchanges(args)
    return _run_conflate_osm(args)


if __name__ == "__main__":
    sys.exit(main())
