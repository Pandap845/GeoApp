"""Tests for feed discovery, feed_id derivation, and robust loading."""

from __future__ import annotations

from pathlib import Path

from gtfs_ingest.loader import derive_feed_id, discover_feeds, load_feed

from conftest import (
    VALID_FEED,
    load_stops_csv_feed,
    write_feed_dir,
    write_feed_zip,
    write_stops_csv,
)


def test_discovers_zips_and_dirs_in_collection_dir(tmp_path: Path) -> None:
    write_feed_zip(tmp_path, "cityA.zip", VALID_FEED)
    write_feed_dir(tmp_path, "cityB", VALID_FEED)
    (tmp_path / "notes.txt").write_text("not a feed", encoding="utf-8")
    (tmp_path / "empty_dir").mkdir()

    sources = discover_feeds([tmp_path])

    assert [s.feed_id for s in sources] == ["citya", "cityb"]
    assert [s.kind for s in sources] == ["zip", "dir"]


def test_explicit_paths_and_dedup(tmp_path: Path) -> None:
    zip_path = write_feed_zip(tmp_path, "cityA.zip", VALID_FEED)
    sources = discover_feeds([zip_path, zip_path, tmp_path])
    assert len(sources) == 1


def test_feed_id_is_deterministic_slug() -> None:
    assert derive_feed_id(Path("My Feed (2026-Q1).zip")) == "my_feed_2026_q1"
    assert derive_feed_id(Path("MBTA.zip")) == "mbta"
    assert derive_feed_id(Path("__.zip")) == "feed"


def test_feed_id_collision_gets_stable_hash_suffix(tmp_path: Path) -> None:
    a = write_feed_dir(tmp_path / "one", "city", VALID_FEED)
    b = write_feed_dir(tmp_path / "two", "city", VALID_FEED)
    sources = discover_feeds([a, b])
    ids = [s.feed_id for s in sources]
    assert ids[0] == "city"
    assert ids[1].startswith("city_") and len(ids[1]) == len("city_") + 8
    # Deterministic: rediscovery yields the same ids.
    assert [s.feed_id for s in discover_feeds([a, b])] == ids


def test_load_zip_feed(tmp_path: Path) -> None:
    zip_path = write_feed_zip(tmp_path, "metro.zip", VALID_FEED)
    (source,) = discover_feeds([zip_path])
    loaded = load_feed(source)

    assert loaded.load_error is None
    assert "stops.txt" in loaded.files_present
    assert loaded.row_counts()["stops.txt"] == 3
    assert loaded.row_counts()["stop_times.txt"] == 2


def test_corrupt_zip_yields_load_error_not_exception(tmp_path: Path) -> None:
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"this is not a zip archive")
    (source,) = discover_feeds([bad])
    loaded = load_feed(source)

    assert loaded.feed is None
    assert loaded.load_error is not None
    assert loaded.row_counts() == {}


def test_unreadable_table_is_recorded_not_raised(tmp_path: Path) -> None:
    # Unclosed quote: pandas raises ParserError (an empty file is a different case).
    files = dict(VALID_FEED, **{"stops.txt": 'stop_id,stop_name\n"S1,unclosed quote\nS2,ok\n'})
    path = write_feed_dir(tmp_path, "metro", files)
    (source,) = discover_feeds([path])
    loaded = load_feed(source)

    assert loaded.table("stops.txt") is None
    assert "stops.txt" in loaded.table_errors


# Stops-only CSV sources
def test_discovers_explicit_csv_as_stops_only_feed(tmp_path: Path) -> None:
    path = write_stops_csv(tmp_path, "extra_stops.csv")

    (source,) = discover_feeds([path])

    assert source.kind == "csv"
    assert source.is_stops_only is True
    assert source.feed_id == "extra_stops"  # extension dropped, like a .zip


def test_csv_in_collection_dir_is_not_discovered(tmp_path: Path) -> None:
    """Globbing *.csv would let a previous run's near_stations.csv be re-ingested."""
    write_feed_dir(tmp_path, "cityB", VALID_FEED)
    write_stops_csv(tmp_path, "near_stations.csv")

    sources = discover_feeds([tmp_path])

    assert [s.feed_id for s in sources] == ["cityb"]


def test_directory_named_like_a_zip_is_still_a_dir(tmp_path: Path) -> None:
    """The kind is decided by layout, not by suffix."""
    write_feed_dir(tmp_path, "cityA.zip", VALID_FEED)

    (source,) = discover_feeds([tmp_path / "cityA.zip"])

    assert source.kind == "dir"


def test_load_csv_seeds_stops_table_without_partridge(tmp_path: Path) -> None:
    loaded = load_stops_csv_feed(tmp_path, "extra_stops.csv")

    assert loaded.feed is None  # no partridge object in play
    assert loaded.load_error is None
    assert loaded.files_present == {"stops.txt"}
    stops = loaded.table("stops.txt")
    assert stops is not None
    assert list(stops.columns) == ["stop_id", "stop_name", "stop_lat", "stop_lon"]
    assert loaded.row_counts() == {"stops.txt": 2}


def test_csv_with_bad_header_is_load_error_not_exception(tmp_path: Path) -> None:
    loaded = load_stops_csv_feed(tmp_path, "bad.csv", "ID_PARADA,DESC,X,Y\n1,Centro,20.6,-103.3\n")

    assert loaded.load_error is not None
    assert loaded.table("stops.txt") is None
    assert [f.code for f in loaded.load_findings] == ["unresolved_csv_header"]
