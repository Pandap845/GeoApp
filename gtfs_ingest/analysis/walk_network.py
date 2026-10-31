"""OSM street-network walking distances for the near-stations relation.

The default walking times in ``near_stations`` are straight-line - honest
but optimistic where a barrier (highway, river, railyard) separates two
stops that are close as the crow flies. This module replaces the pair
distances with **network distances walked along OSM streets**, using the
``planet_osm_line`` layer already in PostGIS (no pgRouting required):

1. Fetch walkable ways (``highway`` tagged, motorways excluded) inside the
   analyzed stops' convex hull + buffer, in one indexed query.
2. Build a vertex graph in a metric CRS: nodes are way vertices (merged on
   coordinates rounded to 0.1 m), edges are consecutive-vertex segments
   weighted by planar length.
3. Snap every stop of a near pair to its nearest graph vertex (KD-tree).
4. For each source stop, run a **bounded Dijkstra** (early exit past
   ``cutoff_m``) and read the distances to its neighbours' vertices. The
   snap offsets of both endpoints are added, so times stay conservative.

A pair keeps its straight-line distance when no path exists within the
cutoff (disconnected islands, snapping misses) - the fallback count is
reported. Opt-in via ``analyze-frequency --network-walk``: the computation
covers ~40k pairs against a metro-area street graph, which costs minutes,
not seconds.

Provenance for the quality sidecar (``near_stations_quality.csv``): the
returned pairs carry ``walk_source`` (``network``/``straight_line``),
``line_m`` and per-endpoint snap distances ``snap_m_a``/``snap_m_b``;
stops snapping farther than :data:`MAX_SNAP_M` are classified
``unsnapped`` downstream (classification only - routing is unchanged).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely import wkt as shapely_wkt
from sqlalchemy import Engine, bindparam, text

from . import graph_cache

log = logging.getLogger(__name__)

#: highway values never walkable.
EXCLUDED_HIGHWAYS: tuple[str, ...] = ("motorway", "motorway_link", "proposed", "construction")

#: Vertex merge tolerance (meters) - OSM shared endpoints coincide exactly;
#: the rounding just guards float noise.
_MERGE_M = 0.1

#: Classification-only threshold for the quality sidecar - a stop farther
#: than this from any walkable way is labeled ``unsnapped``. Routing
#: behavior is unchanged (snapping itself has no upper bound).
MAX_SNAP_M = 200.0


def fetch_walk_lines(
    engine: Engine,
    feed_ids: list[str],
    buffer_m: float = 1000.0,
    line_table: str = "planet_osm_line",
    geom_column: str = "way",
) -> gpd.GeoSeries:
    """Walkable OSM ways (EPSG:4326) inside the stops' buffered hull.

    Args:
        engine: database engine (stops + OSM in the same PostGIS).
        feed_ids: restrict the hull to these feeds' stops.
        buffer_m: hull buffer so edge stops keep their surroundings.
        line_table / geom_column: OSM layer names (osm2pgsql classic).

    Returns:
        GeoSeries of LineStrings; empty when nothing intersects.
    """
    sql = text(
        f"""
        WITH area AS (
            SELECT ST_Buffer(ST_ConvexHull(ST_Collect(geom))::geography,
                             :buffer_m)::geometry AS hull
            FROM stops WHERE feed_id = ANY(:feeds)
        )
        SELECT ST_AsText(ST_Transform(l.{geom_column}, 4326)) AS wkt
        FROM {line_table} l, area a
        WHERE l.highway IS NOT NULL
          AND l.highway NOT IN :excluded
          AND ST_Intersects(ST_Transform(l.{geom_column}, 4326), a.hull)
        """
    ).bindparams(
        bindparam("excluded", value=list(EXCLUDED_HIGHWAYS), expanding=True),
        bindparam("buffer_m", value=float(buffer_m)),
        bindparam("feeds", value=feed_ids),
    )
    with engine.connect() as conn:
        rows = [r[0] for r in conn.execute(sql)]
    lines = gpd.GeoSeries([shapely_wkt.loads(w) for w in rows], crs="EPSG:4326")
    log.info("Walk network: %d walkable OSM way(s) fetched", len(lines))
    return lines


def build_walk_graph(
    lines_metric: gpd.GeoSeries,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vertex graph (CSR adjacency) from projected street lines.

    Args:
        lines_metric: LineStrings in a metric CRS.

    Returns:
        ``(node_xy, indptr, indices, weights)`` - node coordinates and the
        CSR arrays of the undirected graph (each edge stored both ways).
    """
    starts: list[np.ndarray] = []
    ends: list[np.ndarray] = []
    for geom in lines_metric:
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            coords = np.asarray(part.coords, dtype=float)[:, :2]
            if len(coords) >= 2:
                starts.append(coords[:-1])
                ends.append(coords[1:])
    if not starts:
        empty = np.empty((0, 2))
        return empty, np.zeros(1, dtype=np.int64), np.empty(0, dtype=np.int64), np.empty(0)

    a = np.concatenate(starts)
    b = np.concatenate(ends)
    all_pts = np.round(np.concatenate([a, b]) / _MERGE_M) * _MERGE_M
    node_xy, inverse = np.unique(all_pts, axis=0, return_inverse=True)
    u, v = inverse[: len(a)], inverse[len(a):]
    w = np.hypot(a[:, 0] - b[:, 0], a[:, 1] - b[:, 1])
    keep = (u != v) & (w > 0)
    u, v, w = u[keep], v[keep], w[keep]

    # Undirected: store both directions, then CSR-compress by source node.
    src = np.concatenate([u, v])
    dst = np.concatenate([v, u])
    wgt = np.concatenate([w, w])
    order = np.argsort(src, kind="stable")
    src, dst, wgt = src[order], dst[order], wgt[order]
    indptr = np.zeros(len(node_xy) + 1, dtype=np.int64)
    np.add.at(indptr, src + 1, 1)
    indptr = np.cumsum(indptr)
    log.info("Walk network: %d vertices, %d directed edges", len(node_xy), len(dst))
    return node_xy, indptr, dst.astype(np.int64), wgt


#: Source vertices routed per scipy dijkstra call. Each call allocates a
#: dense (chunk, n_vertices) float64 matrix: 16 sources over a 2M-vertex
#: metro graph is ~256 MB - bounded memory while amortizing per-call setup.
_DIJKSTRA_CHUNK = 16


def pair_network_distances(
    node_xy: np.ndarray,
    indptr: np.ndarray,
    indices: np.ndarray,
    weights: np.ndarray,
    stop_xy: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    cutoff_m: float = 800.0,
) -> pd.DataFrame:
    """Network walking distance for each near pair.

    Args:
        node_xy / indptr / indices / weights: :func:`build_walk_graph` output.
        stop_xy: index = stop_uid, columns ``x, y`` (same metric CRS).
        stop_pairs: ``stop_uid_a, stop_uid_b, distance_m`` (straight-line).
        cutoff_m: search radius; pairs without a path within it fall back.

    Returns:
        ``stop_pairs`` plus ``network_m`` (NaN = no path found; callers keep
        the straight-line value) - snap offsets of both stops included -
        and ``snap_m_a``/``snap_m_b`` (each endpoint's distance to its
        nearest graph vertex; NaN when the graph is empty). The snap
        columns feed the quality sidecar's ``unsnapped`` classification.
    """
    result = stop_pairs.copy()
    result["network_m"] = np.nan
    if len(node_xy) == 0 or stop_pairs.empty:
        result["snap_m_a"] = np.nan
        result["snap_m_b"] = np.nan
        return result

    tree = cKDTree(node_xy)
    snap_d, snap_i = tree.query(stop_xy[["x", "y"]].to_numpy())
    node_of = dict(zip(stop_xy.index, snap_i.astype(int)))
    snap_of = dict(zip(stop_xy.index, snap_d))
    result["snap_m_a"] = result["stop_uid_a"].map(snap_of)
    result["snap_m_b"] = result["stop_uid_b"].map(snap_of)

    # Group pair rows by snapped SOURCE VERTEX (stops sharing a vertex are
    # routed once) and batch the sources through scipy's C Dijkstra.
    by_vertex: dict[int, list[int]] = {}
    for row_idx, (a, b) in enumerate(zip(result["stop_uid_a"], result["stop_uid_b"])):
        if a in node_of and b in node_of:
            by_vertex.setdefault(node_of[a], []).append(row_idx)

    network = np.full(len(result), np.nan)
    if by_vertex:
        graph = csr_matrix(
            (weights, indices, indptr), shape=(len(node_xy), len(node_xy))
        )
        a_uids = result["stop_uid_a"].to_numpy()
        b_uids = result["stop_uid_b"].to_numpy()
        source_vertices = sorted(by_vertex)
        for start in range(0, len(source_vertices), _DIJKSTRA_CHUNK):
            chunk = source_vertices[start : start + _DIJKSTRA_CHUNK]
            # limit= bounds the search radius; unreached vertices read inf.
            dmat = dijkstra(graph, directed=True, indices=chunk, limit=cutoff_m)
            for row_in_chunk, vertex in enumerate(chunk):
                for i in by_vertex[vertex]:
                    d = dmat[row_in_chunk, node_of[b_uids[i]]]
                    if np.isfinite(d):
                        network[i] = d + snap_of[a_uids[i]] + snap_of[b_uids[i]]
    result["network_m"] = network
    return result


def _finalize_pairs(pairs: pd.DataFrame) -> pd.DataFrame:
    """Fold ``network_m`` into ``distance_m`` and label each pair's source.

    Args:
        pairs: :func:`pair_network_distances` output (has ``network_m``).

    Returns:
        The frame with ``distance_m`` network-refined where a path exists
        (never shorter than the straight line), ``line_m`` preserving the
        original straight-line value, and ``walk_source``
        (``"network"``/``"straight_line"``); ``network_m`` dropped.
    """
    pairs["line_m"] = pairs["distance_m"]
    # A street path can never beat the straight line; clip guards snap noise.
    routed = pairs["network_m"].notna()
    pairs.loc[routed, "distance_m"] = np.maximum(
        pairs.loc[routed, "network_m"], pairs.loc[routed, "line_m"]
    )
    pairs["walk_source"] = np.where(routed, "network", "straight_line")
    measurable = routed & (pairs["line_m"] > 0)  # co-located stops have no ratio
    log.info(
        "Walk network: %d/%d near pair(s) routed on streets (%d fell back to "
        "straight line); mean detour %.0f%%",
        int(routed.sum()), len(pairs), int((~routed).sum()),
        ((pairs.loc[measurable, "distance_m"] / pairs.loc[measurable, "line_m"]).mean() - 1) * 100
        if measurable.any() else 0.0,
    )
    return pairs.drop(columns=["network_m"])


def _build_or_load_graph(
    engine: Engine,
    feed_ids: list[str],
    crs: str,
    line_table: str,
    geom_column: str,
    cache_dir: Optional[Path],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The walk graph for these inputs, via the npz cache when enabled."""
    key = None
    if cache_dir is not None:
        try:
            probes = graph_cache.probe_state(engine, feed_ids, line_table)
            key = graph_cache.cache_key(
                feed_ids, 1000.0, line_table, geom_column, EXCLUDED_HIGHWAYS, crs, probes
            )
            started = time.perf_counter()
            cached = graph_cache.load(cache_dir, key)
            if cached is not None:
                log.info(
                    "Walk graph cache hit (%s): %d vertices loaded in %.2fs",
                    key[:12], len(cached[0]), time.perf_counter() - started,
                )
                return cached
        except Exception as exc:  # noqa: BLE001 - caching must never break a run
            log.warning("Walk-graph cache lookup failed (%s); rebuilding", exc)
            key = None

    started = time.perf_counter()
    lines = fetch_walk_lines(engine, feed_ids, line_table=line_table, geom_column=geom_column)
    node_xy, indptr, indices, weights = build_walk_graph(lines.to_crs(crs))
    log.info(
        "Walk graph built in %.1fs (%d vertices)%s",
        time.perf_counter() - started, len(node_xy),
        "" if key is None else "; caching",
    )
    if key is not None:
        try:
            graph_cache.save(cache_dir, key, node_xy, indptr, indices, weights)
        except Exception as exc:  # noqa: BLE001
            log.warning("Walk-graph cache write failed: %s", exc)
    return node_xy, indptr, indices, weights


def network_distances(
    engine: Engine,
    stops: gpd.GeoDataFrame,
    stop_pairs: pd.DataFrame,
    feed_ids: list[str],
    metric_crs: Optional[str] = None,
    cutoff_m: float = 800.0,
    line_table: str = "planet_osm_line",
    geom_column: str = "way",
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """End to end: OSM walk graph -> per-pair network walking distances.

    Args:
        cache_dir: directory for the on-disk walk-graph cache
            (``graph_cache``); ``None`` disables caching (default).

    Returns:
        ``stop_pairs`` with ``distance_m`` replaced by the network distance
        where a path exists (never shorter than the straight line), plus
        provenance columns for the quality sidecar: ``line_m`` (original
        straight-line value), ``walk_source`` (``network``/``straight_line``
        per pair), and ``snap_m_a``/``snap_m_b`` (endpoint snap distances).
        When *stop_pairs* is empty it is returned unchanged (no columns
        added) - consumers treat the missing columns as "never ran".
    """
    if stop_pairs.empty:
        return stop_pairs
    crs = str(metric_crs or stops.estimate_utm_crs())
    node_xy, indptr, indices, weights = _build_or_load_graph(
        engine, feed_ids, crs, line_table, geom_column, cache_dir
    )

    projected = stops.drop_duplicates("stop_uid").set_index("stop_uid").geometry.to_crs(crs)
    stop_xy = pd.DataFrame({"x": projected.x, "y": projected.y})
    pairs = pair_network_distances(node_xy, indptr, indices, weights,
                                   stop_xy, stop_pairs, cutoff_m)
    return _finalize_pairs(pairs)
