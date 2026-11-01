"""Cross-feed station deduplication: matching, grouping, pair filtering."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from test_near_stations import make_stops, routes_meta

from gtfs_ingest.analysis import connectivity, dedupe

from test_connectivity import rs

#: ~10 m / ~20 m / ~45 m of latitude.
LAT_10M = 0.00009
LAT_20M = 0.00018
LAT_45M = 0.0004


# Name normalization / similarity
def test_normalize_name_accents_case_punctuation_tokens() -> None:
    assert dedupe.normalize_name("Estación Juárez") == "estacion juarez"
    assert dedupe.normalize_name("JUAREZ, Estacion.") == "estacion juarez"  # token sort
    assert dedupe.normalize_name("  Main St  ") == "main st"
    assert dedupe.normalize_name(None) == ""
    assert dedupe.normalize_name(float("nan")) == ""
    assert dedupe.normalize_name("") == ""


def test_name_similarity_bounds() -> None:
    assert dedupe.name_similarity("Estación Juárez", "estacion JUAREZ") == 1.0
    assert dedupe.name_similarity("Terminal Norte", "Estacion Centro") < 0.75
    # A missing name is never evidence.
    assert dedupe.name_similarity("", "Main St") == 0.0
    assert dedupe.name_similarity(None, None) == 0.0


# find_equivalences: the accept rule
def test_cross_feed_same_name_within_distance_is_accepted() -> None:
    stops = make_stops(
        ("fa", "S1", "Estación Juárez", 40.0, -75.0),
        ("fb", "X1", "Juarez, estacion", 40.0 + LAT_10M, -75.0),
    )
    result = dedupe.find_equivalences(stops, rs(), routes_meta())

    (row,) = result.to_dict("records")
    assert (row["stop_uid_a"], row["stop_uid_b"]) == ("fa:S1", "fb:X1")
    assert 9 <= row["distance_m"] <= 11
    assert row["name_score"] == 1.0
    assert row["shared_routes"] == 0


def test_same_feed_pair_is_never_an_alias() -> None:
    stops = make_stops(
        ("fa", "S1", "Main St", 40.0, -75.0),
        ("fa", "S2", "Main St", 40.0 + LAT_10M, -75.0),
    )
    assert dedupe.find_equivalences(stops, rs(), routes_meta()).empty


def test_distance_gate_rejects_beyond_radius() -> None:
    stops = make_stops(
        ("fa", "S1", "Main St", 40.0, -75.0),
        ("fb", "X1", "Main St", 40.0 + LAT_45M, -75.0),
    )
    assert dedupe.find_equivalences(stops, rs(), routes_meta()).empty


def test_dissimilar_names_rescued_only_by_shared_route() -> None:
    stops = make_stops(
        ("fa", "S1", "Terminal Norte", 40.0, -75.0),
        ("fb", "X1", "Estacion Centro", 40.0 + LAT_10M, -75.0),
    )
    route_stops = rs(("fa", "r1", "S1"), ("fb", "rX", "X1"))

    # No shared route short name -> rejected on names alone.
    routes = routes_meta(("fa", "r1", "T1"), ("fb", "rX", "99"))
    assert dedupe.find_equivalences(stops, route_stops, routes).empty

    # Same short name "T1" on both feeds -> accepted despite the names.
    routes = routes_meta(("fa", "r1", "T1"), ("fb", "rX", "T1"))
    (row,) = dedupe.find_equivalences(stops, route_stops, routes).to_dict("records")
    assert row["shared_routes"] == 1
    assert row["name_score"] < 0.75


def test_empty_stop_name_matches_only_via_shared_route() -> None:
    stops = make_stops(
        ("fa", "S1", "", 40.0, -75.0),
        ("fb", "X1", "Main St", 40.0 + LAT_10M, -75.0),
    )
    route_stops = rs(("fa", "r1", "S1"), ("fb", "rX", "X1"))
    routes = routes_meta(("fa", "r1", "T1"), ("fb", "rX", "T1"))

    (row,) = dedupe.find_equivalences(stops, route_stops, routes).to_dict("records")
    assert row["name_score"] == 0.0
    assert row["shared_routes"] == 1
    # Without the shared route there is no evidence at all.
    assert dedupe.find_equivalences(stops, rs(), routes_meta()).empty


# canonical_map: grouping and determinism
def _chain_stops(order: list[int]):
    """A -~10m- B -~10m- C chain (A-C beyond radius), rows in *order*."""
    rows = [
        ("fa", "A", "Main St", 40.0, -75.0),
        ("fb", "B", "Main St", 40.0 + LAT_20M, -75.0),
        ("fa", "C", "Main St", 40.0 + 2 * LAT_20M, -75.0),
    ]
    return make_stops(*[rows[i] for i in order])


def test_transitive_merge_and_input_order_determinism() -> None:
    baseline = None
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        stops = _chain_stops(order)
        accepted = dedupe.find_equivalences(stops, rs(), routes_meta())
        # A-B and B-C are cross-feed and ~20 m; A-C is same-feed (and 40 m).
        assert len(accepted) == 2
        canonical = dedupe.canonical_map(accepted)
        assert list(canonical["stop_uid"]) == ["fa:A", "fa:C", "fb:B"]
        assert set(canonical["canonical_uid"]) == {"fa:A"}  # min of the group
        assert set(canonical["group_id"]) == {0}
        if baseline is None:
            baseline = canonical
        else:
            pd.testing.assert_frame_equal(canonical, baseline)


def test_canonical_map_empty_input() -> None:
    result = dedupe.canonical_map(pd.DataFrame(columns=["stop_uid_a", "stop_uid_b"]))
    assert result.empty
    assert list(result.columns) == dedupe.CANONICAL_COLUMNS


def test_attach_groups_payload_columns() -> None:
    stops = make_stops(
        ("fa", "S1", "Main St", 40.0, -75.0),
        ("fb", "X1", "Main St", 40.0 + LAT_10M, -75.0),
    )
    accepted = dedupe.find_equivalences(stops, rs(), routes_meta())
    tagged = dedupe.attach_groups(accepted, dedupe.canonical_map(accepted))

    assert list(tagged.columns) == dedupe.EQUIVALENCE_COLUMNS
    (row,) = tagged.to_dict("records")
    assert row["group_id"] == 0
    assert row["canonical_uid"] == "fa:S1"
    assert 0.0 <= row["score"] <= 1.0


# filter_stop_pairs
def test_filter_stop_pairs_drops_alias_pairs_only() -> None:
    stops = make_stops(
        ("fa", "S1", "Main St", 40.0, -75.0),
        ("fb", "X1", "Main St", 40.0 + LAT_10M, -75.0),
        ("fa", "S9", "Other Stop", 40.0 + 3 * LAT_10M, -75.0),
    )
    accepted = dedupe.find_equivalences(stops, rs(), routes_meta())
    canonical = dedupe.canonical_map(accepted)
    pairs = connectivity.proximity_pairs_local(stops, threshold_m=150.0)
    assert len(pairs) == 3  # all three combinations are within 150 m

    kept = dedupe.filter_stop_pairs(pairs, canonical)
    kept_keys = set(zip(kept["stop_uid_a"], kept["stop_uid_b"]))
    assert ("fa:S1", "fb:X1") not in kept_keys  # the alias pair is gone
    assert ("fa:S1", "fa:S9") in kept_keys  # genuine neighbours stay
    assert ("fa:S9", "fb:X1") in kept_keys
    # No canonical map -> nothing filtered.
    pd.testing.assert_frame_equal(
        dedupe.filter_stop_pairs(pairs, pd.DataFrame(columns=dedupe.CANONICAL_COLUMNS)), pairs
    )


def test_write_equivalences(tmp_path: Path) -> None:
    stops = make_stops(
        ("fa", "S1", "Main St", 40.0, -75.0),
        ("fb", "X1", "Main St", 40.0 + LAT_10M, -75.0),
    )
    accepted = dedupe.find_equivalences(stops, rs(), routes_meta())
    tagged = dedupe.attach_groups(accepted, dedupe.canonical_map(accepted))

    path = dedupe.write_equivalences(tagged, tmp_path)
    assert path.name == dedupe.EQUIVALENCES_NAME
    reread = pd.read_csv(path)
    assert list(reread.columns) == dedupe.EQUIVALENCE_COLUMNS
    assert len(reread) == 1


# Within-feed dedupe (one station published under two id schemes)
def test_within_feed_is_off_by_default() -> None:
    """The library default reproduces the historical cross-feed-only result."""
    stops = make_stops(
        ("fa", "SITEUR_01", "Capuchina", 40.0, -75.0),
        ("fa", "ST_01", "Capuchina", 40.0 + LAT_20M, -75.0),
    )
    route_stops = rs(("fa", "L1", "SITEUR_01"), ("fa", "L1", "ST_01"))
    routes = routes_meta(("fa", "L1", "L1"))

    assert dedupe.find_equivalences(stops, route_stops, routes).empty


def test_within_feed_pair_sharing_a_route_is_merged() -> None:
    stops = make_stops(
        ("fa", "SITEUR_01", "Capuchina", 40.0, -75.0),
        ("fa", "ST_01", "Capuchina", 40.0 + LAT_20M, -75.0),
    )
    route_stops = rs(("fa", "L1", "SITEUR_01"), ("fa", "L1", "ST_01"))
    routes = routes_meta(("fa", "L1", "L1"))

    result = dedupe.find_equivalences(stops, route_stops, routes, within_feed_distance_m=50.0)

    (row,) = result.to_dict("records")
    assert (row["stop_uid_a"], row["stop_uid_b"]) == ("fa:SITEUR_01", "fa:ST_01")
    assert row["match_kind"] == dedupe.WITHIN_FEED
    assert row["shared_routes"] == 1


def test_within_feed_identical_name_without_shared_route_is_rejected() -> None:
    """The core safety property: names alone never merge two stops of one feed.

    Two genuinely distinct stops of the same feed can share a name (a corridor,
    a plaza). Only shared-route evidence may merge them.
    """
    stops = make_stops(
        ("fa", "S1", "Calle Lopez Cotilla", 40.0, -75.0),
        ("fa", "S2", "Calle Lopez Cotilla", 40.0 + LAT_20M, -75.0),
    )
    route_stops = rs(("fa", "L1", "S1"), ("fa", "L2", "S2"))  # different routes
    routes = routes_meta(("fa", "L1", "L1"), ("fa", "L2", "L2"))

    assert dedupe.find_equivalences(stops, route_stops, routes, within_feed_distance_m=50.0).empty


def test_within_feed_respects_its_own_radius() -> None:
    stops = make_stops(
        ("fa", "SITEUR_01", "Capuchina", 40.0, -75.0),
        ("fa", "ST_01", "Capuchina", 40.0 + LAT_45M, -75.0),
    )
    route_stops = rs(("fa", "L1", "SITEUR_01"), ("fa", "L1", "ST_01"))
    routes = routes_meta(("fa", "L1", "L1"))

    assert dedupe.find_equivalences(stops, route_stops, routes, within_feed_distance_m=30.0).empty
    assert not dedupe.find_equivalences(
        stops, route_stops, routes, within_feed_distance_m=50.0
    ).empty


def test_within_feed_score_stays_in_range_past_the_cross_feed_radius() -> None:
    """score is relative to the radius that admitted the pair, not the narrower one."""
    stops = make_stops(
        ("fa", "SITEUR_01", "Capuchina", 40.0, -75.0),
        ("fa", "ST_01", "Capuchina", 40.0 + LAT_45M, -75.0),
    )
    route_stops = rs(("fa", "L1", "SITEUR_01"), ("fa", "L1", "ST_01"))
    routes = routes_meta(("fa", "L1", "L1"))

    (row,) = dedupe.find_equivalences(
        stops, route_stops, routes, within_feed_distance_m=50.0
    ).to_dict("records")
    assert 0.0 <= row["score"] <= 1.0


def test_cross_feed_still_matches_on_name_alone_with_within_feed_enabled() -> None:
    stops = make_stops(
        ("fa", "S1", "Estación Juárez", 40.0, -75.0),
        ("fb", "X1", "Juarez, estacion", 40.0 + LAT_10M, -75.0),
    )
    result = dedupe.find_equivalences(stops, rs(), routes_meta(), within_feed_distance_m=50.0)

    (row,) = result.to_dict("records")
    assert row["match_kind"] == dedupe.CROSS_FEED
    assert row["shared_routes"] == 0


def test_stops_only_source_can_never_be_merged_within_feed() -> None:
    """MiBici-style stops have no routes, so the route-evidence rule cannot fire."""
    stops = make_stops(
        ("mibici", "C1", "Plaza Universidad", 40.0, -75.0),
        ("mibici", "C2", "Plaza Universidad", 40.0 + LAT_20M, -75.0),
    )
    assert dedupe.find_equivalences(stops, rs(), routes_meta(), within_feed_distance_m=50.0).empty


def test_pipeline_radius_keeps_duplicates_but_not_opposite_platforms() -> None:
    """The 5 m default: a 0 m duplicate record merges, the route's other-direction
    stop across the street (same route, ~20 m) stays a separate stop."""
    stops = make_stops(
        ("fa", "SITEUR_01", "Capuchina", 40.0, -75.0),
        ("fa", "ST_01", "Capuchina", 40.0, -75.0),  # duplicate record
        ("fa", "SITEUR_02", "Capuchina", 40.0 + LAT_20M, -75.0),  # opposite direction
    )
    route_stops = rs(("fa", "L1", "SITEUR_01"), ("fa", "L1", "ST_01"), ("fa", "L1", "SITEUR_02"))
    routes = routes_meta(("fa", "L1", "L1"))

    result = dedupe.find_equivalences(
        stops, route_stops, routes, within_feed_distance_m=dedupe.WITHIN_FEED_DISTANCE_M
    )

    assert result[["stop_uid_a", "stop_uid_b"]].values.tolist() == [["fa:SITEUR_01", "fa:ST_01"]]


def test_excluded_feed_never_matches_cross_feed_on_name() -> None:
    """A dock named like a nearby bus stop must not become its alias."""
    stops = make_stops(
        ("fa", "S1", "Estación Juárez", 40.0, -75.0),
        ("mibici", "C1", "Juarez, estacion", 40.0 + LAT_10M, -75.0),
    )

    assert not dedupe.find_equivalences(stops, rs(), routes_meta()).empty  # the risk
    assert dedupe.find_equivalences(stops, rs(), routes_meta(), exclude_feeds=["mibici"]).empty


def test_match_kind_survives_attach_groups() -> None:
    stops = make_stops(
        ("fa", "SITEUR_01", "Capuchina", 40.0, -75.0),
        ("fa", "ST_01", "Capuchina", 40.0 + LAT_20M, -75.0),
    )
    route_stops = rs(("fa", "L1", "SITEUR_01"), ("fa", "L1", "ST_01"))
    routes = routes_meta(("fa", "L1", "L1"))

    accepted = dedupe.find_equivalences(stops, route_stops, routes, within_feed_distance_m=50.0)
    tagged = dedupe.attach_groups(accepted, dedupe.canonical_map(accepted))

    assert list(tagged.columns) == dedupe.EQUIVALENCE_COLUMNS
    assert tagged["match_kind"].tolist() == [dedupe.WITHIN_FEED]
