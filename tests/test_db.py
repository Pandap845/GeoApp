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
