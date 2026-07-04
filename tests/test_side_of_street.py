"""Side-of-street sign math on fixed geometry (correct side by construction)
plus OSM match scoring and schema-name validation."""

from __future__ import annotations

import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from gtfs_ingest.conflation.osm_match import (
    _check_identifier,
    score_matches,
    type_score,
)
from gtfs_ingest.conflation.side_of_street import compute_sides, side_of_line

# A straight west->east street in metric coordinates: direction is +x, so
# "left of travel" is +y (north) and "right of travel" is -y (south).
STREET = LineString([(0, 0), (100, 0)])


def test_stop_north_of_eastbound_way_is_left() -> None:
    result = side_of_line(STREET, Point(50, 10))
    assert result.side == "left"
    assert result.perpendicular_distance_m == pytest.approx(10.0)
    assert result.confidence == "high"
    assert result.near_endpoint is False


def test_stop_south_of_eastbound_way_is_right() -> None:
    assert side_of_line(STREET, Point(50, -10)).side == "right"


def test_reversed_oneway_flips_side() -> None:
    # oneway=-1: traffic flows against digitization (east->west here), so
    # the same northern stop is now on the RIGHT of travel.
    assert side_of_line(STREET, Point(50, 10), oneway="-1").side == "right"
    assert side_of_line(STREET, Point(50, -10), oneway="-1").side == "left"
    # Plain oneway=yes keeps the digitized direction.
    assert side_of_line(STREET, Point(50, 10), oneway="yes").side == "left"


def test_stop_on_the_line_reports_on_with_low_confidence() -> None:
    result = side_of_line(STREET, Point(50, 0.4))
    assert result.side == "on"
    assert result.confidence == "low"


def test_near_endpoint_is_low_confidence_but_still_sided() -> None:
    result = side_of_line(STREET, Point(3, 8))   # 3 m from the way start
    assert result.side == "left"
    assert result.near_endpoint is True
    assert result.confidence == "low"


def test_polyline_uses_nearest_segment_direction() -> None:
    # L-shaped way: east along y=0, then north along x=50. A stop east of
    # the northbound segment is on its RIGHT (travel +y, right hand = +x).
    bent = LineString([(0, 0), (50, 0), (50, 50)])
    result = side_of_line(bent, Point(55, 25))
    assert result.side == "right"
    assert result.perpendicular_distance_m == pytest.approx(5.0)


def test_compute_sides_flags_stops_without_street() -> None:
    streets = pd.DataFrame(
        [
            {
                "stop_uid": "f:S1", "feed_id": "f", "stop_id": "S1",
                "stop_wkt": "POINT (-75.0 40.0)", "osm_id": 111, "highway": "residential",
                "oneway": None,
                # A short street ~ parallel, ~11m south of the stop.
                "way_wkt": "LINESTRING (-75.0005 39.9999, -74.9995 39.9999)",
                "distance_m": 11.0,
            },
            {   # No highway anywhere near this one.
                "stop_uid": "f:S2", "feed_id": "f", "stop_id": "S2",
                "stop_wkt": "POINT (-76.0 41.0)", "osm_id": None, "highway": None,
                "oneway": None, "way_wkt": None, "distance_m": None,
            },
        ]
    )
    result = compute_sides(streets, threshold_m=50.0)

    s1 = result[result["stop_uid"] == "f:S1"].iloc[0]
    # Street runs west->east south of the stop -> stop is on the LEFT.
    assert s1["side"] == "left"
    assert s1["osm_way_id"] == 111
    assert 8 <= s1["perpendicular_distance_m"] <= 14

    s2 = result[result["stop_uid"] == "f:S2"].iloc[0]
    assert s2["side"] is None
    assert s2["osm_way_id"] is None
    assert s2["side_confidence"] == "low"


# --------------------------------------------------------------------------
# OSM match scoring (Part B, pure parts)
# --------------------------------------------------------------------------

def test_match_confidence_formula() -> None:
    matches = pd.DataFrame(
        [
            # Perfect: platform 0 m from a regular stop.
            {"stop_uid": "f:A", "feed_id": "f", "stop_id": "A", "location_type": 0,
             "osm_id": 1, "osm_kind": "bus_stop", "osm_name": "X", "distance_m": 0.0},
            # Station node for a location_type=1 stop, 25 m away: 0.5 * 1.0.
            {"stop_uid": "f:B", "feed_id": "f", "stop_id": "B", "location_type": 1,
             "osm_id": 2, "osm_kind": "station", "osm_name": "Y", "distance_m": 25.0},
            # Beyond threshold -> confidence 0, review.
            {"stop_uid": "f:C", "feed_id": "f", "stop_id": "C", "location_type": 0,
             "osm_id": 3, "osm_kind": "bus_stop", "osm_name": None, "distance_m": 80.0},
            # No candidate at all.
            {"stop_uid": "f:D", "feed_id": "f", "stop_id": "D", "location_type": 0,
             "osm_id": None, "osm_kind": None, "osm_name": None, "distance_m": None},
        ]
    )
    scored = score_matches(matches, threshold_m=50.0).set_index("stop_uid")

    assert scored.at["f:A", "match_confidence"] == 1.0
    assert not scored.at["f:A", "needs_review"]
    assert scored.at["f:B", "match_confidence"] == 0.5
    assert not scored.at["f:B", "needs_review"]
    assert scored.at["f:C", "match_confidence"] == 0.0
    assert scored.at["f:C", "needs_review"]
    assert scored.at["f:D", "match_confidence"] == 0.0
    assert scored.at["f:D", "needs_review"]


def test_type_score_compatibility() -> None:
    assert type_score(1, "station") == 1.0     # station stop <-> OSM station
    assert type_score(1, "bus_stop") == 0.6    # station stop <-> bus_stop: weak
    assert type_score(0, "bus_stop") == 1.0
    assert type_score(0, "station") == 0.8
    assert type_score(0, None) == 0.7


def test_osm_identifier_validation() -> None:
    _check_identifier("planet_osm_line", "table")          # fine
    _check_identifier("osm.planet_osm_line", "table")      # schema-qualified fine
    with pytest.raises(ValueError):
        _check_identifier("bad-name", "table")
    with pytest.raises(ValueError):
        _check_identifier("t; DROP TABLE stops", "table")
