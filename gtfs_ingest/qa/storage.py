"""QA verdict tables, guarded so no Google content beyond place_id is ever stored.

Google Maps terms allow storing only the opaque place_id. Every record is
checked against a fixed column allowlist before any SQL runs, and a test
checks the SQL in this module too. Rows are upserted (not reloaded) because
QA runs are partial: re-checking 20 items must not erase earlier verdicts.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

from sqlalchemy import Engine, text

log = logging.getLogger(__name__)


class StorageViolation(RuntimeError):
    """A record tried to persist a column outside the QA allowlist."""


#: The only columns that may ever be written to the QA tables.
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
    """Raise StorageViolation if *record* has a column outside *allowed*."""
    unexpected = set(record) - allowed
    if unexpected:
        raise StorageViolation(
            f"refusing to persist non-allowlisted column(s) {sorted(unexpected)}; "
            f"only {sorted(allowed)} may be stored (Google Maps ToS guard)"
        )


def ensure_qa_schema(engine: Engine) -> None:
    """Create the two QA verdict tables if missing."""
    with engine.begin() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info("QA schema ensured (google_qa_interchange, google_qa_stop)")


def upsert_interchange_verdicts(engine: Engine, records: Iterable[Mapping[str, object]]) -> int:
    """Upsert interchange verdicts; raises StorageViolation on a non-allowed column."""
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
    """Upsert stop verdicts; raises StorageViolation on a non-allowed column."""
    rows = [dict(r) for r in records]
    for row in rows:
        _assert_persistable(row, ALLOWED_STOP_COLUMNS)
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(_UPSERT_STOP, rows)
    log.info("google_qa_stop: upserted %d verdict(s)", len(rows))
    return len(rows)
