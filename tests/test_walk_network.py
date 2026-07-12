"""Street-network walking distances (synthetic graph - no DB, no network)."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

from gtfs_ingest.analysis.walk_network import (
    _finalize_pairs,
    build_walk_graph,
    pair_network_distances,
)


def graph_from(*lines: LineString):
    return build_walk_graph(gpd.GeoSeries(list(lines), crs="EPSG:32613"))


def test_graph_merges_shared_vertices() -> None:
    # Two ways sharing the corner (100, 0) must connect there.
    node_xy, indptr, indices, weights = graph_from(
        LineString([(0, 0), (100, 0)]),
        LineString([(100, 0), (100, 80)]),
    )
    assert len(node_xy) == 3                      # corner merged, not duplicated
    assert len(indices) == 4                      # 2 undirected edges
    assert weights.sum() == pytest.approx(2 * (100 + 80))


def test_pair_distance_follows_streets_not_crow_flies() -> None:
    # An L-shaped street: stops at both ends are 128 m apart straight-line,
    # but walking the L is 100 + 80 = 180 m.
    node_xy, indptr, indices, weights = graph_from(
        LineString([(0, 0), (100, 0)]),
        LineString([(100, 0), (100, 80)]),
    )
    stop_xy = pd.DataFrame({"x": [0.0, 100.0], "y": [0.0, 80.0]}, index=["f:A", "f:B"])
    pairs = pd.DataFrame(
        {"stop_uid_a": ["f:A"], "stop_uid_b": ["f:B"], "distance_m": [128.06]}
    )
    out = pair_network_distances(node_xy, indptr, indices, weights, stop_xy, pairs)
    assert out["network_m"].iloc[0] == pytest.approx(180.0, abs=0.5)


def test_barrier_without_crossing_falls_back_to_nan() -> None:
    # Two parallel streets with NO connection: neighbours as the crow flies,
    # unreachable on the network within the cutoff.
    node_xy, indptr, indices, weights = graph_from(
        LineString([(0, 0), (200, 0)]),
        LineString([(0, 60), (200, 60)]),
    )
    stop_xy = pd.DataFrame({"x": [100.0, 100.0], "y": [0.0, 60.0]}, index=["f:A", "f:B"])
    pairs = pd.DataFrame(
        {"stop_uid_a": ["f:A"], "stop_uid_b": ["f:B"], "distance_m": [60.0]}
    )
    out = pair_network_distances(node_xy, indptr, indices, weights, stop_xy, pairs,
                                 cutoff_m=800.0)
    assert np.isnan(out["network_m"].iloc[0])     # caller keeps 60 m straight line


def test_snap_offsets_are_added() -> None:
    # A stop 10 m off the street: the snap distance counts toward the walk.
    node_xy, indptr, indices, weights = graph_from(LineString([(0, 0), (100, 0)]))
    stop_xy = pd.DataFrame({"x": [0.0, 100.0], "y": [10.0, 0.0]}, index=["f:A", "f:B"])
    pairs = pd.DataFrame(
        {"stop_uid_a": ["f:A"], "stop_uid_b": ["f:B"], "distance_m": [100.5]}
    )
    out = pair_network_distances(node_xy, indptr, indices, weights, stop_xy, pairs)
    assert out["network_m"].iloc[0] == pytest.approx(110.0, abs=0.5)  # 100 + 10 snap
    # Per-endpoint snap distances are exposed for the quality sidecar.
    assert out["snap_m_a"].iloc[0] == pytest.approx(10.0, abs=0.1)
    assert out["snap_m_b"].iloc[0] == pytest.approx(0.0, abs=0.1)


def test_empty_graph_returns_all_nan() -> None:
    node_xy, indptr, indices, weights = build_walk_graph(
        gpd.GeoSeries([], crs="EPSG:32613")
    )
    pairs = pd.DataFrame(
        {"stop_uid_a": ["f:A"], "stop_uid_b": ["f:B"], "distance_m": [50.0]}
    )
    stop_xy = pd.DataFrame({"x": [0.0], "y": [0.0]}, index=["f:A"])
    out = pair_network_distances(node_xy, indptr, indices, weights, stop_xy, pairs)
    assert out["network_m"].isna().all()
    assert out["snap_m_a"].isna().all() and out["snap_m_b"].isna().all()


def test_finalize_pairs_labels_walk_source() -> None:
    # One routed pair (network wins, clipped to >= line) and one fallback.
    pairs = pd.DataFrame(
        {
            "stop_uid_a": ["f:A", "f:C"],
            "stop_uid_b": ["f:B", "f:D"],
            "distance_m": [100.0, 60.0],
            "network_m": [95.0, np.nan],   # 95 < line -> clipped up to 100
        }
    )
    out = _finalize_pairs(pairs)
    assert list(out["walk_source"]) == ["network", "straight_line"]
    assert list(out["line_m"]) == [100.0, 60.0]
    assert list(out["distance_m"]) == [100.0, 60.0]   # clip + fallback kept
    assert "network_m" not in out.columns


def test_cli_accepts_network_walk_flag() -> None:
    from gtfs_ingest.cli import build_parser

    args = build_parser().parse_args(
        ["analyze-frequency", "--feeds", "x.zip", "--network-walk"]
    )
    assert args.network_walk is True
