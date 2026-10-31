"""Phase 2 tables: schema and loaders.

Headway tables are replaced per feed. route_connections and stop_equivalences
are reloaded in full each run because their rows can span feeds.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from ..bulk import bulk_insert

log = logging.getLogger(__name__)

#: stop_osm_matches columns read for the quality sidecar.
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
    # Older databases: add service_date to the headway primary keys.
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
    # Older databases lack match_kind.
    "ALTER TABLE stop_equivalences ADD COLUMN IF NOT EXISTS match_kind text",
    "CREATE INDEX IF NOT EXISTS ix_rsh_feed ON route_stop_headways (feed_id)",
    "CREATE INDEX IF NOT EXISTS ix_rh_feed ON route_headways (feed_id)",
    "CREATE INDEX IF NOT EXISTS ix_stop_equiv_group ON stop_equivalences (group_id)",
)

#: Allowed tables -> whether rows can be replaced per feed.
_TABLES: dict[str, bool] = {
    "route_stop_headways": True,
    "route_headways": True,
    "route_connections": False,
    "stop_equivalences": False,
}

def ensure_analysis_schema(engine: Engine) -> None:
    """Create the four analysis tables and their indexes if missing."""
    with engine.begin() as conn:
        for stmt in _DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info(
        "Analysis schema ensured (route_stop_headways, route_headways, "
        "route_connections, stop_equivalences)"
    )


def _insert(conn, table: str, df: pd.DataFrame) -> None:
    """Bulk insert (COPY with executemany fallback)."""
    bulk_insert(conn, table, df)


def replace_feed_rows(engine: Engine, table: str, feed_id: str, df: pd.DataFrame) -> int:
    """Replace one feed's rows in a headway table; return the count.

    Raises:
        ValueError: *table* doesn't support per-feed replacement.
    """
    if not _TABLES.get(table):
        raise ValueError(f"table {table!r} does not support per-feed replacement")
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table} WHERE feed_id = :feed_id"), {"feed_id": feed_id})
        _insert(conn, table, df)
    log.info("%s: replaced %d rows for feed %s", table, len(df), feed_id)
    return len(df)


def fetch_osm_matches(engine: Engine, feed_ids: list[str]) -> Optional[pd.DataFrame]:
    """OSM match rows for these feeds, or None if the table can't be read.

    The table exists only after conflate-osm has run; None makes the sidecar
    report coord_confidence = unavailable.
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
    """Replace the whole contents of an analysis table; return the count.

    Raises:
        ValueError: unknown table name.
    """
    if table not in _TABLES:
        raise ValueError(f"unknown analysis table {table!r}")
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table}"))
        _insert(conn, table, df)
    log.info("%s: reloaded %d rows", table, len(df))
    return len(df)
