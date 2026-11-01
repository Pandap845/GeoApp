"""Route geometry: real paths from shapes.txt and consecutive-stop links.

Shapes follow the streets (one line per route, direction and shape). Stop
links join each stop to the next stop of the same route and direction.
linea = route_short_name, else route_id. Missing direction_id becomes -1.
"""

from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from .near_stations import haversine_m

log = logging.getLogger(__name__)

SHAPE_COLUMNS = ["route_uid", "feed_id", "route_id", "linea", "direction_id", "shape_id"]
#: n_obs = number of timed trips behind the travel_min median (0 when travel_min is NULL).
LINK_COLUMNS = [
    "route_uid",
    "feed_id",
    "route_id",
    "linea",
    "direction_id",
    "stop_uid_a",
    "stop_uid_b",
    "travel_min",
    "n_obs",
]
TRIP_MINUTE_COLUMNS = ["route_uid", "direction_id", "trip_min"]

#: A straight-line speed above this is a wrong time; the real path is longer.
MAX_IN_VEHICLE_KMH = 80.0
#: A route direction slower than this along its shape has placeholder times.
MIN_ROUTE_KMH = 5.0


def _linea_map(feed: Any) -> pd.DataFrame:
    """Map route_id to linea (short name, else the id)."""
    try:
        routes = feed.routes
    except Exception:  # noqa: BLE001 - metadata is cosmetic, never fatal
        routes = pd.DataFrame()
    if routes is None or routes.empty or "route_id" not in routes.columns:
        return pd.DataFrame(columns=["route_id", "linea"])
    routes = routes.drop_duplicates("route_id").copy()
    routes["route_id"] = routes["route_id"].astype(str)
    short = (
        routes["route_short_name"].astype(str).str.strip()
        if "route_short_name" in routes.columns
        else pd.Series("", index=routes.index)
    )
    routes["linea"] = short.where(short.replace("nan", "") != "", routes["route_id"])
    return routes[["route_id", "linea"]]


def direction_series(trips: pd.DataFrame) -> pd.Series:
    """direction_id as int, -1 when unknown."""
    if "direction_id" not in trips.columns:
        return pd.Series(-1, index=trips.index)
    return pd.to_numeric(trips["direction_id"], errors="coerce").fillna(-1).astype(int)


def ordered_stop_times(stop_times: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stop times sorted by trip and stop_sequence, one stop per sequence; returns (kept, dropped).

    Some feeds list two alias stops under one stop_sequence; keeping the
    smallest stop_id stops the trip zig-zagging between them. dropped carries
    kept_stop_id.
    """
    ordered = stop_times.copy()
    ordered["stop_id"] = ordered["stop_id"].astype(str)
    ordered["stop_sequence"] = pd.to_numeric(ordered["stop_sequence"], errors="coerce")
    ordered = ordered.dropna(subset=["stop_sequence"]).sort_values(
        ["trip_id", "stop_sequence", "stop_id"], kind="mergesort"
    )
    duplicate = ordered.duplicated(["trip_id", "stop_sequence"])
    kept = ordered[~duplicate].reset_index(drop=True)
    dropped = ordered[duplicate].merge(
        kept[["trip_id", "stop_sequence", "stop_id"]].rename(columns={"stop_id": "kept_stop_id"}),
        on=["trip_id", "stop_sequence"],
    )
    return kept, dropped


def route_shapes(feed: Any, feed_id: str) -> gpd.GeoDataFrame:
    """One line per (route, direction, shape_id) from shapes.txt; empty if there are no shapes."""
    empty = gpd.GeoDataFrame(
        pd.DataFrame(columns=SHAPE_COLUMNS), geometry=gpd.GeoSeries([], crs="EPSG:4326")
    )
    try:
        shapes = feed.shapes
    except Exception:  # noqa: BLE001 - a broken shapes.txt never aborts phase 2
        log.warning("Feed %s: shapes.txt unreadable; route_shapes layer skipped", feed_id)
        return empty
    trips = feed.trips
    required = {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"}
    if (
        shapes is None
        or shapes.empty
        or not required.issubset(shapes.columns)
        or trips.empty
        or "shape_id" not in trips.columns
    ):
        return empty

    shapes = shapes.dropna(subset=["shape_pt_lat", "shape_pt_lon"]).copy()
    shapes["shape_pt_sequence"] = pd.to_numeric(shapes["shape_pt_sequence"], errors="coerce")
    shapes = shapes.dropna(subset=["shape_pt_sequence"]).sort_values(
        ["shape_id", "shape_pt_sequence"]
    )
    geometries = {
        shape_id: LineString(points[["shape_pt_lon", "shape_pt_lat"]].to_numpy())
        for shape_id, points in shapes.groupby("shape_id")
        if len(points) >= 2
    }
    if not geometries:
        return empty

    combos = trips.dropna(subset=["shape_id"]).copy()
    combos["shape_id"] = combos["shape_id"].astype(str)
    combos["route_id"] = combos["route_id"].astype(str)
    combos["direction_id"] = direction_series(combos)
    combos = combos.drop_duplicates(["route_id", "direction_id", "shape_id"])
    combos = combos[combos["shape_id"].isin({str(k) for k in geometries})]
    if combos.empty:
        return empty

    combos["feed_id"] = feed_id
    combos["route_uid"] = feed_id + ":" + combos["route_id"]
    combos = combos.merge(_linea_map(feed), on="route_id", how="left")
    combos["linea"] = combos["linea"].fillna(combos["route_id"])
    geom = [geometries[s] if s in geometries else geometries[str(s)] for s in combos["shape_id"]]
    return gpd.GeoDataFrame(
        combos[SHAPE_COLUMNS].reset_index(drop=True), geometry=geom, crs="EPSG:4326"
    )


def route_stop_links(feed: Any, feed_id: str) -> pd.DataFrame:
    """Consecutive-stop pairs per route and direction, with scheduled travel time.

    travel_min is the median departure gap across trips (minutes); NULL when
    the stops carry no times, in which case consumers estimate it.
    """
    stop_times, trips = feed.stop_times, feed.trips
    if (
        stop_times.empty
        or trips.empty
        or not {"trip_id", "stop_id", "stop_sequence"}.issubset(stop_times.columns)
    ):
        return pd.DataFrame(columns=LINK_COLUMNS)

    keep = ["trip_id", "stop_id", "stop_sequence"]
    has_times = "departure_time" in stop_times.columns
    if has_times:
        keep.append("departure_time")
    ordered, _ = ordered_stop_times(stop_times[keep])
    ordered["next_stop_id"] = ordered.groupby("trip_id")["stop_id"].shift(-1)
    if has_times:
        ordered["seg_s"] = (
            ordered.groupby("trip_id")["departure_time"].shift(-1) - ordered["departure_time"]
        )
        ordered.loc[ordered["seg_s"] < 0, "seg_s"] = pd.NA  # broken sequences
    else:
        ordered["seg_s"] = pd.NA
    pairs = ordered.dropna(subset=["next_stop_id"])[["trip_id", "stop_id", "next_stop_id", "seg_s"]]

    trip_meta = trips[["trip_id", "route_id"]].copy()
    trip_meta["route_id"] = trip_meta["route_id"].astype(str)
    trip_meta["direction_id"] = direction_series(trips)
    merged = pairs.merge(trip_meta, on="trip_id")
    if merged.empty:
        return pd.DataFrame(columns=LINK_COLUMNS)
    merged["seg_s"] = pd.to_numeric(merged["seg_s"], errors="coerce")
    links = merged.groupby(
        ["route_id", "direction_id", "stop_id", "next_stop_id"], as_index=False
    ).agg(seg_s=("seg_s", "median"), n_obs=("seg_s", "count"))
    links["travel_min"] = (links["seg_s"] / 60).round(1)
    links["n_obs"] = links["n_obs"].astype(int)

    links["feed_id"] = feed_id
    links["route_uid"] = feed_id + ":" + links["route_id"]
    links["stop_uid_a"] = feed_id + ":" + links["stop_id"]
    links["stop_uid_b"] = feed_id + ":" + links["next_stop_id"]
    links = links.merge(_linea_map(feed), on="route_id", how="left")
    links["linea"] = links["linea"].fillna(links["route_id"])
    return links[LINK_COLUMNS].reset_index(drop=True)


def route_trip_minutes(feed: Any, feed_id: str) -> pd.DataFrame:
    """Median scheduled trip duration in minutes per route and direction."""
    stop_times, trips = feed.stop_times, feed.trips
    if stop_times.empty or trips.empty or "departure_time" not in stop_times.columns:
        return pd.DataFrame(columns=TRIP_MINUTE_COLUMNS)
    times = pd.to_numeric(stop_times["departure_time"], errors="coerce").groupby(
        stop_times["trip_id"]
    )
    span = (times.max() - times.min()).rename("trip_s").reset_index()
    meta = trips[["trip_id", "route_id"]].copy()
    meta["route_id"] = meta["route_id"].astype(str)
    meta["direction_id"] = direction_series(trips)
    spans = span.merge(meta, on="trip_id").dropna(subset=["trip_s"])
    if spans.empty:
        return pd.DataFrame(columns=TRIP_MINUTE_COLUMNS)
    minutes = spans.groupby(["route_id", "direction_id"], as_index=False)["trip_s"].median()
    minutes["route_uid"] = feed_id + ":" + minutes["route_id"]
    minutes["trip_min"] = minutes["trip_s"] / 60
    return minutes[TRIP_MINUTE_COLUMNS]


def link_time_quality(
    links: pd.DataFrame,
    stops: pd.DataFrame,
    shapes: gpd.GeoDataFrame,
    trip_minutes: pd.DataFrame,
) -> pd.Series:
    """time_quality per link: untimed, zero, too_fast, too_slow or ok; times are not changed.

    too_fast is a straight-line speed above MAX_IN_VEHICLE_KMH (the real path
    is longer still); too_slow marks every link of a route direction whose
    shape length over its trip duration is below MIN_ROUTE_KMH.
    """
    if links.empty:
        return pd.Series(dtype=object, index=links.index)
    minutes = pd.to_numeric(links["travel_min"], errors="coerce")
    coords = stops.drop_duplicates("stop_uid").set_index("stop_uid")
    a = coords.reindex(links["stop_uid_a"]).set_index(links.index)
    b = coords.reindex(links["stop_uid_b"]).set_index(links.index)
    meters = haversine_m(a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"])
    kmh = (meters / 1000) / (minutes.where(minutes > 0) / 60)
    slow = _slow_route_directions(shapes, trip_minutes)
    is_slow = pd.Series(
        [key in slow for key in zip(links["route_uid"], links["direction_id"].astype(int))],
        index=links.index,
    )
    labels = np.select(
        [minutes.isna(), minutes == 0, kmh > MAX_IN_VEHICLE_KMH, is_slow],
        ["untimed", "zero", "too_fast", "too_slow"],
        default="ok",
    )
    return pd.Series(labels, index=links.index)


def _slow_route_directions(
    shapes: gpd.GeoDataFrame, trip_minutes: pd.DataFrame
) -> set[tuple[str, int]]:
    """(route_uid, direction_id) whose shape length over trip duration is below MIN_ROUTE_KMH."""
    if shapes.empty or trip_minutes.empty or "route_uid" not in shapes.columns:
        return set()
    km = shapes.to_crs(shapes.estimate_utm_crs()).length / 1000
    lengths = (
        pd.DataFrame(
            {
                "route_uid": shapes["route_uid"],
                "direction_id": shapes["direction_id"].astype(int),
                "km": km,
            }
        )
        .groupby(["route_uid", "direction_id"], as_index=False)["km"]
        .max()
    )
    speed = lengths.merge(trip_minutes, on=["route_uid", "direction_id"])
    speed = speed[speed["trip_min"] > 0]
    slow = speed[speed["km"] / (speed["trip_min"] / 60) < MIN_ROUTE_KMH]
    return set(zip(slow["route_uid"], slow["direction_id"].astype(int)))
