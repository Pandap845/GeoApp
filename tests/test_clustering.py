"""Interchange clustering tests: multimodal detection, parent merge, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

from gtfs_ingest.conflation.clustering import (
    build_interchanges,
    cluster_stops,
    normalize_route_type,
)
from gtfs_ingest.conflation.transfers_check import check_transfers

from conftest import feed_files, write_feed_dir


def stops_gdf(*rows: tuple[str, str, float, float, str | None]) -> gpd.GeoDataFrame:
    """Rows of (feed_id, stop_id, lat, lon, parent_station), normalizer-shaped."""
    df = pd.DataFrame(
        list(rows), columns=["feed_id", "stop_id", "stop_lat", "stop_lon", "parent_station"]
    )
    df["stop_uid"] = df["feed_id"] + ":" + df["stop_id"]
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["stop_lon"], df["stop_lat"]), crs="EPSG:4326"
    )


def rs_routes(*rows: tuple[str, str, str, object]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rows of (feed_id, route_id, stop_id, route_type) -> (route_stops, routes)."""
    df = pd.DataFrame(list(rows), columns=["feed_id", "route_id", "stop_id", "route_type"])
    df["route_uid"] = df["feed_id"] + ":" + df["route_id"]
    df["stop_uid"] = df["feed_id"] + ":" + df["stop_id"]
    route_stops = df[["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"]]
    routes = df[["route_uid", "route_type"]].drop_duplicates("route_uid")
    return route_stops, routes


def test_bus_rail_cluster_is_multimodal() -> None:
    # A bus stop and a rail stop ~55 m apart; a lone bus stop far away.
    stops = stops_gdf(
        ("f", "BUS1", 40.0, -75.0, None),
        ("f", "RAIL1", 40.0005, -75.0, None),
        ("f", "LONELY", 41.0, -76.0, None),
    )
    route_stops, routes = rs_routes(
        ("f", "B10", "BUS1", 3),      # bus
        ("f", "T1", "RAIL1", 2),      # rail
        ("f", "B99", "LONELY", 3),
    )
    labels = cluster_stops(stops, eps_m=120, min_samples=2)
    clusters = build_interchanges(stops, labels, route_stops, routes)

    assert len(clusters) == 1
    (cluster,) = clusters.to_dict("records")
    assert cluster["is_multimodal"] is True
    assert cluster["mode_names"] == ["rail", "bus"] or cluster["mode_names"] == ["bus", "rail"]
    assert set(cluster["member_stop_uids"]) == {"f:BUS1", "f:RAIL1"}
    assert cluster["stop_count"] == 2
    assert cluster["centroid_wkt"].startswith("POINT")


def test_bus_only_cluster_is_not_multimodal() -> None:
    stops = stops_gdf(
        ("f", "A", 40.0, -75.0, None),
        ("f", "B", 40.0006, -75.0, None),
    )
    route_stops, routes = rs_routes(
        ("f", "B10", "A", 3),
        ("f", "B20", "B", 700),   # extended bus code normalizes to bus too
    )
    clusters = build_interchanges(
        stops, cluster_stops(stops, eps_m=120, min_samples=2), route_stops, routes
    )
    (cluster,) = clusters.to_dict("records")
    assert cluster["is_multimodal"] is False
    assert cluster["mode_names"] == ["bus"]
    assert set(cluster["routes"]) == {"f:B10", "f:B20"}


def test_parent_station_merges_stops_beyond_eps() -> None:
    # 400 m apart: DBSCAN(eps=120) alone would never join them, the shared
    # parent_station must.
    stops = stops_gdf(
        ("f", "P1", 40.0, -75.0, "STATION"),
        ("f", "P2", 40.0036, -75.0, "STATION"),
    )
    labels = cluster_stops(stops, eps_m=120, min_samples=2)
    assert labels[0] == labels[1] != -1


def test_unclustered_stops_get_minus_one() -> None:
    stops = stops_gdf(
        ("f", "A", 40.0, -75.0, None),
        ("f", "B", 41.0, -76.0, None),   # ~140 km away
    )
    labels = cluster_stops(stops, eps_m=120, min_samples=2)
    assert list(labels) == [-1, -1]
    assert build_interchanges(stops, labels, *rs_routes(("f", "R", "A", 3))).empty


def test_normalize_route_type_extended_codes() -> None:
    assert normalize_route_type(3) == 3
    assert normalize_route_type("2") == 2
    assert normalize_route_type(700) == 3      # extended bus -> bus
    assert normalize_route_type(109) == 2      # suburban railway -> rail
    assert normalize_route_type(401) == 1      # metro
    assert normalize_route_type("junk") is None
    assert normalize_route_type(None) is None


def test_transfers_check_agreement_and_misses() -> None:
    transfers = pd.DataFrame(
        {
            "from_stop_id": ["BUS1", "BUS1", ""],
            "to_stop_id": ["RAIL1", "FARAWAY", "X"],
        }
    )
    result = check_transfers(
        {"f": transfers, "g": None},
        cluster_members=[["f:BUS1", "f:RAIL1"], ["f:C1", "f:C2"]],
    )
    assert result.feeds_with_transfers == ["f"]
    assert result.feeds_without_transfers == ["g"]
    assert result.declared_pairs == 2
    assert result.declared_co_clustered == 1          # BUS1 <-> RAIL1
    assert result.declared_not_co_clustered == 1      # BUS1 <-> FARAWAY
    assert result.clusters_with_declared == 1
    assert result.clusters_without_declared == 1
    assert result.not_inferred_examples == ["f:BUS1 <-> f:FARAWAY"]


def test_detect_interchanges_cli_dry_run(tmp_path: Path) -> None:
    """End-to-end: bus + rail feed -> one multimodal cluster in the report."""
    from gtfs_ingest import cli

    files = feed_files(
        replace={
            "stops.txt": (
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "S1,Bus Stop,40.0,-75.0,0,\n"
                "S4,Rail Halt,40.0005,-75.0,0,\n"
            ),
            "routes.txt": (
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                "R1,A1,10,Main Bus,3\n"
                "R2,A1,RE1,Regional Rail,2\n"
            ),
            "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\nR2,WK,T2\n",
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,S1,1\n"
                "T2,08:05:00,08:05:00,S4,1\n"
            ),
            "transfers.txt": "from_stop_id,to_stop_id,transfer_type\nS1,S4,0\n",
        }
    )
    write_feed_dir(tmp_path / "feeds", "hub", files)
    out = tmp_path / "out"

    exit_code = cli.main(
        [
            "detect-interchanges",
            "--feeds", str(tmp_path / "feeds"),
            "--report", str(out),
            "--eps", "120",
            "--dry-run",
        ]
    )
    assert exit_code == 0

    report = json.loads((out / "interchanges_report.json").read_text(encoding="utf-8"))
    assert report["clusters_total"] == 1
    assert report["multimodal_count"] == 1
    assert report["mode_combinations"] == {"bus+rail": 1}
    assert report["transfers"]["declared_pairs"] == 1
    assert report["transfers"]["declared_co_clustered"] == 1
    md = (out / "interchanges_report.md").read_text(encoding="utf-8")
    assert "bus+rail" in md
