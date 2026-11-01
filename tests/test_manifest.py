"""Tests for run_manifest.json (input hashes, secrets, determinism)."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from gtfs_ingest import cli, manifest
from gtfs_ingest.manifest import MANIFEST_NAME, build_manifest, clean_parameters, write_manifest

from conftest import feed_files, write_feed_zip


def test_input_hash_matches_hashlib(tmp_path: Path) -> None:
    data = tmp_path / "feed.zip"
    data.write_bytes(b"abc" * 1000)

    entry = build_manifest("ingest", {}, [data])

    (item,) = entry["inputs"]
    assert item["sha256"] == hashlib.sha256(b"abc" * 1000).hexdigest()
    assert item["bytes"] == 3000


def test_folder_hash_changes_with_content(tmp_path: Path) -> None:
    folder = tmp_path / "feed"
    folder.mkdir()
    (folder / "stops.txt").write_text("a", encoding="utf-8")
    before = build_manifest("ingest", {}, [folder])["inputs"][0]["sha256"]

    (folder / "stops.txt").write_text("b", encoding="utf-8")

    assert build_manifest("ingest", {}, [folder])["inputs"][0]["sha256"] != before


def test_secrets_and_output_paths_are_dropped() -> None:
    params = {
        "database_url": "postgresql://user:pw@host/db",
        "api_key": "SECRET",
        "nested": {"google_maps_api_key": "SECRET", "radius_m": 50.0},
        "report_dir": Path("out"),
        "mode_codes": frozenset({3, 0}),
        "service_date": date(2024, 1, 2),
    }

    cleaned = clean_parameters(params)

    assert "SECRET" not in json.dumps(cleaned) and "pw@" not in json.dumps(cleaned)
    assert cleaned == {
        "nested": {"radius_m": 50.0},
        "mode_codes": [0, 3],
        "service_date": "2024-01-02",
    }


def test_code_hash_ignores_line_endings(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_bytes(b"x = 1\r\n")
    monkeypatch.setattr(manifest, "__file__", str(package / "manifest.py"))
    windows = manifest.code_sha256()

    (package / "a.py").write_bytes(b"x = 1\n")

    assert manifest.code_sha256() == windows
    (package / "a.py").write_bytes(b"x = 2\n")
    assert manifest.code_sha256() != windows


def test_works_without_git(monkeypatch) -> None:
    monkeypatch.setattr(manifest, "_git", lambda *args: None)

    assert build_manifest("ingest", {})["git"] is None


def test_entries_of_several_commands_share_one_file(tmp_path: Path) -> None:
    write_manifest(tmp_path, build_manifest("ingest", {"a": 1}))
    write_manifest(tmp_path, build_manifest("analyze-frequency", {"b": 2}))

    data = json.loads((tmp_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(data) == {"ingest", "analyze-frequency"}


def _analyze(feeds: Path, out: Path) -> dict:
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
    return json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))


def test_two_equal_runs_write_equal_manifests(tmp_path: Path) -> None:
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    write_feed_zip(feeds, "city.zip", feed_files())

    first = _analyze(feeds, tmp_path / "run1")
    second = _analyze(feeds, tmp_path / "run2")

    assert first == second
    entry = first["analyze-frequency"]
    assert [i["name"] for i in entry["inputs"]] == ["city.zip"]
    assert entry["parameters"]["proximity_m"] == 150.0
    assert "report_dir" not in entry["parameters"]
