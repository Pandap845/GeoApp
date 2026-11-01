"""Interchange detection: DBSCAN on stops, merged with parent stations and dedupe groups.

DBSCAN runs in a metric CRS (default eps 120 m, min_samples 2). Stops of the
same parent station or dedupe group are always kept together. Groups of 2+
stops become clusters, numbered by their smallest stop_uid. A cluster served
by more than one mode is multimodal; physical_stop_count counts members after
dedupe.
"""

from __future__ import annotations

import logging
from itertools import pairwise

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
    (100, 199, 2),  # railway service        -> rail
    (200, 299, 3),  # coach service          -> bus
    (400, 404, 1),  # urban railway/metro    -> metro
    (405, 405, 12),  # monorail
    (700, 799, 3),  # bus service            -> bus
    (800, 899, 11),  # trolleybus
    (900, 999, 0),  # tram service           -> tram
    (1000, 1099, 4),  # water transport        -> ferry
    (1200, 1299, 4),  # ferry
    (1300, 1399, 6),  # aerial lift
    (1400, 1499, 7),  # funicular
)

CLUSTER_COLUMNS = [
    "cluster_id",
    "is_multimodal",
    "stop_count",
    "physical_stop_count",
    "modes",
    "mode_names",
    "member_stop_uids",
    "routes",
    "feed_ids",
    "centroid_wkt",
    "hull_wkt",
]


def normalize_route_type(route_type: object) -> int | None:
    """Basic 0-12 code for a basic or extended route_type; None if unparseable."""
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
    """Mode name for a basic route_type code ("mode_<n>" if unknown)."""
    return MODE_NAMES.get(basic_code, f"mode_{basic_code}")


def cluster_stops(
    stops: gpd.GeoDataFrame,
    eps_m: float = 120.0,
    min_samples: int = 2,
    metric_crs: str | None = None,
    canonical: pd.DataFrame | None = None,
) -> np.ndarray:
    """Cluster label per stop row (-1 = no cluster); deterministic for the same input."""
    n = len(stops)
    if n == 0:
        return np.array([], dtype=int)

    projected = stops.to_crs(metric_crs or stops.estimate_utm_crs())
    xy = np.column_stack([projected.geometry.x, projected.geometry.y])
    dbscan_labels = DBSCAN(eps=eps_m, min_samples=min_samples).fit_predict(xy)

    graph = nx.Graph()
    graph.add_nodes_from(range(n))
    # Join stops that share a DBSCAN cluster, a parent station or a dedupe group.
    order = np.argsort(dbscan_labels, kind="stable")
    for prev, cur in pairwise(order):
        if dbscan_labels[prev] == dbscan_labels[cur] and dbscan_labels[cur] != -1:
            graph.add_edge(int(prev), int(cur))
    pos_by_uid = {uid: i for i, uid in enumerate(stops["stop_uid"])}
    stations = station_map(pd.DataFrame(stops.drop(columns="geometry")))
    for _, group in stations.groupby("station_uid"):
        members = [pos_by_uid[u] for u in group["stop_uid"] if u in pos_by_uid]
        for prev, cur in pairwise(members):
            graph.add_edge(prev, cur)
    if canonical is not None and not canonical.empty:
        for _, group in canonical.groupby("canonical_uid"):
            members = [pos_by_uid[u] for u in sorted(group["stop_uid"]) if u in pos_by_uid]
            for prev, cur in pairwise(members):
                graph.add_edge(prev, cur)

    labels = np.full(n, -1, dtype=int)
    components = [c for c in nx.connected_components(graph) if len(c) >= 2]
    components.sort(key=lambda c: min(stops["stop_uid"].iloc[i] for i in c))
    for cluster_id, component in enumerate(components):
        for i in component:
            labels[i] = cluster_id
    log.info(
        "Clustering: %d stops -> %d clusters (%d stops unclustered)",
        n,
        len(components),
        int((labels == -1).sum()),
    )
    return labels


def build_interchanges(
    stops: gpd.GeoDataFrame,
    labels: np.ndarray,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
    canonical: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per cluster: modes, members, routes, feeds, centroid and hull (WKT)."""
    route_types: dict[str, int | None] = {}
    if not routes.empty and "route_type" in routes.columns:
        route_types = {
            r["route_uid"]: normalize_route_type(r["route_type"])
            for r in routes[["route_uid", "route_type"]].to_dict("records")
        }
    routes_by_stop = (
        route_stops.groupby("stop_uid")["route_uid"].agg(set)
        if not route_stops.empty
        else pd.Series(dtype=object)
    )
    canon_of: dict[str, str] = {}
    if canonical is not None and not canonical.empty:
        canon_of = dict(zip(canonical["stop_uid"], canonical["canonical_uid"]))

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
                "physical_stop_count": len({canon_of.get(u, u) for u in uids}),
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
