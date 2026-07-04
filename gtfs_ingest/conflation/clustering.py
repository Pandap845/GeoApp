"""Interchange detection: DBSCAN clustering merged with parent_station.

Method (documented in the README):

1. Stops are projected to a metric CRS (configurable; default: UTM zone
   estimated from the data extent) and clustered with DBSCAN
   (``eps`` = 120 m, ``min_samples`` = 2 by default - both configurable).
2. DBSCAN clusters are then **merged with parent_station groupings** via
   connected components: stops sharing a station (and the station stop
   itself) always land in the same cluster, so a declared station is never
   split - even when its platforms are farther apart than ``eps``.
3. Components with >= 2 stops become interchange clusters; the rest are
   noise. Cluster ids are deterministic (assigned in order of each
   cluster's lexicographically smallest stop_uid).
4. Each cluster collects the GTFS ``route_type``s of the routes serving its
   member stops. Extended route types (Google extension, e.g. 700-series
   bus) are normalized to the basic 0-12 codes first. A cluster with more
   than one distinct mode is a **multimodal interchange**; single-mode
   clusters are kept and labeled ``is_multimodal = false``.
"""

from __future__ import annotations

import logging
from typing import Optional

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.ops import unary_union
from sklearn.cluster import DBSCAN

from ..analysis.connectivity import station_map

log = logging.getLogger(__name__)

#: Basic GTFS route_type codes -> mode label.
MODE_NAMES: dict[int, str] = {
    0: "tram",
    1: "metro",
    2: "rail",
    3: "bus",
    4: "ferry",
    5: "cable_tram",
    6: "aerial_lift",
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
}

#: Extended route_type ranges (TPEG / Google extension) -> basic code.
_EXTENDED_RANGES: tuple[tuple[int, int, int], ...] = (
    (100, 199, 2),    # railway service        -> rail
    (200, 299, 3),    # coach service          -> bus
    (400, 404, 1),    # urban railway/metro    -> metro
    (405, 405, 12),   # monorail
    (700, 799, 3),    # bus service            -> bus
    (800, 899, 11),   # trolleybus
    (900, 999, 0),    # tram service           -> tram
    (1000, 1099, 4),  # water transport        -> ferry
    (1200, 1299, 4),  # ferry
    (1300, 1399, 6),  # aerial lift
    (1400, 1499, 7),  # funicular
)

#: Columns of the interchange cluster frame.
CLUSTER_COLUMNS = [
    "cluster_id", "is_multimodal", "stop_count", "modes", "mode_names",
    "member_stop_uids", "routes", "feed_ids", "centroid_wkt", "hull_wkt",
]


def normalize_route_type(route_type: object) -> Optional[int]:
    """Map a GTFS route_type (basic or extended) to a basic 0-12 code.

    Args:
        route_type: raw value (int, numeric string, or garbage).

    Returns:
        The basic code, or ``None`` when the value is missing/unparseable
        (never raises - broken feeds are a fact of life here).
    """
    try:
        code = int(float(route_type))  # tolerate "3", 3, "3.0"
    except (TypeError, ValueError):
        return None
    if code in MODE_NAMES:
        return code
    for lo, hi, basic in _EXTENDED_RANGES:
        if lo <= code <= hi:
            return basic
    return None


def mode_name(basic_code: int) -> str:
    """Human label for a basic route_type code (``mode_<n>`` if unknown)."""
    return MODE_NAMES.get(basic_code, f"mode_{basic_code}")


def cluster_stops(
    stops: gpd.GeoDataFrame,
    eps_m: float = 120.0,
    min_samples: int = 2,
    metric_crs: Optional[str] = None,
) -> np.ndarray:
    """Assign a cluster label to every stop (``-1`` = not in any cluster).

    DBSCAN in a metric CRS, then merged with parent_station groups (see
    module docstring). Deterministic for identical input.

    Args:
        stops: normalized stops (all feeds; EPSG:4326 geometry, needs
            ``stop_uid``, ``feed_id``, ``parent_station``).
        eps_m: DBSCAN neighborhood radius in meters.
        min_samples: DBSCAN core-point threshold.
        metric_crs: metric CRS override (e.g. ``"EPSG:32618"``); default
            estimates a UTM zone from the stop extent.

    Returns:
        Integer labels aligned with ``stops`` rows; clusters are numbered
        from 0, non-clustered stops get ``-1``.
    """
    n = len(stops)
    if n == 0:
        return np.array([], dtype=int)

    projected = stops.to_crs(metric_crs or stops.estimate_utm_crs())
    xy = np.column_stack([projected.geometry.x, projected.geometry.y])
    dbscan_labels = DBSCAN(eps=eps_m, min_samples=min_samples).fit_predict(xy)

    graph = nx.Graph()
    graph.add_nodes_from(range(n))
    # Chain members of each DBSCAN cluster.
    order = np.argsort(dbscan_labels, kind="stable")
    for prev, cur in zip(order, order[1:]):
        if dbscan_labels[prev] == dbscan_labels[cur] and dbscan_labels[cur] != -1:
            graph.add_edge(int(prev), int(cur))
    # Chain members of each parent_station group (station never split).
    pos_by_uid = {uid: i for i, uid in enumerate(stops["stop_uid"])}
    stations = station_map(pd.DataFrame(stops.drop(columns="geometry")))
    for _, group in stations.groupby("station_uid"):
        members = [pos_by_uid[u] for u in group["stop_uid"] if u in pos_by_uid]
        for prev, cur in zip(members, members[1:]):
            graph.add_edge(prev, cur)

    labels = np.full(n, -1, dtype=int)
    components = [c for c in nx.connected_components(graph) if len(c) >= 2]
    components.sort(key=lambda c: min(stops["stop_uid"].iloc[i] for i in c))
    for cluster_id, component in enumerate(components):
        for i in component:
            labels[i] = cluster_id
    log.info(
        "Clustering: %d stops -> %d clusters (%d stops unclustered)",
        n, len(components), int((labels == -1).sum()),
    )
    return labels


def build_interchanges(
    stops: gpd.GeoDataFrame,
    labels: np.ndarray,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate clustered stops into interchange records.

    Args:
        stops: the frame passed to :func:`cluster_stops` (EPSG:4326).
        labels: its output.
        route_stops: (route_uid, stop_uid) service pairs, all feeds
            (``analysis.connectivity.route_stop_map`` output).
        routes: route metadata with ``route_uid`` and ``route_type``.

    Returns:
        One row per cluster with :data:`CLUSTER_COLUMNS`: mode codes/names
        (sorted, deduplicated after normalization), ``is_multimodal``
        (> 1 distinct mode), member/route/feed lists, and centroid + convex
        hull WKT in EPSG:4326. Clusters whose stops are served by no known
        route get empty mode lists and ``is_multimodal = false``.
    """
    route_types: dict[str, Optional[int]] = {}
    if not routes.empty and "route_type" in routes.columns:
        route_types = {
            r["route_uid"]: normalize_route_type(r["route_type"])
            for r in routes[["route_uid", "route_type"]].to_dict("records")
        }
    routes_by_stop = route_stops.groupby("stop_uid")["route_uid"].agg(set) if not route_stops.empty \
        else pd.Series(dtype=object)

    records: list[dict] = []
    for cluster_id in sorted(set(labels) - {-1}):
        members = stops[labels == cluster_id]
        uids = sorted(members["stop_uid"])
        serving: set[str] = set()
        for uid in uids:
            serving |= routes_by_stop.get(uid, set())
        modes = sorted({m for m in (route_types.get(r) for r in serving) if m is not None})
        union = unary_union(list(members.geometry))
        records.append(
            {
                "cluster_id": int(cluster_id),
                "is_multimodal": len(modes) > 1,
                "stop_count": len(uids),
                "modes": modes,
                "mode_names": [mode_name(m) for m in modes],
                "member_stop_uids": uids,
                "routes": sorted(serving),
                "feed_ids": sorted(set(members["feed_id"])),
                "centroid_wkt": union.centroid.wkt,
                "hull_wkt": union.convex_hull.wkt,
            }
        )
    return pd.DataFrame(records, columns=CLUSTER_COLUMNS)
