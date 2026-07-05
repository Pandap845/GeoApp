"""Route geometry for the GeoPackage: real paths and stop-to-stop links.

Two complementary representations of "the route", both per analyzed feed:

- **Route shapes** — the polylines from ``shapes.txt`` (service-filtered:
  only shapes referenced by trips active on the analyzed date). This is the
  honest geometry: it follows the streets. One feature per distinct
  ``(route, direction, shape_id)`` combination. Feeds without shapes yield
  an empty frame, never an error.
- **Route stop links** — one straight segment per distinct pair of
  *consecutive* stops of a route/direction, derived from ``stop_times``
  order. This is the route *topology* projected onto the stops: each stop
  visibly chains to the next stop of its own route (unlike the
  ``near_links`` walking overlay, which connects anything within the
  proximity threshold regardless of route).

``linea`` follows the near-stations convention: ``route_short_name``,
falling back to ``route_id``. Missing/blank ``direction_id`` becomes the
``-1`` sentinel (same rule as the headway math).
"""

from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

log = logging.getLogger(__name__)

SHAPE_COLUMNS = ["route_uid", "feed_id", "route_id", "linea", "direction_id", "shape_id"]
LINK_COLUMNS = ["route_uid", "feed_id", "route_id", "linea", "direction_id",
                "stop_uid_a", "stop_uid_b", "travel_min"]


def _linea_map(feed: Any) -> pd.DataFrame:
    """``route_id -> linea`` (short name, falling back to the id)."""
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
    """``direction_id`` as int with the ``-1`` unknown sentinel."""
    if "direction_id" not in trips.columns:
        return pd.Series(-1, index=trips.index)
    return pd.to_numeric(trips["direction_id"], errors="coerce").fillna(-1).astype(int)


def route_shapes(feed: Any, feed_id: str) -> gpd.GeoDataFrame:
    """Route path polylines from ``shapes.txt`` (may be empty).

    Args:
        feed: typed partridge feed filtered to the analyzed service date.
        feed_id: Phase 1 provenance id.

    Returns:
        GeoDataFrame (EPSG:4326) with one LINESTRING per distinct
        ``(route, direction, shape_id)``; empty when the feed ships no
        usable shapes.
    """
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
    """Distinct consecutive-stop pairs per route/direction (no geometry).

    ``travel_min`` is the SCHEDULED in-vehicle time between the two stops:
    the median departure-time difference across all trips serving the pair
    (minutes, 1 decimal). NULL when the segment has no timed stop_times
    rows (non-timepoints) - consumers fall back to an estimate.

    Args:
        feed: typed partridge feed filtered to the analyzed service date.
        feed_id: Phase 1 provenance id.

    Returns:
        Columns :data:`LINK_COLUMNS`; the GeoPackage writer draws the
        segments from the stops' geometry. Empty inputs yield an empty
        frame.
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
        .agg(seg_s=("seg_s", "median"))
    )
    links["travel_min"] = (links["seg_s"] / 60).round(1)

    links["feed_id"] = feed_id
    links["route_uid"] = feed_id + ":" + links["route_id"]
    links["stop_uid_a"] = feed_id + ":" + links["stop_id"]
    links["stop_uid_b"] = feed_id + ":" + links["next_stop_id"]
    links = links.merge(_linea_map(feed), on="route_id", how="left")
    links["linea"] = links["linea"].fillna(links["route_id"])
    return links[LINK_COLUMNS].reset_index(drop=True)
