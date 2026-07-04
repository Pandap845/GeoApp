"""Command-line entry point.

Usage:
    python -m gtfs_ingest.cli --feeds ./data/feeds --db-url $DATABASE_URL --report ./out
    python -m gtfs_ingest.cli --feeds feedA.zip ./feedB_dir --dry-run

Exit codes:
    0 - run completed (individual feeds may still have findings; consult
        the report and check ``--fail-on-error`` if you need a hard signal)
    1 - fatal run-level problem (no feeds found, DB unreachable/missing
        PostGIS, unwritable report directory, missing DB URL)
    2 - completed but at least one ERROR finding, only with --fail-on-error
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from .config import Settings, setup_logging
from .models import Severity
from .pipeline import run_pipeline

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(
        prog="gtfs-ingest",
        description="Validate GTFS feeds, normalize stops, and load them into PostGIS.",
    )
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
        help="Directory for the JSON + Markdown reports (default: ./out).",
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
        help="Validate and write reports only; no database writes.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with code 2 if any feed produced an ERROR finding.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: argument vector override (for tests); defaults to sys.argv.

    Returns:
        Process exit code (see module docstring). Never raises for
        anticipated failure modes - they are logged and mapped to exit 1.
    """
    args = build_parser().parse_args(argv)

    # Only pass explicitly-set CLI values so env vars / .env keep working
    # as fallbacks (pydantic-settings handles that precedence).
    overrides: dict = {"feed_paths": list(args.feeds), "dry_run": args.dry_run}
    if args.db_url:
        overrides["database_url"] = args.db_url
    if args.report:
        overrides["report_dir"] = args.report
    if args.log_level:
        overrides["log_level"] = args.log_level
    settings = Settings(**overrides)

    setup_logging(settings.log_level)

    try:
        run_report = run_pipeline(settings)
    except Exception as exc:  # noqa: BLE001 - map fatal run-level errors to exit code 1
        log.error("Pipeline aborted: %s", exc)
        logging.getLogger(__name__).debug("Traceback:", exc_info=True)
        return 1

    if not run_report.feeds:
        log.error("No feeds were found to process")
        return 1

    error_count = sum(f.count(Severity.ERROR) for f in run_report.feeds)
    if args.fail_on_error and error_count:
        log.warning("%d ERROR finding(s) across feeds; exiting 2 (--fail-on-error)", error_count)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
