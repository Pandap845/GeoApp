"""Routable stop-level network: the data's shortest-path graph, exposed.

``route_connectivity.graphml`` answers "can these two ROUTES transfer?" -
adjacency, no times, no stops. But the pipeline already computes everything
a shortest-path network needs; this module assembles it into one
``nx.MultiDiGraph``:

* **nodes** - every analyzed transit stop (key ``stop_uid``), carrying the
  near_stations.csv ``ID``, name, feed, dedupe ``canonical_uid`` and
  coordinates. Stops of stops-only sources (``exclude_feeds``, e.g. MiBici
  docks) are kept as nodes flagged ``stops_only`` but get **no** walk or
  same_station arcs: a node with walk arcs is a waypoint any shortest path may
  pass through, and a dock between two bus stops would chain two walks into
  one the proximity radius never allowed. Their transit neighbours remain in
  ``near_stations.csv``;
* **``in_vehicle`` edges** (directed) - consecutive-stop segments from
  ``route_geometry.route_stop_links``, weighted by scheduled ``travel_min``;
* **``walk`` edges** (reciprocal arc pairs) - the dedupe-filtered proximity
  pairs, weighted by ``walk_minutes(distance_m)``;
* **``same_station`` edges** (reciprocal, 0 minutes) - cross-feed alias
  pairs from the dedupe stage, so routes of one feed connect to the other's
  through their shared physical stations. Alias groups may form chains
  rather than cliques - routing is unaffected (the edges cost 0).

MultiDiGraph because a stop pair can legitimately carry several parallel
edges (a walk link AND one in_vehicle link per line/direction); directed
because ``travel_min`` is direction-specific - symmetric relations are
stored as two arcs, so ``nx.shortest_path(g, a, b, weight="time_min")``
works out of the box. Untimed segments carry no ``time_min`` attribute
(GraphML cannot serialize NaN); weight-based routing simply skips them.

Gephi note: enable "parallel edges" on import or it merges the typed edges;
yEd, igraph and networkx read the multigraph natively.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString

from .graph_export import _clean_attrs
from .near_stations import walk_minutes

log = logging.getLogger(__name__)

STATION_GRAPHML_NAME = "station_network.graphml"
STATION_LAYER_NAME = "station_network"

#: Columns of the edges layer/CSV (one row per undirected pair per type for
#: walk/same_station; one per direction for in_vehicle).
EDGE_COLUMNS = [
    "edge_type", "stop_uid_a", "stop_uid_b", "time_min",
    "linea", "direction_id", "route_uid", "distance_m",
]

NODE_COLUMNS = ["stop_uid", "near_id", "name", "feed", "canonical_uid", "lat", "lon"]


def build_station_graph(
    stops: pd.DataFrame,
    near: pd.DataFrame,
    stop_links: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    equivalences: pd.DataFrame,
    canonical: Optional[pd.DataFrame] = None,
    exclude_feeds: Collection[str] = (),
) -> nx.MultiDiGraph:
    """Assemble the routable stop-level network.

    Args:
        stops: all analyzed stops (``stop_uid``, ``feed_id``, ``stop_name``,
            ``stop_lat``, ``stop_lon``).
        near: ``build_near_stations`` output (provides the sequential ``ID``).
        stop_links: directed consecutive-stop segments
            (``route_geometry.LINK_COLUMNS``).
        stop_pairs: dedupe-filtered proximity pairs (``stop_uid_a/b``,
            ``distance_m``; optional ``walk_source``).
        equivalences: dedupe alias pairs (``stop_uid_a/b`` used).
        canonical: dedupe membership map for the ``canonical_uid`` node
            attribute; ``None``/empty -> each stop is its own canonical.
        exclude_feeds: feed ids of stops-only sources; their stops become
            ``stops_only`` nodes and every walk/same_station pair touching one
            is dropped, so they can never sit in the middle of a path.

    Returns:
        The MultiDiGraph described in the module docstring. Deterministic
        node/edge order for identical inputs. Empty inputs yield an empty
        graph.
    """
    graph = nx.MultiDiGraph()
    if stops.empty:
        return graph

    stops_only: set[str] = set()
    if exclude_feeds:
        stops_only = set(stops.loc[stops["feed_id"].isin(list(exclude_feeds)), "stop_uid"])
    if stops_only:
        def _without_stops_only(pairs: pd.DataFrame) -> pd.DataFrame:
            if pairs.empty:
                return pairs
            touches = pairs["stop_uid_a"].isin(stops_only) | pairs["stop_uid_b"].isin(stops_only)
            return pairs[~touches]

        stop_pairs = _without_stops_only(stop_pairs)
        equivalences = _without_stops_only(equivalences)
        log.info("Station network: %d stops-only stop(s) kept as unlinked nodes", len(stops_only))

    near_id = (
        near.set_index("stop_uid")["ID"] if not near.empty else pd.Series(dtype=object)
    )
    canon = (
        canonical.set_index("stop_uid")["canonical_uid"]
        if canonical is not None and not canonical.empty
        else pd.Series(dtype=object)
    )
    for row in stops[["stop_uid", "feed_id", "stop_name", "stop_lat", "stop_lon"]].to_dict("records"):
        uid = row["stop_uid"]
        graph.add_node(
            uid,
            **_clean_attrs(
                {
                    "near_id": int(near_id.get(uid, 0) or 0),
                    "name": row["stop_name"] if pd.notna(row["stop_name"]) else "",
                    "feed": row["feed_id"],
                    "canonical_uid": canon.get(uid, uid),
                    "lat": float(row["stop_lat"]),
                    "lon": float(row["stop_lon"]),
                }
            ),
        )
        if uid in stops_only:
            graph.nodes[uid]["stops_only"] = True

    # in_vehicle: one directed arc per link row (per route/direction).
    if not stop_links.empty:
        links = stop_links.sort_values(
            ["route_uid", "direction_id", "stop_uid_a", "stop_uid_b"], kind="mergesort"
        )
        for row in links.to_dict("records"):
            travel = pd.to_numeric(pd.Series([row.get("travel_min")]), errors="coerce").iloc[0]
            graph.add_edge(
                row["stop_uid_a"],
                row["stop_uid_b"],
                key=f"in_vehicle:{row['route_uid']}:{int(row['direction_id'])}",
                **_clean_attrs(
                    {
                        "edge_type": "in_vehicle",
                        "time_min": float(travel) if pd.notna(travel) else None,
                        "linea": str(row.get("linea", "")),
                        "direction_id": int(row["direction_id"]),
                        "route_uid": row["route_uid"],
                        "feed_id": row["feed_id"],
                    }
                ),
            )

    # walk: two reciprocal arcs per proximity pair.
    if not stop_pairs.empty:
        pairs = stop_pairs.sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")
        minutes = walk_minutes(pairs["distance_m"])
        for (row, walk_min) in zip(pairs.to_dict("records"), minutes):
            attrs = _clean_attrs(
                {
                    "edge_type": "walk",
                    "time_min": float(walk_min),
                    "distance_m": float(row["distance_m"]),
                    "walk_source": row.get("walk_source"),
                }
            )
            graph.add_edge(row["stop_uid_a"], row["stop_uid_b"], key="walk", **attrs)
            graph.add_edge(row["stop_uid_b"], row["stop_uid_a"], key="walk", **attrs)

    # same_station: 0-minute reciprocal arcs between cross-feed aliases.
    if not equivalences.empty:
        alias = equivalences[["stop_uid_a", "stop_uid_b"]].sort_values(
            ["stop_uid_a", "stop_uid_b"], kind="mergesort"
        )
        for row in alias.to_dict("records"):
            attrs = {"edge_type": "same_station", "time_min": 0.0}
            graph.add_edge(row["stop_uid_a"], row["stop_uid_b"], key="same_station", **attrs)
            graph.add_edge(row["stop_uid_b"], row["stop_uid_a"], key="same_station", **attrs)

    log.info(
        "Station network: %d stops, %d edges (%s)",
        graph.number_of_nodes(), graph.number_of_edges(),
        ", ".join(f"{k}={v}" for k, v in _edges_by_type(graph).items()) or "none",
    )
    return graph


def _edges_by_type(graph: nx.MultiDiGraph) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, _, data in graph.edges(data=True):
        kind = data.get("edge_type", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def station_graph_stats(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """Summary stats for the report (weakly-connected: valid for digraphs).

    Stops-only nodes are unlinked by construction, so counting each as its own
    component would bury the transit network's real connectivity under one
    singleton per dock. They are left out of ``n_weak_components`` and counted
    separately in ``n_stops_only``.
    """
    transit = [u for u, d in graph.nodes(data=True) if not d.get("stops_only")]
    return {
        "n_stops": graph.number_of_nodes(),
        "n_stops_only": graph.number_of_nodes() - len(transit),
        "n_edges": graph.number_of_edges(),
        "edges_by_type": _edges_by_type(graph),
        "n_weak_components": (
            len(list(nx.weakly_connected_components(graph.subgraph(transit)))) if transit else 0
        ),
    }


def write_station_graphml(graph: nx.MultiDiGraph, path: Path) -> Path:
    """Write the network as GraphML (attributes pre-sanitized at build time).

    Raises:
        OSError: if the file cannot be written (run-level problem).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, path)
    log.info(
        "Station network written: %s (%d nodes, %d edges)",
        path, graph.number_of_nodes(), graph.number_of_edges(),
    )
    return path


def station_edges_frame(graph: nx.MultiDiGraph) -> pd.DataFrame:
    """The network's edges as a flat frame (:data:`EDGE_COLUMNS`).

    Reciprocal walk/same_station arcs collapse to one row per unordered
    pair; in_vehicle rows stay one per direction.
    """
    rows = []
    seen: set[tuple] = set()
    for a, b, data in graph.edges(data=True):
        kind = data.get("edge_type", "unknown")
        if kind in ("walk", "same_station"):
            key = (kind, *sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
        rows.append(
            {
                "edge_type": kind,
                "stop_uid_a": a,
                "stop_uid_b": b,
                "time_min": data.get("time_min"),
                "linea": data.get("linea"),
                "direction_id": data.get("direction_id"),
                "route_uid": data.get("route_uid"),
                "distance_m": data.get("distance_m"),
            }
        )
    frame = pd.DataFrame(rows, columns=EDGE_COLUMNS)
    return frame.sort_values(
        ["edge_type", "stop_uid_a", "stop_uid_b", "route_uid"], kind="mergesort"
    ).reset_index(drop=True)


def station_nodes_frame(graph: nx.MultiDiGraph) -> pd.DataFrame:
    """The network's nodes as a flat frame (:data:`NODE_COLUMNS`)."""
    rows = [
        {
            "stop_uid": uid,
            "near_id": data.get("near_id"),
            "name": data.get("name", ""),
            "feed": data.get("feed"),
            "canonical_uid": data.get("canonical_uid", uid),
            "lat": data.get("lat"),
            "lon": data.get("lon"),
        }
        for uid, data in graph.nodes(data=True)
    ]
    frame = pd.DataFrame(rows, columns=NODE_COLUMNS)
    return frame.sort_values("stop_uid", kind="mergesort").reset_index(drop=True)


def station_edges_layer(
    graph: nx.MultiDiGraph, stops: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """GeoPackage layer: one LineString per edge row of the flat frame."""
    edges = station_edges_frame(graph)
    if edges.empty:
        return gpd.GeoDataFrame(
            pd.DataFrame(columns=EDGE_COLUMNS), geometry=[], crs=stops.crs or "EPSG:4326"
        )
    geom_by_uid = stops.set_index("stop_uid").geometry
    lines = [
        LineString([geom_by_uid[a], geom_by_uid[b]])
        for a, b in zip(edges["stop_uid_a"], edges["stop_uid_b"])
    ]
    return gpd.GeoDataFrame(edges, geometry=lines, crs=stops.crs or "EPSG:4326")
