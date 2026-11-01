"""Offline Neo4j load package built from an analyze-frequency report folder.

Reads the station-network CSVs (plus route_connectivity.graphml and
stop_equivalences.csv when present) and writes <report>/neo4j/: CSVs with
neo4j-admin headers, load.cypher for cypher-shell, and post_import.cypher
plus admin_import.txt for a bulk import.

Graph: Stop, Route and Station nodes (Station only for groups with 2+ stops);
IN_VEHICLE (directed), WALK and SAME_STATION (stored once, query undirected),
SERVES and PART_OF. No data value is ever written into the .cypher text;
everything goes through quoted CSV.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import pandas as pd

log = logging.getLogger(__name__)

PACKAGE_DIR_NAME = "neo4j"

REQUIRED_FILES = ("station_network_nodes.csv", "station_network_edges.csv")
GRAPHML_FILE = "route_connectivity.graphml"
EQUIVALENCES_FILE = "stop_equivalences.csv"

#: frame key -> output CSV filename.
FRAME_FILES: dict[str, str] = {
    "stops": "stops.csv",
    "routes": "routes.csv",
    "stations": "stations.csv",
    "rel_in_vehicle": "rel_in_vehicle.csv",
    "rel_walk": "rel_walk.csv",
    "rel_same_station": "rel_same_station.csv",
    "rel_serves": "rel_serves.csv",
    "rel_part_of": "rel_part_of.csv",
}

#: frame key -> {column -> neo4j-admin header}. Writers and Cypher both read this map.
ADMIN_HEADERS: dict[str, dict[str, str]] = {
    "stops": {
        "stop_uid": "stop_uid:ID(Stop)",
        "name": "name",
        "feed": "feed",
        "near_id": "near_id:long",
        "lat": "lat:double",
        "lon": "lon:double",
        "canonical_uid": "canonical_uid",
    },
    "routes": {
        "route_uid": "route_uid:ID(Route)",
        "feed_id": "feed_id",
        "route_id": "route_id",
        "route_short_name": "route_short_name",
        "route_long_name": "route_long_name",
        "route_type": "route_type",
        "modalidad": "modalidad",
    },
    "stations": {
        "canonical_uid": "canonical_uid:ID(Station)",
        "name": "name",
        "lat": "lat:double",
        "lon": "lon:double",
        "n_stops": "n_stops:long",
    },
    "rel_in_vehicle": {
        "stop_uid_a": ":START_ID(Stop)",
        "stop_uid_b": ":END_ID(Stop)",
        "time_min": "time_min:double",
        "linea": "linea",
        "direction_id": "direction_id",
        "route_uid": "route_uid",
    },
    "rel_walk": {
        "stop_uid_a": ":START_ID(Stop)",
        "stop_uid_b": ":END_ID(Stop)",
        "time_min": "time_min:double",
        "distance_m": "distance_m:double",
    },
    "rel_same_station": {
        "stop_uid_a": ":START_ID(Stop)",
        "stop_uid_b": ":END_ID(Stop)",
        "distance_m": "distance_m:double",
        "name_score": "name_score:double",
        "score": "score:double",
    },
    "rel_serves": {
        "route_uid": ":START_ID(Route)",
        "stop_uid": ":END_ID(Stop)",
    },
    "rel_part_of": {
        "stop_uid": ":START_ID(Stop)",
        "canonical_uid": ":END_ID(Station)",
    },
}

#: Node label or relationship type per frame key.
FRAME_KIND: dict[str, tuple[str, str]] = {
    "stops": ("node", "Stop"),
    "routes": ("node", "Route"),
    "stations": ("node", "Station"),
    "rel_in_vehicle": ("rel", "IN_VEHICLE"),
    "rel_walk": ("rel", "WALK"),
    "rel_same_station": ("rel", "SAME_STATION"),
    "rel_serves": ("rel", "SERVES"),
    "rel_part_of": ("rel", "PART_OF"),
}


@dataclass(frozen=True)
class Neo4jInputs:
    """Report-directory frames the package is built from."""

    nodes: pd.DataFrame
    edges: pd.DataFrame
    routes: pd.DataFrame
    equivalences: pd.DataFrame | None


def load_inputs(report_dir: Path) -> Neo4jInputs:
    """Read the report files the exporter needs.

    Raises:
        ValueError: a required file is missing (run analyze-frequency first).
    """
    missing = [name for name in REQUIRED_FILES if not (report_dir / name).is_file()]
    if missing:
        raise ValueError(
            f"missing {', '.join(missing)} in {report_dir} - run analyze-frequency "
            "with this --report directory first"
        )
    nodes = pd.read_csv(report_dir / REQUIRED_FILES[0])
    edges = pd.read_csv(report_dir / REQUIRED_FILES[1])

    graphml = report_dir / GRAPHML_FILE
    if graphml.is_file():
        graph = nx.read_graphml(graphml)
        routes = pd.DataFrame(
            [{"route_uid": uid, **attrs} for uid, attrs in graph.nodes(data=True)]
        )
    else:
        log.warning("%s not found; :Route nodes will carry only route_uid/feed", GRAPHML_FILE)
        routes = pd.DataFrame(columns=["route_uid"])

    equivalences = None
    equiv_path = report_dir / EQUIVALENCES_FILE
    if equiv_path.is_file():
        equivalences = pd.read_csv(equiv_path)
    return Neo4jInputs(nodes=nodes, edges=edges, routes=routes, equivalences=equivalences)


def _edge_subset(edges: pd.DataFrame, kind: str) -> pd.DataFrame:
    subset = edges[edges["edge_type"] == kind].copy()
    return subset.sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")


def build_package(inputs: Neo4jInputs) -> dict[str, pd.DataFrame]:
    """Build the eight package tables, keyed as in FRAME_FILES."""
    nodes, edges = inputs.nodes, inputs.edges

    stops = pd.DataFrame(columns=list(ADMIN_HEADERS["stops"]))
    if not nodes.empty:
        stops = nodes[["stop_uid", "name", "feed", "near_id", "lat", "lon", "canonical_uid"]].copy()
        stops["canonical_uid"] = stops["canonical_uid"].fillna(stops["stop_uid"])
        stops = stops.sort_values("stop_uid", kind="mergesort").reset_index(drop=True)

    # Routes: GraphML attributes when available, else just the uid.
    route_cols = list(ADMIN_HEADERS["routes"])
    routes = inputs.routes.copy()
    if routes.empty or "route_uid" not in routes.columns:
        uids = (
            edges.loc[edges["edge_type"] == "in_vehicle", "route_uid"].dropna().unique()
            if not edges.empty
            else []
        )
        routes = pd.DataFrame({"route_uid": sorted(uids)})
        routes["feed_id"] = routes["route_uid"].str.split(":", n=1).str[0]
    for col in route_cols:
        if col not in routes.columns:
            routes[col] = None
    routes = routes[route_cols].sort_values("route_uid", kind="mergesort").reset_index(drop=True)

    # Stations: only groups with more than one stop.
    stations = pd.DataFrame(columns=list(ADMIN_HEADERS["stations"]))
    part_of = pd.DataFrame(columns=list(ADMIN_HEADERS["rel_part_of"]))
    if not stops.empty:
        sizes = stops.groupby("canonical_uid")["stop_uid"].transform("size")
        members = stops[sizes > 1]
        if not members.empty:
            canon_rows = stops.set_index("stop_uid")
            stations = members.groupby("canonical_uid", as_index=False).agg(
                n_stops=("stop_uid", "size")
            )
            stations["name"] = stations["canonical_uid"].map(canon_rows["name"])
            stations["lat"] = stations["canonical_uid"].map(canon_rows["lat"])
            stations["lon"] = stations["canonical_uid"].map(canon_rows["lon"])
            stations = (
                stations[list(ADMIN_HEADERS["stations"])]
                .sort_values("canonical_uid", kind="mergesort")
                .reset_index(drop=True)
            )
            part_of = (
                members[["stop_uid", "canonical_uid"]]
                .sort_values("stop_uid", kind="mergesort")
                .reset_index(drop=True)
            )

    # Stop-Stop relationships from the typed edges.
    in_vehicle = pd.DataFrame(columns=list(ADMIN_HEADERS["rel_in_vehicle"]))
    walk = pd.DataFrame(columns=list(ADMIN_HEADERS["rel_walk"]))
    same_station = pd.DataFrame(columns=list(ADMIN_HEADERS["rel_same_station"]))
    serves = pd.DataFrame(columns=list(ADMIN_HEADERS["rel_serves"]))
    if not edges.empty:
        iv = _edge_subset(edges, "in_vehicle")
        if not iv.empty:
            iv["direction_id"] = (
                pd.to_numeric(iv["direction_id"], errors="coerce")
                .fillna(-1)
                .astype(int)
                .astype(str)
            )
            in_vehicle = iv[list(ADMIN_HEADERS["rel_in_vehicle"])].reset_index(drop=True)

        wk = _edge_subset(edges, "walk")
        if not wk.empty:
            walk = wk[list(ADMIN_HEADERS["rel_walk"])].reset_index(drop=True)

        ss = _edge_subset(edges, "same_station")
        if not ss.empty:
            ss = ss[["stop_uid_a", "stop_uid_b"]]
            if inputs.equivalences is not None and not inputs.equivalences.empty:
                ss = ss.merge(
                    inputs.equivalences[
                        ["stop_uid_a", "stop_uid_b", "distance_m", "name_score", "score"]
                    ],
                    on=["stop_uid_a", "stop_uid_b"],
                    how="left",
                )
            for col in ("distance_m", "name_score", "score"):
                if col not in ss.columns:
                    ss[col] = None
            same_station = ss[list(ADMIN_HEADERS["rel_same_station"])].reset_index(drop=True)

        if not iv.empty:
            serves = (
                pd.concat(
                    [
                        iv[["route_uid", "stop_uid_a"]].rename(columns={"stop_uid_a": "stop_uid"}),
                        iv[["route_uid", "stop_uid_b"]].rename(columns={"stop_uid_b": "stop_uid"}),
                    ],
                    ignore_index=True,
                )
                .drop_duplicates()
                .sort_values(["route_uid", "stop_uid"], kind="mergesort")
                .reset_index(drop=True)
            )

    frames = {
        "stops": stops,
        "routes": routes,
        "stations": stations,
        "rel_in_vehicle": in_vehicle,
        "rel_walk": walk,
        "rel_same_station": same_station,
        "rel_serves": serves,
        "rel_part_of": part_of,
    }
    log.info(
        "Neo4j package: %s",
        ", ".join(f"{key}={len(frame)}" for key, frame in frames.items()),
    )
    return frames


def _col(frame_key: str, plain: str) -> str:
    """Backticked admin-header column name for Cypher."""
    return f"row.`{ADMIN_HEADERS[frame_key][plain]}`"


_CONSTRAINTS = """\
CREATE CONSTRAINT stop_uid IF NOT EXISTS FOR (s:Stop) REQUIRE s.stop_uid IS UNIQUE;
CREATE CONSTRAINT route_uid IF NOT EXISTS FOR (r:Route) REQUIRE r.route_uid IS UNIQUE;
CREATE CONSTRAINT station_uid IF NOT EXISTS FOR (st:Station) REQUIRE st.canonical_uid IS UNIQUE;
CREATE POINT INDEX stop_location IF NOT EXISTS FOR (s:Stop) ON (s.location);
"""


def render_load_cypher(frames: dict[str, pd.DataFrame]) -> str:
    """Re-runnable LOAD CSV script (MERGE everywhere)."""
    f = FRAME_FILES
    lines = [
        "// Generated by gtfs-ingest export-neo4j.",
        "// Run: cypher-shell -f load.cypher   (Browser: prefix LOAD CSV blocks with :auto)",
        "// CSVs must be in the DBMS import directory.",
        "// WALK/SAME_STATION are stored once per pair - query undirected: -[:WALK]-.",
        "",
        _CONSTRAINTS,
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['stops']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MERGE (s:Stop {{stop_uid: {_col('stops', 'stop_uid')}}})",
        "  SET s.name = row.name, s.feed = row.feed,",
        f"      s.near_id = toInteger({_col('stops', 'near_id')}),",
        f"      s.lat = toFloat({_col('stops', 'lat')}), s.lon = toFloat({_col('stops', 'lon')}),",
        "      s.canonical_uid = row.canonical_uid,",
        f"      s.location = point({{latitude: toFloat({_col('stops', 'lat')}),",
        f"                           longitude: toFloat({_col('stops', 'lon')})}})",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['routes']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MERGE (r:Route {{route_uid: {_col('routes', 'route_uid')}}})",
        "  SET r.feed_id = row.feed_id, r.route_id = row.route_id,",
        "      r.route_short_name = row.route_short_name,",
        "      r.route_long_name = row.route_long_name,",
        "      r.route_type = toInteger(row.route_type), r.modalidad = row.modalidad",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['stations']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MERGE (st:Station {{canonical_uid: {_col('stations', 'canonical_uid')}}})",
        f"  SET st.name = row.name, st.n_stops = toInteger({_col('stations', 'n_stops')}),",
        f"      st.lat = toFloat({_col('stations', 'lat')}), st.lon = toFloat({_col('stations', 'lon')})",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['rel_in_vehicle']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MATCH (a:Stop {{stop_uid: {_col('rel_in_vehicle', 'stop_uid_a')}}})",
        f"  MATCH (b:Stop {{stop_uid: {_col('rel_in_vehicle', 'stop_uid_b')}}})",
        "  MERGE (a)-[r:IN_VEHICLE {route_uid: row.route_uid, direction_id: row.direction_id}]->(b)",
        f"  SET r.time_min = toFloat({_col('rel_in_vehicle', 'time_min')}), r.linea = row.linea",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['rel_walk']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MATCH (a:Stop {{stop_uid: {_col('rel_walk', 'stop_uid_a')}}})",
        f"  MATCH (b:Stop {{stop_uid: {_col('rel_walk', 'stop_uid_b')}}})",
        "  MERGE (a)-[r:WALK]-(b)",
        f"  SET r.time_min = toFloat({_col('rel_walk', 'time_min')}),",
        f"      r.distance_m = toFloat({_col('rel_walk', 'distance_m')})",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['rel_same_station']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MATCH (a:Stop {{stop_uid: {_col('rel_same_station', 'stop_uid_a')}}})",
        f"  MATCH (b:Stop {{stop_uid: {_col('rel_same_station', 'stop_uid_b')}}})",
        "  MERGE (a)-[r:SAME_STATION]-(b)",
        f"  SET r.distance_m = toFloat({_col('rel_same_station', 'distance_m')}),",
        f"      r.name_score = toFloat({_col('rel_same_station', 'name_score')})",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['rel_serves']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MATCH (r:Route {{route_uid: {_col('rel_serves', 'route_uid')}}})",
        f"  MATCH (s:Stop {{stop_uid: {_col('rel_serves', 'stop_uid')}}})",
        "  MERGE (r)-[:SERVES]->(s)",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
        f"LOAD CSV WITH HEADERS FROM 'file:///{f['rel_part_of']}' AS row",
        "CALL {",
        "  WITH row",
        f"  MATCH (s:Stop {{stop_uid: {_col('rel_part_of', 'stop_uid')}}})",
        f"  MATCH (st:Station {{canonical_uid: {_col('rel_part_of', 'canonical_uid')}}})",
        "  MERGE (s)-[:PART_OF]->(st)",
        "} IN TRANSACTIONS OF 1000 ROWS;",
        "",
    ]
    return "\n".join(lines)


def render_post_import_cypher() -> str:
    """Constraints and location points to run after a bulk import."""
    return "\n".join(
        [
            "// Run after `neo4j-admin database import` (see admin_import.txt):",
            "// constraints, the location point property and its index.",
            "",
            _CONSTRAINTS,
            "MATCH (s:Stop) WHERE s.lat IS NOT NULL",
            "CALL {",
            "  WITH s",
            "  SET s.location = point({latitude: s.lat, longitude: s.lon})",
            "} IN TRANSACTIONS OF 1000 ROWS;",
            "",
        ]
    )


def render_admin_command() -> str:
    """The neo4j-admin bulk-import command."""
    parts = []
    for key, (kind, label) in FRAME_KIND.items():
        flag = "--nodes" if kind == "node" else "--relationships"
        parts.append(f"{flag}={label}={FRAME_FILES[key]}")
    joined_bash = " \\\n  ".join(parts)
    joined_ps = " `\n  ".join(parts)
    return "\n".join(
        [
            "# Bulk import with neo4j-admin (database must not exist / DBMS stopped).",
            "# Run from this directory; afterwards apply post_import.cypher:",
            "#   cypher-shell -f post_import.cypher",
            "",
            "# bash:",
            "neo4j-admin database import full neo4j \\",
            f"  {joined_bash} \\",
            "  --skip-duplicate-nodes",
            "",
            "# PowerShell:",
            "neo4j-admin database import full neo4j `",
            f"  {joined_ps} `",
            "  --skip-duplicate-nodes",
            "",
        ]
    )


def write_package(frames: dict[str, pd.DataFrame], out_dir: Path) -> list[Path]:
    """Write the CSVs and the three text files; return every path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key, filename in FRAME_FILES.items():
        frame = frames[key].rename(columns=ADMIN_HEADERS[key])
        path = out_dir / filename
        frame.to_csv(path, index=False, encoding="utf-8")
        log.info("Neo4j CSV written: %s (%d rows)", path, len(frame))
        written.append(path)
    for name, content in (
        ("load.cypher", render_load_cypher(frames)),
        ("post_import.cypher", render_post_import_cypher()),
        ("admin_import.txt", render_admin_command()),
    ):
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    log.info("Neo4j load package written to %s (%d files)", out_dir, len(written))
    return written


def run_export(report_dir: Path, out_dir: Path | None = None) -> list[Path]:
    """Build and write the package; *out_dir* defaults to <report_dir>/neo4j.

    Raises:
        ValueError: required inputs are missing.
    """
    inputs = load_inputs(report_dir)
    frames = build_package(inputs)
    return write_package(frames, out_dir or report_dir / PACKAGE_DIR_NAME)
