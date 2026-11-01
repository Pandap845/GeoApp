"""Routable stop-level network: nx.shortest_path(g, a, b, weight="time_min") works on it.

Nodes are stops. Arcs: in_vehicle (directed, scheduled minutes), walk (both
ways, walking minutes) and same_station (both ways, 0 minutes between dedupe
aliases). It is a MultiDiGraph because one stop pair can have a walk arc and
several in_vehicle arcs. Stops-only nodes (MiBici docks) get no arcs, so no
path can pass through them. Untimed in_vehicle arcs have no time_min.
In Gephi, enable "parallel edges" when importing.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString

from .graph_export import _clean_attrs
from .near_stations import walk_minutes

log = logging.getLogger(__name__)

STATION_GRAPHML_NAME = "station_network.graphml"
STATION_LAYER_NAME = "station_network"

#: Edge CSV columns: one row per pair for walk/same_station, per direction for in_vehicle.
EDGE_COLUMNS = [
    "edge_type",
    "stop_uid_a",
    "stop_uid_b",
    "time_min",
    "linea",
    "direction_id",
    "route_uid",
    "distance_m",
    "time_quality",
]

NODE_COLUMNS = ["stop_uid", "near_id", "name", "feed", "canonical_uid", "lat", "lon"]

#: Write every walk/same_station arc unless pruning is asked for; CLI default too.
PRUNE_STATION_NETWORK = False


def build_station_graph(
    stops: pd.DataFrame,
    near: pd.DataFrame,
    stop_links: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    equivalences: pd.DataFrame,
    canonical: pd.DataFrame | None = None,
    exclude_feeds: Collection[str] = (),
) -> nx.MultiDiGraph:
    """Build the network; stops of *exclude_feeds* become unlinked "stops_only" nodes."""
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

    near_id = near.set_index("stop_uid")["ID"] if not near.empty else pd.Series(dtype=object)
    canon = (
        canonical.set_index("stop_uid")["canonical_uid"]
        if canonical is not None and not canonical.empty
        else pd.Series(dtype=object)
    )
    for row in stops[["stop_uid", "feed_id", "stop_name", "stop_lat", "stop_lon"]].to_dict(
        "records"
    ):
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

    # in_vehicle: one directed arc per route, direction and stop pair.
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
                        "time_quality": row.get("time_quality"),
                    }
                ),
            )

    # walk: two arcs (both ways) per proximity pair.
    if not stop_pairs.empty:
        pairs = stop_pairs.sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")
        minutes = walk_minutes(pairs["distance_m"])
        for row, walk_min in zip(pairs.to_dict("records"), minutes):
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

    # same_station: 0-minute arcs both ways between dedupe aliases.
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
        graph.number_of_nodes(),
        graph.number_of_edges(),
        ", ".join(f"{k}={v}" for k, v in _edges_by_type(graph).items()) or "none",
    )
    return graph


def _pairs(graph: nx.MultiDiGraph, kind: str) -> dict[tuple[str, str], dict]:
    """{(a, b): attrs} of the two-way arcs of *kind*, one entry per stop pair (a < b)."""
    pairs: dict[tuple[str, str], dict] = {}
    for a, b, data in graph.edges(data=True):
        if data.get("edge_type") == kind:
            pairs.setdefault((min(a, b), max(a, b)), data)
    return pairs


def _redundant_same_station(pairs: Collection[tuple[str, str]]) -> set[tuple[str, str]]:
    """Pairs that close a cycle: a spanning tree per station keeps every 0-minute link."""
    root: dict[str, str] = {}

    def find(uid: str) -> str:
        root.setdefault(uid, uid)
        while root[uid] != uid:
            root[uid] = root[root[uid]]
            uid = root[uid]
        return uid

    redundant = set()
    for a, b in sorted(pairs):
        root_a, root_b = find(a), find(b)
        if root_a == root_b:
            redundant.add((a, b))
        else:
            root[root_b] = root_a
    return redundant


def _redundant_walk(
    walk: dict[tuple[str, str], dict], same_station: Collection[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Walk pairs another walking path matches in minutes; removed longest first.

    A pair is dropped only when the remaining walking graph still links its
    stops within its own minutes, so no shortest walking time ever changes.
    """
    walking = nx.Graph()
    for (a, b), data in walk.items():
        walking.add_edge(a, b, time_min=float(data["time_min"]))
    for a, b in same_station:
        walking.add_edge(a, b, time_min=0.0)

    order = sorted(
        walk.items(),
        key=lambda item: (-item[1]["time_min"], -(item[1].get("distance_m") or 0.0), item[0]),
    )
    redundant = set()
    for (a, b), data in order:
        minutes = float(data["time_min"])
        attrs = walking[a][b]
        if attrs["time_min"] < minutes:  # the same pair is also a 0-minute alias link
            redundant.add((a, b))
            continue
        walking.remove_edge(a, b)
        reach = nx.single_source_dijkstra_path_length(walking, a, cutoff=minutes, weight="time_min")
        if b in reach:
            redundant.add((a, b))
        else:
            walking.add_edge(a, b, **attrs)
    return redundant


def redundant_pairs(
    graph: nx.MultiDiGraph,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(walk, same_station) stop pairs whose arcs change no shortest time_min path."""
    same = _pairs(graph, "same_station")
    same_redundant = _redundant_same_station(same)
    kept_same = [pair for pair in same if pair not in same_redundant]
    walk_redundant = _redundant_walk(_pairs(graph, "walk"), kept_same)
    return walk_redundant, same_redundant


def prune_station_graph(
    graph: nx.MultiDiGraph,
    walk: Collection[tuple[str, str]],
    same_station: Collection[tuple[str, str]],
) -> nx.MultiDiGraph:
    """Copy of *graph* without the arcs (both ways) of the given redundant pairs."""
    drop_by_kind = {"walk": set(walk), "same_station": set(same_station)}
    pruned = graph.copy()
    pruned.remove_edges_from(
        [
            (a, b, key)
            for a, b, key, data in graph.edges(keys=True, data=True)
            if (min(a, b), max(a, b)) in drop_by_kind.get(data.get("edge_type"), ())
        ]
    )
    return pruned


def _edges_by_type(graph: nx.MultiDiGraph) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, _, data in graph.edges(data=True):
        kind = data.get("edge_type", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def station_graph_stats(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """Report stats. Weak components count transit stops only; docks go in n_stops_only."""
    transit = [u for u, d in graph.nodes(data=True) if not d.get("stops_only")]
    quality: dict[str, int] = {}
    for _, _, data in graph.edges(data=True):
        if data.get("edge_type") == "in_vehicle" and data.get("time_quality"):
            quality[data["time_quality"]] = quality.get(data["time_quality"], 0) + 1
    return {
        "n_stops": graph.number_of_nodes(),
        "n_stops_only": graph.number_of_nodes() - len(transit),
        "n_edges": graph.number_of_edges(),
        "edges_by_type": _edges_by_type(graph),
        "n_weak_components": (
            len(list(nx.weakly_connected_components(graph.subgraph(transit)))) if transit else 0
        ),
        "in_vehicle_time_quality": dict(sorted(quality.items())),
    }


def write_station_graphml(graph: nx.MultiDiGraph, path: Path) -> Path:
    """Write the network as GraphML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, path)
    log.info(
        "Station network written: %s (%d nodes, %d edges)",
        path,
        graph.number_of_nodes(),
        graph.number_of_edges(),
    )
    return path


def station_edges_frame(graph: nx.MultiDiGraph) -> pd.DataFrame:
    """Edges as a table; two-way arcs become one row per pair."""
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
                "time_quality": data.get("time_quality"),
            }
        )
    frame = pd.DataFrame(rows, columns=EDGE_COLUMNS)
    return frame.sort_values(
        ["edge_type", "stop_uid_a", "stop_uid_b", "route_uid"], kind="mergesort"
    ).reset_index(drop=True)


def station_nodes_frame(graph: nx.MultiDiGraph) -> pd.DataFrame:
    """Nodes as a table."""
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


def station_edges_layer(graph: nx.MultiDiGraph, stops: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Edges as lines for the GeoPackage."""
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
