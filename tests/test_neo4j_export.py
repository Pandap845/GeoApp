"""Neo4j export package: model building, rendering, CLI."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from test_headways import ANALYSIS_FEED

from gtfs_ingest.export.neo4j import (
    ADMIN_HEADERS,
    FRAME_FILES,
    Neo4jInputs,
    build_package,
    load_inputs,
    render_load_cypher,
    write_package,
)

from conftest import write_feed_dir, write_stops_csv


def _inputs(equivalences: pd.DataFrame | None = None) -> Neo4jInputs:
    """Tiny network: 3 stops on route fa:r1, one alias pair fa:A ~ fb:X."""
    nodes = pd.DataFrame(
        {
            "stop_uid": ["fa:A", "fa:B", "fb:X"],
            "near_id": [1, 2, 3],
            "name": ["Main", "Second", "Main"],
            "feed": ["fa", "fa", "fb"],
            "canonical_uid": ["fa:A", "fa:B", "fa:A"],  # X aliases A; B is its own
            "lat": [40.0, 40.01, 40.0001],
            "lon": [-75.0, -75.0, -75.0],
        }
    )
    edges = pd.DataFrame(
        {
            "edge_type": ["in_vehicle", "in_vehicle", "walk", "same_station"],
            "stop_uid_a": ["fa:A", "fa:B", "fa:B", "fa:A"],
            "stop_uid_b": ["fa:B", "fa:A", "fb:X", "fb:X"],
            "time_min": [4.0, None, 1.0, 0.0],  # reverse leg untimed
            "linea": ["10", "10", None, None],
            "direction_id": [0.0, 1.0, None, None],  # arrives as float
            "route_uid": ["fa:r1", "fa:r1", None, None],
            "distance_m": [None, None, 55.0, 8.0],
        }
    )
    routes = pd.DataFrame(
        {
            "route_uid": ["fa:r1"],
            "feed_id": ["fa"],
            "route_id": ["r1"],
            "route_short_name": ["10"],
            "route_long_name": ["Main Line"],
            "route_type": [3],
            "modalidad": ["Mi Macro"],
        }
    )
    return Neo4jInputs(nodes=nodes, edges=edges, routes=routes, equivalences=equivalences)


def test_build_package_frames_and_counts() -> None:
    frames = build_package(_inputs())

    assert set(frames) == set(FRAME_FILES)
    for key, frame in frames.items():
        assert list(frame.columns) == list(ADMIN_HEADERS[key])
    assert len(frames["stops"]) == 3
    assert len(frames["routes"]) == 1
    assert len(frames["rel_in_vehicle"]) == 2
    assert len(frames["rel_walk"]) == 1
    assert len(frames["rel_same_station"]) == 1


def test_serves_is_union_of_endpoints_deduped() -> None:
    frames = build_package(_inputs())
    serves = frames["rel_serves"]
    # Two directed legs over the same pair -> exactly two (route, stop) rows.
    assert len(serves) == 2
    assert set(serves["stop_uid"]) == {"fa:A", "fa:B"}
    assert set(serves["route_uid"]) == {"fa:r1"}


def test_station_only_for_multi_member_groups() -> None:
    frames = build_package(_inputs())
    stations = frames["stations"]
    (station,) = stations.to_dict("records")
    assert station["canonical_uid"] == "fa:A"  # the alias group
    assert station["n_stops"] == 2
    # fa:B is a singleton canonical -> no Station, no PART_OF.
    part_of = frames["rel_part_of"]
    assert set(part_of["stop_uid"]) == {"fa:A", "fb:X"}  # canonical stop included
    assert set(part_of["canonical_uid"]) == {"fa:A"}


def test_same_station_props_from_equivalences() -> None:
    equiv = pd.DataFrame(
        {
            "group_id": [0],
            "canonical_uid": ["fa:A"],
            "stop_uid_a": ["fa:A"],
            "stop_uid_b": ["fb:X"],
            "distance_m": [8.2],
            "name_score": [1.0],
            "shared_routes": [0],
            "score": [0.9],
        }
    )
    enriched = build_package(_inputs(equivalences=equiv))["rel_same_station"]
    assert enriched["name_score"].iloc[0] == 1.0
    bare = build_package(_inputs())["rel_same_station"]
    assert bare["name_score"].isna().all()  # no equivalences: bare rels


def test_untimed_in_vehicle_keeps_empty_time(tmp_path: Path) -> None:
    frames = build_package(_inputs())
    iv = frames["rel_in_vehicle"]
    assert iv["time_min"].isna().sum() == 1  # the reverse leg
    assert set(iv["direction_id"]) == {"0", "1"}  # normalized to string

    write_package(frames, tmp_path)
    reread = pd.read_csv(tmp_path / "rel_in_vehicle.csv")
    assert reread["time_min:double"].isna().sum() == 1  # empty CSV field survives


def test_render_cypher_covers_every_file_and_key() -> None:
    cypher = render_load_cypher(build_package(_inputs()))
    for fragment in (
        "CREATE CONSTRAINT stop_uid",
        "CREATE CONSTRAINT route_uid",
        "CREATE CONSTRAINT station_uid",
        "point(",
        "IN TRANSACTIONS",
        "route_uid: row.route_uid, direction_id: row.direction_id",  # merge keys
        "`stop_uid:ID(Stop)`",  # ID space
    ):
        assert fragment in cypher
    for filename in FRAME_FILES.values():
        assert f"file:///{filename}" in cypher


def test_write_package_headers_have_id_spaces(tmp_path: Path) -> None:
    write_package(build_package(_inputs()), tmp_path)
    assert (tmp_path / "stops.csv").read_text(encoding="utf-8").startswith("stop_uid:ID(Stop)")
    assert (
        ":START_ID(Route)"
        in (tmp_path / "rel_serves.csv").read_text(encoding="utf-8").splitlines()[0]
    )
    assert (tmp_path / "load.cypher").is_file()
    assert (tmp_path / "post_import.cypher").is_file()
    assert "neo4j-admin database import" in (tmp_path / "admin_import.txt").read_text(
        encoding="utf-8"
    )


def test_load_inputs_missing_edges_raises(tmp_path: Path) -> None:
    (tmp_path / "station_network_nodes.csv").write_text("stop_uid\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"station_network_edges\.csv.*analyze-frequency"):
        load_inputs(tmp_path)


def test_export_neo4j_cli_missing_inputs(tmp_path: Path) -> None:
    from gtfs_ingest import cli

    assert cli.main(["export-neo4j", "--report", str(tmp_path)]) == 1


def test_export_neo4j_cli_e2e(tmp_path: Path) -> None:
    """analyze-frequency dry run -> export-neo4j builds a complete package."""
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
    assert cli.main(["export-neo4j", "--report", str(out)]) == 0

    package = out / "neo4j"
    for filename in (
        *FRAME_FILES.values(),
        "load.cypher",
        "post_import.cypher",
        "admin_import.txt",
    ):
        assert (package / filename).is_file()
    stops = pd.read_csv(package / "stops.csv")
    nodes = pd.read_csv(out / "station_network_nodes.csv")
    assert len(stops) == len(nodes)  # one :Stop per node
    cypher = (package / "load.cypher").read_text(encoding="utf-8")
    assert all(f"file:///{name}" in cypher for name in FRAME_FILES.values())


def test_export_neo4j_includes_stops_only_csv_stops(tmp_path: Path) -> None:
    """The Neo4j package is built from Phase 2 outputs, so CSV stops must reach it."""
    from gtfs_ingest import cli

    write_feed_dir(tmp_path / "feeds", "metro", ANALYSIS_FEED)
    csv_path = write_stops_csv(tmp_path, "extra_stops.csv")
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "analyze-frequency",
                "--feeds",
                str(tmp_path / "feeds"),
                str(csv_path),
                "--report",
                str(out),
                "--service-date",
                "2026-01-06",
                "--dry-run",
            ]
        )
        == 0
    )
    assert cli.main(["export-neo4j", "--report", str(out)]) == 0

    stops = pd.read_csv(out / "neo4j" / "stops.csv")
    uid_column = next(c for c in stops.columns if c.startswith("stop_uid"))
    assert "extra_stops:C1" in set(stops[uid_column])
