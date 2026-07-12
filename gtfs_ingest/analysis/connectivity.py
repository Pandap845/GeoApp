"""Route-to-route connectivity edges.

Two routes are *connected* when (checked in this precedence order, strongest
wins as the edge's ``connection_type``):

1. ``same_stop``            - they serve at least one identical stop
                              (same ``stop_uid``, therefore same feed).
2. ``same_parent_station``  - they serve stops sharing a ``parent_station``
                              (a stop that *is* the parent counts as part of
                              its own station); feed-scoped, like stop ids.
3. ``same_station_dedup``   - they serve stops that cross-feed station
                              deduplication (``analysis.dedupe``) merged into
                              one canonical physical station. Inferred, so it
                              is kept distinguishable from the GTFS-declared
                              ``same_parent_station``.
4. ``proximity``            - they serve stops within a configurable
                              threshold (default 150 m). Distances are
                              geodesic: computed in PostGIS on ``geography``
                              (authoritative path) or locally in a projected
                              metric CRS (UTM estimate) when no database is
                              available (dry runs, tests). Proximity and
                              same_station_dedup are the only connection
                              types that can cross feeds - ids never match
                              across feeds, but geography does.

Routes are identified by ``route_uid = "{feed_id}:{route_id}"`` so edges are
unambiguous across feeds. Edges are undirected and normalized to
``route_a < route_b`` (string order); every pair appears at most once with:
``connection_type`` (strongest), ``shared_stop_count`` (identical stops, 0
if none) and ``min_stop_distance_m`` (0.0 for same-stop pairs, the smallest
inter-stop distance found for proximity pairs, NULL for parent-only pairs
whose stops are farther apart than the threshold).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import geopandas as gpd
import pandas as pd
from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

PAIR_KEY = ["route_a", "route_b"]
EDGE_COLUMNS = PAIR_KEY + [
    "feed_id_a", "feed_id_b", "connection_type", "shared_stop_count", "min_stop_distance_m",
]


def route_stop_map(feed: Any, feed_id: str) -> pd.DataFrame:
    """Distinct (route, stop) service pairs of one filtered feed.

    Args:
        feed: typed partridge feed filtered to the analyzed service date -
            connectivity thus reflects stops *actually served* that day.
        feed_id: Phase 1 provenance id, used to build the ``*_uid`` keys.

    Returns:
        Columns ``feed_id, route_id, route_uid, stop_id, stop_uid``, one row
        per distinct route/stop pairing. Empty inputs yield an empty frame.
    """
    columns = ["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"]
    stop_times, trips = feed.stop_times, feed.trips
    if stop_times.empty or trips.empty:
        return pd.DataFrame(columns=columns)
    pairs = (
        stop_times[["trip_id", "stop_id"]]
        .merge(trips[["trip_id", "route_id"]], on="trip_id")
        .drop_duplicates(["route_id", "stop_id"])
        .astype({"route_id": str, "stop_id": str})
    )
    pairs["feed_id"] = feed_id
    pairs["route_uid"] = feed_id + ":" + pairs["route_id"]
    pairs["stop_uid"] = feed_id + ":" + pairs["stop_id"]
    return pairs[columns].reset_index(drop=True)


def station_map(stops: pd.DataFrame) -> pd.DataFrame:
    """Map stops to a station key derived from ``parent_station``.

    A child stop maps to its parent's ``stop_uid``; a stop that appears as
    someone's parent maps to itself (so routes serving the station stop
    directly connect with routes serving its platforms).

    Args:
        stops: normalized stops of one or more feeds (Phase 1 normalizer
            output: has ``stop_uid``, ``feed_id``, ``parent_station``).

    Returns:
        Columns ``stop_uid, station_uid`` - only stops belonging to some
        station are included.
    """
    columns = ["stop_uid", "station_uid"]
    if stops.empty:
        return pd.DataFrame(columns=columns)
    has_parent = stops[stops["parent_station"].notna()].copy()
    if has_parent.empty:
        return pd.DataFrame(columns=columns)
    children = pd.DataFrame(
        {
            "stop_uid": has_parent["stop_uid"],
            "station_uid": has_parent["feed_id"] + ":" + has_parent["parent_station"].astype(str),
        }
    )
    parents = pd.DataFrame({"stop_uid": children["station_uid"].unique()})
    parents["station_uid"] = parents["stop_uid"]
    return pd.concat([children, parents], ignore_index=True).drop_duplicates("stop_uid")[columns]


def proximity_pairs_postgis(
    engine: Engine, feed_ids: list[str], threshold_m: float
) -> pd.DataFrame:
    """Stop pairs within *threshold_m* meters, computed in PostGIS.

    Uses ``geography`` casts so distances are geodesic meters regardless of
    latitude, against the Phase 1 ``stops`` table (the pipeline refreshes it
    for the analyzed feeds before calling this).

    Args:
        engine: Phase 1 database engine.
        feed_ids: restrict candidates to these feeds' stops.
        threshold_m: maximum separation in meters.

    Returns:
        Columns ``stop_uid_a, stop_uid_b, distance_m`` with
        ``stop_uid_a < stop_uid_b`` (each unordered pair once).

    Raises:
        sqlalchemy.exc.SQLAlchemyError: on database failure.
    """
    sql = text(
        """
        SELECT a.stop_uid AS stop_uid_a,
               b.stop_uid AS stop_uid_b,
               ST_Distance(a.geom::geography, b.geom::geography) AS distance_m
        FROM stops a
        JOIN stops b
          ON a.stop_uid < b.stop_uid
         AND ST_DWithin(a.geom::geography, b.geom::geography, :threshold)
        WHERE a.feed_id = ANY(:feeds) AND b.feed_id = ANY(:feeds)
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"threshold": threshold_m, "feeds": feed_ids}).mappings().all()
    return pd.DataFrame(rows, columns=["stop_uid_a", "stop_uid_b", "distance_m"])


def proximity_pairs_local(stops: gpd.GeoDataFrame, threshold_m: float) -> pd.DataFrame:
    """Stop pairs within *threshold_m* meters, computed locally (no DB).

    Fallback for dry runs and tests: reprojects the stops to an estimated
    UTM zone and measures planar distances there. Accurate to well under a
    meter at these scales for data within one region; the PostGIS geography
    path is authoritative for production loads.

    Args:
        stops: Phase 1 normalized stops (EPSG:4326 GeoDataFrame).
        threshold_m: maximum separation in meters.

    Returns:
        Same shape as :func:`proximity_pairs_postgis`.
    """
    columns = ["stop_uid_a", "stop_uid_b", "distance_m"]
    if len(stops) < 2:
        return pd.DataFrame(columns=columns)
    projected = stops[["stop_uid", "geometry"]].to_crs(stops.estimate_utm_crs())
    joined = gpd.sjoin(projected, projected, predicate="dwithin", distance=threshold_m)
    joined = joined[joined["stop_uid_left"] < joined["stop_uid_right"]]
    if joined.empty:
        return pd.DataFrame(columns=columns)
    right_geoms = projected.geometry.iloc[joined["index_right"]].to_numpy()
    distances = joined.geometry.distance(
        gpd.GeoSeries(right_geoms, index=joined.index, crs=projected.crs)
    )
    return pd.DataFrame(
        {
            "stop_uid_a": joined["stop_uid_left"].to_numpy(),
            "stop_uid_b": joined["stop_uid_right"].to_numpy(),
            "distance_m": distances.to_numpy(),
        }
    )


def _normalize_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Drop self-pairs and order each pair as route_a < route_b."""
    df = df[df["route_a"] != df["route_b"]].copy()
    swap = df["route_a"] > df["route_b"]
    df.loc[swap, ["route_a", "route_b"]] = df.loc[swap, ["route_b", "route_a"]].to_numpy()
    return df


def _same_stop_pairs(route_stops: pd.DataFrame) -> pd.DataFrame:
    """Pairs serving an identical stop, with the count of shared stops."""
    joined = route_stops.merge(route_stops, on="stop_uid", suffixes=("_a", "_b"))
    pairs = _normalize_pairs(
        joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    )
    if pairs.empty:
        return pd.DataFrame(columns=PAIR_KEY + ["shared_stop_count"])
    return (
        pairs.groupby(PAIR_KEY)["stop_uid"].nunique().rename("shared_stop_count").reset_index()
    )


def _parent_station_pairs(route_stops: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """Pairs serving stops of the same station (parent_station grouping)."""
    with_station = route_stops.merge(stations, on="stop_uid")
    joined = with_station.merge(with_station, on="station_uid", suffixes=("_a", "_b"))
    pairs = _normalize_pairs(
        joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    )
    return pairs[PAIR_KEY].drop_duplicates().reset_index(drop=True)


def _dedup_station_pairs(route_stops: pd.DataFrame, canonical: pd.DataFrame) -> pd.DataFrame:
    """Pairs serving stops merged into one canonical station by dedupe."""
    if canonical.empty:
        return pd.DataFrame(columns=PAIR_KEY)
    with_canon = route_stops.merge(
        canonical[["stop_uid", "canonical_uid"]], on="stop_uid"
    )
    joined = with_canon.merge(with_canon, on="canonical_uid", suffixes=("_a", "_b"))
    pairs = _normalize_pairs(
        joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    )
    return pairs[PAIR_KEY].drop_duplicates().reset_index(drop=True)


def _proximity_pairs(route_stops: pd.DataFrame, stop_pairs: pd.DataFrame) -> pd.DataFrame:
    """Pairs serving stops within threshold, with the minimum distance."""
    if stop_pairs.empty:
        return pd.DataFrame(columns=PAIR_KEY + ["min_stop_distance_m"])
    routes_at = route_stops[["stop_uid", "route_uid"]]
    joined = (
        stop_pairs.merge(routes_at.rename(columns={"stop_uid": "stop_uid_a", "route_uid": "route_a"}), on="stop_uid_a")
        .merge(routes_at.rename(columns={"stop_uid": "stop_uid_b", "route_uid": "route_b"}), on="stop_uid_b")
    )
    pairs = _normalize_pairs(joined)
    if pairs.empty:
        return pd.DataFrame(columns=PAIR_KEY + ["min_stop_distance_m"])
    return (
        pairs.groupby(PAIR_KEY)["distance_m"].min().rename("min_stop_distance_m").reset_index()
    )


def build_edges(
    route_stops: pd.DataFrame,
    stations: pd.DataFrame,
    stop_pairs: Optional[pd.DataFrame] = None,
    canonical: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Combine all connection evidence into the final undirected edge list.

    Args:
        route_stops: concatenated :func:`route_stop_map` frames (all feeds).
        stations: concatenated :func:`station_map` frames.
        stop_pairs: proximity stop pairs from one of the
            ``proximity_pairs_*`` functions (``None`` -> no proximity edges).
        canonical: cross-feed dedupe membership map
            (``dedupe.canonical_map``); ``None``/empty -> no dedup edges.

    Returns:
        One row per connected route pair with :data:`EDGE_COLUMNS`;
        ``connection_type`` follows the precedence documented in the module
        docstring. Empty inputs yield an empty frame.
    """
    if route_stops.empty:
        return pd.DataFrame(columns=EDGE_COLUMNS)

    same = _same_stop_pairs(route_stops)
    parent = _parent_station_pairs(route_stops, stations).assign(via_station=True)
    dedup = _dedup_station_pairs(
        route_stops, canonical if canonical is not None else pd.DataFrame()
    ).assign(via_dedup=True)
    prox = _proximity_pairs(route_stops, stop_pairs if stop_pairs is not None else pd.DataFrame())

    edges = (
        same.merge(parent, on=PAIR_KEY, how="outer")
        .merge(dedup, on=PAIR_KEY, how="outer")
        .merge(prox, on=PAIR_KEY, how="outer")
    )
    if edges.empty:
        return pd.DataFrame(columns=EDGE_COLUMNS)

    edges["shared_stop_count"] = edges.get("shared_stop_count", 0)
    edges["shared_stop_count"] = edges["shared_stop_count"].fillna(0).astype(int)
    edges["via_station"] = edges.get("via_station", False)
    edges["via_station"] = edges["via_station"].fillna(False)
    edges["via_dedup"] = edges.get("via_dedup", False)
    edges["via_dedup"] = edges["via_dedup"].fillna(False)
    if "min_stop_distance_m" not in edges.columns:
        edges["min_stop_distance_m"] = pd.NA

    edges["connection_type"] = "proximity"
    edges.loc[edges["via_dedup"], "connection_type"] = "same_station_dedup"
    edges.loc[edges["via_station"], "connection_type"] = "same_parent_station"
    edges.loc[edges["shared_stop_count"] > 0, "connection_type"] = "same_stop"
    # Sharing a stop means zero distance by definition.
    edges.loc[edges["shared_stop_count"] > 0, "min_stop_distance_m"] = 0.0

    edges["feed_id_a"] = edges["route_a"].str.split(":", n=1).str[0]
    edges["feed_id_b"] = edges["route_b"].str.split(":", n=1).str[0]
    result = edges[EDGE_COLUMNS].sort_values(PAIR_KEY).reset_index(drop=True)
    log.info(
        "Connectivity: %d route pairs (%s)",
        len(result),
        ", ".join(f"{k}={v}" for k, v in result["connection_type"].value_counts().items()) or "none",
    )
    return result
