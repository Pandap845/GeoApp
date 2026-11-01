"""PostGIS stops table: engine, schema, and delete-and-reload of one feed's stops."""

from __future__ import annotations

import logging
import time

import pandas as pd
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .bulk import copy_rows

log = logging.getLogger(__name__)

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
    # The proximity query compares geom::geography; without this index it
    # checks every pair of stops (minutes instead of seconds).
    "CREATE INDEX IF NOT EXISTS ix_stops_geog ON stops USING GIST ((geom::geography))",
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

_CHUNK_SIZE = 1000

#: Columns copied into the temp table; geom is built in SQL.
_STOP_COLUMNS = [
    "stop_uid",
    "stop_num_id",
    "feed_id",
    "stop_id",
    "stop_name",
    "stop_lat",
    "stop_lon",
    "location_type",
    "parent_station",
]

_TEMP_TABLE_SQL = text(
    """
    CREATE TEMP TABLE tmp_stops (
        stop_uid       text,
        stop_num_id    bigint,
        feed_id        text,
        stop_id        text,
        stop_name      text,
        stop_lat       double precision,
        stop_lon       double precision,
        location_type  integer,
        parent_station text
    ) ON COMMIT DROP
    """
)

_INSERT_FROM_TEMP_SQL = text(
    """
    INSERT INTO stops (
        stop_uid, stop_num_id, feed_id, stop_id, stop_name,
        stop_lat, stop_lon, location_type, parent_station, geom
    )
    SELECT stop_uid, stop_num_id, feed_id, stop_id, stop_name,
           stop_lat, stop_lon, location_type, parent_station,
           ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326)
    FROM tmp_stops
    """
)


def make_engine(db_url: str) -> Engine:
    """SQLAlchemy engine on psycopg v3; a plain postgresql:// URL is upgraded."""
    url = make_url(db_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def ensure_schema(engine: Engine) -> None:
    """Enable PostGIS if possible and create the stops table.

    Raises:
        RuntimeError: PostGIS is not available.
    """
    # Own transaction: CREATE EXTENSION may fail without superuser rights.
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    except SQLAlchemyError as exc:
        log.warning(
            "Could not create postgis extension (may already exist / need superuser): %s", exc
        )

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
    """Stops as dicts without geometry, NaN turned into None."""
    df = stops.drop(columns="geometry", errors="ignore").copy()
    df = df.astype(object).where(pd.notna(df), None)
    return df.to_dict("records")


def load_feed_stops(engine: Engine, feed_id: str, stops: pd.DataFrame) -> int:
    """Replace this feed's rows in the stops table (one transaction) and return the count."""
    started = time.perf_counter()
    frame = stops.drop(columns="geometry", errors="ignore")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM stops WHERE feed_id = :feed_id"), {"feed_id": feed_id})
        if len(frame):
            try:
                # COPY into a temp table, then build geom in one INSERT (savepoint for fallback).
                with conn.begin_nested():
                    conn.execute(_TEMP_TABLE_SQL)
                    copy_rows(conn, "tmp_stops", frame[_STOP_COLUMNS])
                    conn.execute(_INSERT_FROM_TEMP_SQL)
            except Exception as exc:  # noqa: BLE001 - performance path, never a gate
                log.warning(
                    "stops: COPY load failed (%s: %s); falling back to executemany",
                    type(exc).__name__,
                    exc,
                )
                records = _records(stops)
                for start in range(0, len(records), _CHUNK_SIZE):
                    conn.execute(_INSERT_SQL, records[start : start + _CHUNK_SIZE])
    log.info(
        "Feed %s: loaded %d stops into PostGIS in %.1fs",
        feed_id,
        len(frame),
        time.perf_counter() - started,
    )
    return len(frame)
