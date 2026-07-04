"""Tests for feed discovery, feed_id derivation, and robust loading."""

from __future__ import annotations

from pathlib import Path

from gtfs_ingest.loader import derive_feed_id, discover_feeds, load_feed

from conftest import VALID_FEED, write_feed_dir, write_feed_zip


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
    # Malformed CSV (unclosed quote): pandas raises ParserError on read.
    # (A zero-byte file is different: partridge returns an empty DataFrame,
    # which the validators flag as empty_required_file instead.)
    files = dict(VALID_FEED, **{"stops.txt": 'stop_id,stop_name\n"S1,unclosed quote\nS2,ok\n'})
    path = write_feed_dir(tmp_path, "metro", files)
    (source,) = discover_feeds([path])
    loaded = load_feed(source)

    assert loaded.table("stops.txt") is None
    assert "stops.txt" in loaded.table_errors
