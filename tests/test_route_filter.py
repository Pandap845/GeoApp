"""Route/mode filter parsing and selection (--transport-mode / --route)."""

from __future__ import annotations

import pandas as pd
import pytest

from gtfs_ingest.analysis.route_filter import (
    allowed_route_ids,
    mode_labels,
    parse_modes,
    parse_routes,
)


ROUTES = pd.DataFrame(
    {
        "route_id": ["R1", "R2", "R3", "R4"],
        "route_short_name": ["L1", "C98", "T5", ""],
        "route_type": ["3", "700", "0", "2"],  # bus, extended bus, tram, rail
    }
)


def test_parse_modes_accepts_names_codes_and_commas() -> None:
    assert parse_modes(["bus"]) == frozenset({3})
    assert parse_modes(["BUS", "rail"]) == frozenset({3, 2})
    assert parse_modes(["bus,tram", "metro"]) == frozenset({3, 0, 1})
    assert parse_modes(["700"]) == frozenset({3})     # extended code normalizes
    assert parse_modes(["3"]) == frozenset({3})


def test_parse_modes_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown mode name"):
        parse_modes(["hovercraft"])
    with pytest.raises(ValueError, match="route_type code"):
        parse_modes(["9999"])


def test_parse_routes_and_mode_labels() -> None:
    assert parse_routes(["L1, C98", "R3"]) == frozenset({"L1", "C98", "R3"})
    assert mode_labels(frozenset({3, 0})) == ["bus", "tram"]
    assert mode_labels(None) == []


def test_no_filter_returns_none() -> None:
    assert allowed_route_ids(ROUTES, None, None) is None
    assert allowed_route_ids(ROUTES, frozenset(), frozenset()) is None


def test_mode_filter_selects_by_normalized_route_type() -> None:
    # bus (3) matches both the basic "3" and the extended "700".
    assert allowed_route_ids(ROUTES, frozenset({3}), None) == {"R1", "R2"}
    assert allowed_route_ids(ROUTES, frozenset({0}), None) == {"R3"}


def test_route_filter_matches_id_or_short_name() -> None:
    assert allowed_route_ids(ROUTES, None, frozenset({"C98"})) == {"R2"}   # short name
    assert allowed_route_ids(ROUTES, None, frozenset({"R4"})) == {"R4"}    # route_id


def test_mode_and_route_filters_are_a_union() -> None:
    got = allowed_route_ids(ROUTES, frozenset({0}), frozenset({"C98"}))
    assert got == {"R2", "R3"}  # tram by mode + C98 by name


def test_filter_with_no_matching_routes_is_empty_not_none() -> None:
    assert allowed_route_ids(ROUTES, frozenset({4}), None) == set()       # no ferry
    assert allowed_route_ids(None, frozenset({3}), None) == set()         # no routes.txt
    assert allowed_route_ids(pd.DataFrame(), frozenset({3}), None) == set()


def test_cli_accepts_transport_mode_and_route_flags() -> None:
    from gtfs_ingest.cli import build_parser

    args = build_parser().parse_args(
        ["analyze-frequency", "--feeds", "x.zip",
         "--transport-mode", "bus,rail", "--transport-mode", "0",
         "--route", "C98"]
    )
    assert args.transport_mode == ["bus,rail", "0"]
    assert args.route == ["C98"]
