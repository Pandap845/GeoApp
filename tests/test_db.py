"""Database-layer tests.

Unit tests run without a database. The integration test at the bottom
exercises real PostGIS DDL + idempotent reload and only runs when
``GTFS_TEST_DATABASE_URL`` is set (e.g. in CI with a postgis container).
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from gtfs_ingest.db import _records, make_engine
from gtfs_ingest.normalizer import normalize_stops


def test_make_engine_upgrades_scheme_to_psycopg() -> None:
    engine = make_engine("postgresql://user:pw@localhost:5432/gtfs")
    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()


def test_make_engine_keeps_explicit_driver() -> None:
    engine = make_engine("postgresql+psycopg://user:pw@localhost:5432/gtfs")
    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()


def test_records_convert_missing_values_to_none(valid_feed) -> None:
    stops, _ = normalize_stops(valid_feed)
    records = _records(stops)

    assert len(records) == 3
    by_id = {r["stop_id"]: r for r in records}
    # S1 has no parent_station: must be None (SQL NULL), never NaN/pd.NA.
    assert by_id["S1"]["parent_station"] is None
    assert by_id["S3"]["parent_station"] == "S2"
    assert isinstance(by_id["S1"]["stop_lat"], float)
    assert isinstance(by_id["S1"]["location_type"], int)
    assert "geometry" not in by_id["S1"]


@pytest.mark.skipif(
    not os.environ.get("GTFS_TEST_DATABASE_URL"),
    reason="GTFS_TEST_DATABASE_URL not set; skipping live PostGIS test",
)
def test_live_load_is_idempotent(valid_feed) -> None:
    from sqlalchemy import text

    from gtfs_ingest.db import ensure_schema, load_feed_stops

    engine = make_engine(os.environ["GTFS_TEST_DATABASE_URL"])
    ensure_schema(engine)
    stops, _ = normalize_stops(valid_feed)

    first = load_feed_stops(engine, valid_feed.feed_id, stops)
    second = load_feed_stops(engine, valid_feed.feed_id, stops)  # re-run: same result
    assert first == second == 3

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM stops WHERE feed_id = :f"),
            {"f": valid_feed.feed_id},
        ).scalar_one()
        srid = conn.execute(
            text("SELECT DISTINCT ST_SRID(geom) FROM stops WHERE feed_id = :f"),
            {"f": valid_feed.feed_id},
        ).scalar_one()
    assert count == 3
    assert srid == 4326
    engine.dispose()


def test_headway_pk_migration_blocks_are_idempotent_and_complete() -> None:
    """The DO-blocks widen both headway PKs to include service_date."""
    from gtfs_ingest.analysis.storage import _DDL_STATEMENTS

    blocks = [s for s in _DDL_STATEMENTS if "DO $$" in s]
    assert len(blocks) == 2
    for name in ("route_stop_headways_pkey", "route_headways_pkey"):
        (block,) = [b for b in blocks if name in b]
        assert "service_date" in block  # both the guard and the new PK
        assert "ADD CONSTRAINT" in block and "DROP CONSTRAINT" in block
        assert "NOT EXISTS" in block  # idempotency guard
    # Fresh-database CREATEs already carry the widened PKs.
    creates = [
        s
        for s in _DDL_STATEMENTS
        if "CREATE TABLE IF NOT EXISTS route_stop_headways" in s
        or "CREATE TABLE IF NOT EXISTS route_headways" in s
    ]
    assert len(creates) == 2
    assert all("service_date, route_id" in s for s in creates)


@pytest.mark.skipif(
    not os.environ.get("GTFS_TEST_DATABASE_URL"),
    reason="GTFS_TEST_DATABASE_URL not set; skipping live PostGIS test",
)
def test_live_bulk_copy_nan_becomes_null() -> None:
    """COPY path writes SQL NULLs for NaN stats (route_stop_headways)."""
    from sqlalchemy import text

    from gtfs_ingest.analysis.storage import ensure_analysis_schema, replace_feed_rows

    engine = make_engine(os.environ["GTFS_TEST_DATABASE_URL"])
    ensure_analysis_schema(engine)
    frame = pd.DataFrame(
        {
            "feed_id": ["copytest"],
            "service_date": ["2026-01-06"],
            "route_id": ["R1"],
            "direction_id": [0],
            "stop_id": ["S1"],
            "time_window": ["am_peak"],
            "trip_count": [1],
            "mean_headway_s": [float("nan")],  # single departure: undefined
            "median_headway_s": [float("nan")],
            "min_headway_s": [float("nan")],
            "max_headway_s": [float("nan")],
        }
    )
    assert replace_feed_rows(engine, "route_stop_headways", "copytest", frame) == 1
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT trip_count, mean_headway_s FROM route_stop_headways "
                "WHERE feed_id = 'copytest'"
            )
        ).one()
    assert row.trip_count == 1
    assert row.mean_headway_s is None  # NaN arrived as NULL
    engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("GTFS_TEST_DATABASE_URL"),
    reason="GTFS_TEST_DATABASE_URL not set; skipping live PostGIS test",
)
def test_live_csv_and_gtfs_stops_coexist_under_colliding_ids(tmp_path, valid_feed) -> None:
    """stop_uid namespaces each feed, so identical stop_ids cannot collide."""
    from sqlalchemy import text

    from gtfs_ingest.db import ensure_schema, load_feed_stops

    from conftest import load_stops_csv_feed

    # The CSV reuses VALID_FEED's stop ids on purpose.
    csv_feed = load_stops_csv_feed(
        tmp_path,
        "extra_stops.csv",
        "id,name,latitude,longitude\nS1,Other Network S1,20.67,-103.34\n",
    )
    engine = make_engine(os.environ["GTFS_TEST_DATABASE_URL"])
    ensure_schema(engine)

    gtfs_stops, _ = normalize_stops(valid_feed)
    csv_stops, _ = normalize_stops(csv_feed)
    load_feed_stops(engine, valid_feed.feed_id, gtfs_stops)
    load_feed_stops(engine, csv_feed.feed_id, csv_stops)

    with engine.begin() as conn:
        uids = {
            row[0]
            for row in conn.execute(
                text("SELECT stop_uid FROM stops WHERE feed_id IN (:a, :b)"),
                {"a": valid_feed.feed_id, "b": csv_feed.feed_id},
            )
        }
    assert f"{valid_feed.feed_id}:S1" in uids
    assert f"{csv_feed.feed_id}:S1" in uids

    # Reloading one feed must leave the other's rows untouched.
    load_feed_stops(engine, csv_feed.feed_id, csv_stops)
    with engine.begin() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM stops WHERE feed_id = :f"),
            {"f": valid_feed.feed_id},
        ).scalar_one()
    assert remaining == 3
