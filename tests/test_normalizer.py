"""Tests for normalization into the common stops schema."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from gtfs_ingest.models import STOP_COLUMNS
from gtfs_ingest.normalizer import CRS, normalize_stops

from conftest import feed_files, load_dir_feed


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_valid_feed_normalizes_fully(valid_feed) -> None:
    stops, findings = normalize_stops(valid_feed)

    assert len(stops) == 3
    assert list(stops.columns) == list(STOP_COLUMNS) + ["geometry"]
    assert stops.crs is not None and stops.crs.to_string() == CRS
    assert findings == []

    row = stops.set_index("stop_id").loc["S1"]
    assert row["stop_uid"] == "metro_valid:S1"
    assert row["feed_id"] == "metro_valid"
    assert row["stop_name"] == "Main St"  # whitespace stripped
    assert row["stop_lat"] == 40.0 and row["stop_lon"] == -75.0
    assert row["location_type"] == 0
    assert pd.isna(row["parent_station"])  # blank in source -> standardized NULL

    s3 = stops.set_index("stop_id").loc["S3"]
    assert s3["parent_station"] == "S2"
    assert s3["geometry"].x == -75.0099 and s3["geometry"].y == 40.0101


def test_bad_coordinates_dropped_with_finding(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "S1,Ok,40.0,-75.0\n"
                "S2,Bad lat,95.0,-75.0\n"
                "S3,Bad lon,40.0,not_a_number\n"
                "S4,Missing,,\n"
            )
        }
    )
    stops, findings = normalize_stops(load_dir_feed(tmp_path, "bad_coords", files))

    assert list(stops["stop_id"]) == ["S1"]
    (finding,) = [f for f in findings if f.code == "dropped_invalid_coordinates"]
    assert finding.count == 3


def test_duplicate_stop_ids_keep_first(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "S1,First wins,40.0,-75.0\n"
                "S1,Second dropped,41.0,-76.0\n"
            )
        }
    )
    stops, findings = normalize_stops(load_dir_feed(tmp_path, "dupes", files))

    assert len(stops) == 1
    assert stops.iloc[0]["stop_name"] == "First wins"
    assert "dropped_duplicate_stop_id" in codes(findings)
    # stop_uid stays unique - the DB primary key can never conflict.
    assert stops["stop_uid"].is_unique


def test_blank_stop_id_dropped(tmp_path: Path) -> None:
    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                ",No id,40.0,-75.0\n"
                "S1,Ok,40.0,-75.0\n"
            )
        }
    )
    stops, findings = normalize_stops(load_dir_feed(tmp_path, "blank_id", files))
    assert list(stops["stop_id"]) == ["S1"]
    assert "dropped_null_stop_id" in codes(findings)


def test_missing_stops_file_yields_empty_frame(tmp_path: Path) -> None:
    loaded = load_dir_feed(tmp_path, "no_stops", feed_files(omit=("stops.txt",)))
    stops, findings = normalize_stops(loaded)
    assert stops.empty
    assert "no_stops_normalized" in codes(findings)


def test_missing_optional_columns_are_filled(tmp_path: Path) -> None:
    files = feed_files(
        replace={"stops.txt": "stop_id,stop_lat,stop_lon\nS1,40.0,-75.0\nS3,40.01,-75.01\n"}
    )
    stops, _ = normalize_stops(load_dir_feed(tmp_path, "minimal_cols", files))
    assert len(stops) == 2
    assert set(STOP_COLUMNS) <= set(stops.columns)
    assert stops["location_type"].tolist() == [0, 0]
