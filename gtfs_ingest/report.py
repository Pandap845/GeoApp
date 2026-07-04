"""Validation/coverage report emission (JSON + Markdown).

The JSON file is the machine-readable source of truth: it is the direct
serialization of :class:`~gtfs_ingest.models.RunReport`, so its schema is
exactly the pydantic model. The Markdown file renders the same data for
humans: a run summary, the feed x optional-file matrix, and one section per
feed with row counts and findings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .models import OPTIONAL_FILES, FeedReport, RunReport, Severity

log = logging.getLogger(__name__)

JSON_NAME = "validation_report.json"
MARKDOWN_NAME = "validation_report.md"


def build_run_report(
    feeds: Sequence[FeedReport],
    dry_run: bool,
    generated_at: Optional[datetime] = None,
) -> RunReport:
    """Assemble the top-level report object.

    Args:
        feeds: one report per processed feed.
        dry_run: whether DB writes were skipped this run.
        generated_at: timestamp override (mainly for tests); defaults to now
            in UTC.

    Returns:
        The complete :class:`RunReport`.
    """
    ts = generated_at or datetime.now(timezone.utc)
    return RunReport(
        generated_at=ts.isoformat(timespec="seconds"),
        dry_run=dry_run,
        feeds=list(feeds),
    )


def write_reports(report: RunReport, out_dir: Path) -> tuple[Path, Path]:
    """Write the JSON and Markdown reports to *out_dir* (created if needed).

    Args:
        report: the assembled run report.
        out_dir: target directory.

    Returns:
        ``(json_path, markdown_path)``.

    Raises:
        OSError: if the directory/files cannot be written - this is a fatal
            configuration problem, not a per-feed one, so it propagates.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / JSON_NAME
    md_path = out_dir / MARKDOWN_NAME
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    log.info("Reports written: %s, %s", json_path, md_path)
    return json_path, md_path


def render_markdown(report: RunReport) -> str:
    """Render the run report as human-readable Markdown.

    Args:
        report: the assembled run report.

    Returns:
        Markdown text with a run summary table, the feed x optional-file
        matrix, and a per-feed detail section (row counts + findings).
    """
    lines: list[str] = [
        "# GTFS Ingestion & Validation Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Mode: {'dry run (no DB writes)' if report.dry_run else 'full run'}  ",
        f"Feeds processed: {len(report.feeds)}",
        "",
        "## Summary",
        "",
        "| Feed | Status | Errors | Warnings | Stops normalized | Stops loaded |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for f in report.feeds:
        lines.append(
            f"| {f.feed_id} | {f.status} | {f.count(Severity.ERROR)} "
            f"| {f.count(Severity.WARNING)} | {f.stops_normalized} | {f.stops_loaded} |"
        )

    lines += ["", "## Optional file coverage", ""]
    header = "| Feed | " + " | ".join(name.removesuffix(".txt") for name in OPTIONAL_FILES) + " |"
    lines.append(header)
    lines.append("| --- |" + " :-: |" * len(OPTIONAL_FILES))
    for f in report.feeds:
        cells = " | ".join(
            "Y" if f.optional_files.get(name, False) else "-" for name in OPTIONAL_FILES
        )
        lines.append(f"| {f.feed_id} | {cells} |")

    for f in report.feeds:
        lines += ["", f"## Feed: {f.feed_id}", "", f"Source: `{f.source_path}`  ", f"Status: **{f.status}**", ""]
        if f.row_counts:
            lines += ["### Row counts", "", "| File | Rows |", "| --- | ---: |"]
            lines += [f"| {name} | {count} |" for name, count in sorted(f.row_counts.items())]
            lines.append("")
        if f.findings:
            lines += ["### Findings", "", "| Severity | Code | File | Count | Message |", "| --- | --- | --- | ---: | --- |"]
            for finding in f.findings:
                msg = finding.message.replace("|", "\\|")
                lines.append(
                    f"| {finding.severity.value} | {finding.code} | {finding.file or '-'} "
                    f"| {finding.count if finding.count is not None else '-'} | {msg} |"
                )
            lines.append("")
        else:
            lines += ["### Findings", "", "None - feed is clean.", ""]

    return "\n".join(lines) + "\n"
