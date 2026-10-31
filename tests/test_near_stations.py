"""Tests for the near-stations export (numeric ids, combined ids, format)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from gtfs_ingest.analysis import connectivity
from gtfs_ingest.analysis.near_stations import (
    NEAR_STATIONS_NAME,
    QUALITY_COLUMNS,
    QUALITY_NAME,
    UNSERVED_LINE,
    build_near_stations,
    build_quality,
    walk_minutes,
    write_near_stations,
    write_quality,
)
from gtfs_ingest.models import numeric_stop_id

from test_connectivity import rs, stops_geo


def nid(stop_uid: str) -> int:
    return numeric_stop_id(stop_uid)


def routes_meta(*rows: tuple[str, str, str | None]) -> pd.DataFrame:
    """Rows of (feed_id, route_id, route_short_name)."""
    df = pd.DataFrame(list(rows), columns=["feed_id", "route_id", "route_short_name"])
    df["route_uid"] = df["feed_id"] + ":" + df["route_id"]
    return df


def make_stops(*rows: tuple[str, str, str, float, float]) -> pd.DataFrame:
    """Rows of (feed_id, stop_id, stop_name, lat, lon) shaped like normalizer output."""
    geo = stops_geo(*[(f, s, lat, lon) for f, s, _, lat, lon in rows])
    geo["stop_name"] = [name for _, _, name, _, _ in rows]
    geo["stop_num_id"] = geo["stop_uid"].map(numeric_stop_id)
    return geo


def row_of(result: pd.DataFrame, stop_uid: str) -> dict:
    (row,) = result[result["stop_uid"] == stop_uid].to_dict("records")
    return row


def test_near_stations_numeric_ids_and_format(tmp_path: Path) -> None:
    # Two stops ~100 m apart: walkable in ~1 min at 5 km/h.
    stops = make_stops(
        ("f", "S1", "First Stop", 40.0, -75.0),
        ("f", "S2", "Second Stop", 40.0009, -75.0),
    )
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S2"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    result = build_near_stations(stops, route_stops, routes, pairs)

    assert list(result.columns) == [
        "ID", "stop_uid", "station", "system", "stop_id",
        "stop_lat", "stop_lon", "stop_line_ids", "near_stations", "same_line_stations",
    ]
    # No stop_links passed -> the same-line relation is empty, not missing.
    assert row_of(result, "f:S1")["same_line_stations"] == "[]"
    s1 = row_of(result, "f:S1")
    # Sequential node id: 1..N in row order.
    assert s1["ID"] == 1
    # Node relation tuples use the sequential ID; times are bare minutes.
    assert s1["near_stations"] == "[(2, Second Stop, 20, 1)]"
    # Combined stop+line id of the row's own stop.
    assert s1["stop_line_ids"] == "1-10"
    # No agency metadata in this fixture -> empty, never missing.
    assert s1["system"] == ""

    s2 = row_of(result, "f:S2")
    assert s2["ID"] == 2
    assert s2["near_stations"] == "[(1, First Stop, 10, 1)]"
    assert s2["stop_line_ids"] == "2-20"

    path = write_near_stations(result, tmp_path)
    assert path.name == NEAR_STATIONS_NAME
    reread = pd.read_csv(path)
    assert list(reread["ID"]) == [1, 2]  # starts at 1, row order


def test_multiple_lines_yield_one_tuple_and_one_combo_each() -> None:
    stops = make_stops(
        ("f", "S1", "Hub", 40.0, -75.0),
        ("f", "S2", "Side", 40.0009, -75.0),
    )
    # S2 is served by two lines; the short name is missing for r3 -> route_id used.
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S2"), ("f", "r3", "S2"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"), ("f", "r3", None))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    result = build_near_stations(stops, route_stops, routes, pairs)
    assert row_of(result, "f:S1")["near_stations"] == (
        "[(2, Side, 20, 1), (2, Side, r3, 1)]"
    )
    assert row_of(result, "f:S2")["stop_line_ids"] == "2-20, 2-r3"


def test_dedupe_filtered_pairs_drop_alias_but_keep_ids() -> None:
    # fb:A2 is a cross-feed alias of fa:A (~10 m, same name); fa:B is a
    # genuine neighbour ~100 m away.
    from gtfs_ingest.analysis import dedupe

    stops = make_stops(
        ("fa", "A", "Main St", 40.0, -75.0),
        ("fa", "B", "Second Stop", 40.0009, -75.0),
        ("fb", "A2", "Main St", 40.00009, -75.0),
    )
    route_stops = rs(("fa", "r1", "A"), ("fa", "r2", "B"), ("fb", "rX", "A2"))
    routes = routes_meta(("fa", "r1", "10"), ("fa", "r2", "20"), ("fb", "rX", "X9"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    unfiltered = build_near_stations(stops, route_stops, routes, pairs)
    accepted = dedupe.find_equivalences(stops, route_stops, routes)
    filtered_pairs = dedupe.filter_stop_pairs(pairs, dedupe.canonical_map(accepted))
    filtered = build_near_stations(stops, route_stops, routes, filtered_pairs)

    # The CSV contract is untouched: same rows, same sequential IDs.
    assert list(filtered["ID"]) == list(unfiltered["ID"])
    assert list(filtered["stop_uid"]) == list(unfiltered["stop_uid"])

    # Unfiltered, the alias is listed as a "near station" of itself...
    a2_id = row_of(unfiltered, "fb:A2")["ID"]
    assert f"({a2_id}, Main St, X9, 1)" in row_of(unfiltered, "fa:A")["near_stations"]
    # ...filtered, only the genuine neighbour remains (in both directions).
    b_id = row_of(filtered, "fa:B")["ID"]
    assert row_of(filtered, "fa:A")["near_stations"] == f"[({b_id}, Second Stop, 20, 1)]"
    a_id = row_of(filtered, "fa:A")["ID"]
    assert f"({a_id}, Main St," not in row_of(filtered, "fb:A2")["near_stations"]


def test_stops_without_neighbours_get_empty_list() -> None:
    stops = make_stops(
        ("f", "S1", "Lonely", 40.0, -75.0),
        ("f", "S9", "Far Away", 41.0, -76.0),   # ~140 km away
    )
    route_stops = rs(("f", "r1", "S1"), ("f", "r9", "S9"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r9", "90"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    result = build_near_stations(stops, route_stops, routes, pairs)
    assert row_of(result, "f:S1")["near_stations"] == "[]"
    assert row_of(result, "f:S9")["near_stations"] == "[]"


def test_unserved_near_stop_is_listed_with_the_sentinel_line() -> None:
    # S2 is within range but no route serves it (a bare station node, or a
    # stop from a stops-only CSV). It is still a real, walkable neighbour.
    stops = make_stops(
        ("f", "S1", "Stop", 40.0, -75.0),
        ("f", "S2", "Station Node", 40.0009, -75.0),
    )
    route_stops = rs(("f", "r1", "S1"))
    routes = routes_meta(("f", "r1", "10"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    result = build_near_stations(stops, route_stops, routes, pairs)
    assert row_of(result, "f:S1")["near_stations"] == f"[(2, Station Node, {UNSERVED_LINE}, 1)]"
    assert row_of(result, "f:S2")["near_stations"] == "[(1, Stop, 10, 1)]"
    # The sentinel must not leak into the unserved stop's OWN columns: it is
    # a valid neighbour, but it still serves no line.
    assert row_of(result, "f:S2")["stop_line_ids"] == ""
    assert row_of(result, "f:S2")["system"] == ""


def test_system_joins_distinct_agency_names() -> None:
    stops = make_stops(
        ("f", "S1", "Hub", 40.0, -75.0),
        ("f", "S2", "Side", 40.0009, -75.0),
    )
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S1"), ("f", "r3", "S2"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"), ("f", "r3", "30"))
    routes["modalidad"] = ["Mi Macro", "Sitren", "Mi Macro"]
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    result = build_near_stations(stops, route_stops, routes, pairs)
    assert row_of(result, "f:S1")["system"] == "Mi Macro, Sitren"  # distinct, sorted
    assert row_of(result, "f:S2")["system"] == "Mi Macro"


def test_numeric_ids_are_stable_and_unique() -> None:
    uids = [f"feed{i % 3}:S{i}" for i in range(500)]
    ids = [numeric_stop_id(u) for u in uids]
    assert ids == [numeric_stop_id(u) for u in uids]   # deterministic
    assert len(set(ids)) == len(ids)                   # no collisions here
    assert all(0 <= i < 2**48 for i in ids)            # fits BIGINT and JS numbers


def test_walk_minutes_floor_and_rounding() -> None:
    minutes = walk_minutes(pd.Series([10.0, 100.0, 300.0]))
    assert minutes.tolist() == [1, 1, 4]   # 0.12 -> floor 1; 1.2 -> 1; 3.6 -> 4


# --------------------------------------------------------------------------
# Quality sidecar (near_stations_quality.csv)
# --------------------------------------------------------------------------

def _quality_fixture():
    """Three stops: S1-S2 near each other, S3 isolated far away."""
    stops = make_stops(
        ("f", "S1", "Hub", 40.0, -75.0),
        ("f", "S2", "Side", 40.0009, -75.0),
        ("f", "S3", "Lonely", 41.0, -76.0),
    )
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S2"), ("f", "r3", "S3"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"), ("f", "r3", "30"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    return stops, route_stops, routes, pairs


def test_quality_rows_and_ids_match_near_stations() -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    near = build_near_stations(stops, route_stops, routes, pairs)
    quality = build_quality(near, route_stops, routes, pairs)

    assert list(quality.columns) == QUALITY_COLUMNS
    assert quality["ID"].tolist() == near["ID"].tolist()
    assert quality["stop_uid"].tolist() == near["stop_uid"].tolist()


def test_walk_source_defaults_straight_line_and_none() -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    near = build_near_stations(stops, route_stops, routes, pairs)
    quality = build_quality(near, route_stops, routes, pairs)   # plain pairs

    assert row_of(quality, "f:S1")["walk_source"] == "straight_line"
    assert row_of(quality, "f:S3")["walk_source"] == "none"     # no neighbours
    # No conflation data / no dedupe map / no links in this fixture.
    assert row_of(quality, "f:S1")["coord_confidence"] == "unavailable"
    assert row_of(quality, "f:S1")["station_dedup"] == ""
    assert row_of(quality, "f:S1")["travel_source"] == "none"
    assert row_of(quality, "f:S1")["travel_n_obs"] == 0


def test_walk_source_unserved_neighbour_counts_as_straight_line() -> None:
    # S2 is near S1 but unserved. It now renders as a neighbour with the
    # sentinel line, so S1 genuinely carries a walking estimate and the
    # provenance must say so rather than "none".
    stops = make_stops(
        ("f", "S1", "Stop", 40.0, -75.0),
        ("f", "S2", "Station Node", 40.0009, -75.0),
    )
    route_stops = rs(("f", "r1", "S1"))                 # nothing serves S2
    routes = routes_meta(("f", "r1", "10"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    near = build_near_stations(stops, route_stops, routes, pairs)
    quality = build_quality(near, route_stops, routes, pairs)

    assert UNSERVED_LINE in row_of(near, "f:S1")["near_stations"]
    assert row_of(quality, "f:S1")["walk_source"] == "straight_line"


def test_walk_source_mixed_and_unsnapped_override() -> None:
    stops = make_stops(
        ("f", "S1", "Hub", 40.0, -75.0),
        ("f", "S2", "Side", 40.0009, -75.0),
        ("f", "S3", "Third", 40.0, -75.0011),
    )
    route_stops = rs(("f", "r1", "S1"), ("f", "r2", "S2"), ("f", "r3", "S3"))
    routes = routes_meta(("f", "r1", "10"), ("f", "r2", "20"), ("f", "r3", "30"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0).sort_values(
        ["stop_uid_a", "stop_uid_b"]).reset_index(drop=True)
    # Annotate as the network walk would: S1-S2 routed, S1-S3 fell back.
    pairs["walk_source"] = ["network", "straight_line", "network"]
    pairs["snap_m_a"] = [5.0, 5.0, 8.0]
    pairs["snap_m_b"] = [8.0, 250.0, 250.0]   # S3 snaps 250 m away
    near = build_near_stations(stops, route_stops, routes, pairs)
    quality = build_quality(near, route_stops, routes, pairs)

    assert row_of(quality, "f:S1")["walk_source"] == "mixed"      # network + straight
    assert row_of(quality, "f:S2")["walk_source"] == "network"    # both S2 pairs routed
    assert row_of(quality, "f:S3")["walk_source"] == "unsnapped"  # snap > MAX_SNAP_M


def test_travel_source_and_min_n_obs() -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    stop_links = pd.DataFrame(
        {
            "route_uid": ["f:r1", "f:r1"],
            "feed_id": ["f", "f"],
            "route_id": ["r1", "r1"],
            "linea": ["10", "10"],
            "direction_id": [0, 0],
            "stop_uid_a": ["f:S1", "f:S2"],
            "stop_uid_b": ["f:S2", "f:S3"],
            "travel_min": [4.0, pd.NA],   # observed / estimated
            "n_obs": [5, 0],
        }
    )
    near = build_near_stations(stops, route_stops, routes, pairs, stop_links=stop_links)
    quality = build_quality(near, route_stops, routes, pairs, stop_links=stop_links)

    # S2 sees both links: one observed (n=5), one estimated (n=0) -> mixed, worst 0.
    assert row_of(quality, "f:S2")["travel_source"] == "mixed"
    assert row_of(quality, "f:S2")["travel_n_obs"] == 0
    # S1 sees only the observed link.
    assert row_of(quality, "f:S1")["travel_source"] == "observed"
    assert row_of(quality, "f:S1")["travel_n_obs"] == 5
    # S3 sees only the estimated link.
    assert row_of(quality, "f:S3")["travel_source"] == "estimated"
    assert row_of(quality, "f:S3")["travel_n_obs"] == 0


def test_coord_confidence_states() -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    near = build_near_stations(stops, route_stops, routes, pairs)
    osm = pd.DataFrame(
        {
            "stop_uid": ["f:S1", "f:S2"],            # S3 absent -> unavailable
            "osm_id": [901, pd.NA],                   # S2: no OSM node at all
            "distance_m": [12.0, pd.NA],
            "match_confidence": [0.812, 0.0],
            "needs_review": [False, True],
        }
    )
    quality = build_quality(near, route_stops, routes, pairs, osm_matches=osm)
    assert row_of(quality, "f:S1")["coord_confidence"] == "0.812"
    assert row_of(quality, "f:S2")["coord_confidence"] == "no_match"
    assert row_of(quality, "f:S3")["coord_confidence"] == "unavailable"

    # Matched but flagged: needs_review with an osm_id present.
    osm2 = osm.assign(osm_id=[901, 902], needs_review=[True, True])
    quality2 = build_quality(near, route_stops, routes, pairs, osm_matches=osm2)
    assert row_of(quality2, "f:S1")["coord_confidence"] == "needs_review"

    # No data at all.
    quality3 = build_quality(near, route_stops, routes, pairs, osm_matches=None)
    assert set(quality3["coord_confidence"]) == {"unavailable"}


def test_station_dedup_from_canonical() -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    near = build_near_stations(stops, route_stops, routes, pairs)
    canonical = pd.DataFrame(
        {"stop_uid": ["f:S1"], "canonical_uid": ["e:X1"], "group_id": [0]}
    )
    quality = build_quality(near, route_stops, routes, pairs, canonical=canonical)
    assert row_of(quality, "f:S1")["station_dedup"] == "e:X1"
    assert row_of(quality, "f:S2")["station_dedup"] == ""


def test_write_quality_roundtrip(tmp_path: Path) -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    near = build_near_stations(stops, route_stops, routes, pairs)
    quality = build_quality(near, route_stops, routes, pairs)

    path = write_quality(quality, tmp_path)
    assert path.name == QUALITY_NAME
    reread = pd.read_csv(path)
    assert list(reread.columns) == QUALITY_COLUMNS
    assert len(reread) == len(near)


def test_near_stations_unaffected_by_provenance_columns() -> None:
    stops, route_stops, routes, pairs = _quality_fixture()
    stop_links = pd.DataFrame(
        {
            "route_uid": ["f:r1"], "feed_id": ["f"], "route_id": ["r1"],
            "linea": ["10"], "direction_id": [0],
            "stop_uid_a": ["f:S1"], "stop_uid_b": ["f:S2"],
            "travel_min": [4.0],
        }
    )
    plain = build_near_stations(stops, route_stops, routes, pairs, stop_links=stop_links)

    annotated_pairs = pairs.copy()
    annotated_pairs["walk_source"] = "network"
    annotated_pairs["snap_m_a"] = 5.0
    annotated_pairs["snap_m_b"] = 5.0
    annotated_links = stop_links.assign(n_obs=7)
    with_extras = build_near_stations(
        stops, route_stops, routes, annotated_pairs, stop_links=annotated_links
    )
    pd.testing.assert_frame_equal(plain, with_extras)   # contract file untouched
