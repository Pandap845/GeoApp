"""Phase 3 tables: schema and loaders. Every table is fully reloaded each run."""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import Engine, text

from ..bulk import bulk_insert

log = logging.getLogger(__name__)

_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS interchanges (
        cluster_id          integer PRIMARY KEY,
        is_multimodal       boolean NOT NULL,
        stop_count          integer NOT NULL,
        physical_stop_count integer NOT NULL DEFAULT 0,
        modes               integer[] NOT NULL,
        mode_names          text[] NOT NULL,
        member_stop_uids    text[] NOT NULL,
        routes              text[] NOT NULL,
        feed_ids            text[] NOT NULL,
        centroid            geometry(Point, 4326) NOT NULL,
        hull                geometry(Geometry, 4326) NOT NULL,
        loaded_at           timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Adds the column to databases created before it existed.
    "ALTER TABLE interchanges ADD COLUMN IF NOT EXISTS physical_stop_count integer NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_interchanges_centroid ON interchanges USING GIST (centroid)",
    """
    CREATE TABLE IF NOT EXISTS stop_osm_matches (
        stop_uid         text PRIMARY KEY,
        feed_id          text NOT NULL,
        stop_id          text NOT NULL,
        osm_id           bigint,
        osm_kind         text,
        osm_name         text,
        distance_m       double precision,
        match_confidence double precision NOT NULL,
        needs_review     boolean NOT NULL,
        loaded_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stop_side_of_street (
        stop_uid                  text PRIMARY KEY,
        feed_id                   text NOT NULL,
        stop_id                   text NOT NULL,
        osm_way_id                bigint,
        highway                   text,
        oneway                    text,
        perpendicular_distance_m  double precision,
        side                      text,
        side_confidence           text NOT NULL,
        near_endpoint             boolean NOT NULL,
        loaded_at                 timestamptz NOT NULL DEFAULT now()
    )
    """,
)

_CHUNK_SIZE = 1000


def ensure_conflation_schema(engine: Engine) -> None:
    """Create the Phase 3 tables and indexes if missing."""
    with engine.begin() as conn:
        for stmt in _DDL_STATEMENTS:
            conn.execute(text(stmt))
    log.info("Conflation schema ensured (interchanges, stop_osm_matches, stop_side_of_street)")


def _to_records(df: pd.DataFrame) -> list[dict]:
    """Rows as dicts with NaN turned into None; list values pass through."""
    cleaned = df.copy()
    for col in cleaned.columns:
        cleaned[col] = [None if not isinstance(v, (list, tuple)) and pd.isna(v) else v
                        for v in cleaned[col]]
    return cleaned.to_dict("records")


def load_interchanges(engine: Engine, clusters: pd.DataFrame) -> int:
    """Replace the interchanges table with *clusters*; return the row count."""
    sql = text(
        """
        INSERT INTO interchanges (
            cluster_id, is_multimodal, stop_count, physical_stop_count,
            modes, mode_names,
            member_stop_uids, routes, feed_ids, centroid, hull
        ) VALUES (
            :cluster_id, :is_multimodal, :stop_count, :physical_stop_count,
            :modes, :mode_names,
            :member_stop_uids, :routes, :feed_ids,
            ST_GeomFromText(:centroid_wkt, 4326), ST_GeomFromText(:hull_wkt, 4326)
        )
        """
    )
    records = _to_records(clusters)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM interchanges"))
        for start in range(0, len(records), _CHUNK_SIZE):
            conn.execute(sql, records[start : start + _CHUNK_SIZE])
    log.info("interchanges: reloaded %d rows", len(records))
    return len(records)


def _reload_plain(engine: Engine, table: str, df: pd.DataFrame) -> int:
    """Delete every row of *table*, then bulk-insert *df*."""
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table}"))
        bulk_insert(conn, table, df)
    log.info("%s: reloaded %d rows", table, len(df))
    return len(df)


def load_osm_matches(engine: Engine, matches: pd.DataFrame) -> int:
    """Reload ``stop_osm_matches`` from the scored match frame."""
    return _reload_plain(engine, "stop_osm_matches", matches.drop(columns=["location_type"]))


def load_sides(engine: Engine, sides: pd.DataFrame) -> int:
    """Reload ``stop_side_of_street`` from the side frame."""
    return _reload_plain(engine, "stop_side_of_street", sides)
