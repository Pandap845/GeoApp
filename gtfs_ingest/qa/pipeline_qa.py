"""Phase 4 orchestration: gated, budgeted Google QA cross-check.

Gating (checked before anything else; both cause a clean NO-OP, exit 0):

1. ``enabled`` must be True (the ``--enable-google-qa`` flag);
2. an API key must be present (``GOOGLE_MAPS_API_KEY`` env var only).

When it runs: eligible records only (multimodal interchanges, needs-review
stops), one API call per record, hard budget ceiling, verdicts upserted to
the two QA tables (place_id + verdict + flag + timestamp - nothing else),
and a JSON + Markdown report referencing OUR ids and verdicts only.

This phase never mutates GTFS/OSM data - it appends/updates verdict rows.
``--dry-run`` makes NO API calls and NO DB writes: it reports what would be
checked (eligibility counts vs the budget).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .. import db
from . import crosscheck, storage
from .google_client import CallBudgetExhausted, GooglePlacesClient

log = logging.getLogger(__name__)

QA_JSON = "google_qa_report.json"
QA_MD = "google_qa_report.md"


class QaConfig(BaseModel):
    """Configuration of one google-qa run.

    ``api_key`` is excluded from reprs; it must come from the environment
    (the CLI has no flag for it, by design).
    """

    database_url: Optional[str] = None
    report_dir: Path = Path("out")
    enabled: bool = False
    api_key: Optional[str] = Field(default=None, repr=False)
    max_calls: int = Field(default=200, ge=1)
    radius_m: float = Field(default=50.0, gt=0)
    interchange_radius_m: float = Field(default=150.0, gt=0)
    rate_per_s: float = Field(default=5.0, ge=0)
    dry_run: bool = False


class QaReport(BaseModel):
    """Top-level QA report - our ids and verdicts only, no Google content."""

    generated_at: str
    status: str  # "ran" | "dry_run" | "skipped_disabled" | "skipped_no_key"
    max_calls: int = 0
    calls_made: int = 0
    budget_remaining: int = 0
    eligible_interchanges: int = 0
    eligible_stops: int = 0
    checked_interchanges: int = 0
    checked_stops: int = 0
    interchange_verdicts: dict[str, int] = Field(default_factory=dict)
    stop_verdicts: dict[str, int] = Field(default_factory=dict)
    conflicts: list[dict] = Field(default_factory=list)  # our cluster_id + our modes
    unchecked_due_to_budget: int = 0
    notes: list[str] = Field(default_factory=list)


def run_google_qa(config: QaConfig) -> QaReport:
    """Execute (or cleanly skip) the Phase 4 QA cross-check.

    Returns:
        The report (also written to ``config.report_dir``). Skips are
        reported with a clear status, never raised.

    Raises:
        ValueError: enabled run without a database URL.
        sqlalchemy errors / OSError: run-level problems (DB, report dir).
    """
    now = datetime.now(timezone.utc)
    report = QaReport(
        generated_at=now.isoformat(timespec="seconds"),
        status="ran",
        max_calls=config.max_calls,
        budget_remaining=config.max_calls,
    )

    # ---- gating: OFF unless flag AND key are both present -----------------
    if not config.enabled:
        report.status = "skipped_disabled"
        report.notes.append("google-qa is OFF by default; pass --enable-google-qa to run it")
        log.info("Google QA skipped: not enabled (--enable-google-qa absent)")
        _write(report, config.report_dir)
        return report
    if not config.api_key:
        report.status = "skipped_no_key"
        report.notes.append("GOOGLE_MAPS_API_KEY is not set in the environment; nothing was called")
        log.info("Google QA skipped: GOOGLE_MAPS_API_KEY not set")
        _write(report, config.report_dir)
        return report

    if not config.database_url:
        raise ValueError("google-qa requires a database URL (--db-url / DATABASE_URL)")
    engine = db.make_engine(config.database_url)

    interchanges = crosscheck.eligible_interchanges(engine)
    stops = crosscheck.eligible_stops(engine)
    report.eligible_interchanges = len(interchanges)
    report.eligible_stops = len(stops)

    if config.dry_run:
        report.status = "dry_run"
        planned = min(config.max_calls, len(interchanges) + len(stops))
        report.notes.append(
            f"dry run: {len(interchanges)} interchange(s) + {len(stops)} stop(s) eligible; "
            f"{planned} API call(s) would be made (budget {config.max_calls}); "
            "no API calls or DB writes were performed"
        )
        _write(report, config.report_dir)
        return report

    storage.ensure_qa_schema(engine)
    client = GooglePlacesClient(
        api_key=config.api_key, max_calls=config.max_calls, rate_per_s=config.rate_per_s
    )
    interchange_rows: list[dict] = []
    stop_rows: list[dict] = []
    budget_hit = False
    try:
        # ---- (a) multimodal interchanges: verify the mode combination -----
        for row in interchanges:
            try:
                places = client.search_nearby(
                    row["lat"], row["lon"], config.interchange_radius_m,
                    field_mask=crosscheck.INTERCHANGE_FIELD_MASK,
                    included_types=crosscheck.TRANSIT_PLACE_TYPES,
                )
            except CallBudgetExhausted:
                budget_hit = True
                break
            our_modes = set(row["modes"] or [])
            verdict, agreement, place_id = crosscheck.interchange_verdict(our_modes, places)
            # `places` (Google content) goes out of scope here - by design.
            interchange_rows.append(
                {
                    "interchange_cluster_id": int(row["cluster_id"]),
                    "verdict": verdict,
                    "mode_agreement": agreement,
                    "google_place_id": place_id,
                    "checked_at": now,
                }
            )
            if verdict == "CONFLICT":
                report.conflicts.append(
                    {"cluster_id": int(row["cluster_id"]), "our_modes": list(row["mode_names"] or [])}
                )

        # ---- (b) needs-review stops: verify a transit place exists --------
        if not budget_hit:
            for row in stops:
                try:
                    places = client.search_nearby(
                        row["lat"], row["lon"], config.radius_m,
                        field_mask=crosscheck.STOP_FIELD_MASK,
                        included_types=crosscheck.TRANSIT_PLACE_TYPES,
                    )
                except CallBudgetExhausted:
                    budget_hit = True
                    break
                verdict, place_id = crosscheck.stop_verdict(places)
                stop_rows.append(
                    {
                        "stop_uid": row["stop_uid"],
                        "verdict": verdict,
                        "google_place_id": place_id,
                        "checked_at": now,
                    }
                )
    finally:
        report.calls_made = client.calls_made
        report.budget_remaining = client.budget_remaining
        client.close()

    storage.upsert_interchange_verdicts(engine, interchange_rows)
    storage.upsert_stop_verdicts(engine, stop_rows)

    report.checked_interchanges = len(interchange_rows)
    report.checked_stops = len(stop_rows)
    report.unchecked_due_to_budget = (
        len(interchanges) - len(interchange_rows) + len(stops) - len(stop_rows)
    )
    if budget_hit:
        report.notes.append(
            f"call budget of {config.max_calls} reached; "
            f"{report.unchecked_due_to_budget} eligible record(s) left unchecked"
        )
        log.warning("Google QA: %s", report.notes[-1])
    report.interchange_verdicts = _count(interchange_rows)
    report.stop_verdicts = _count(stop_rows)

    _write(report, config.report_dir)
    log.info(
        "Google QA complete: %d interchange(s), %d stop(s) checked with %d call(s); %d conflict(s)",
        report.checked_interchanges, report.checked_stops, report.calls_made, len(report.conflicts),
    )
    return report


def _count(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return counts


def _write(report: QaReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / QA_JSON).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / QA_MD).write_text(_markdown(report), encoding="utf-8")
    log.info("QA reports written to %s", out_dir)


def _markdown(report: QaReport) -> str:
    lines = [
        "# Google Maps QA Cross-Check Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Status: **{report.status}**  ",
        f"API calls: {report.calls_made} of {report.max_calls} "
        f"(remaining budget: {report.budget_remaining})",
        "",
        "Only our identifiers and computed verdicts appear below - no Google",
        "content is stored or reported (see the README licensing note).",
        "",
        "## Eligibility",
        "",
        f"- Multimodal interchanges: {report.eligible_interchanges} eligible, "
        f"{report.checked_interchanges} checked",
        f"- Needs-review stops: {report.eligible_stops} eligible, {report.checked_stops} checked",
        f"- Left unchecked (budget): {report.unchecked_due_to_budget}",
        "",
        "## Interchange verdicts",
        "",
        "| Verdict | Count |",
        "| --- | ---: |",
        *[f"| {v} | {c} |" for v, c in sorted(report.interchange_verdicts.items())],
        "",
        "## Stop verdicts",
        "",
        "| Verdict | Count |",
        "| --- | ---: |",
        *[f"| {v} | {c} |" for v, c in sorted(report.stop_verdicts.items())],
        "",
        "## CONFLICT cases for human review",
        "",
    ]
    if report.conflicts:
        lines += ["| Cluster | Our inferred modes |", "| --- | --- |"]
        lines += [
            f"| {c['cluster_id']} | {', '.join(c['our_modes']) or '-'} |" for c in report.conflicts
        ]
    else:
        lines.append("None.")
    if report.notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in report.notes]
    lines.append("")
    return "\n".join(lines)
