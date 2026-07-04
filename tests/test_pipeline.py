"""End-to-end dry-run tests: mixed good/broken feeds, reports, CLI exit codes."""

from __future__ import annotations

import json
from pathlib import Path

from gtfs_ingest import cli
from gtfs_ingest.report import JSON_NAME, MARKDOWN_NAME

from conftest import VALID_FEED, feed_files, write_feed_dir, write_feed_zip


def make_mixed_feeds_dir(tmp_path: Path) -> Path:
    """A collection dir: one valid zip, one broken dir, one corrupt zip."""
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    write_feed_zip(feeds, "good_city.zip", VALID_FEED)
    write_feed_dir(feeds, "broken_city", feed_files(omit=("stop_times.txt", "routes.txt")))
    (feeds / "corrupt.zip").write_bytes(b"not really a zip")
    return feeds


def test_dry_run_survives_broken_feeds_and_writes_reports(tmp_path: Path) -> None:
    feeds = make_mixed_feeds_dir(tmp_path)
    out = tmp_path / "out"

    exit_code = cli.main(["--feeds", str(feeds), "--report", str(out), "--dry-run"])

    assert exit_code == 0
    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    assert report["dry_run"] is True

    by_id = {f["feed_id"]: f for f in report["feeds"]}
    assert set(by_id) == {"good_city", "broken_city", "corrupt"}

    assert by_id["good_city"]["status"] == "loaded"
    assert by_id["good_city"]["stops_normalized"] == 3
    assert not [f for f in by_id["good_city"]["findings"] if f["severity"] == "ERROR"]

    broken_codes = {f["code"] for f in by_id["broken_city"]["findings"]}
    assert "missing_required_file" in broken_codes

    assert by_id["corrupt"]["status"] == "failed"
    assert {f["code"] for f in by_id["corrupt"]["findings"]} >= {"feed_unreadable"}

    # No DB writes in dry-run mode.
    assert all(f["stops_loaded"] == 0 for f in report["feeds"])

    md = (out / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert "## Optional file coverage" in md
    assert "good_city" in md and "broken_city" in md


def test_fail_on_error_exit_code(tmp_path: Path) -> None:
    feeds = make_mixed_feeds_dir(tmp_path)
    out = tmp_path / "out"
    exit_code = cli.main(
        ["--feeds", str(feeds), "--report", str(out), "--dry-run", "--fail-on-error"]
    )
    assert exit_code == 2


def test_no_feeds_found_is_exit_1(tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    exit_code = cli.main(
        ["--feeds", str(empty), "--report", str(tmp_path / "out"), "--dry-run"]
    )
    assert exit_code == 1


def test_missing_db_url_without_dry_run_is_exit_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GTFS_DATABASE_URL", raising=False)
    # Ensure no .env from the repo is picked up.
    monkeypatch.chdir(tmp_path)
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    write_feed_zip(feeds, "good_city.zip", VALID_FEED)

    exit_code = cli.main(["--feeds", str(feeds), "--report", str(tmp_path / "out")])
    assert exit_code == 1
