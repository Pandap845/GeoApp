"""Shared fixtures: tiny synthetic GTFS feeds written to tmp_path.

``VALID_FEED`` is a complete, internally consistent minimal feed. Broken
variants are derived from it via :func:`feed_files` (omit files, replace
file contents) so each test states only its delta from the valid baseline.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from gtfs_ingest.loader import discover_feeds, load_feed
from gtfs_ingest.models import LoadedFeed

VALID_FEED: dict[str, str] = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "A1,Metro Transit,https://metro.example,America/New_York\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "S1, Main St ,40.0,-75.0,0,\n"
        "S2,Central Station,40.01,-75.01,1,\n"
        "S3,Central Platform,40.0101,-75.0099,0,S2\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,A1,10,Main Line,3\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\n"
        "T1,08:10:00,08:10:00,S3,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
    "feed_info.txt": (
        "feed_publisher_name,feed_publisher_url,feed_lang\n"
        "Metro Transit,https://metro.example,en\n"
    ),
}


def feed_files(
    omit: tuple[str, ...] = (),
    replace: dict[str, str] | None = None,
) -> dict[str, str]:
    """The valid feed minus *omit* files, with *replace* contents swapped in."""
    files = {k: v for k, v in VALID_FEED.items() if k not in omit}
    files.update(replace or {})
    return files


def write_feed_dir(base: Path, name: str, files: dict[str, str]) -> Path:
    """Write *files* as an unzipped feed directory under *base*."""
    feed_dir = base / name
    feed_dir.mkdir(parents=True)
    for filename, content in files.items():
        (feed_dir / filename).write_text(content, encoding="utf-8")
    return feed_dir


def write_feed_zip(base: Path, name: str, files: dict[str, str]) -> Path:
    """Write *files* as a .zip feed under *base*."""
    zip_path = base / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return zip_path


def load_dir_feed(base: Path, name: str, files: dict[str, str]) -> LoadedFeed:
    """Write a feed directory and load it through the real loader path."""
    path = write_feed_dir(base, name, files)
    (source,) = discover_feeds([path])
    return load_feed(source)


@pytest.fixture
def valid_feed(tmp_path: Path) -> LoadedFeed:
    """A fully valid, loaded feed (directory form)."""
    return load_dir_feed(tmp_path, "metro_valid", VALID_FEED)
