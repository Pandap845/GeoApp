"""Feed discovery and partridge-based loading.

Discovery accepts any mix of:
  * a path to a single ``.zip`` GTFS feed,
  * a path to a single unzipped feed directory (detected by the presence of
    ``agency.txt`` or ``stops.txt``),
  * a path to a *collection* directory, whose ``*.zip`` files and feed-like
    subdirectories are each treated as one feed.

feed_id derivation (deterministic, documented contract):
  1. Take the zip filename without extension, or the directory name.
  2. Lowercase it and replace every run of non ``[a-z0-9]`` characters with
     a single underscore; strip leading/trailing underscores.
  3. If the result is empty, use ``"feed"``.
  4. If the slug collides with an already-assigned feed_id in this run, a
     suffix ``_<first 8 hex chars of sha256(absolute path)>`` is appended,
     which is stable across runs on the same machine layout.

"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

import partridge as ptg

from .models import FeedSource, LoadedFeed

log = logging.getLogger(__name__)

#: A directory is considered a single GTFS feed if it contains any of these.
_FEED_MARKERS: tuple[str, ...] = ("agency.txt", "stops.txt")


def _looks_like_feed_dir(path: Path) -> bool:
    """True if *path* is a directory that itself looks like one GTFS feed."""
    return path.is_dir() and any((path / m).is_file() for m in _FEED_MARKERS)


def derive_feed_id(path: Path, taken: Iterable[str] = ()) -> str:
    """Derive the deterministic feed_id for a feed path.

    Args:
        path: path to the feed zip or directory.
        taken: feed_ids already assigned in this run, for collision handling.

    Returns:
        A slug as documented in the module docstring. Same path (and same
        set of sibling collisions) always yields the same id.
    """
    stem = path.stem if path.suffix.lower() == ".zip" else path.name
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "feed"
    if slug in set(taken):
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}_{digest}"
    return slug


def discover_feeds(paths: Sequence[Path]) -> list[FeedSource]:
    """Find all feeds under the given input paths.

    Args:
        paths: feed files/directories or collection directories (see module
            docstring for the accepted layouts).

    Returns:
        One :class:`FeedSource` per discovered feed, in deterministic order
        (inputs in given order; within a collection directory, sorted by
        name). Paths that don't exist or don't look like feeds are logged
        and skipped - discovery never raises for bad inputs.
    """
    candidates: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() == ".zip":
            candidates.append(p)
        elif _looks_like_feed_dir(p):
            candidates.append(p)
        elif p.is_dir():
            found = sorted(p.glob("*.zip")) + sorted(
                c for c in p.iterdir() if _looks_like_feed_dir(c)
            )
            if not found:
                log.warning("No feeds found under directory %s", p)
            candidates.extend(found)
        else:
            log.warning("Skipping %s: not a .zip file or feed directory", p)

    sources: list[FeedSource] = []
    seen_paths: set[Path] = set()
    taken_ids: set[str] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        feed_id = derive_feed_id(path, taken_ids)
        taken_ids.add(feed_id)
        kind = "zip" if path.suffix.lower() == ".zip" else "dir"
        sources.append(FeedSource(feed_id=feed_id, path=resolved, kind=kind))
        log.info("Discovered feed %s (%s: %s)", feed_id, kind, resolved)
    return sources


def list_feed_files(source: FeedSource) -> set[str]:
    """List the ``.txt`` member basenames of a feed.

    For zips, basenames of all ``.txt`` entries are collected (so a zip whose
    files sit inside a single nested folder still reports its files; whether
    partridge can *read* such a zip is surfaced separately as table errors).
    For directories, only top-level ``*.txt`` files count.

    Args:
        source: the feed to inspect.

    Returns:
        Set of filenames such as ``{"agency.txt", "stops.txt", ...}``.

    Raises:
        OSError / zipfile.BadZipFile: if the container itself is unreadable.
        (``load_feed`` catches these and turns them into a load_error.)
    """
    if source.kind == "zip":
        with zipfile.ZipFile(source.path) as zf:
            return {
                PurePosixPath(name).name
                for name in zf.namelist()
                if name.lower().endswith(".txt") and not name.endswith("/")
            }
    return {f.name for f in source.path.glob("*.txt") if f.is_file()}


def load_feed(source: FeedSource) -> LoadedFeed:
    """Open a feed with partridge, never raising on broken input.

    Args:
        source: the feed to load.

    Returns:
        A :class:`LoadedFeed`. If the container (zip/directory) cannot be
        opened at all, ``feed`` is ``None`` and ``load_error`` explains why;
        the pipeline then records the failure and moves on. Per-table parse
        problems are *not* detected here - they surface lazily through
        ``LoadedFeed.table()``.
    """
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
