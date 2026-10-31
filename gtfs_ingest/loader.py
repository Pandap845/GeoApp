"""Find feeds and open them.

Accepts .zip feeds, unzipped feed folders, folders of feeds, and stops-only
.csv files. A .csv must be named explicitly; folders are never scanned for
them, so an old near_stations.csv can't sneak back in as a second network.

feed_id = file or folder name, lowercased, non-alphanumerics -> "_".
On a clash, "_" + the first 8 hex chars of sha256(path) is appended.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

import partridge as ptg

from . import stops_csv
from .models import FeedSource, LoadedFeed

log = logging.getLogger(__name__)

#: A folder holding any of these files is one feed.
_FEED_MARKERS: tuple[str, ...] = ("agency.txt", "stops.txt")


def _looks_like_feed_dir(path: Path) -> bool:
    """True if *path* is a directory that itself looks like one GTFS feed."""
    return path.is_dir() and any((path / m).is_file() for m in _FEED_MARKERS)


def derive_feed_id(path: Path, taken: Iterable[str] = ()) -> str:
    """Deterministic feed_id for *path*; *taken* holds ids already used in this run."""
    stem = path.stem if path.suffix.lower() in (".zip", ".csv") else path.name
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "feed"
    if slug in set(taken):
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}_{digest}"
    return slug


def discover_feeds(paths: Sequence[Path]) -> list[FeedSource]:
    """One FeedSource per feed, in input order (sorted inside folders). Bad paths are skipped."""
    # Decide the kind here: a folder named "foo.zip" is still a folder.
    candidates: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() == ".zip":
            candidates.append((p, "zip"))
        elif p.is_file() and p.suffix.lower() == ".csv":
            candidates.append((p, "csv"))
        elif _looks_like_feed_dir(p):
            candidates.append((p, "dir"))
        elif p.is_dir():
            # Folder of feeds: zips and feed folders only, never CSVs.
            found = sorted(p.glob("*.zip")) + sorted(
                c for c in p.iterdir() if _looks_like_feed_dir(c)
            )
            if not found:
                log.warning("No feeds found under directory %s", p)
            candidates.extend((c, "zip" if c.is_file() else "dir") for c in found)
        else:
            log.warning("Skipping %s: not a .zip/.csv file or feed directory", p)

    sources: list[FeedSource] = []
    seen_paths: set[Path] = set()
    taken_ids: set[str] = set()
    for path, kind in candidates:
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        feed_id = derive_feed_id(path, taken_ids)
        taken_ids.add(feed_id)
        sources.append(FeedSource(feed_id=feed_id, path=resolved, kind=kind))
        log.info("Discovered feed %s (%s: %s)", feed_id, kind, resolved)
    return sources


def list_feed_files(source: FeedSource) -> set[str]:
    """Names of the .txt files in a feed (zips: any depth; folders: top level only).

    Raises:
        OSError / zipfile.BadZipFile: the container can't be read.
    """
    if source.kind == "csv":
        return {"stops.txt"}  # a stops CSV is one stops.txt table
    if source.kind == "zip":
        with zipfile.ZipFile(source.path) as zf:
            return {
                PurePosixPath(name).name
                for name in zf.namelist()
                if name.lower().endswith(".txt") and not name.endswith("/")
            }
    return {f.name for f in source.path.glob("*.txt") if f.is_file()}


def load_feed(source: FeedSource) -> LoadedFeed:
    """Open a feed without raising; if the container can't be opened, load_error says why.

    Per-table problems show up later, when LoadedFeed.table() reads the table.
    """
    if source.kind == "csv":
        return _load_stops_csv(source)

    try:
        files_present = list_feed_files(source)
    except Exception as exc:  # noqa: BLE001 - bad zip, permissions, ...
        log.error("Feed %s: cannot list contents of %s: %s", source.feed_id, source.path, exc)
        return LoadedFeed(
            source=source,
            feed=None,
            files_present=set(),
            load_error=f"cannot read feed container: {type(exc).__name__}: {exc}",
        )

    try:
        feed = ptg.load_raw_feed(str(source.path))
    except Exception as exc:  # noqa: BLE001 - partridge failures become findings
        log.error("Feed %s: partridge failed to open %s: %s", source.feed_id, source.path, exc)
        return LoadedFeed(
            source=source,
            feed=None,
            files_present=files_present,
            load_error=f"partridge could not open feed: {type(exc).__name__}: {exc}",
        )

    log.info("Feed %s: opened with %d .txt files", source.feed_id, len(files_present))
    return LoadedFeed(source=source, feed=feed, files_present=files_present)


def _load_stops_csv(source: FeedSource) -> LoadedFeed:
    """Open a stops CSV as a feed whose only table is stops.txt.

    The frame is pre-seeded in _tables, so every consumer reads it like a real
    feed. A bad header sets load_error and adds a finding naming the column.
    """
    try:
        stops, findings = stops_csv.read_stops_csv(source.path)
    except Exception as exc:  # noqa: BLE001 - unreadable file is a finding, not a crash
        log.error("Feed %s: cannot read stops CSV %s: %s", source.feed_id, source.path, exc)
        return LoadedFeed(
            source=source,
            feed=None,
            files_present=set(),
            load_error=f"cannot read stops CSV: {type(exc).__name__}: {exc}",
        )

    if stops is None:
        log.error("Feed %s: unresolvable header in %s", source.feed_id, source.path)
        return LoadedFeed(
            source=source,
            feed=None,
            files_present=set(),
            load_error="stops CSV header could not be resolved",
            load_findings=findings,
        )

    log.info("Feed %s: opened stops CSV with %d row(s)", source.feed_id, len(stops))
    return LoadedFeed(
        source=source,
        feed=None,
        files_present={"stops.txt"},
        load_findings=findings,
        _tables={"stops.txt": stops},
    )
