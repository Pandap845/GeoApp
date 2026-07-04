"""networkx graph construction, metrics, and GraphML export.

Nodes are ``route_uid`` strings carrying feed/route metadata; edges carry
the connectivity attributes. Isolated routes (analyzed but connected to
nothing) are explicit nodes so they show up in the metrics and the export.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

log = logging.getLogger(__name__)


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop None/NaN/NA values - GraphML cannot serialize them."""
    return {
        key: value
        for key, value in attrs.items()
        if value is not None and not (pd.isna(value) is True)
    }


def build_graph(routes: pd.DataFrame, edges: pd.DataFrame) -> nx.Graph:
    """Build the undirected route-connectivity graph.

    Args:
        routes: one row per analyzed route with ``route_uid, feed_id,
            route_id`` and optional ``route_short_name, route_long_name,
            route_type`` metadata; ALL analyzed routes become nodes, so
            unconnected ones appear as isolates.
        edges: edge list from ``connectivity.build_edges``.

    Returns:
        ``networkx.Graph`` with attribute-carrying nodes and edges.
    """
    graph = nx.Graph()
    for row in routes.to_dict("records"):
        uid = row.pop("route_uid")
        graph.add_node(uid, **_clean_attrs(row))
    for row in edges.to_dict("records"):
        a, b = row.pop("route_a"), row.pop("route_b")
        graph.add_edge(a, b, **_clean_attrs(row))
    return graph


def graph_metrics(graph: nx.Graph) -> dict[str, Any]:
    """Basic connectivity metrics for the report.

    Args:
        graph: graph from :func:`build_graph`.

    Returns:
        Dict with ``n_routes``, ``n_edges``, ``n_components``,
        ``component_sizes`` (descending), ``isolated_routes`` (degree 0,
        sorted) and ``degree`` (route_uid -> connection count).
    """
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
    """Write the graph as GraphML (attributes already sanitized).

    Args:
        graph: graph from :func:`build_graph`.
        path: target file path; parent directories are created.

    Returns:
        The written path.

    Raises:
        OSError: if the file cannot be written (run-level problem).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, path)
    log.info("GraphML written: %s (%d nodes, %d edges)", path, graph.number_of_nodes(), graph.number_of_edges())
    return path
