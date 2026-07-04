"""Guarded persistence for the QA verdict tables.

THE STORAGE CONSTRAINT LIVES HERE: per the Google Maps Platform Terms, the
only Google-derived value that may reach the database is the opaque
``place_id``. Everything else in these tables is computed by US (verdicts,
agreement flag, timestamp) or references OUR keys (cluster_id, stop_uid).

Enforcement is mechanical, not conventional: every record passes
:func:`_assert_persistable` against a frozen column allowlist before any
SQL runs; an unexpected column raises :class:`StorageViolation`. The test
suite additionally parses the DDL/INSERT statements in this module and
fails if a column outside the allowlist ever appears.

Idempotency: **per-row upsert** (``ON CONFLICT DO UPDATE``) keyed on our
ids, unlike the full-reload of Phase 3 tables - QA runs are budgeted and
partial by design, and a re-check of 20 interchanges must not wipe the 180
verdicts from a previous run. Re-running the same check replaces exactly
that row.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

from sqlalchemy import Engine, text

log = logging.getLogger(__name__)


class StorageViolation(RuntimeError):
    """A record tried to persist a column outside the QA allowlist."""


#: The ONLY columns that may ever be written to the QA tables.
ALLOWED_INTERCHANGE_COLUMNS: frozenset[str] = frozenset(
    {"interchange_cluster_id", "verdict", "mode_agreement", "google_place_id", "checked_at"}
)
ALLOWED_STOP_COLUMNS: frozenset[str] = frozenset(
    {"stop_uid", "verdict", "google_place_id", "checked_at"}
)

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS google_qa_interchange (
        interchange_cluster_id integer PRIMARY KEY,
        verdict                text NOT NULL,
        mode_agreement         boolean,
        google_place_id        text,
        checked_at             timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_qa_stop (
        stop_uid        text PRIMARY KEY,
        verdict         text NOT NULL,
        google_place_id text,
        checked_at      timestamptz NOT NULL
    )
    """,
)

_UPSERT_INTERCHANGE = text(
    """
    INSERT INTO google_qa_interchange
        (interchange_cluster_id, verdict, mode_agreement, google_place_id, checked_at)
    VALUES (:interchange_cluster_id, :verdict, :mode_agreement, :google_place_id, :checked_at)
    ON CONFLICT (interchange_cluster_id) DO UPDATE SET
        verdict = EXCLUDED.verdict,
        mode_agreement = EXCLUDED.mode_agreement,
        google_place_id = EXCLUDED.google_place_id,
        checked_at = EXCLUDED.checked_at
    """
)

_UPSERT_STOP = text(
    """
    INSERT INTO google_qa_stop (stop_uid, verdict, google_place_id, checked_at)
    VALUES (:stop_uid, :verdict, :google_place_id, :checked_at)
    ON CONFLICT (stop_uid) DO UPDATE SET
        verdict = EXCLUDED.verdict,
        google_place_id = EXCLUDED.google_place_id,
        checked_at = EXCLUDED.checked_at
    """
)


def _assert_persistable(record: Mapping[str, object], allowed: frozenset[str]) -> None:
    """Raise :class:`StorageViolation` if *record* carries extra columns.

    This is the runtime guard for the licensing constraint - it runs before
    any SQL is issued, so a coding mistake elsewhere cannot leak Google
    content into the database.
    """
    unexpected = set(record) - allowed
    if unexpected:
        raise StorageViolation(
            f"refusing to persist non-allowlisted column(s) {sorted(unexpected)}; "
            f"only {sorted(allowed)} may be stored (Google Maps ToS guard)"
        )


def ensure_qa_schema(engine: Engine) -> None:
    """Create the two QA verdict tables (idempotent).

    Raises:
        sqlalchemy.exc.SQLAlchemyError: on connection/DDL failure.
    """
    with engine.begin() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info("QA schema ensured (google_qa_interchange, google_qa_stop)")


def upsert_interchange_verdicts(engine: Engine, records: Iterable[Mapping[str, object]]) -> int:
    """Upsert interchange verdict rows (validated against the allowlist).

    Raises:
        StorageViolation: a record carries a non-allowlisted column.
    """
    rows = [dict(r) for r in records]
    for row in rows:
        _assert_persistable(row, ALLOWED_INTERCHANGE_COLUMNS)
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(_UPSERT_INTERCHANGE, rows)
    log.info("google_qa_interchange: upserted %d verdict(s)", len(rows))
    return len(rows)


def upsert_stop_verdicts(engine: Engine, records: Iterable[Mapping[str, object]]) -> int:
    """Upsert stop verdict rows (validated against the allowlist).

    Raises:
        StorageViolation: a record carries a non-allowlisted column.
    """
    rows = [dict(r) for r in records]
    for row in rows:
        _assert_persistable(row, ALLOWED_STOP_COLUMNS)
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(_UPSERT_STOP, rows)
    log.info("google_qa_stop: upserted %d verdict(s)", len(rows))
    return len(rows)
