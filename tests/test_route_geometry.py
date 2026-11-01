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
    assert s1.coords[0] == (-103.0, 20.0)  # ordered by shape_pt_sequence


def test_route_shapes_empty_without_shapes_txt() -> None:
    assert route_shapes(fake_feed(trips=TRIPS), "feedx").empty
    # unordered sequences are sorted, single-point shapes dropped
    shapes = pd.DataFrame(
        {
            "shape_id": ["only"],
            "shape_pt_lat": [20.0],
            "shape_pt_lon": [-103.0],
            "shape_pt_sequence": [1],
        }
    )
    assert route_shapes(fake_feed(shapes, TRIPS), "feedx").empty


def test_route_stop_links_follow_stop_sequence_not_proximity() -> None:
    stop_times = pd.DataFrame(
        {
            "trip_id": ["t1", "t1", "t1", "t2", "t2"],
            "stop_id": ["A", "B", "C", "C", "A"],
            "stop_sequence": [1, 2, 3, 1, 2],
            "departure_time": [0.0, 120.0, 300.0, 0.0, 240.0],
        }
    )
    links = route_stop_links(fake_feed(None, TRIPS, stop_times, ROUTES), "feedx")
    got = set(zip(links["stop_uid_a"], links["stop_uid_b"], links["direction_id"]))
    assert got == {
        ("feedx:A", "feedx:B", 0),
        ("feedx:B", "feedx:C", 0),
        ("feedx:C", "feedx:A", 1),  # reverse direction is its own edge
    }
    assert set(links["linea"]) == {"Línea 2"}
    # Scheduled segment times: median departure difference, in minutes.
    minutes = {
        (a, b): t for a, b, t in zip(links["stop_uid_a"], links["stop_uid_b"], links["travel_min"])
    }
    assert minutes[("feedx:A", "feedx:B")] == 2.0  # 120 s
    assert minutes[("feedx:B", "feedx:C")] == 3.0  # 180 s
    assert minutes[("feedx:C", "feedx:A")] == 4.0  # 240 s
    # n_obs = trips with a timed gap behind each median (sidecar provenance).
    assert "n_obs" in links.columns
    assert set(links["n_obs"]) == {1}  # one timed trip per segment


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
    # No departure_time at all: travel_min NULL and zero observations.
    assert pd.isna(links["travel_min"].iloc[0])
    assert links["n_obs"].iloc[0] == 0


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
        {
            "route_uid": ["feedx:R1"],
            "feed_id": ["feedx"],
            "route_id": ["R1"],
            "linea": ["L1"],
            "direction_id": [0],
            "shape_id": ["s1"],
        },
        geometry=[LineString([(-103.0, 20.0), (-103.1, 20.1)])],
        crs="EPSG:4326",
    )
    links = pd.DataFrame(
        {
            "route_uid": ["feedx:R1"],
            "feed_id": ["feedx"],
            "route_id": ["R1"],
            "linea": ["L1"],
            "direction_id": [0],
            "stop_uid_a": ["feedx:A"],
            "stop_uid_b": ["feedx:B"],
        }
    )
    path = write_geopackage(
        stops, near, pairs, tmp_path, route_shapes=shapes_layer, stop_links=links
    )

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
    out = build_near_stations(
        stops, route_stops, routes, stop_pairs, stop_links=stop_links
    ).set_index("stop_uid")

    # B is the middle stop: its same-line neighbours are A (prev) and C (next).
    # IDs are the sequential file ids (A=1, B=2, C=3 in row order).
    b = out.loc["f:B", "same_line_stations"]
    assert "1, Alfa, L1, dir0" in b and "3, Gamma, L1, dir0" in b
    # A is an endpoint: only B is adjacent. The fixture links carry no
    # schedule times, so minutes fall back to ~1.11 km at 25 km/h = 2.7.
    a = out.loc["f:A", "same_line_stations"]
    assert a.count("(") == 1 and "2, Beta, L1, dir0" in a
    assert ", 2.7)" in a
    # near_stations (proximity) stays empty - no stop_pairs given.
    assert out.loc["f:A", "near_stations"] == "[]"


def test_coverage_rows_to_frame() -> None:
    rows = [
        {
            "osm_id": 1,
            "kind": "bus_stop",
            "osm_name": "Parada X",
            "nearest_stop_m": 512.345,
            "lat": 20.5,
            "lon": -103.4,
        },
    ]
    gdf = rows_to_frame(rows)
    assert gdf.crs.to_epsg() == 4326
    assert gdf["nearest_stop_m"].iloc[0] == 512.3
    assert gdf.geometry.iloc[0].x == pytest.approx(-103.4)
    assert rows_to_frame([]).empty


def test_two_stops_on_one_sequence_give_one_arc_not_a_zig_zag() -> None:
    from gtfs_ingest.analysis.route_geometry import ordered_stop_times

    # A1/A2 and B1/B2 are alias pairs listed under one stop_sequence each.
    stop_times = pd.DataFrame(
        {
            "trip_id": ["t1"] * 4,
            "stop_id": ["A2", "A1", "B1", "B2"],
            "stop_sequence": [1, 1, 2, 2],
            "departure_time": [0.0, 0.0, 120.0, 120.0],
        }
    )
    trips = pd.DataFrame({"trip_id": ["t1"], "route_id": ["R1"], "direction_id": [0]})
    links = route_stop_links(fake_feed(None, trips, stop_times), "feedx")
    assert list(zip(links["stop_uid_a"], links["stop_uid_b"])) == [("feedx:A1", "feedx:B1")]
    assert links["travel_min"].iloc[0] == 2.0

    _, dropped = ordered_stop_times(stop_times)
    assert sorted(zip(dropped["stop_id"], dropped["kept_stop_id"])) == [
        ("A2", "A1"),
        ("B2", "B1"),
    ]
    shuffled = route_stop_links(fake_feed(None, trips, stop_times.iloc[::-1]), "feedx")
    pd.testing.assert_frame_equal(shuffled, links)


def test_route_trip_minutes_is_the_median_trip_duration() -> None:
    from gtfs_ingest.analysis.route_geometry import route_trip_minutes

    stop_times = pd.DataFrame(
        {
            "trip_id": ["t1", "t1", "t2", "t2", "t3", "t3"],
            "stop_id": ["A", "B", "A", "B", "B", "A"],
            "stop_sequence": [1, 2, 1, 2, 1, 2],
            "departure_time": [0.0, 600.0, 0.0, 1200.0, 0.0, 300.0],
        }
    )
    trips = pd.DataFrame(
        {"trip_id": ["t1", "t2", "t3"], "route_id": ["R1"] * 3, "direction_id": [0, 0, 1]}
    )
    minutes = route_trip_minutes(fake_feed(None, trips, stop_times), "feedx")
    got = dict(zip(zip(minutes["route_uid"], minutes["direction_id"]), minutes["trip_min"]))
    assert got == {("feedx:R1", 0): 15.0, ("feedx:R1", 1): 5.0}


def test_link_time_quality_labels() -> None:
    from gtfs_ingest.analysis.route_geometry import link_time_quality

    # B is ~1.1 km north of A; C is next to B.
    stops = pd.DataFrame(
        {
            "stop_uid": ["f:A", "f:B", "f:C"],
            "stop_lat": [20.0, 20.01, 20.0101],
            "stop_lon": [-103.0, -103.0, -103.0],
        }
    )
    links = pd.DataFrame(
        {
            "route_uid": ["f:R1", "f:R1", "f:R1", "f:R1", "f:SLOW"],
            "direction_id": [0, 0, 0, 0, 0],
            "stop_uid_a": ["f:A", "f:A", "f:B", "f:A", "f:A"],
            "stop_uid_b": ["f:B", "f:B", "f:C", "f:B", "f:B"],
            # 2 min = 33 km/h, 0.5 min = 133 km/h, 0 s, no time, slow route.
            "travel_min": [2.0, 0.5, 0.0, None, 2.0],
        }
    )
    shapes = gpd.GeoDataFrame(
        {"route_uid": ["f:SLOW"], "direction_id": [0]},
        geometry=[LineString([(-103.0, 20.0), (-103.0, 20.01)])],
        crs="EPSG:4326",
    )
    # The SLOW route needs an hour for its 1.1 km shape.
    trip_minutes = pd.DataFrame({"route_uid": ["f:SLOW"], "direction_id": [0], "trip_min": [60.0]})
    labels = link_time_quality(links, stops, shapes, trip_minutes)
    assert labels.tolist() == ["ok", "too_fast", "zero", "untimed", "too_slow"]


def test_analyze_reports_stops_sharing_a_sequence(tmp_path: Path) -> None:
    import json

    from gtfs_ingest import cli
    from gtfs_ingest.analysis.pipeline_phase2 import JSON_NAME

    from conftest import feed_files, write_feed_dir

    # S1 and its alias S1B (0 m apart) share stop_sequence 1.
    feed = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "S1,Main,40.0,-75.0,0,\nS1B,Main,40.0,-75.0,0,\nS3,End,40.01,-75.0,0,\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,S1B,1\nT1,08:00:00,08:00:00,S1,1\n"
                "T1,08:10:00,08:10:00,S3,2\n"
            ),
        }
    )
    write_feed_dir(tmp_path / "feeds", "metro", feed)
    out = tmp_path / "out"
    args = ["analyze-frequency", "--feeds", str(tmp_path / "feeds"), "--report", str(out)]
    assert cli.main([*args, "--service-date", "2026-01-06", "--dry-run"]) == 0

    (result,) = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))["feeds"]
    assert result["status"] == "analyzed"
    assert result["duplicate_sequence_rows"] == 1
    assert any("at most 0 m apart" in note for note in result["notes"])
    edges = pd.read_csv(out / "station_network_edges.csv")
    rides = edges[edges["edge_type"] == "in_vehicle"]
    assert list(zip(rides["stop_uid_a"], rides["stop_uid_b"])) == [("metro:S1", "metro:S3")]
