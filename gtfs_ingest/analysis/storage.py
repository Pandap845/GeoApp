"""DDL and idempotent loading for the Phase 2 analysis tables.

Kept in the analysis subpackage (rather than bloating Phase 1's ``db.py``)
while reusing its engine and conventions.

Idempotency strategies (documented in the README):

- ``route_stop_headways`` / ``route_headways``: **per-feed delete-and-reload**
  in one transaction, same as Phase 1 stops - re-analyzing a feed replaces
  exactly that feed's rows.
- ``route_connections``: **full truncate-and-reload per run**. Proximity
  edges can span feeds, so rows aren't attributable to a single feed;
  the table is cheap derived data recomputed from the analyzed feed set.
- ``stop_equivalences``: **full truncate-and-reload per run**, for the same
  reason - a row pairs two stops that describe one physical station, either
  across feeds or within a single feed (``analysis.dedupe``; the ``match_kind``
  column says which), so rows aren't attributable to a single feed either.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from ..bulk import bulk_insert

log = logging.getLogger(__name__)

#: Columns fetched from the Phase 3 ``stop_osm_matches`` table for the
#: quality sidecar's ``coord_confidence``.
OSM_MATCH_COLUMNS = ["stop_uid", "osm_id", "distance_m", "match_confidence", "needs_review"]

_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS route_stop_headways (
        feed_id          text NOT NULL,
        service_date     date NOT NULL,
        route_id         text NOT NULL,
        direction_id     integer NOT NULL,
        stop_id          text NOT NULL,
        time_window      text NOT NULL,
        trip_count       integer NOT NULL,
        mean_headway_s   double precision,
        median_headway_s double precision,
        min_headway_s    double precision,
        max_headway_s    double precision,
        loaded_at        timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (feed_id, service_date, route_id, direction_id, stop_id, time_window)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_headways (
        feed_id             text NOT NULL,
        service_date        date NOT NULL,
        route_id            text NOT NULL,
        direction_id        integer NOT NULL,
        time_window         text NOT NULL,
        mean_headway_s      double precision,
        stop_count          integer NOT NULL,
        stops_with_headway  integer NOT NULL,
        trip_count          integer NOT NULL,
        loaded_at           timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (feed_id, service_date, route_id, direction_id, time_window)
    )
    """,
    # Upgrade path for databases created before --service-date-range existed:
    # widen the headway PKs to include service_date (idempotent - only fires
    # when the live constraint still lacks the column; widening a PK cannot
    # violate uniqueness because the old key is a subset of the new one).
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'route_stop_headways_pkey')
           AND NOT EXISTS (
            SELECT 1
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.conname = 'route_stop_headways_pkey' AND a.attname = 'service_date'
        ) THEN
            ALTER TABLE route_stop_headways DROP CONSTRAINT route_stop_headways_pkey;
            ALTER TABLE route_stop_headways ADD CONSTRAINT route_stop_headways_pkey
                PRIMARY KEY (feed_id, service_date, route_id, direction_id, stop_id, time_window);
        END IF;
    END $$
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'route_headways_pkey')
           AND NOT EXISTS (
            SELECT 1
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.conname = 'route_headways_pkey' AND a.attname = 'service_date'
        ) THEN
            ALTER TABLE route_headways DROP CONSTRAINT route_headways_pkey;
            ALTER TABLE route_headways ADD CONSTRAINT route_headways_pkey
                PRIMARY KEY (feed_id, service_date, route_id, direction_id, time_window);
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS route_connections (
        route_a             text NOT NULL,
        route_b             text NOT NULL,
        feed_id_a           text NOT NULL,
        feed_id_b           text NOT NULL,
        connection_type     text NOT NULL,
        shared_stop_count   integer NOT NULL,
        min_stop_distance_m double precision,
        loaded_at           timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (route_a, route_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stop_equivalences (
        group_id      integer NOT NULL,
        canonical_uid text NOT NULL,
        stop_uid_a    text NOT NULL,
        stop_uid_b    text NOT NULL,
        distance_m    double precision NOT NULL,
        name_score    double precision NOT NULL,
        shared_routes integer NOT NULL,
        score         double precision NOT NULL,
        match_kind    text,
        loaded_at     timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (stop_uid_a, stop_uid_b)
    )
    """,
    # Databases created before within-feed dedupe lack match_kind; without
    # this the next replace_all_rows fails on a column mismatch.
    "ALTER TABLE stop_equivalences ADD COLUMN IF NOT EXISTS match_kind text",
    "CREATE INDEX IF NOT EXISTS ix_rsh_feed ON route_stop_headways (feed_id)",
    "CREATE INDEX IF NOT EXISTS ix_rh_feed ON route_headways (feed_id)",
    "CREATE INDEX IF NOT EXISTS ix_stop_equiv_group ON stop_equivalences (group_id)",
)

#: Whitelist: table name -> has a feed_id column usable for per-feed replace.
_TABLES: dict[str, bool] = {
    "route_stop_headways": True,
    "route_headways": True,
    "route_connections": False,
    "stop_equivalences": False,
}

def ensure_analysis_schema(engine: Engine) -> None:
    """Create the four analysis tables and their indexes (idempotent).

    Args:
        engine: engine from Phase 1 ``db.make_engine``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: on connection/DDL failure
            (run-level problem - every feed would fail identically).
    """
    with engine.begin() as conn:
        for stmt in _DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info(
        "Analysis schema ensured (route_stop_headways, route_headways, "
        "route_connections, stop_equivalences)"
    )


def _insert(conn, table: str, df: pd.DataFrame) -> None:
    """psycopg3 COPY with executemany fallback (``bulk.bulk_insert``)."""
    bulk_insert(conn, table, df)


def replace_feed_rows(engine: Engine, table: str, feed_id: str, df: pd.DataFrame) -> int:
    """Idempotently replace one feed's rows in a headway table.

    Args:
        engine: database engine.
        table: ``route_stop_headways`` or ``route_headways``.
        feed_id: feed whose rows are replaced.
        df: new rows (must include the table's columns; ``feed_id`` column
            expected to equal *feed_id*).

    Returns:
        Number of rows inserted.

    Raises:
        ValueError: unknown table name (guards the SQL interpolation).
        sqlalchemy.exc.SQLAlchemyError: on DB failure (transaction rolls
            back; previous rows stay intact).
    """
    if not _TABLES.get(table):
        raise ValueError(f"table {table!r} does not support per-feed replacement")
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table} WHERE feed_id = :feed_id"), {"feed_id": feed_id})
        _insert(conn, table, df)
    log.info("%s: replaced %d rows for feed %s", table, len(df), feed_id)
    return len(df)


def fetch_osm_matches(engine: Engine, feed_ids: list[str]) -> Optional[pd.DataFrame]:
    """Read the Phase 3 ``stop_osm_matches`` rows for these feeds.

    Never raises: the table exists only after ``conflate-osm`` has run
    against the same database, so any failure (missing table, connection
    problem) degrades to ``None`` - the sidecar then reports
    ``coord_confidence = unavailable``.

    Args:
        engine: database engine.
        feed_ids: restrict to these feeds' stops.

    Returns:
        :data:`OSM_MATCH_COLUMNS` frame (possibly empty), or ``None`` when
        the data cannot be read.
    """
    sql = text(
        "SELECT stop_uid, osm_id, distance_m, match_confidence, needs_review "
        "FROM stop_osm_matches WHERE feed_id = ANY(:feeds)"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"feeds": feed_ids}).mappings().all()
    except SQLAlchemyError as exc:
        log.info(
            "stop_osm_matches unavailable (%s); coord_confidence = unavailable",
            type(exc).__name__,
        )
        return None
    return pd.DataFrame(rows, columns=OSM_MATCH_COLUMNS)


def replace_all_rows(engine: Engine, table: str, df: pd.DataFrame) -> int:
    """Idempotently replace the full contents of a run-scoped table.

    Used for ``route_connections`` and ``stop_equivalences`` (see module
    docstring for why).

    Args:
        engine: database engine.
        table: whitelisted table name.
        df: complete new contents.

    Returns:
        Number of rows inserted.

    Raises:
        ValueError: unknown table name.
        sqlalchemy.exc.SQLAlchemyError: on DB failure (transactional).
    """
    if table not in _TABLES:
        raise ValueError(f"unknown analysis table {table!r}")
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table}"))
        _insert(conn, table, df)
    log.info("%s: reloaded %d rows", table, len(df))
    return len(df)
