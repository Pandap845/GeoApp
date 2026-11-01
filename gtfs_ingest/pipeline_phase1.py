"""Phase 1 orchestration: discover, load, validate, normalize, store stops, report.

A failure in one feed becomes an ERROR finding and the run moves on; only
run-level problems (database, report folder) stop it. Stops are loaded even
when other files of the feed have errors: partial data beats no data.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import Engine

from . import db, loader, normalizer, report, validators
from .config import Settings
from .models import FeedReport, FeedSource, Finding, RunReport, Severity

log = logging.getLogger(__name__)


def process_feed(
    source: FeedSource,
    engine: Engine | None,
    analysis_date: date | None = None,
) -> FeedReport:
    """Load, validate, normalize and store one feed. Never raises.

    *engine* is None for dry runs. Unexpected errors become an ERROR finding
    and a "failed" status.
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
            except Exception as exc:  # DB failure for one feed must not kill the run
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
            source_kind=source.kind,
            status="failed" if loaded.load_error else "loaded",
            row_counts=loaded.row_counts(),
            optional_files=validators.optional_file_matrix(loaded),
            findings=findings,
            stops_normalized=len(stops),
            stops_loaded=stops_loaded,
        )
    except Exception as exc:  # last-resort isolation per feed
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
            source_kind=source.kind,
            status="failed",
            findings=findings,
        )


def run_pipeline(settings: Settings) -> RunReport:
    """Run Phase 1 for every discovered feed and write the reports.

    Raises:
        ValueError: a non-dry run without a database URL.
        OSError / database errors: run-level problems.
    """
    sources = loader.discover_feeds(settings.feed_paths)
    if not sources:
        log.warning("No feeds discovered under: %s", ", ".join(map(str, settings.feed_paths)))

    engine: Engine | None = None
    if not settings.dry_run:
        if not settings.database_url:
            raise ValueError(
                "No database URL configured; set DATABASE_URL or pass --db-url "
                "(or use --dry-run to validate without a database)"
            )
        engine = db.make_engine(settings.database_url)
        db.ensure_schema(engine)

    # Fix the date once so expiry checks are reproducible within a run.
    analysis_date = settings.analysis_date or date.today()
    feed_reports = [process_feed(source, engine, analysis_date=analysis_date) for source in sources]

    run_report = report.build_run_report(
        feed_reports, dry_run=settings.dry_run, analysis_date=analysis_date.isoformat()
    )
    report.write_reports(run_report, settings.report_dir)

    errors = sum(f.count(Severity.ERROR) for f in feed_reports)
    warnings = sum(f.count(Severity.WARNING) for f in feed_reports)
    loaded_total = sum(f.stops_loaded for f in feed_reports)
    log.info(
        "Run complete: %d feed(s), %d error(s), %d warning(s), %d stops loaded",
        len(feed_reports),
        errors,
        warnings,
        loaded_total,
    )
    return run_report
