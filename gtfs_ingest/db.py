"""
GTFS ingest database schema and load functions.
Basic PostGIS schema for normalized stops, plus idempotent load of one feed's stops.
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)

#: Table + index DDL. IF NOT EXISTS keeps schema creation idempotent.
_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS stops (
        stop_uid       text PRIMARY KEY,
        stop_num_id    bigint NOT NULL,
        feed_id        text NOT NULL,
        stop_id        text NOT NULL,
        stop_name      text,
        stop_lat       double precision NOT NULL,
        stop_lon       double precision NOT NULL,
        location_type  integer NOT NULL DEFAULT 0,
        parent_station text,
        geom           geometry(Point, 4326) NOT NULL,
        loaded_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_stops_geom ON stops USING GIST (geom)",
    "CREATE INDEX IF NOT EXISTS ix_stops_feed_id ON stops (feed_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_stops_num_id ON stops (stop_num_id)",
)

_INSERT_SQL = text(
    """
    INSERT INTO stops (
        stop_uid, stop_num_id, feed_id, stop_id, stop_name,
        stop_lat, stop_lon, location_type, parent_station, geom
    )
    VALUES (
        :stop_uid, :stop_num_id, :feed_id, :stop_id, :stop_name,
        :stop_lat, :stop_lon, :location_type, :parent_station,
        ST_SetSRID(ST_MakePoint(:stop_lon, :stop_lat), 4326)
    )
    """
)

#: Rows per executemany batch (keeps memory bounded on huge feeds).
_CHUNK_SIZE = 1000


def make_engine(db_url: str) -> Engine:
    """Create a SQLAlchemy 2.x engine bound to the psycopg (v3) driver.

    Args:
        db_url: SQLAlchemy URL. A bare ``postgresql://`` scheme is upgraded
            to ``postgresql+psycopg://`` so psycopg v3 is always used.

    Returns:
        A lazily-connecting engine (no connection is attempted here).

    Raises:
        sqlalchemy.exc.ArgumentError: if the URL is not parseable.
    """
    url = make_url(db_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def ensure_schema(engine: Engine) -> None:
    """Create the PostGIS extension (best effort) and the stops table/indexes.

    Args:
        engine: engine from :func:`make_engine`.

    Raises:
        RuntimeError: if PostGIS is not installed and cannot be enabled
            (the geometry column is unusable without it).
        sqlalchemy.exc.SQLAlchemyError: on connection/DDL failure.
    """
    # Separate transaction: CREATE EXTENSION needs elevated rights and its
    # failure would poison the transaction the DDL runs in.
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    except SQLAlchemyError as exc:
        log.warning("Could not create postgis extension (may already exist / need superuser): %s", exc)

    with engine.begin() as conn:
        try:
            conn.execute(text("SELECT PostGIS_Version()"))
        except SQLAlchemyError as exc:
            raise RuntimeError(
                "PostGIS is not available in the target database; install/enable "
                "the postgis extension before running the pipeline"
            ) from exc

    with engine.begin() as conn:
        for stmt in _DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info("Database schema ensured (stops table + GiST index)")


def _records(stops: pd.DataFrame) -> list[dict]:
    """Convert the normalized frame to psycopg-friendly dicts (NaN/NA -> None)."""
    df = stops.drop(columns="geometry", errors="ignore").copy()
    df = df.astype(object).where(pd.notna(df), None)
    return df.to_dict("records")


def load_feed_stops(engine: Engine, feed_id: str, stops: pd.DataFrame) -> int:
    """Idempotently (re)load one feed's normalized stops.

    Deletes the feed's existing rows and inserts the new set in a single
    transaction (see module docstring for why delete-and-reload was chosen).

    Args:
        engine: engine from :func:`make_engine`; schema must already exist.
        feed_id: provenance id whose rows are replaced.
        stops: normalized stops frame from ``normalizer.normalize_stops``.
            An empty frame simply clears the feed's rows.

    Returns:
        Number of rows inserted.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: on database failure; the transaction
            rolls back, leaving the previously loaded data intact. The
            pipeline catches this per feed and records it as a finding.
    """
    records = _records(stops)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM stops WHERE feed_id = :feed_id"), {"feed_id": feed_id})
        for start in range(0, len(records), _CHUNK_SIZE):
            conn.execute(_INSERT_SQL, records[start : start + _CHUNK_SIZE])
    log.info("Feed %s: loaded %d stops into PostGIS", feed_id, len(records))
    return len(records)
