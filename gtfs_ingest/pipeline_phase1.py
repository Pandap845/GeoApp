"""End-to-end orchestration of the Phase 1 flow.

Per run:  discover feeds -> (ensure DB schema unless dry-run) -> per feed:
load -> validate -> normalize -> load stops into PostGIS -> emit reports.

Failure isolation contract: any exception while processing one feed is
caught, logged with traceback, recorded as an ERROR finding on that feed's
report entry, and the run continues with the next feed. Only run-level
problems (unwritable report dir, unreachable/misconfigured database) abort
the run, because every subsequent feed would fail identically.

A feed's stops are loaded whenever its stops.txt yielded normalized rows,
even if the feed has other validation ERRORs (e.g. broken stop_times) -
partial data with provenance beats no data, and the report documents every
defect. Feeds whose stops.txt is unusable simply contribute zero rows.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy import Engine

from . import db, loader, normalizer, report, validators
from .config import Settings
from .models import FeedReport, FeedSource, Finding, RunReport, Severity

log = logging.getLogger(__name__)


def process_feed(
    source: FeedSource,
    engine: Optional[Engine],
    analysis_date: Optional[date] = None,
) -> FeedReport:
    """Run the full load/validate/normalize/store flow for one feed.

    Args:
        source: discovered feed.
        engine: database engine, or ``None`` for dry runs (skip DB writes).
        analysis_date: the run's reference date for expiry judgments
            (injectable clock); ``None`` uses today.

    Returns:
        The feed's report entry. Never raises: any unexpected failure is
        captured as an ERROR finding with ``status="failed"``.
    """
    findings: list[Finding] = []
    try:
        loaded = loader.load_feed(source)
        findings.extend(validators.validate_feed(loaded, analysis_date=analysis_date))
        stops, norm_findings = normalizer.normalize_stops(loaded)
        findings.extend(norm_findings)

        stops_loaded = 0
        if engine is not None:
            try:
                stops_loaded = db.load_feed_stops(engine, source.feed_id, stops)
            except Exception as exc:  # noqa: BLE001 - DB failure for one feed must not kill the run
                log.exception("Feed %s: database load failed", source.feed_id)
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code="db_load_failed",
                        message=f"Loading stops into PostGIS failed: {type(exc).__name__}: {exc}",
                    )
                )

        return FeedReport(
            feed_id=source.feed_id,
            source_path=str(source.path),
            status="failed" if loaded.load_error else "loaded",
            row_counts=loaded.row_counts(),
            optional_files=validators.optional_file_matrix(loaded),
            findings=findings,
            stops_normalized=int(len(stops)),
            stops_loaded=stops_loaded,
        )
    except Exception as exc:  # noqa: BLE001 - last-resort isolation per feed
        log.exception("Feed %s: unexpected failure", source.feed_id)
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code="pipeline_crash",
                message=f"Unexpected error while processing feed: {type(exc).__name__}: {exc}",
            )
        )
        return FeedReport(
            feed_id=source.feed_id,
            source_path=str(source.path),
            status="failed",
            findings=findings,
        )


def run_pipeline(settings: Settings) -> RunReport:
    """Execute one full pipeline run and write the reports.

    Args:
        settings: resolved configuration (CLI + environment).

    Returns:
        The run report (also written to ``settings.report_dir`` as JSON
        and Markdown).

    Raises:
        ValueError: if a non-dry run has no database URL configured.
        RuntimeError / sqlalchemy errors: if the database is unreachable or
            lacks PostGIS (run-level problems).
        OSError: if the report directory is unwritable.
    """
    sources = loader.discover_feeds(settings.feed_paths)
    if not sources:
        log.warning("No feeds discovered under: %s", ", ".join(map(str, settings.feed_paths)))

    engine: Optional[Engine] = None
    if not settings.dry_run:
        if not settings.database_url:
            raise ValueError(
                "No database URL configured; set DATABASE_URL or pass --db-url "
                "(or use --dry-run to validate without a database)"
            )
        engine = db.make_engine(settings.database_url)
        db.ensure_schema(engine)

    # One resolved analysis date per run: expiry judgments become
    # reproducible facts instead of moving wall-clock targets.
    analysis_date = settings.analysis_date or date.today()
    feed_reports = [
        process_feed(source, engine, analysis_date=analysis_date) for source in sources
    ]

    run_report = report.build_run_report(
        feed_reports, dry_run=settings.dry_run, analysis_date=analysis_date.isoformat()
    )
    report.write_reports(run_report, settings.report_dir)

    errors = sum(f.count(Severity.ERROR) for f in feed_reports)
    warnings = sum(f.count(Severity.WARNING) for f in feed_reports)
    loaded_total = sum(f.stops_loaded for f in feed_reports)
    log.info(
        "Run complete: %d feed(s), %d error(s), %d warning(s), %d stops loaded",
        len(feed_reports), errors, warnings, loaded_total,
    )
    return run_report
