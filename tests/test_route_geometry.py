"""Route shapes + consecutive-stop links + GeoPackage layers (no network,
no database - fake partridge feeds built from plain DataFrames)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from gtfs_ingest.analysis.geopackage import write_geopackage
from gtfs_ingest.analysis.route_geometry import route_shapes, route_stop_links
from gtfs_ingest.conflation.coverage import rows_to_frame


def fake_feed(shapes=None, trips=None, stop_times=None, routes=None) -> SimpleNamespace:
    empty = pd.DataFrame()
    return SimpleNamespace(
        shapes=shapes if shapes is not None else empty,
        trips=trips if trips is not None else empty,
        stop_times=stop_times if stop_times is not None else empty,
        routes=routes if routes is not None else empty,
    )


TRIPS = pd.DataFrame(
    {
        "trip_id": ["t1", "t2"],
        "route_id": ["R1", "R1"],
        "direction_id": [0, 1],
        "shape_id": ["s1", "s2"],
    }
)
ROUTES = pd.DataFrame({"route_id": ["R1"], "route_short_name": ["Línea 2"]})


def test_route_shapes_one_feature_per_route_direction_shape() -> None:
    shapes = pd.DataFrame(
        {
            "shape_id": ["s1", "s1", "s1", "s2", "s2"],
            "shape_pt_lat": [20.0, 20.1, 20.2, 20.2, 20.0],
            "shape_pt_lon": [-103.0, -103.1, -103.2, -103.2, -103.0],
            "shape_pt_sequence": [1, 2, 3, 1, 2],
        }
    )
    gdf = route_shapes(fake_feed(shapes, TRIPS, routes=ROUTES), "feedx")
    assert len(gdf) == 2
    assert set(gdf["route_uid"]) == {"feedx:R1"}
    assert set(gdf["direction_id"]) == {0, 1}
    assert list(gdf["linea"].unique()) == ["Línea 2"]
    s1 = gdf[gdf["shape_id"] == "s1"].geometry.iloc[0]
    assert list(s1.coords)[0] == (-103.0, 20.0)  # ordered by shape_pt_sequence


def test_route_shapes_empty_without_shapes_txt() -> None:
    assert route_shapes(fake_feed(trips=TRIPS), "feedx").empty
    # unordered sequences are sorted, single-point shapes dropped
    shapes = pd.DataFrame(
        {"shape_id": ["only"], "shape_pt_lat": [20.0],
         "shape_pt_lon": [-103.0], "shape_pt_sequence": [1]}
    )
    assert route_shapes(fake_feed(shapes, TRIPS), "feedx").empty


def test_route_stop_links_follow_stop_sequence_not_proximity() -> None:
    stop_times = pd.DataFrame(
        {
            "trip_id": ["t1", "t1", "t1", "t2", "t2"],
            "stop_id": ["A", "B", "C", "C", "A"],
            "stop_sequence": [1, 2, 3, 1, 2],
        }
    )
    links = route_stop_links(fake_feed(None, TRIPS, stop_times, ROUTES), "feedx")
    got = set(zip(links["stop_uid_a"], links["stop_uid_b"], links["direction_id"]))
    assert got == {
        ("feedx:A", "feedx:B", 0),
        ("feedx:B", "feedx:C", 0),
        ("feedx:C", "feedx:A", 1),   # reverse direction is its own edge
    }
    assert set(links["linea"]) == {"Línea 2"}


def test_route_stop_links_dedupes_repeated_trips() -> None:
    trips = pd.DataFrame(
        {"trip_id": ["t1", "t2"], "route_id": ["R1", "R1"], "direction_id": [0, 0]}
    )
    stop_times = pd.DataFrame(
        {
            "trip_id": ["t1", "t1", "t2", "t2"],
            "stop_id": ["A", "B", "A", "B"],
            "stop_sequence": [1, 2, 1, 2],
        }
    )
    links = route_stop_links(fake_feed(None, trips, stop_times), "feedx")
    assert len(links) == 1  # same segment served by many trips appears once
    assert links["linea"].iloc[0] == "R1"  # no routes.txt -> route_id fallback


def test_geopackage_gets_route_layers(tmp_path: Path) -> None:
    stops = gpd.GeoDataFrame(
        {
            "stop_num_id": [1, 2],
            "stop_uid": ["feedx:A", "feedx:B"],
            "feed_id": ["feedx", "feedx"],
            "stop_id": ["A", "B"],
            "location_type": [0, 0],
            "parent_station": [None, None],
        },
        geometry=[Point(-103.0, 20.0), Point(-103.1, 20.1)],
        crs="EPSG:4326",
    )
    near = pd.DataFrame()
    pairs = pd.DataFrame(columns=["stop_uid_a", "stop_uid_b", "distance_m"])
    shapes_layer = gpd.GeoDataFrame(
        {"route_uid": ["feedx:R1"], "feed_id": ["feedx"], "route_id": ["R1"],
         "linea": ["L1"], "direction_id": [0], "shape_id": ["s1"]},
        geometry=[LineString([(-103.0, 20.0), (-103.1, 20.1)])],
        crs="EPSG:4326",
    )
    links = pd.DataFrame(
        {"route_uid": ["feedx:R1"], "feed_id": ["feedx"], "route_id": ["R1"],
         "linea": ["L1"], "direction_id": [0],
         "stop_uid_a": ["feedx:A"], "stop_uid_b": ["feedx:B"]}
    )
    path = write_geopackage(stops, near, pairs, tmp_path,
                            route_shapes=shapes_layer, stop_links=links)

    import pyogrio

    layers = {name for name, _ in pyogrio.list_layers(path)}
    assert {"stops", "near_links", "route_shapes", "route_stop_links"} <= layers
    read = gpd.read_file(path, layer="route_stop_links")
    assert len(read) == 1
    assert read["stop_uid_a"].iloc[0] == "feedx:A"


def test_same_line_stations_lists_adjacent_stops_in_route_order() -> None:
    from gtfs_ingest.analysis.near_stations import build_near_stations

    # Three stops of one line; A-B-C in direction 0.
    stops = pd.DataFrame(
        {
            "stop_num_id": [11, 22, 33],
            "stop_uid": ["f:A", "f:B", "f:C"],
            "feed_id": ["f", "f", "f"],
            "stop_id": ["A", "B", "C"],
            "stop_name": ["Alfa", "Beta", "Gamma"],
            "stop_lat": [20.00, 20.01, 20.02],
            "stop_lon": [-103.0, -103.0, -103.0],
        }
    )
    route_stops = pd.DataFrame(
        {
            "stop_uid": ["f:A", "f:B", "f:C"],
            "route_uid": ["f:R1", "f:R1", "f:R1"],
        }
    )
    routes = pd.DataFrame({"route_uid": ["f:R1"], "route_id": ["R1"], "route_short_name": ["L1"]})
    stop_pairs = pd.DataFrame(columns=["stop_uid_a", "stop_uid_b", "distance_m"])
    stop_links = pd.DataFrame(
        {
            "route_uid": ["f:R1", "f:R1"],
            "feed_id": ["f", "f"],
            "linea": ["L1", "L1"],
            "direction_id": [0, 0],
            "stop_uid_a": ["f:A", "f:B"],
            "stop_uid_b": ["f:B", "f:C"],
        }
    )
    out = build_near_stations(stops, route_stops, routes, stop_pairs, stop_links=stop_links).set_index("stop_uid")

    # B is the middle stop: its same-line neighbours are A (prev) and C (next).
    # IDs are the sequential file ids (A=1, B=2, C=3 in row order).
    b = out.loc["f:B", "same_line_stations"]
    assert "1, Alfa, L1, dir0" in b and "3, Gamma, L1, dir0" in b
    # A is an endpoint: only B is adjacent; distance ~1.1 km (0.01 deg lat).
    a = out.loc["f:A", "same_line_stations"]
    assert a.count("(") == 1 and "2, Beta, L1, dir0" in a
    assert "1112.0m" in a or "1111." in a  # ~0.01 deg latitude
    # near_stations (proximity) stays empty - no stop_pairs given.
    assert out.loc["f:A", "near_stations"] == "[]"


def test_coverage_rows_to_frame() -> None:
    rows = [
        {"osm_id": 1, "kind": "bus_stop", "osm_name": "Parada X",
         "nearest_stop_m": 512.345, "lat": 20.5, "lon": -103.4},
    ]
    gdf = rows_to_frame(rows)
    assert gdf.crs.to_epsg() == 4326
    assert gdf["nearest_stop_m"].iloc[0] == 512.3
    assert gdf.geometry.iloc[0].x == pytest.approx(-103.4)
    assert rows_to_frame([]).empty
