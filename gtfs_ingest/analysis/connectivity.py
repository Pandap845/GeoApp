"""Route-to-route connections, plus the stop maps and proximity pairs they come from.

Two routes connect when they serve stops that are (strongest first):
same_stop (identical stop), same_parent_station, same_station_dedup (one
deduplicated station) or proximity (within the threshold). Only the last two
can link different feeds. Each route pair appears once, route_a < route_b,
labelled with its strongest connection.
"""

from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
import pandas as pd
from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

PAIR_KEY = ["route_a", "route_b"]
EDGE_COLUMNS = PAIR_KEY + [
    "feed_id_a",
    "feed_id_b",
    "connection_type",
    "shared_stop_count",
    "min_stop_distance_m",
]


def route_stop_map(feed: Any, feed_id: str) -> pd.DataFrame:
    """Distinct (route, stop) pairs served on the analyzed day."""
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
    """Map each child stop, and each parent itself, to its station's stop_uid."""
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
    """Stop pairs (a < b) within *threshold_m* geodesic meters, computed in PostGIS."""
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
        ORDER BY a.stop_uid, b.stop_uid
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"threshold": threshold_m, "feeds": feed_ids}).mappings().all()
    return pd.DataFrame(rows, columns=["stop_uid_a", "stop_uid_b", "distance_m"])


def proximity_pairs_local(stops: gpd.GeoDataFrame, threshold_m: float) -> pd.DataFrame:
    """Same as proximity_pairs_postgis, computed locally in a UTM projection (no database)."""
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
    pairs = pd.DataFrame(
        {
            "stop_uid_a": joined["stop_uid_left"].to_numpy(),
            "stop_uid_b": joined["stop_uid_right"].to_numpy(),
            "distance_m": distances.to_numpy(),
        }
    )
    # Same order as the PostGIS query.
    return pairs.sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort").reset_index(drop=True)


def _normalize_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Drop self-pairs and order each pair so route_a < route_b."""
    df = df[df["route_a"] != df["route_b"]].copy()
    swap = df["route_a"] > df["route_b"]
    df.loc[swap, ["route_a", "route_b"]] = df.loc[swap, ["route_b", "route_a"]].to_numpy()
    return df


def _same_stop_pairs(route_stops: pd.DataFrame) -> pd.DataFrame:
    """Route pairs serving the same stop, with how many stops they share."""
    joined = route_stops.merge(route_stops, on="stop_uid", suffixes=("_a", "_b"))
    pairs = _normalize_pairs(
        joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    )
    if pairs.empty:
        return pd.DataFrame(columns=PAIR_KEY + ["shared_stop_count"])
    return pairs.groupby(PAIR_KEY)["stop_uid"].nunique().rename("shared_stop_count").reset_index()


def _parent_station_pairs(route_stops: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """Route pairs serving stops of the same parent station."""
    with_station = route_stops.merge(stations, on="stop_uid")
    joined = with_station.merge(with_station, on="station_uid", suffixes=("_a", "_b"))
    pairs = _normalize_pairs(
        joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    )
    return pairs[PAIR_KEY].drop_duplicates().reset_index(drop=True)


def _dedup_station_pairs(route_stops: pd.DataFrame, canonical: pd.DataFrame) -> pd.DataFrame:
    """Route pairs serving stops of the same dedupe group."""
    if canonical.empty:
        return pd.DataFrame(columns=PAIR_KEY)
    with_canon = route_stops.merge(canonical[["stop_uid", "canonical_uid"]], on="stop_uid")
    joined = with_canon.merge(with_canon, on="canonical_uid", suffixes=("_a", "_b"))
    pairs = _normalize_pairs(
        joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    )
    return pairs[PAIR_KEY].drop_duplicates().reset_index(drop=True)


def _proximity_pairs(route_stops: pd.DataFrame, stop_pairs: pd.DataFrame) -> pd.DataFrame:
    """Route pairs serving nearby stops, with the smallest distance between them."""
    if stop_pairs.empty:
        return pd.DataFrame(columns=PAIR_KEY + ["min_stop_distance_m"])
    routes_at = route_stops[["stop_uid", "route_uid"]]
    joined = stop_pairs.merge(
        routes_at.rename(columns={"stop_uid": "stop_uid_a", "route_uid": "route_a"}),
        on="stop_uid_a",
    ).merge(
        routes_at.rename(columns={"stop_uid": "stop_uid_b", "route_uid": "route_b"}),
        on="stop_uid_b",
    )
    pairs = _normalize_pairs(joined)
    if pairs.empty:
        return pd.DataFrame(columns=PAIR_KEY + ["min_stop_distance_m"])
    return pairs.groupby(PAIR_KEY)["distance_m"].min().rename("min_stop_distance_m").reset_index()


def build_edges(
    route_stops: pd.DataFrame,
    stations: pd.DataFrame,
    stop_pairs: pd.DataFrame | None = None,
    canonical: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per connected route pair, labelled with its strongest connection type."""
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
        ", ".join(f"{k}={v}" for k, v in result["connection_type"].value_counts().items())
        or "none",
    )
    return result


#: One row per route pair and stop pair: where two routes connect.
CONNECTION_STOP_COLUMNS = [
    "route_a",
    "route_b",
    "stop_uid_a",
    "stop_uid_b",
    "connection_type",
    "distance_m",
]

#: Strongest evidence first; a stop pair keeps only its strongest type.
_TYPE_ORDER = ["same_stop", "same_parent_station", "same_station_dedup", "proximity"]


def _orient(df: pd.DataFrame) -> pd.DataFrame:
    """Drop self-pairs and swap rows so route_a < route_b, keeping each stop with its route."""
    df = df[df["route_a"] != df["route_b"]].copy()
    swap = df["route_a"] > df["route_b"]
    cols = ["route_a", "route_b", "stop_uid_a", "stop_uid_b"]
    df.loc[swap, cols] = df.loc[swap, ["route_b", "route_a", "stop_uid_b", "stop_uid_a"]].to_numpy()
    return df


def _stops_sharing(route_stops: pd.DataFrame, groups: pd.DataFrame, key: str) -> pd.DataFrame:
    """Route/stop pairs whose two different stops share *key* (a station or dedupe group)."""
    with_key = route_stops[["route_uid", "stop_uid"]].merge(
        groups[["stop_uid", key]], on="stop_uid"
    )
    joined = with_key.merge(with_key, on=key, suffixes=("_a", "_b"))
    joined = joined[joined["stop_uid_a"] != joined["stop_uid_b"]]
    return joined.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})


def connection_stops(
    route_stops: pd.DataFrame,
    stations: pd.DataFrame,
    stop_pairs: pd.DataFrame | None,
    canonical: pd.DataFrame | None,
    stops: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Every stop pair that connects two routes, with its type and distance.

    Uses the same evidence as build_edges, so its route pairs and their
    strongest types match the route graph exactly.
    """
    if route_stops.empty:
        return pd.DataFrame(columns=CONNECTION_STOP_COLUMNS)
    served = route_stops[["route_uid", "stop_uid"]]
    frames = []

    same = served.merge(served, on="stop_uid", suffixes=("_a", "_b"))
    same = same.rename(columns={"route_uid_a": "route_a", "route_uid_b": "route_b"})
    same["stop_uid_a"] = same["stop_uid_b"] = same["stop_uid"]
    frames.append(same.assign(connection_type="same_stop", distance_m=0.0))

    if not stations.empty:
        parent = _stops_sharing(route_stops, stations, "station_uid")
        frames.append(parent.assign(connection_type="same_parent_station", distance_m=pd.NA))
    if canonical is not None and not canonical.empty:
        dedup = _stops_sharing(route_stops, canonical, "canonical_uid")
        frames.append(dedup.assign(connection_type="same_station_dedup", distance_m=pd.NA))

    if stop_pairs is not None and not stop_pairs.empty:
        prox = stop_pairs[["stop_uid_a", "stop_uid_b", "distance_m"]].merge(
            served.rename(columns={"stop_uid": "stop_uid_a", "route_uid": "route_a"}),
            on="stop_uid_a",
        )
        prox = prox.merge(
            served.rename(columns={"stop_uid": "stop_uid_b", "route_uid": "route_b"}),
            on="stop_uid_b",
        )
        frames.append(prox.assign(connection_type="proximity"))

    rows = _orient(pd.concat([f[CONNECTION_STOP_COLUMNS] for f in frames], ignore_index=True))
    rows["rank"] = rows["connection_type"].map(_TYPE_ORDER.index)
    key = ["route_a", "route_b", "stop_uid_a", "stop_uid_b"]
    rows = (
        rows.sort_values([*key, "rank"], kind="mergesort")
        .drop_duplicates(key)
        .reset_index(drop=True)
    )
    rows["distance_m"] = _fill_distances(rows, stops)
    return rows[CONNECTION_STOP_COLUMNS]


def _fill_distances(rows: pd.DataFrame, stops: gpd.GeoDataFrame) -> pd.Series:
    """Metres between the two stops where the evidence carried no distance (UTM, as local pairs)."""
    distance = pd.to_numeric(rows["distance_m"], errors="coerce")
    missing = distance.isna()
    if not missing.any() or stops.empty:
        return distance.round(2)
    projected = stops[["stop_uid", "geometry"]].to_crs(stops.estimate_utm_crs())
    geom = projected.set_index("stop_uid")["geometry"]
    a = gpd.GeoSeries(rows.loc[missing, "stop_uid_a"].map(geom), crs=projected.crs)
    b = gpd.GeoSeries(rows.loc[missing, "stop_uid_b"].map(geom), crs=projected.crs)
    distance.loc[missing] = a.distance(b).to_numpy()
    return distance.round(2)
