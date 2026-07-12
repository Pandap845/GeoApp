"""Connectivity edge construction, graph export, and the analyze CLI."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd

from gtfs_ingest.analysis import connectivity
from gtfs_ingest.analysis.graph_export import build_graph, graph_metrics, write_graphml
from gtfs_ingest.analysis.pipeline_phase2 import GRAPHML_NAME, JSON_NAME, MARKDOWN_NAME

from test_headways import ANALYSIS_FEED
from conftest import write_feed_dir


def rs(*rows: tuple[str, str, str]) -> pd.DataFrame:
    """Route-stop map rows of (feed_id, route_id, stop_id)."""
    df = pd.DataFrame(list(rows), columns=["feed_id", "route_id", "stop_id"])
    df["route_uid"] = df["feed_id"] + ":" + df["route_id"]
    df["stop_uid"] = df["feed_id"] + ":" + df["stop_id"]
    return df[["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"]]


def stops_meta(*rows: tuple[str, str, str | None]) -> pd.DataFrame:
    """Stops rows of (feed_id, stop_id, parent_station) for station_map."""
    df = pd.DataFrame(list(rows), columns=["feed_id", "stop_id", "parent_station"])
    df["stop_uid"] = df["feed_id"] + ":" + df["stop_id"]
    return df


def stops_geo(*rows: tuple[str, str, float, float]) -> gpd.GeoDataFrame:
    """Stops rows of (feed_id, stop_id, lat, lon) as an EPSG:4326 frame."""
    df = pd.DataFrame(list(rows), columns=["feed_id", "stop_id", "stop_lat", "stop_lon"])
    df["stop_uid"] = df["feed_id"] + ":" + df["stop_id"]
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["stop_lon"], df["stop_lat"]), crs="EPSG:4326"
    )


def edge(edges: pd.DataFrame, a: str, b: str) -> dict:
    match = edges[(edges["route_a"] == a) & (edges["route_b"] == b)]
    assert len(match) == 1, f"expected exactly one edge {a} -- {b}, got {len(match)}"
    return match.iloc[0].to_dict()


def test_same_stop_connection() -> None:
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S1"), ("f", "r2", "S9"))
    edges = connectivity.build_edges(route_stops, connectivity.station_map(pd.DataFrame()))

    e = edge(edges, "f:r1", "f:r2")
    assert e["connection_type"] == "same_stop"
    assert e["shared_stop_count"] == 1
    assert e["min_stop_distance_m"] == 0.0
    assert (e["feed_id_a"], e["feed_id_b"]) == ("f", "f")


def test_same_parent_station_connection() -> None:
    # r3 serves platform S2a, r4 serves platform S2b; both belong to station P.
    route_stops = rs(("f", "r3", "S2a"), ("f", "r4", "S2b"))
    stations = connectivity.station_map(
        stops_meta(("f", "S2a", "P"), ("f", "S2b", "P"), ("f", "P", None))
    )
    edges = connectivity.build_edges(route_stops, stations)

    e = edge(edges, "f:r3", "f:r4")
    assert e["connection_type"] == "same_parent_station"
    assert e["shared_stop_count"] == 0
    assert pd.isna(e["min_stop_distance_m"])


def test_route_serving_station_itself_connects_to_platform_routes() -> None:
    # r5 stops at the parent station stop P directly; r3 uses its platform.
    route_stops = rs(("f", "r3", "S2a"), ("f", "r5", "P"))
    stations = connectivity.station_map(stops_meta(("f", "S2a", "P"), ("f", "P", None)))
    edges = connectivity.build_edges(route_stops, stations)
    assert edge(edges, "f:r3", "f:r5")["connection_type"] == "same_parent_station"


def test_proximity_connection_within_threshold_only() -> None:
    # Two stops ~100 m apart (0.0009 degrees of latitude).
    stops = stops_geo(("fa", "S5", 40.0, -75.0), ("fb", "S6", 40.0009, -75.0))
    route_stops = rs(("fa", "r5", "S5"), ("fb", "r6", "S6"))

    near = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    edges = connectivity.build_edges(route_stops, connectivity.station_map(pd.DataFrame()), near)
    e = edge(edges, "fa:r5", "fb:r6")
    assert e["connection_type"] == "proximity"
    assert 90 <= e["min_stop_distance_m"] <= 110       # ~100 m geodesically
    assert (e["feed_id_a"], e["feed_id_b"]) == ("fa", "fb")  # cross-feed works

    far = connectivity.proximity_pairs_local(stops, threshold_m=50.0)
    edges_far = connectivity.build_edges(route_stops, connectivity.station_map(pd.DataFrame()), far)
    assert edges_far.empty


def test_connection_type_precedence_same_stop_wins() -> None:
    # r1/r2 share S1 *and* are within proximity via S1/S9: same_stop wins.
    stops = stops_geo(("f", "S1", 40.0, -75.0), ("f", "S9", 40.0005, -75.0))
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S1"), ("f", "r2", "S9"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    edges = connectivity.build_edges(route_stops, connectivity.station_map(pd.DataFrame()), pairs)

    e = edge(edges, "f:r1", "f:r2")
    assert e["connection_type"] == "same_stop"
    assert e["min_stop_distance_m"] == 0.0


def test_dedup_station_connection_upgrades_cross_feed_proximity() -> None:
    # fa:S5 and fb:S6 are aliases of one physical station: with the canonical
    # map their routes connect as same_station_dedup, not mere proximity.
    route_stops = rs(("fa", "r5", "S5"), ("fb", "r6", "S6"))
    canonical = pd.DataFrame(
        {
            "stop_uid": ["fa:S5", "fb:S6"],
            "canonical_uid": ["fa:S5", "fa:S5"],
            "group_id": [0, 0],
        }
    )
    # The pipeline filters alias pairs out of stop_pairs before build_edges.
    edges = connectivity.build_edges(
        route_stops, connectivity.station_map(pd.DataFrame()), None, canonical
    )

    e = edge(edges, "fa:r5", "fb:r6")
    assert e["connection_type"] == "same_station_dedup"
    assert e["shared_stop_count"] == 0
    assert pd.isna(e["min_stop_distance_m"])
    assert (e["feed_id_a"], e["feed_id_b"]) == ("fa", "fb")


def test_same_parent_station_beats_dedup() -> None:
    # Declared GTFS station evidence outranks the inferred dedup merge.
    route_stops = rs(("f", "r3", "S2a"), ("f", "r4", "S2b"))
    stations = connectivity.station_map(
        stops_meta(("f", "S2a", "P"), ("f", "S2b", "P"), ("f", "P", None))
    )
    canonical = pd.DataFrame(
        {
            "stop_uid": ["f:S2a", "f:S2b"],
            "canonical_uid": ["f:S2a", "f:S2a"],
            "group_id": [0, 0],
        }
    )
    edges = connectivity.build_edges(route_stops, stations, None, canonical)
    assert edge(edges, "f:r3", "f:r4")["connection_type"] == "same_parent_station"


def test_graph_metrics_and_graphml_roundtrip(tmp_path: Path) -> None:
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S1"))
    edges = connectivity.build_edges(route_stops, connectivity.station_map(pd.DataFrame()))
    routes = pd.DataFrame(
        {
            "route_uid": ["f:r1", "f:r2", "f:r7"],   # r7 analyzed but unconnected
            "feed_id": ["f", "f", "f"],
            "route_id": ["r1", "r2", "r7"],
        }
    )

    graph = build_graph(routes, edges)
    metrics = graph_metrics(graph)
    assert metrics["n_routes"] == 3
    assert metrics["n_edges"] == 1
    assert metrics["n_components"] == 2
    assert metrics["isolated_routes"] == ["f:r7"]
    assert metrics["degree"]["f:r1"] == 1

    path = write_graphml(graph, tmp_path / "graph.graphml")
    reread = nx.read_graphml(path)
    assert set(reread.nodes) == {"f:r1", "f:r2", "f:r7"}
    assert reread.edges["f:r1", "f:r2"]["connection_type"] == "same_stop"


def test_analyze_frequency_cli_dry_run(tmp_path: Path) -> None:
    """End-to-end: CLI subcommand, dry run, reports + GraphML on disk."""
    from gtfs_ingest import cli

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    out = tmp_path / "out"
    exit_code = cli.main(
        [
            "analyze-frequency",
            "--feeds", str(tmp_path / "feeds"),
            "--report", str(out),
            "--service-date", "2026-01-06",
            "--dry-run",
        ]
    )
    assert exit_code == 0

    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    (feed,) = report["feeds"]
    assert feed["status"] == "analyzed"
    assert feed["service_date"] == "2026-01-06"
    assert feed["routes_analyzed"] == 2                 # R1 + R2 run that Tuesday
    assert report["connections_by_type"] == {"same_stop": 1}   # R1/R2 share S1
    assert report["graph"]["n_routes"] == 2

    assert (out / GRAPHML_NAME).is_file()
    md = (out / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert "## Route connectivity" in md and "am_peak" in md

    # Near-stations export: S3 (platform) sits ~14 m from S2 (its station).
    near = pd.read_csv(out / "near_stations.csv")
    s3_id = int(near.loc[near["stop_uid"] == "metro:S3", "ID"].item())
    s2 = near.loc[near["stop_uid"] == "metro:S2", "near_stations"].item()
    assert s2.startswith(f"[({s3_id}, Central Platform, ")
    assert ", 1)" in s2
    # S2 is an unserved station node, so it appears in no one's list.
    s3 = near.loc[near["stop_uid"] == "metro:S3", "near_stations"].item()
    assert s3 == "[]"
    # The sequential node ids start at 1 and cover every row.
    assert sorted(near["ID"]) == list(range(1, len(near) + 1))

    # GeoPackage export exists with both layers.
    import geopandas as gpd

    gpkg = out / "transit_analysis.gpkg"
    assert gpkg.is_file()
    assert len(gpd.read_file(gpkg, layer="stops")) == 3
    assert len(gpd.read_file(gpkg, layer="near_links")) == 1   # S2 <-> S3

    # Quality sidecar: same rows/IDs; dry run -> no OSM/no network data.
    quality = pd.read_csv(out / "near_stations_quality.csv")
    assert list(quality["ID"]) == list(near["ID"])
    assert set(quality["coord_confidence"]) == {"unavailable"}
    assert set(quality["walk_source"]) <= {"straight_line", "none"}
    assert report["quality_summary"]["walk_source"]
    assert "Estimate provenance" in md


#: Second feed whose only stop duplicates ANALYSIS_FEED's S1 ("Main St",
#: 40.0/-75.0) ~10 m north - a cross-feed alias of the same physical stop.
OTHER_FEED = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "A2,Other Transit,https://other.example,America/New_York\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "X1,Main St,40.00009,-75.0,0,\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "RX,A2,X9,Cross Town,3\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\nRX,WK,TX1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "TX1,08:00:00,08:00:00,X1,1\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}


def _run_analyze_two_feeds(tmp_path: Path, out_name: str, *extra: str) -> dict:
    from gtfs_ingest import cli

    feeds = tmp_path / "feeds"
    if not feeds.exists():
        write_feed_dir(feeds, "metro", ANALYSIS_FEED)
        write_feed_dir(feeds, "other", OTHER_FEED)
    out = tmp_path / out_name
    exit_code = cli.main(
        [
            "analyze-frequency",
            "--feeds", str(feeds),
            "--report", str(out),
            "--service-date", "2026-01-06",
            "--dry-run",
            *extra,
        ]
    )
    assert exit_code == 0
    return json.loads((out / JSON_NAME).read_text(encoding="utf-8"))


def test_analyze_cli_two_feeds_dedupe_default(tmp_path: Path) -> None:
    """Cross-feed alias: filtered from near-stations, upgraded in the graph."""
    report = _run_analyze_two_feeds(tmp_path, "out_dedupe")

    assert report["dedupe"] is True
    assert report["station_groups"] == 1
    assert report["stops_deduplicated"] == 2          # metro:S1 + other:X1
    # metro:R1/R2 both connect to other:RX through the deduped station.
    assert report["connections_by_type"] == {"same_stop": 1, "same_station_dedup": 2}

    out = tmp_path / "out_dedupe"
    equiv = pd.read_csv(out / "stop_equivalences.csv")
    (row,) = equiv.to_dict("records")
    assert (row["stop_uid_a"], row["stop_uid_b"]) == ("metro:S1", "other:X1")
    assert row["canonical_uid"] == "metro:S1"
    assert row["distance_m"] <= 30.0

    # The alias no longer appears in each other's near_stations lists.
    near = pd.read_csv(out / "near_stations.csv")
    assert near.loc[near["stop_uid"] == "metro:S1", "near_stations"].item() == "[]"
    assert near.loc[near["stop_uid"] == "other:X1", "near_stations"].item() == "[]"
    # Alias link filtered from the GeoPackage too: only S2 <-> S3 remains.
    assert len(gpd.read_file(out / "transit_analysis.gpkg", layer="near_links")) == 1

    # Quality sidecar carries the canonical id for exactly the alias stops.
    quality = pd.read_csv(out / "near_stations_quality.csv").fillna({"station_dedup": ""})
    dedup_of = dict(zip(quality["stop_uid"], quality["station_dedup"]))
    assert dedup_of["metro:S1"] == "metro:S1"
    assert dedup_of["other:X1"] == "metro:S1"
    assert dedup_of["metro:S3"] == ""


def test_analyze_cli_two_feeds_no_dedupe(tmp_path: Path) -> None:
    """--no-dedupe restores the alias as a proximity neighbour everywhere."""
    report_on = _run_analyze_two_feeds(tmp_path, "out_dedupe")
    report_off = _run_analyze_two_feeds(tmp_path, "out_nodedupe", "--no-dedupe")

    assert report_off["dedupe"] is False
    assert report_off["station_groups"] == 0
    assert report_off["connections_by_type"] == {"same_stop": 1, "proximity": 2}

    out_on, out_off = tmp_path / "out_dedupe", tmp_path / "out_nodedupe"
    assert not (out_off / "stop_equivalences.csv").exists()

    near_on = pd.read_csv(out_on / "near_stations.csv")
    near_off = pd.read_csv(out_off / "near_stations.csv")
    # The CSV contract is unchanged: same rows, same IDs, either way.
    assert list(near_on["ID"]) == list(near_off["ID"])
    assert list(near_on["stop_uid"]) == list(near_off["stop_uid"])
    # Without dedupe the alias is (wrongly) each other's nearest station.
    x1_id = int(near_off.loc[near_off["stop_uid"] == "other:X1", "ID"].item())
    s1 = near_off.loc[near_off["stop_uid"] == "metro:S1", "near_stations"].item()
    assert s1 == f"[({x1_id}, Main St, X9, 1)]"
    assert len(gpd.read_file(out_off / "transit_analysis.gpkg", layer="near_links")) == 2


def test_analyze_cli_rejects_bad_service_date(tmp_path: Path) -> None:
    from gtfs_ingest import cli

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    exit_code = cli.main(
        [
            "analyze-frequency",
            "--feeds", str(tmp_path / "feeds"),
            "--report", str(tmp_path / "out"),
            "--service-date", "junk",
            "--dry-run",
        ]
    )
    assert exit_code == 1
