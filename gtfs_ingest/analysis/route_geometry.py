"""Route geometry: real paths from shapes.txt and consecutive-stop links.

Shapes follow the streets (one line per route, direction and shape). Stop
links join each stop to the next stop of the same route and direction.
linea = route_short_name, else route_id. Missing direction_id becomes -1.
"""

from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

log = logging.getLogger(__name__)

SHAPE_COLUMNS = ["route_uid", "feed_id", "route_id", "linea", "direction_id", "shape_id"]
#: n_obs = number of timed trips behind the travel_min median (0 when travel_min is NULL).
LINK_COLUMNS = ["route_uid", "feed_id", "route_id", "linea", "direction_id",
                "stop_uid_a", "stop_uid_b", "travel_min", "n_obs"]


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
    short = routes["route_short_name"].astype(str).str.strip() \
        if "route_short_name" in routes.columns else pd.Series("", index=routes.index)
    routes["linea"] = short.where(short.replace("nan", "") != "", routes["route_id"])
    return routes[["route_id", "linea"]]


def _direction_series(trips: pd.DataFrame) -> pd.Series:
    """direction_id as int, -1 when unknown."""
    if "direction_id" not in trips.columns:
        return pd.Series(-1, index=trips.index)
    return pd.to_numeric(trips["direction_id"], errors="coerce").fillna(-1).astype(int)


def route_shapes(feed: Any, feed_id: str) -> gpd.GeoDataFrame:
    """One line per (route, direction, shape_id) from shapes.txt; empty if there are no shapes."""
    empty = gpd.GeoDataFrame(pd.DataFrame(columns=SHAPE_COLUMNS),
                             geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    try:
        shapes = feed.shapes
    except Exception:  # noqa: BLE001 - a broken shapes.txt never aborts phase 2
        log.warning("Feed %s: shapes.txt unreadable; route_shapes layer skipped", feed_id)
        return empty
    trips = feed.trips
    required = {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"}
    if (
        shapes is None or shapes.empty or not required.issubset(shapes.columns)
        or trips.empty or "shape_id" not in trips.columns
    ):
        return empty

    shapes = shapes.dropna(subset=["shape_pt_lat", "shape_pt_lon"]).copy()
    shapes["shape_pt_sequence"] = pd.to_numeric(shapes["shape_pt_sequence"], errors="coerce")
    shapes = shapes.dropna(subset=["shape_pt_sequence"]).sort_values(["shape_id", "shape_pt_sequence"])
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
    combos["direction_id"] = _direction_series(combos)
    combos = combos.drop_duplicates(["route_id", "direction_id", "shape_id"])
    combos = combos[combos["shape_id"].isin({str(k) for k in geometries})]
    if combos.empty:
        return empty

    combos["feed_id"] = feed_id
    combos["route_uid"] = feed_id + ":" + combos["route_id"]
    combos = combos.merge(_linea_map(feed), on="route_id", how="left")
    combos["linea"] = combos["linea"].fillna(combos["route_id"])
    geom = [geometries[s] if s in geometries else geometries[str(s)] for s in combos["shape_id"]]
    return gpd.GeoDataFrame(combos[SHAPE_COLUMNS].reset_index(drop=True),
                            geometry=geom, crs="EPSG:4326")


def route_stop_links(feed: Any, feed_id: str) -> pd.DataFrame:
    """Consecutive-stop pairs per route and direction, with scheduled travel time.

    travel_min is the median departure gap across trips (minutes); NULL when
    the stops carry no times, in which case consumers estimate it.
    """
    stop_times, trips = feed.stop_times, feed.trips
    if (
        stop_times.empty or trips.empty
        or not {"trip_id", "stop_id", "stop_sequence"}.issubset(stop_times.columns)
    ):
        return pd.DataFrame(columns=LINK_COLUMNS)

    keep = ["trip_id", "stop_id", "stop_sequence"]
    has_times = "departure_time" in stop_times.columns
    if has_times:
        keep.append("departure_time")
    ordered = stop_times[keep].copy()
    ordered["stop_sequence"] = pd.to_numeric(ordered["stop_sequence"], errors="coerce")
    ordered = ordered.dropna(subset=["stop_sequence"]).sort_values(["trip_id", "stop_sequence"])
    ordered["stop_id"] = ordered["stop_id"].astype(str)
    ordered["next_stop_id"] = ordered.groupby("trip_id")["stop_id"].shift(-1)
    if has_times:
        ordered["seg_s"] = (
            ordered.groupby("trip_id")["departure_time"].shift(-1) - ordered["departure_time"]
        )
        ordered.loc[ordered["seg_s"] < 0, "seg_s"] = pd.NA  # broken sequences
    else:
        ordered["seg_s"] = pd.NA
    pairs = ordered.dropna(subset=["next_stop_id"])[
        ["trip_id", "stop_id", "next_stop_id", "seg_s"]
    ]

    trip_meta = trips[["trip_id", "route_id"]].copy()
    trip_meta["route_id"] = trip_meta["route_id"].astype(str)
    trip_meta["direction_id"] = _direction_series(trips)
    merged = pairs.merge(trip_meta, on="trip_id")
    if merged.empty:
        return pd.DataFrame(columns=LINK_COLUMNS)
    merged["seg_s"] = pd.to_numeric(merged["seg_s"], errors="coerce")
    links = (
        merged.groupby(["route_id", "direction_id", "stop_id", "next_stop_id"], as_index=False)
        .agg(seg_s=("seg_s", "median"), n_obs=("seg_s", "count"))
    )
    links["travel_min"] = (links["seg_s"] / 60).round(1)
    links["n_obs"] = links["n_obs"].astype(int)

    links["feed_id"] = feed_id
    links["route_uid"] = feed_id + ":" + links["route_id"]
    links["stop_uid_a"] = feed_id + ":" + links["stop_id"]
    links["stop_uid_b"] = feed_id + ":" + links["next_stop_id"]
    links = links.merge(_linea_map(feed), on="route_id", how="left")
    links["linea"] = links["linea"].fillna(links["route_id"])
    return links[LINK_COLUMNS].reset_index(drop=True)
