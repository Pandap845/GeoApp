"""On-disk cache of the OSM walk graph used by --network-walk.

The key covers every input that shapes the graph (feeds, buffer, OSM table,
CRS) plus cheap database probes, so an OSM reimport or a stop reload gives a
new key. A broken cache file counts as a miss. Use --no-walk-cache to bypass it.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np
from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

#: Bump when the cached format or the graph-building algorithm changes.
CACHE_VERSION = 1

#: (node_xy, indptr, indices, weights) as returned by build_walk_graph.
GraphArrays = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]

_ARRAY_NAMES = ("node_xy", "indptr", "indices", "weights")


def probe_state(engine: Engine, feed_ids: list[str], line_table: str) -> tuple:
    """Return (line_count, max_osm_id, latest stop load time): changes when the data changes."""
    with engine.connect() as conn:
        count, max_id = conn.execute(
            text(
                f"SELECT count(*), max(osm_id) FROM {line_table}"
            )  # table and column names are internal, never user input
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
    """Deterministic hash of every input that shapes the graph."""
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


def load(cache_dir: Path, key: str) -> GraphArrays | None:
    """Cached graph arrays for *key*, or None if missing or unreadable."""
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
    """Save the graph arrays under *key* and return the file path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _path(cache_dir, key)
    np.savez_compressed(path, node_xy=node_xy, indptr=indptr, indices=indices, weights=weights)
    log.info("Walk graph cached: %s (%d vertices)", path, len(node_xy))
    return path
