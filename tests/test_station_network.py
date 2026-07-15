"""Routable station network: nodes, typed edges, routing, exports."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pandas as pd

from gtfs_ingest.analysis import connectivity, station_network
from gtfs_ingest.analysis.near_stations import build_near_stations
from gtfs_ingest.analysis.station_network import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    build_station_graph,
    station_edges_frame,
    station_graph_stats,
    station_nodes_frame,
    write_station_graphml,
)

from test_connectivity import rs
from test_near_stations import make_stops, routes_meta


def _fixture():
    """Two feeds: fa:A --(r1)--> fa:B; fb:X1 aliases fa:A; fb:X2 near fa:B."""
    stops = make_stops(
        ("fa", "A", "Main St", 40.0, -75.0),
        ("fa", "B", "Second", 40.009, -75.0),          # ~1 km from A
        ("fb", "X1", "Main St", 40.00009, -75.0),      # ~10 m: alias of A
        ("fb", "X2", "Third", 40.0095, -75.0),         # ~55 m from B
    )
    route_stops = rs(("fa", "r1", "A"), ("fa", "r1", "B"), ("fb", "rX", "X1"), ("fb", "rX", "X2"))
    routes = routes_meta(("fa", "r1", "10"), ("fb", "rX", "X9"))
    stop_links = pd.DataFrame(
        {
            "route_uid": ["fa:r1"], "feed_id": ["fa"], "route_id": ["r1"],
            "linea": ["10"], "direction_id": [0],
            "stop_uid_a": ["fa:A"], "stop_uid_b": ["fa:B"],
            "travel_min": [4.0], "n_obs": [3],
        }
    )
    # Dedupe outputs: A and X1 are one physical station.
    equivalences = pd.DataFrame(
        {
            "group_id": [0], "canonical_uid": ["fa:A"],
            "stop_uid_a": ["fa:A"], "stop_uid_b": ["fb:X1"],
            "distance_m": [10.0], "name_score": [1.0],
            "shared_routes": [0], "score": [0.8],
        }
    )
    canonical = pd.DataFrame(
        {"stop_uid": ["fa:A", "fb:X1"], "canonical_uid": ["fa:A", "fa:A"], "group_id": [0, 0]}
    )
    # Proximity pairs: B <-> X2 (alias pair A/X1 already filtered by dedupe).
    stop_pairs = pd.DataFrame(
        {"stop_uid_a": ["fa:B"], "stop_uid_b": ["fb:X2"], "distance_m": [55.0]}
    )
    near = build_near_stations(stops, route_stops, routes, stop_pairs, stop_links=stop_links)
    return stops, near, stop_links, stop_pairs, equivalences, canonical


def test_nodes_carry_attributes_and_canonical_fallback() -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    graph = build_station_graph(stops, near, links, pairs, equiv, canonical)

    assert graph.number_of_nodes() == 4
    a = graph.nodes["fa:A"]
    assert a["name"] == "Main St" and a["feed"] == "fa"
    assert a["canonical_uid"] == "fa:A"
    assert graph.nodes["fb:X1"]["canonical_uid"] == "fa:A"     # alias mapped
    assert graph.nodes["fa:B"]["canonical_uid"] == "fa:B"      # fallback: own uid
    assert a["near_id"] == int(near.loc[near["stop_uid"] == "fa:A", "ID"].item())
    assert isinstance(a["lat"], float) and isinstance(a["lon"], float)


def test_typed_edges_and_reciprocity() -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    graph = build_station_graph(stops, near, links, pairs, equiv, canonical)

    # in_vehicle: directed, single arc.
    assert graph.has_edge("fa:A", "fa:B", key="in_vehicle:fa:r1:0")
    assert not graph.has_edge("fa:B", "fa:A", key="in_vehicle:fa:r1:0")
    iv = graph.edges["fa:A", "fa:B", "in_vehicle:fa:r1:0"]
    assert iv["time_min"] == 4.0 and iv["linea"] == "10"
    # walk + same_station: reciprocal arcs with equal attributes.
    assert graph.edges["fa:B", "fb:X2", "walk"] == graph.edges["fb:X2", "fa:B", "walk"]
    assert graph.edges["fa:A", "fb:X1", "same_station"]["time_min"] == 0.0
    assert graph.has_edge("fb:X1", "fa:A", "same_station")


def test_cross_feed_routing_through_alias() -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    graph = build_station_graph(stops, near, links, pairs, equiv, canonical)

    # fb:X1 -> fa:A (0, alias) -> fa:B (4.0 in vehicle) -> fb:X2 (1 min walk).
    total = nx.shortest_path_length(graph, "fb:X1", "fb:X2", weight="time_min")
    assert total == 5.0


def test_parallel_edges_coexist() -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    # A walk pair on the SAME stops as the in_vehicle link.
    pairs2 = pd.concat(
        [pairs, pd.DataFrame(
            {"stop_uid_a": ["fa:A"], "stop_uid_b": ["fa:B"], "distance_m": [995.0]}
        )],
        ignore_index=True,
    )
    graph = build_station_graph(stops, near, links, pairs2, equiv, canonical)
    keys = set(graph["fa:A"]["fa:B"])
    assert keys == {"in_vehicle:fa:r1:0", "walk"}                # both survive


def test_graphml_roundtrip(tmp_path: Path) -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    graph = build_station_graph(stops, near, links, pairs, equiv, canonical)

    path = write_station_graphml(graph, tmp_path / "station_network.graphml")
    reread = nx.read_graphml(path)
    assert reread.number_of_nodes() == graph.number_of_nodes()
    assert reread.number_of_edges() == graph.number_of_edges()
    kinds = {d["edge_type"] for _, _, d in reread.edges(data=True)}
    assert kinds == {"in_vehicle", "walk", "same_station"}


def test_stats_and_flat_frames() -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    graph = build_station_graph(stops, near, links, pairs, equiv, canonical)

    stats = station_graph_stats(graph)
    assert stats["n_stops"] == 4
    assert stats["edges_by_type"] == {"in_vehicle": 1, "same_station": 2, "walk": 2}
    assert stats["n_weak_components"] == 1                       # all connected

    edges = station_edges_frame(graph)
    assert list(edges.columns) == EDGE_COLUMNS
    # Reciprocal arcs collapse to one row per pair; in_vehicle per direction.
    assert edges["edge_type"].value_counts().to_dict() == {
        "in_vehicle": 1, "same_station": 1, "walk": 1,
    }
    nodes = station_nodes_frame(graph)
    assert list(nodes.columns) == NODE_COLUMNS
    assert len(nodes) == 4


def test_empty_inputs_yield_empty_graph(tmp_path: Path) -> None:
    import geopandas as gpd

    empty = gpd.GeoDataFrame(pd.DataFrame(), geometry=[], crs="EPSG:4326")
    graph = build_station_graph(
        empty, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    )
    assert graph.number_of_nodes() == 0
    write_station_graphml(graph, tmp_path / "empty.graphml")     # still writes
    assert station_edges_frame(graph).empty


def test_deterministic_construction() -> None:
    stops, near, links, pairs, equiv, canonical = _fixture()
    g1 = build_station_graph(stops, near, links, pairs, equiv, canonical)
    g2 = build_station_graph(stops, near, links, pairs, equiv, canonical)
    assert sorted(g1.edges(keys=True)) == sorted(g2.edges(keys=True))
    assert list(g1.nodes) == list(g2.nodes)
