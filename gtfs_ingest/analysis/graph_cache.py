"""On-disk cache of the OSM walk graph (``--network-walk``).

Building the walk graph - fetching every walkable way inside the stops'
buffered hull from PostGIS and assembling the CSR arrays - dominates the
network-walk wall clock and produces the same result run after run. This
module persists the built arrays as a compressed ``.npz`` keyed by
everything that determines them:

* the request: sorted feed ids, hull buffer, OSM line table + geometry
  column, excluded highway values, and the resolved metric CRS;
* the database state, probed cheaply: ``count(*)`` + ``max(osm_id)`` of the
  line table (an osm2pgsql reimport changes both) and ``max(loaded_at)`` of
  the feeds' stops (a stop reload moves the hull).

Any component changing produces a different key, so staleness handles
itself for the common cases; ``--no-walk-cache`` (or deleting the cache
directory) is the escape hatch for in-place edits the probes cannot see.
A corrupt or unreadable cache file is treated as a miss, never an error.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

#: Bump on any change to the cached format or the graph-building algorithm.
CACHE_VERSION = 1

#: (node_xy, indptr, indices, weights) - build_walk_graph's output.
GraphArrays = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]

_ARRAY_NAMES = ("node_xy", "indptr", "indices", "weights")


def probe_state(engine: Engine, feed_ids: list[str], line_table: str) -> tuple:
    """Cheap DB-state probes that invalidate the cache when data changes.

    Args:
        engine: database engine.
        feed_ids: feeds whose stops define the hull.
        line_table: OSM line table name (trusted config, interpolated the
            same way as ``walk_network.fetch_walk_lines``).

    Returns:
        ``(line_count, max_osm_id, max_loaded_at_iso)`` - any change in the
        OSM import or a stop reload flips at least one component.
    """
    with engine.connect() as conn:
        count, max_id = conn.execute(
            text(f"SELECT count(*), max(osm_id) FROM {line_table}")  # noqa: S608
        ).one()
        loaded_at = conn.execute(
            text("SELECT max(loaded_at) FROM stops WHERE feed_id = ANY(:feeds)"),
            {"feeds": feed_ids},
        ).scalar()
    return (int(count), max_id, loaded_at.isoformat() if loaded_at else None)


def cache_key(
    feed_ids: list[str],
    buffer_m: float,
    line_table: str,
    geom_column: str,
    excluded: tuple[str, ...],
    metric_crs: str,
    probes: tuple,
) -> str:
    """Deterministic key over every input that shapes the graph."""
    canonical = "|".join(
        [
            f"v{CACHE_VERSION}",
            ",".join(sorted(feed_ids)),
            f"{buffer_m:g}",
            line_table,
            geom_column,
            ",".join(excluded),
            metric_crs,
            *[str(p) for p in probes],
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"walkgraph-{key[:12]}.npz"


def load(cache_dir: Path, key: str) -> Optional[GraphArrays]:
    """The cached graph arrays for *key*, or ``None`` on miss/corruption."""
    path = _path(cache_dir, key)
    if not path.is_file():
        return None
    try:
        with np.load(path) as data:
            return tuple(data[name] for name in _ARRAY_NAMES)  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001 - a broken cache is a miss, never an error
        log.warning("Walk-graph cache unreadable (%s): %s", path.name, exc)
        return None


def save(
    cache_dir: Path,
    key: str,
    node_xy: np.ndarray,
    indptr: np.ndarray,
    indices: np.ndarray,
    weights: np.ndarray,
) -> Path:
    """Persist the graph arrays under *key*; returns the written path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _path(cache_dir, key)
    np.savez_compressed(
        path, node_xy=node_xy, indptr=indptr, indices=indices, weights=weights
    )
    log.info("Walk graph cached: %s (%d vertices)", path, len(node_xy))
    return path
