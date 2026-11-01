"""Route-connectivity graph: build it, measure it, write it as GraphML.

Nodes are route_uids; every analyzed route is a node, so unconnected routes show up.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

log = logging.getLogger(__name__)


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop None/NaN values, which GraphML cannot store."""
    return {
        key: value
        for key, value in attrs.items()
        if value is not None and pd.isna(value) is not True
    }


def build_graph(routes: pd.DataFrame, edges: pd.DataFrame) -> nx.Graph:
    """Undirected graph: one node per analyzed route, one edge per connected route pair."""
    graph = nx.Graph()
    for row in routes.to_dict("records"):
        uid = row.pop("route_uid")
        graph.add_node(uid, **_clean_attrs(row))
    for row in edges.to_dict("records"):
        a, b = row.pop("route_a"), row.pop("route_b")
        graph.add_edge(a, b, **_clean_attrs(row))
    return graph


def graph_metrics(graph: nx.Graph) -> dict[str, Any]:
    """Counts, component sizes, isolated routes and per-route degree for the report."""
    components = [len(c) for c in nx.connected_components(graph)]
    return {
        "n_routes": graph.number_of_nodes(),
        "n_edges": graph.number_of_edges(),
        "n_components": len(components),
        "component_sizes": sorted(components, reverse=True),
        "isolated_routes": sorted(n for n, d in graph.degree() if d == 0),
        "degree": {n: int(d) for n, d in sorted(graph.degree())},
    }


def write_graphml(graph: nx.Graph, path: Path) -> Path:
    """Write the graph as GraphML, creating parent folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, path)
    log.info(
        "GraphML written: %s (%d nodes, %d edges)",
        path,
        graph.number_of_nodes(),
        graph.number_of_edges(),
    )
    return path
