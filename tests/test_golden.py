"""Golden-file test: a fixed two-feed run must reproduce the committed CSVs.

Any change to ordering, rounding or formatting fails here. To accept a change
on purpose, rerun with GTFS_UPDATE_GOLDEN=1 and review the diff in tests/golden/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gtfs_ingest import cli

from conftest import write_feed_dir
from test_connectivity import ANALYSIS_FEED, OTHER_FEED

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_FILES = (
    "near_stations.csv",
    "near_stations_quality.csv",
    "stop_equivalences.csv",
    "station_network_nodes.csv",
    "station_network_edges.csv",
    "route_headways.csv",
)


def _text(path: Path) -> str:
    # Line endings depend on the OS and on git's autocrlf; compare content only.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("golden")
    feeds = base / "feeds"
    write_feed_dir(feeds, "metro", ANALYSIS_FEED)
    write_feed_dir(feeds, "other", OTHER_FEED)
    out = base / "out"
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(feeds),
                "--report",
                str(out),
                "--service-date",
                "2026-01-06",
                "--analysis-date",
                "2026-01-06",
                "--dry-run",
            ]
        )
        == 0
    )
    return out


@pytest.mark.parametrize("name", GOLDEN_FILES)
def test_output_matches_golden_file(run_dir: Path, name: str) -> None:
    produced = _text(run_dir / name)
    golden = GOLDEN_DIR / name
    if os.environ.get("GTFS_UPDATE_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden.write_text(produced, encoding="utf-8", newline="\n")
    assert golden.is_file(), f"missing {golden}; run with GTFS_UPDATE_GOLDEN=1"
    assert produced == _text(golden)
