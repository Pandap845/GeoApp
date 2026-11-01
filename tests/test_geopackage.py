"""GeoPackage export tests (layers, attributes, round-trip)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from test_near_stations import make_stops, routes_meta

from gtfs_ingest.analysis import connectivity
from gtfs_ingest.analysis.geopackage import GEOPACKAGE_NAME, write_geopackage
from gtfs_ingest.analysis.near_stations import build_near_stations
from gtfs_ingest.models import numeric_stop_id

from test_connectivity import rs


def test_geopackage_two_layers_roundtrip(tmp_path: Path) -> None:
    stops = make_stops(
        ("f", "S1", "First Stop", 40.0, -75.0),
        ("f", "S2", "Second Stop", 40.0009, -75.0),
        ("f", "S9", "Far Away", 41.0, -76.0),
    )
    stops["location_type"] = 0
    stops["parent_station"] = None
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S2"), ("f", "r9", "S9"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"), ("f", "r9", "90"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    near = build_near_stations(stops, route_stops, routes, pairs)

    path = write_geopackage(stops, near, pairs, tmp_path)
    assert path.name == GEOPACKAGE_NAME

    stops_layer = gpd.read_file(path, layer="stops")
    assert len(stops_layer) == 3
    assert stops_layer.geometry.geom_type.eq("Point").all()
    s1 = stops_layer[stops_layer["stop_uid"] == "f:S1"].iloc[0]
    assert int(s1["stop_num_id"]) == numeric_stop_id("f:S1")  # int64 survives GPKG
    assert int(s1["ID"]) == 1  # sequential file id
    # Relation strings reference the sequential ID (S1=1, S2=2 in row order).
    assert s1["stop_line_ids"] == "1-10"
    assert s1["near_stations"].startswith("[(2,")

    links = gpd.read_file(path, layer="near_links")
    assert len(links) == 1  # only S1<->S2 are near
    link = links.iloc[0]
    assert link.geometry.geom_type == "LineString"
    assert {int(link["num_id_a"]), int(link["num_id_b"])} == {
        numeric_stop_id("f:S1"),
        numeric_stop_id("f:S2"),
    }
    assert 90 <= link["distance_m"] <= 110
    assert int(link["walk_min"]) == 1


def test_geopackage_layer_schemas_ignore_provenance_columns(tmp_path: Path) -> None:
    """Quality-sidecar columns on the input frames never reach the layers."""
    import pandas as pd

    stops = make_stops(
        ("f", "S1", "First Stop", 40.0, -75.0),
        ("f", "S2", "Second Stop", 40.0009, -75.0),
    )
    stops["location_type"] = 0
    stops["parent_station"] = None
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S2"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    near = build_near_stations(stops, route_stops, routes, pairs)

    plain_path = write_geopackage(stops, near, pairs, tmp_path / "plain")
    plain_links = gpd.read_file(plain_path, layer="near_links")

    annotated = pairs.assign(
        walk_source="network", snap_m_a=5.0, snap_m_b=5.0, line_m=pairs["distance_m"]
    )
    stop_links = pd.DataFrame(
        {
            "route_uid": ["f:r1"],
            "feed_id": ["f"],
            "route_id": ["r1"],
            "linea": ["10"],
            "direction_id": [0],
            "stop_uid_a": ["f:S1"],
            "stop_uid_b": ["f:S2"],
            "travel_min": [4.0],
            "n_obs": [7],
        }
    )
    path = write_geopackage(stops, near, annotated, tmp_path / "annotated", stop_links=stop_links)

    links = gpd.read_file(path, layer="near_links")
    assert list(links.columns) == list(plain_links.columns)  # schema unchanged
    route_links = gpd.read_file(path, layer="route_stop_links")
    assert "n_obs" not in route_links.columns
    assert "walk_source" not in links.columns


def test_geopackage_rewrites_cleanly(tmp_path: Path) -> None:
    """A second run replaces the file instead of accumulating stale layers."""
    stops = make_stops(("f", "S1", "Solo", 40.0, -75.0))
    stops["location_type"] = 0
    stops["parent_station"] = None
    route_stops = rs(("f", "r1", "S1"))
    routes = routes_meta(("f", "r1", "10"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    near = build_near_stations(stops, route_stops, routes, pairs)

    write_geopackage(stops, near, pairs, tmp_path)
    path = write_geopackage(stops, near, pairs, tmp_path)  # second run, same dir

    assert len(gpd.read_file(path, layer="stops")) == 1
    assert gpd.read_file(path, layer="near_links").empty
