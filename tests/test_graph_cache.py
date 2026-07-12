"""Walk-graph cache: keying, roundtrip, corruption, and integration."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from gtfs_ingest.analysis import graph_cache, walk_network


def _key(**overrides) -> str:
    parts = dict(
        feed_ids=["fb", "fa"],
        buffer_m=1000.0,
        line_table="planet_osm_line",
        geom_column="way",
        excluded=("motorway", "proposed"),
        metric_crs="EPSG:32613",
        probes=(120000, 987654, "2026-07-11T10:00:00"),
    )
    parts.update(overrides)
    return graph_cache.cache_key(**parts)


def test_cache_key_deterministic_and_feed_order_independent() -> None:
    assert _key() == _key()
    assert _key(feed_ids=["fa", "fb"]) == _key(feed_ids=["fb", "fa"])  # sorted


def test_cache_key_sensitive_to_every_component() -> None:
    base = _key()
    assert _key(buffer_m=500.0) != base
    assert _key(line_table="other_lines") != base
    assert _key(geom_column="geom") != base
    assert _key(excluded=("motorway",)) != base
    assert _key(metric_crs="EPSG:32614") != base
    assert _key(probes=(120001, 987654, "2026-07-11T10:00:00")) != base   # OSM changed
    assert _key(probes=(120000, 987654, "2026-07-12T09:00:00")) != base   # stops reloaded


def test_save_load_roundtrip_preserves_arrays(tmp_path: Path) -> None:
    node_xy = np.array([[0.0, 0.0], [100.0, 0.0]])
    indptr = np.array([0, 1, 2])
    indices = np.array([1, 0], dtype=np.int64)
    weights = np.array([100.0, 100.0])

    key = _key()
    graph_cache.save(tmp_path, key, node_xy, indptr, indices, weights)
    loaded = graph_cache.load(tmp_path, key)

    assert loaded is not None
    for original, reread in zip((node_xy, indptr, indices, weights), loaded):
        np.testing.assert_array_equal(original, reread)
        assert original.dtype == reread.dtype


def test_load_misses_are_none(tmp_path: Path) -> None:
    assert graph_cache.load(tmp_path, _key()) is None            # nothing saved
    # Corrupt file: treated as a miss, never an error.
    key = _key()
    path = tmp_path / f"walkgraph-{key[:12]}.npz"
    path.write_bytes(b"not an npz at all")
    assert graph_cache.load(tmp_path, key) is None


def test_network_distances_uses_cache(tmp_path: Path, monkeypatch) -> None:
    """Second run with the same key loads the graph without fetching OSM."""
    from shapely.geometry import LineString
    import geopandas as gpd

    lines = gpd.GeoSeries([LineString([(0, 0), (100, 0)])], crs="EPSG:32613")
    fetch_calls = {"n": 0}

    def fake_fetch(engine, feed_ids, buffer_m=1000.0, line_table="planet_osm_line",
                   geom_column="way"):
        fetch_calls["n"] += 1
        return lines

    monkeypatch.setattr(walk_network, "fetch_walk_lines", fake_fetch)
    monkeypatch.setattr(
        graph_cache, "probe_state", lambda engine, feeds, table: (1, 42, "t0")
    )

    stops = gpd.GeoDataFrame(
        {"stop_uid": ["f:A", "f:B"]},
        geometry=gpd.points_from_xy([0.0, 100.0], [0.0, 0.0]),
        crs="EPSG:32613",
    )
    pairs = pd.DataFrame(
        {"stop_uid_a": ["f:A"], "stop_uid_b": ["f:B"], "distance_m": [100.0]}
    )

    first = walk_network.network_distances(
        object(), stops, pairs.copy(), ["f"], metric_crs="EPSG:32613",
        cache_dir=tmp_path,
    )
    assert fetch_calls["n"] == 1
    assert list(tmp_path.glob("walkgraph-*.npz"))                # cached

    second = walk_network.network_distances(
        object(), stops, pairs.copy(), ["f"], metric_crs="EPSG:32613",
        cache_dir=tmp_path,
    )
    assert fetch_calls["n"] == 1                                 # cache hit: no fetch
    pd.testing.assert_frame_equal(first, second)
    assert first["walk_source"].tolist() == ["network"]

    # cache_dir=None never touches the cache (rebuilds every time).
    walk_network.network_distances(
        object(), stops, pairs.copy(), ["f"], metric_crs="EPSG:32613", cache_dir=None,
    )
    assert fetch_calls["n"] == 2
