"""Live PostGIS integration test for conflate-osm (Parts B + C).

Runs only when ``GTFS_TEST_DATABASE_URL`` points at a PostGIS database.
Builds tiny synthetic OSM tables (SRID 4326), loads one stop via the
Phase 1 loader, and drives ``run_conflation`` end to end.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("GTFS_TEST_DATABASE_URL"),
    reason="GTFS_TEST_DATABASE_URL not set; skipping live conflation test",
)


def test_conflate_osm_live(valid_feed, tmp_path: Path) -> None:
    from sqlalchemy import text

    from gtfs_ingest.conflation.pipeline_day3 import ConflationConfig, run_conflation
    from gtfs_ingest.db import ensure_schema, load_feed_stops, make_engine
    from gtfs_ingest.normalizer import normalize_stops

    url = os.environ["GTFS_TEST_DATABASE_URL"]
    engine = make_engine(url)
    ensure_schema(engine)
    stops, _ = normalize_stops(valid_feed)
    load_feed_stops(engine, valid_feed.feed_id, stops)

    # Synthetic OSM layers around stop S1 (40.0, -75.0): a bus_stop node
    # ~11 m north and a west->east street ~11 m south.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS test_osm_point, test_osm_line"))
        conn.execute(text(
            "CREATE TABLE test_osm_point (osm_id bigint, highway text, railway text, "
            "public_transport text, name text, way geometry(Point, 4326))"
        ))
        conn.execute(text(
            "CREATE TABLE test_osm_line (osm_id bigint, highway text, oneway text, "
            "name text, way geometry(LineString, 4326))"
        ))
        conn.execute(text(
            "INSERT INTO test_osm_point VALUES "
            "(9001, 'bus_stop', NULL, NULL, 'Main St Stop', "
            " ST_SetSRID(ST_MakePoint(-75.0, 40.0001), 4326))"
        ))
        conn.execute(text(
            "INSERT INTO test_osm_line VALUES "
            "(9101, 'residential', NULL, 'Main St', "
            " ST_SetSRID(ST_MakeLine(ST_MakePoint(-75.001, 39.9999), "
            "                        ST_MakePoint(-74.999, 39.9999)), 4326))"
        ))

    report = run_conflation(
        ConflationConfig(
            database_url=url,
            report_dir=tmp_path,
            osm_line_table="test_osm_line",
            osm_point_table="test_osm_point",
            dry_run=False,
        )
    )
    assert report.stops_total >= 3
    assert report.matched >= 1                       # S1 matches the bus_stop
    assert report.side_distribution.get("left", 0) >= 1   # S1 north of the eastbound street

    with engine.connect() as conn:
        match = conn.execute(text(
            "SELECT osm_id, needs_review FROM stop_osm_matches WHERE stop_uid = :u"
        ), {"u": f"{valid_feed.feed_id}:S1"}).one()
        side = conn.execute(text(
            "SELECT osm_way_id, side FROM stop_side_of_street WHERE stop_uid = :u"
        ), {"u": f"{valid_feed.feed_id}:S1"}).one()
        conn.execute(text("DROP TABLE IF EXISTS test_osm_point, test_osm_line"))
    assert match.osm_id == 9001 and match.needs_review is False
    assert side.osm_way_id == 9101 and side.side == "left"
    engine.dispose()
