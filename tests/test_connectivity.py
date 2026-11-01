"""Connectivity edge construction, graph export, and the analyze CLI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
from test_headways import ANALYSIS_FEED

from gtfs_ingest.analysis import connectivity
from gtfs_ingest.analysis.graph_export import build_graph, graph_metrics, write_graphml
from gtfs_ingest.analysis.near_stations import UNSERVED_LINE
from gtfs_ingest.analysis.pipeline_phase2 import GRAPHML_NAME, JSON_NAME, MARKDOWN_NAME

from conftest import feed_files, write_feed_dir, write_stops_csv


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
    assert 90 <= e["min_stop_distance_m"] <= 110  # ~100 m geodesically
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
            "route_uid": ["f:r1", "f:r2", "f:r7"],  # r7 analyzed but unconnected
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
            "--feeds",
            str(tmp_path / "feeds"),
            "--report",
            str(out),
            "--service-date",
            "2026-01-06",
            "--dry-run",
        ]
    )
    assert exit_code == 0

    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    (feed,) = report["feeds"]
    assert feed["status"] == "analyzed"
    assert feed["service_date"] == "2026-01-06"
    assert feed["routes_analyzed"] == 2  # R1 + R2 run that Tuesday
    assert report["connections_by_type"] == {"same_stop": 1}  # R1/R2 share S1
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
    # S2 is an unserved station node: still a neighbour, with the sentinel line.
    s3 = near.loc[near["stop_uid"] == "metro:S3", "near_stations"].item()
    s2_id = int(near.loc[near["stop_uid"] == "metro:S2", "ID"].item())
    assert s3.startswith(f"[({s2_id}, Central Station, {UNSERVED_LINE}, ")
    # The sequential node ids start at 1 and cover every row.
    assert sorted(near["ID"]) == list(range(1, len(near) + 1))

    # GeoPackage export exists with both layers.
    import geopandas as gpd

    gpkg = out / "transit_analysis.gpkg"
    assert gpkg.is_file()
    assert len(gpd.read_file(gpkg, layer="stops")) == 3
    assert len(gpd.read_file(gpkg, layer="near_links")) == 1  # S2 <-> S3

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
        "route_id,agency_id,route_short_name,route_long_name,route_type\nRX,A2,X9,Cross Town,3\n"
    ),
    "trips.txt": "route_id,service_id,trip_id\nRX,WK,TX1\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nTX1,08:00:00,08:00:00,X1,1\n"
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
            "--feeds",
            str(feeds),
            "--report",
            str(out),
            "--service-date",
            "2026-01-06",
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
    assert report["stops_deduplicated"] == 2  # metro:S1 + other:X1
    # metro:R1/R2 both connect to other:RX through the deduped station.
    assert report["connections_by_type"] == {"same_stop": 1, "same_station_dedup": 2}

    out = tmp_path / "out_dedupe"
    equiv = pd.read_csv(out / "stop_equivalences.csv")
    (row,) = equiv.to_dict("records")
    assert (row["stop_uid_a"], row["stop_uid_b"]) == ("metro:S1", "other:X1")
    # No same-feed conflict here: the rejected file exists and is empty.
    assert report["dedupe_rejected_pairs"] == 0
    assert pd.read_csv(out / "stop_equivalences_rejected.csv").empty
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
    _run_analyze_two_feeds(tmp_path, "out_dedupe")
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
            "--feeds",
            str(tmp_path / "feeds"),
            "--report",
            str(tmp_path / "out"),
            "--service-date",
            "junk",
            "--dry-run",
        ]
    )
    assert exit_code == 1


# Date coherence, --common-date, --service-date-range, analysis clock
def test_parse_date_range_validation() -> None:
    from datetime import date as _date

    import pytest

    from gtfs_ingest.cli import _parse_date_range

    assert _parse_date_range("2026-01-05..2026-01-11") == (_date(2026, 1, 5), _date(2026, 1, 11))
    for bad in ("2026-01-05", "2026-01-11..2026-01-05", "2026-01-01..2026-03-01", "x..y"):
        with pytest.raises(ValueError):
            _parse_date_range(bad)


def test_date_mode_flags_are_mutually_exclusive(tmp_path: Path) -> None:
    from gtfs_ingest import cli

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    base = [
        "analyze-frequency",
        "--feeds",
        str(tmp_path / "feeds"),
        "--report",
        str(tmp_path / "out"),
        "--dry-run",
    ]
    assert cli.main(base + ["--service-date", "2026-01-06", "--common-date"]) == 1
    assert cli.main(base + ["--service-date-range", "2026-01-05..2026-01-06", "--common-date"]) == 1


def test_incoherent_dates_flagged_and_analysis_date_recorded(tmp_path: Path) -> None:
    """Two feeds with disjoint calendars -> coherence warning + clock field."""
    from gtfs_ingest import cli

    old = feed_files(
        replace={
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20200101,20201231\n"
            )
        }
    )
    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    write_feed_dir(tmp_path / "feeds", "old", old)
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                "--report",
                str(out),
                "--analysis-date",
                "2026-07-12",
                "--dry-run",
            ]
        )
        == 0
    )

    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    assert report["analysis_date"] == "2026-07-12"
    assert report["service_dates_coherent"] is False
    assert len(report["service_dates"]) == 2  # busiest 2020 vs 2026
    md = (out / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert "DIFFERENT service dates" in md
    # The expired 2020 feed gets deterministic notes against the clock.
    old_feed = next(f for f in report["feeds"] if f["feed_id"] == "old")
    assert old_feed["calendar_status"] == "expired"

    # --common-date on disjoint calendars: loud exit 1.
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                "--report",
                str(tmp_path / "out2"),
                "--common-date",
                "--dry-run",
            ]
        )
        == 1
    )


def test_common_date_resolves_shared_day(tmp_path: Path) -> None:
    from gtfs_ingest import cli

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    write_feed_dir(tmp_path / "feeds", "valid", feed_files())  # both serve 2026 WK
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                "--report",
                str(out),
                "--common-date",
                "--dry-run",
            ]
        )
        == 0
    )

    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    assert report["service_dates_coherent"] is True
    dates = {f["service_date"] for f in report["feeds"]}
    assert len(dates) == 1  # one shared day
    assert {f["service_date_source"] for f in report["feeds"]} == {"common"}


def test_service_date_range_analyzes_served_days(tmp_path: Path, monkeypatch) -> None:
    from gtfs_ingest import cli
    from gtfs_ingest.analysis import pipeline_phase2

    loaded_sets: list[frozenset] = []
    real_load = pipeline_phase2.load_feed_for_service_ids

    def counting_load(source, ids, **kwargs):
        loaded_sets.append(frozenset(ids))
        return real_load(source, ids, **kwargs)

    monkeypatch.setattr(pipeline_phase2, "load_feed_for_service_ids", counting_load)
    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                "--report",
                str(out),
                "--service-date-range",
                "2026-01-05..2026-01-11",
                "--dry-run",
            ]
        )
        == 0
    )

    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    (feed,) = report["feeds"]
    assert feed["status"] == "analyzed"
    assert feed["service_date"] == "2026-01-05..2026-01-11"
    assert feed["service_date_source"] == "range"
    assert feed["days_analyzed"] == 5  # Mon removed by calendar_dates, Sun no service
    # Per-day headway rows: the CSV export carries one service_date per day.
    headways = pd.read_csv(out / "route_stop_headways.csv")
    assert headways["service_date"].nunique() == 5
    # Each distinct service_id set is loaded once, not once per day.
    assert len(loaded_sets) == len(set(loaded_sets)) < 5


def test_new_csv_and_station_network_outputs(tmp_path: Path) -> None:
    from gtfs_ingest import cli

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                "--report",
                str(out),
                "--service-date",
                "2026-01-06",
                "--dry-run",
            ]
        )
        == 0
    )

    # New CSVs exist with sane contents.
    nodes = pd.read_csv(out / "station_network_nodes.csv")
    near = pd.read_csv(out / "near_stations.csv")
    assert len(nodes) == len(near)  # one node per stop
    edges = pd.read_csv(out / "station_network_edges.csv")
    assert set(edges["edge_type"]) <= {"in_vehicle", "walk", "same_station"}
    assert (out / "route_headways.csv").is_file()
    # Station network GraphML + report stats.
    assert (out / "station_network.graphml").is_file()
    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    assert report["station_graph"]["n_stops"] == len(near)
    # New gpkg layer alongside the existing four.
    assert len(gpd.read_file(out / "transit_analysis.gpkg", layer="station_network")) == len(edges)


# Stops-only CSV sources in Phase 2
def _run_analysis(tmp_path: Path, out_name: str, *extra: str) -> dict:
    """Run analyze-frequency over the feeds dir plus *extra* args; return the JSON."""
    from gtfs_ingest import cli

    out = tmp_path / out_name
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                *extra,
                "--report",
                str(out),
                "--service-date",
                "2026-01-06",
                "--dry-run",
            ]
        )
        == 0
    )
    return json.loads((out / JSON_NAME).read_text(encoding="utf-8"))


def test_analyze_frequency_with_stops_only_csv(tmp_path: Path) -> None:
    """A CSV feed contributes geometry everywhere and schedule nowhere."""
    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    csv_path = write_stops_csv(tmp_path, "extra_stops.csv")

    baseline = _run_analysis(tmp_path, "out_baseline")
    report = _run_analysis(tmp_path, "out", str(csv_path))
    out = tmp_path / "out"

    by_id = {f["feed_id"]: f for f in report["feeds"]}
    assert set(by_id) == {"metro", "extra_stops"}

    csv_feed = by_id["extra_stops"]
    assert csv_feed["status"] == "stops_only"
    assert csv_feed["service_date"] is None  # must not enter the coherence check
    assert csv_feed["routes_analyzed"] == 0
    assert csv_feed["trips_analyzed"] == 0
    assert csv_feed["stop_headway_rows"] == 0
    assert report["stops_only_feeds"] == ["extra_stops"]
    assert report["stops_only_stops"] == 2

    # A dateless feed must not make the run look incoherent.
    assert report["service_dates_coherent"] is True
    assert report["service_dates"] == ["2026-01-06"]

    # Zero contamination of the route-level analysis.
    assert report["connections_by_type"] == baseline["connections_by_type"]
    assert report["graph"] == baseline["graph"]

    near = pd.read_csv(out / "near_stations.csv", keep_default_na=False)
    c1 = near.loc[near["stop_uid"] == "extra_stops:C1"].iloc[0]
    s1_id = int(near.loc[near["stop_uid"] == "metro:S1", "ID"].item())
    c1_id = int(c1["ID"])

    # The CSV stop serves no line, so its own line columns stay empty...
    assert c1["stop_line_ids"] == ""
    assert c1["system"] == ""
    assert c1["same_line_stations"] == "[]"
    # ...but it lists the scheduled stop with that stop's real line...
    assert f"({s1_id}, Main St, 10, " in c1["near_stations"]
    # ...and, crucially, the scheduled stop lists it back with the sentinel.
    s1_near = near.loc[near["stop_uid"] == "metro:S1", "near_stations"].item()
    assert f"({c1_id}, Main St Kiosk, {UNSERVED_LINE}, " in s1_near

    quality = pd.read_csv(out / "near_stations_quality.csv", keep_default_na=False)
    q = quality.loc[quality["stop_uid"] == "extra_stops:C1"].iloc[0]
    assert q["walk_source"] == "straight_line"
    assert q["travel_source"] == "none"
    assert int(q["travel_n_obs"]) == 0

    # In the GeoPackage and the station network, but never deduplicated or walk-linked.
    layer = gpd.read_file(out / "transit_analysis.gpkg", layer="stops")
    assert "extra_stops:C1" in set(layer["stop_uid"])
    nodes = pd.read_csv(out / "station_network_nodes.csv")
    assert "extra_stops:C1" in set(nodes["stop_uid"])
    assert report["station_graph"]["n_stops_only"] == 2
    edges = pd.read_csv(out / "station_network_edges.csv")
    assert not edges[["stop_uid_a", "stop_uid_b"]].stack().str.startswith("extra_stops:").any()
    equiv = pd.read_csv(out / "stop_equivalences.csv")
    assert (
        not equiv[["stop_uid_a", "stop_uid_b"]]
        .stack()
        .astype(str)
        .str.startswith("extra_stops:")
        .any()
    )
    assert report["dedupe_within_feed_distance_m"] == 5.0


def test_common_date_ignores_stops_only_feeds(tmp_path: Path) -> None:
    """--common-date resolves against schedule feeds only; a CSV must not abort the run."""
    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    csv_path = write_stops_csv(tmp_path, "extra_stops.csv")
    from gtfs_ingest import cli

    out = tmp_path / "out"
    exit_code = cli.main(
        [
            "analyze-frequency",
            "--feeds",
            str(tmp_path / "feeds"),
            str(csv_path),
            "--report",
            str(out),
            "--common-date",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    report = json.loads((out / JSON_NAME).read_text(encoding="utf-8"))
    by_id = {f["feed_id"]: f for f in report["feeds"]}
    assert by_id["metro"]["status"] == "analyzed"
    assert by_id["extra_stops"]["status"] == "stops_only"


def test_mode_filter_does_not_drop_stops_only_feed(tmp_path: Path) -> None:
    """A route filter must not silently delete stops from a feed that has no routes."""
    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    csv_path = write_stops_csv(tmp_path, "extra_stops.csv")

    report = _run_analysis(tmp_path, "out", str(csv_path), "--transport-mode", "bus")
    near = pd.read_csv(tmp_path / "out" / "near_stations.csv", keep_default_na=False)

    by_id = {f["feed_id"]: f for f in report["feeds"]}
    assert by_id["extra_stops"]["status"] == "stops_only"
    assert "extra_stops:C1" in set(near["stop_uid"])


def test_stops_only_feed_is_included_in_the_postgis_proximity_query(
    tmp_path: Path, monkeypatch
) -> None:
    """The PostGIS proximity query must include stops-only feeds (dry runs can't catch this)."""
    from gtfs_ingest import db
    from gtfs_ingest.analysis import pipeline_phase2, storage

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    csv_path = write_stops_csv(tmp_path, "extra_stops.csv")

    seen: dict[str, list[str]] = {}

    def fake_postgis(engine, feed_ids, threshold_m):
        seen["feed_ids"] = list(feed_ids)
        return pd.DataFrame(columns=["stop_uid_a", "stop_uid_b", "distance_m"])

    monkeypatch.setattr(db, "make_engine", lambda url: object())
    monkeypatch.setattr(db, "ensure_schema", lambda engine: None)
    monkeypatch.setattr(db, "load_feed_stops", lambda engine, feed_id, stops: len(stops))
    monkeypatch.setattr(storage, "ensure_analysis_schema", lambda engine: None)
    monkeypatch.setattr(storage, "replace_feed_rows", lambda *a, **k: 0)
    monkeypatch.setattr(storage, "replace_all_rows", lambda *a, **k: 0)
    monkeypatch.setattr(storage, "fetch_osm_matches", lambda engine, feed_ids: None)
    monkeypatch.setattr(connectivity, "proximity_pairs_postgis", fake_postgis)

    pipeline_phase2.run_analysis(
        pipeline_phase2.AnalysisConfig(
            feed_paths=[tmp_path / "feeds", csv_path],
            database_url="postgresql+psycopg://unused",
            report_dir=tmp_path / "out",
            service_date=date(2026, 1, 6),
        )
    )

    assert "extra_stops" in seen["feed_ids"]
    assert "metro" in seen["feed_ids"]


# Within-feed dedupe: one line published under two id schemes
#: Route R1's stop listed twice (SITEUR_* and ST_*), as in the real mdb feed.
DUAL_SCHEME_FEED = feed_files(
    replace={
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "SITEUR_01,Capuchina,40.0,-75.0,0,\n"
            "ST_01,Capuchina,40.00002,-75.0,0,\n"  # ~2 m: same platform
            "S9,Lejos,40.05,-75.05,0,\n"
        ),
        "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\nR1,WK,T2\n",
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,SITEUR_01,1\n"
            "T1,08:10:00,08:10:00,S9,2\n"
            "T2,09:00:00,09:00:00,ST_01,1\n"
            "T2,09:10:00,09:10:00,S9,2\n"
        ),
    }
)


def test_within_feed_duplicate_ids_are_merged_end_to_end(tmp_path: Path) -> None:
    write_feed_dir(tmp_path / "feeds", "metro", DUAL_SCHEME_FEED)

    report = _run_analysis(tmp_path, "out")
    equiv = pd.read_csv(tmp_path / "out" / "stop_equivalences.csv")

    (row,) = equiv.to_dict("records")
    assert (row["stop_uid_a"], row["stop_uid_b"]) == ("metro:SITEUR_01", "metro:ST_01")
    assert row["match_kind"] == "within_feed"
    assert row["shared_routes"] == 1
    assert report["station_groups"] == 1

    # The two records now resolve to one physical station...
    quality = pd.read_csv(tmp_path / "out" / "near_stations_quality.csv", keep_default_na=False)
    canon = quality.apply(lambda r: r["station_dedup"] or r["stop_uid"], axis=1)
    assert canon.nunique() == 2  # Capuchina + Lejos, not 3
    # ...but nothing is dropped: every record still has its own row.
    assert len(quality) == 3


def test_within_feed_dedupe_does_not_change_route_topology(tmp_path: Path) -> None:
    """Duplicates serve the same route, so no route may appear or vanish."""
    write_feed_dir(tmp_path / "feeds", "metro", DUAL_SCHEME_FEED)

    on = _run_analysis(tmp_path, "out_on")
    off = _run_analysis(tmp_path, "out_off", "--dedupe-within-feed-distance-m", "0")

    assert off["station_groups"] == 0  # the flag really is off
    assert on["graph"]["n_routes"] == off["graph"]["n_routes"]
    assert on["graph"]["n_edges"] == off["graph"]["n_edges"]


def test_within_feed_default_is_the_same_in_cli_and_configs() -> None:
    """The CLI and the Python API must not silently run different dedupe rules."""
    from gtfs_ingest import cli
    from gtfs_ingest.analysis.dedupe import WITHIN_FEED_DISTANCE_M
    from gtfs_ingest.analysis.pipeline_phase2 import AnalysisConfig
    from gtfs_ingest.conflation.pipeline_phase3 import InterchangeConfig

    parser = cli.build_parser()
    for command in ("analyze-frequency", "detect-interchanges"):
        args = parser.parse_args([command, "--feeds", "x", "--dry-run"])
        assert args.dedupe_within_feed_distance_m == WITHIN_FEED_DISTANCE_M
    assert AnalysisConfig(feed_paths=[]).dedupe_within_feed_distance_m == WITHIN_FEED_DISTANCE_M
    assert InterchangeConfig(feed_paths=[]).dedupe_within_feed_distance_m == WITHIN_FEED_DISTANCE_M


def test_within_feed_dedupe_can_be_disabled(tmp_path: Path) -> None:
    write_feed_dir(tmp_path / "feeds", "metro", DUAL_SCHEME_FEED)

    report = _run_analysis(tmp_path, "out", "--dedupe-within-feed-distance-m", "0")
    equiv = pd.read_csv(tmp_path / "out" / "stop_equivalences.csv")

    assert equiv.empty
    assert report["station_groups"] == 0
