"""Which items Google QA checks, and the verdict rules (all comparisons in memory).

Checked: multimodal interchanges (are our modes right?) and needs-review stops
(is there a transit place nearby?). The interchange mask includes
transitStation, Google's most expensive SKU, hence the targeting and budget.

Interchange verdicts: CONFIRMED (Google covers all our modes), PARTIAL
(some overlap), CONFLICT (no overlap), NO_DATA. Stop verdicts: STATION_FOUND,
NOT_FOUND, NO_DATA. Google's vehicle types are mapped to GTFS route_type codes
and discarded after the verdict.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import Engine, text

from .google_client import PlaceResult

log = logging.getLogger(__name__)

INTERCHANGE_FIELD_MASK: tuple[str, ...] = ("places.id", "places.types", "places.transitStation")
STOP_FIELD_MASK: tuple[str, ...] = ("places.id", "places.types")

#: Place types accepted as "transit" for both checks.
TRANSIT_PLACE_TYPES: tuple[str, ...] = (
    "transit_station",
    "bus_station",
    "bus_stop",
    "train_station",
    "subway_station",
    "light_rail_station",
    "ferry_terminal",
)

#: Google vehicleType -> our basic GTFS route_type code.
VEHICLE_TYPE_TO_MODE: dict[str, int] = {
    "BUS": 3, "INTERCITY_BUS": 3, "TROLLEYBUS": 11,
    "TRAM": 0, "LIGHT_RAIL": 0,
    "SUBWAY": 1, "METRO_RAIL": 1,
    "RAIL": 2, "HEAVY_RAIL": 2, "COMMUTER_TRAIN": 2,
    "HIGH_SPEED_TRAIN": 2, "LONG_DISTANCE_TRAIN": 2,
    "FERRY": 4,
    "CABLE_CAR": 5, "GONDOLA_LIFT": 6, "FUNICULAR": 7, "MONORAIL": 12,
}

#: Fallback when no vehicleType is given: Google place type -> mode.
PLACE_TYPE_TO_MODE: dict[str, int] = {
    "bus_station": 3, "bus_stop": 3,
    "train_station": 2, "subway_station": 1,
    "light_rail_station": 0, "ferry_terminal": 4,
}


def eligible_interchanges(engine: Engine) -> list[dict[str, Any]]:
    """Multimodal interchanges with their centroid and inferred modes."""
    sql = text(
        """
        SELECT cluster_id, modes, mode_names,
               ST_Y(centroid) AS lat, ST_X(centroid) AS lon
        FROM interchanges
        WHERE is_multimodal
        ORDER BY cluster_id
        """
    )
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).mappings()]
    log.info("Google QA: %d multimodal interchange(s) eligible", len(rows))
    return rows


def eligible_stops(engine: Engine) -> list[dict[str, Any]]:
    """Stops flagged needs_review by the OSM match, with their coordinates."""
    sql = text(
        """
        SELECT m.stop_uid, s.stop_lat AS lat, s.stop_lon AS lon
        FROM stop_osm_matches m
        JOIN stops s USING (stop_uid)
        WHERE m.needs_review
        ORDER BY m.stop_uid
        """
    )
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).mappings()]
    log.info("Google QA: %d needs-review stop(s) eligible", len(rows))
    return rows


def extract_vehicle_types(transit_station: Optional[dict[str, Any]]) -> set[str]:
    """All vehicleType values anywhere in the payload (unknown shapes just yield nothing)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "vehicleType" and isinstance(value, str):
                    found.add(value.upper())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(transit_station)
    return found


def google_mode_set(places: list[PlaceResult]) -> set[int]:
    """Mode codes seen across the places: vehicleType first, place type as fallback."""
    modes: set[int] = set()
    for place in places:
        vehicle_types = extract_vehicle_types(place.transit_station)
        mapped = {VEHICLE_TYPE_TO_MODE[v] for v in vehicle_types if v in VEHICLE_TYPE_TO_MODE}
        if not mapped:
            mapped = {PLACE_TYPE_TO_MODE[t] for t in place.types if t in PLACE_TYPE_TO_MODE}
        modes |= mapped
    return modes


def is_transit_place(place: PlaceResult) -> bool:
    """True when the place carries any transit-typed category."""
    return any(t in TRANSIT_PLACE_TYPES for t in place.types)


def interchange_verdict(
    our_modes: set[int], places: Optional[list[PlaceResult]]
) -> tuple[str, Optional[bool], Optional[str]]:
    """Return (verdict, mode_agreement, place_id); *places* is None when the call failed."""
    if not places:  # None (failure) or empty (nothing returned)
        return "NO_DATA", None, None
    google_modes = google_mode_set(places)
    place_id = next((p.id for p in places if is_transit_place(p) and p.id), None)
    if not google_modes:
        return "NO_DATA", None, place_id  # places exist but no mode evidence
    if our_modes and our_modes <= google_modes:
        return "CONFIRMED", True, place_id
    if our_modes & google_modes:
        return "PARTIAL", False, place_id
    return "CONFLICT", False, place_id


def stop_verdict(places: Optional[list[PlaceResult]]) -> tuple[str, Optional[str]]:
    """Return (verdict, place_id); place_id is set only for STATION_FOUND."""
    if places is None:
        return "NO_DATA", None
    transit = [p for p in places if is_transit_place(p)]
    if transit:
        return "STATION_FOUND", next((p.id for p in transit if p.id), None)
    return "NOT_FOUND", None
