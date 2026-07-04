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
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

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
        PRIMARY KEY (feed_id, route_id, direction_id, stop_id, time_window)
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
        PRIMARY KEY (feed_id, route_id, direction_id, time_window)
    )
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
    "CREATE INDEX IF NOT EXISTS ix_rsh_feed ON route_stop_headways (feed_id)",
    "CREATE INDEX IF NOT EXISTS ix_rh_feed ON route_headways (feed_id)",
)

#: Whitelist: table name -> has a feed_id column usable for per-feed replace.
_TABLES: dict[str, bool] = {
    "route_stop_headways": True,
    "route_headways": True,
    "route_connections": False,
}

_CHUNK_SIZE = 1000


def ensure_analysis_schema(engine: Engine) -> None:
    """Create the three analysis tables and their indexes (idempotent).

    Args:
        engine: engine from Phase 1 ``db.make_engine``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: on connection/DDL failure
            (run-level problem - every feed would fail identically).
    """
    with engine.begin() as conn:
        for stmt in _DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info("Analysis schema ensured (route_stop_headways, route_headways, route_connections)")


def _to_records(df: pd.DataFrame) -> list[dict]:
    """NaN/NA -> None so psycopg writes SQL NULLs."""
    cleaned = df.astype(object).where(pd.notna(df), None)
    return cleaned.to_dict("records")


def _insert(conn, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    cols = list(df.columns)
    sql = text(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    )
    records = _to_records(df)
    for start in range(0, len(records), _CHUNK_SIZE):
        conn.execute(sql, records[start : start + _CHUNK_SIZE])


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


def replace_all_rows(engine: Engine, table: str, df: pd.DataFrame) -> int:
    """Idempotently replace the full contents of a run-scoped table.

    Used for ``route_connections`` (see module docstring for why).

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
