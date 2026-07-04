"""Tests for the near-stations export (numeric ids, combined ids, format)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from gtfs_ingest.analysis import connectivity
from gtfs_ingest.analysis.near_stations import (
    NEAR_STATIONS_NAME,
    build_near_stations,
    walk_minutes,
    write_near_stations,
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
        "stop_num_id", "stop_uid", "feed_id", "stop_id", "station",
        "stop_lat", "stop_lon", "stop_line_ids", "near_stations",
    ]
    s1 = row_of(result, "f:S1")
    # Numeric id column matches the deterministic derivation.
    assert s1["stop_num_id"] == nid("f:S1")
    # Node relation tuples use the NUMERIC id of the near stop.
    assert s1["near_stations"] == f"[({nid('f:S2')}, Second Stop, 20, 1 min)]"
    # Combined stop+line id of the row's own stop.
    assert s1["stop_line_ids"] == f"{nid('f:S1')}-10"

    s2 = row_of(result, "f:S2")
    assert s2["near_stations"] == f"[({nid('f:S1')}, First Stop, 10, 1 min)]"
    assert s2["stop_line_ids"] == f"{nid('f:S2')}-20"

    path = write_near_stations(result, tmp_path)
    assert path.name == NEAR_STATIONS_NAME
    reread = pd.read_csv(path)
    assert int(reread.loc[reread["stop_uid"] == "f:S1", "stop_num_id"].item()) == nid("f:S1")


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
    s2 = nid("f:S2")
    assert row_of(result, "f:S1")["near_stations"] == (
        f"[({s2}, Side, 20, 1 min), ({s2}, Side, r3, 1 min)]"
    )
    assert row_of(result, "f:S2")["stop_line_ids"] == f"{s2}-20, {s2}-r3"


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


def test_unserved_near_stop_is_omitted_from_lists() -> None:
    # S2 is within range but no route serves it (e.g. a bare station node).
    stops = make_stops(
        ("f", "S1", "Stop", 40.0, -75.0),
        ("f", "S2", "Station Node", 40.0009, -75.0),
    )
    route_stops = rs(("f", "r1", "S1"))
    routes = routes_meta(("f", "r1", "10"))
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)

    result = build_near_stations(stops, route_stops, routes, pairs)
    assert row_of(result, "f:S1")["near_stations"] == "[]"          # S2 has no line
    assert row_of(result, "f:S2")["near_stations"] == f"[({nid('f:S1')}, Stop, 10, 1 min)]"
    assert row_of(result, "f:S2")["stop_line_ids"] == ""            # unserved itself


def test_numeric_ids_are_stable_and_unique() -> None:
    uids = [f"feed{i % 3}:S{i}" for i in range(500)]
    ids = [numeric_stop_id(u) for u in uids]
    assert ids == [numeric_stop_id(u) for u in uids]   # deterministic
    assert len(set(ids)) == len(ids)                   # no collisions here
    assert all(0 <= i < 2**48 for i in ids)            # fits BIGINT and JS numbers


def test_walk_minutes_floor_and_rounding() -> None:
    minutes = walk_minutes(pd.Series([10.0, 100.0, 300.0]))
    assert minutes.tolist() == [1, 1, 4]   # 0.12 -> floor 1; 1.2 -> 1; 3.6 -> 4
