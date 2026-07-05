"""GeoPackage export of the analysis outputs (QGIS/GDAL-ready).

Writes ``transit_analysis.gpkg`` with up to four layers, all EPSG:4326:

- ``stops`` (POINT): every analyzed stop with ``stop_num_id``, ``stop_uid``,
  feed/stop ids, name, ``location_type``, ``parent_station``, plus the
  ``stop_line_ids`` and ``near_stations`` strings from the CSV export - the
  layer is self-contained for map use.
- ``near_links`` (LINESTRING): one line per near-stop pair (within the
  proximity threshold), carrying both numeric ids, the geodesic distance,
  and the estimated walking minutes. This is the WALKING overlay - it
  connects any two stops that are close, regardless of route or direction.
- ``route_shapes`` (LINESTRING): the real route paths from ``shapes.txt``
  per (route, direction, shape) - follows the streets. Only written when
  at least one analyzed feed ships shapes.
- ``route_stop_links`` (LINESTRING): one straight segment per consecutive
  stop pair of a route/direction (``stop_times`` order) - the route
  topology on the stops, distinguishing "next stop of my route" from
  "stop I can walk to".

GeoPackage supports 64-bit integers, so the numeric ids survive intact
(unlike e.g. shapefiles, which would truncate them).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from ..models import numeric_stop_id
from .near_stations import walk_minutes

log = logging.getLogger(__name__)

GEOPACKAGE_NAME = "transit_analysis.gpkg"

#: Attribute columns of the stops layer (geometry column excluded).
#: ``ID`` / ``Modalidad`` mirror near_stations.csv (the relation strings
#: reference ``ID``); ``stop_num_id`` remains the database join key.
STOPS_LAYER_COLUMNS = [
    "ID", "stop_num_id", "stop_uid", "feed_id", "stop_id", "station",
    "Modalidad", "location_type", "parent_station", "stop_line_ids",
    "near_stations", "same_line_stations",
]


def write_geopackage(
    stops: gpd.GeoDataFrame,
    near: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    out_dir: Path,
    route_shapes: Optional[gpd.GeoDataFrame] = None,
    stop_links: Optional[pd.DataFrame] = None,
) -> Path:
    """Write the analysis GeoPackage.

    Args:
        stops: normalized stops of all feeds (EPSG:4326 geometry).
        near: ``near_stations.build_near_stations`` output (provides the
            ``station``/``stop_line_ids``/``near_stations`` attributes).
        stop_pairs: proximity stop pairs (``stop_uid_a, stop_uid_b,
            distance_m``) used to draw the ``near_links`` lines.
        out_dir: report directory (created if needed).
        route_shapes: ``route_geometry.route_shapes`` output (all feeds
            concatenated); the layer is skipped when None/empty.
        stop_links: ``route_geometry.route_stop_links`` output (all feeds
            concatenated); the layer is skipped when None/empty.

    Returns:
        The written path.

    Raises:
        OSError / pyogrio errors: if the file cannot be (re)written - a
            run-level problem, consistent with the other report outputs.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / GEOPACKAGE_NAME
    if path.exists():
        path.unlink()  # rewrite atomically per run rather than appending layers

    stops_layer = _stops_layer(stops, near)
    stops_layer.to_file(path, layer="stops", driver="GPKG")

    links_layer = _near_links_layer(stops, stop_pairs)
    links_layer.to_file(path, layer="near_links", driver="GPKG", mode="a")

    n_shapes = n_route_links = 0
    if route_shapes is not None and not route_shapes.empty:
        route_shapes.to_file(path, layer="route_shapes", driver="GPKG", mode="a")
        n_shapes = len(route_shapes)
    route_links_layer = _route_stop_links_layer(stops, stop_links)
    if not route_links_layer.empty:
        route_links_layer.to_file(path, layer="route_stop_links", driver="GPKG", mode="a")
        n_route_links = len(route_links_layer)

    log.info(
        "GeoPackage written: %s (stops: %d, near_links: %d, route_shapes: %d, "
        "route_stop_links: %d)",
        path, len(stops_layer), len(links_layer), n_shapes, n_route_links,
    )
    return path


def _stops_layer(stops: gpd.GeoDataFrame, near: pd.DataFrame) -> gpd.GeoDataFrame:
    """Stops as a self-contained point layer (near attributes merged in)."""
    layer = stops[
        ["stop_num_id", "stop_uid", "feed_id", "stop_id",
         "location_type", "parent_station", "geometry"]
    ].copy()
    extra_cols = ["stop_uid", "ID", "station", "Modalidad", "stop_line_ids",
                  "near_stations", "same_line_stations"]
    extras = near[extra_cols] if not near.empty else pd.DataFrame(columns=extra_cols)
    layer = layer.merge(extras, on="stop_uid", how="left")
    layer["ID"] = pd.to_numeric(layer["ID"], errors="coerce").fillna(0).astype(int)
    layer["station"] = layer["station"].fillna("")
    layer["Modalidad"] = layer["Modalidad"].fillna("")
    layer["stop_line_ids"] = layer["stop_line_ids"].fillna("")
    layer["near_stations"] = layer["near_stations"].fillna("[]")
    layer["same_line_stations"] = layer["same_line_stations"].fillna("[]")
    # GPKG has no NaN for strings; parent_station NULLs are fine as None.
    layer["parent_station"] = layer["parent_station"].where(layer["parent_station"].notna(), None)
    gdf = gpd.GeoDataFrame(layer[STOPS_LAYER_COLUMNS + ["geometry"]],
                           geometry="geometry", crs=stops.crs or "EPSG:4326")
    return gdf


def _near_links_layer(stops: gpd.GeoDataFrame, stop_pairs: pd.DataFrame) -> gpd.GeoDataFrame:
    """One LINESTRING per near pair, attributed with ids/distance/minutes."""
    columns = ["num_id_a", "num_id_b", "stop_uid_a", "stop_uid_b", "distance_m", "walk_min"]
    if stop_pairs.empty:
        return gpd.GeoDataFrame(pd.DataFrame(columns=columns),
                                geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    geom_by_uid = stops.set_index("stop_uid")["geometry"]
    pairs = stop_pairs[
        stop_pairs["stop_uid_a"].isin(geom_by_uid.index)
        & stop_pairs["stop_uid_b"].isin(geom_by_uid.index)
    ].copy()
    pairs["num_id_a"] = pairs["stop_uid_a"].map(numeric_stop_id)
    pairs["num_id_b"] = pairs["stop_uid_b"].map(numeric_stop_id)
    pairs["walk_min"] = walk_minutes(pairs["distance_m"])
    pairs["distance_m"] = pairs["distance_m"].round(2)
    lines = [
        LineString([geom_by_uid[a], geom_by_uid[b]])
        for a, b in zip(pairs["stop_uid_a"], pairs["stop_uid_b"])
    ]
    return gpd.GeoDataFrame(pairs[columns], geometry=lines, crs=stops.crs or "EPSG:4326")


def _route_stop_links_layer(
    stops: gpd.GeoDataFrame, stop_links: Optional[pd.DataFrame]
) -> gpd.GeoDataFrame:
    """One LINESTRING per consecutive stop pair of a route/direction.

    Pairs whose stops were dropped during normalization (no geometry) are
    skipped silently - the layer shows what can be drawn, the DB tables
    remain the source of truth.
    """
    columns = ["route_uid", "feed_id", "linea", "direction_id", "stop_uid_a", "stop_uid_b"]
    if stop_links is None or stop_links.empty:
        return gpd.GeoDataFrame(pd.DataFrame(columns=columns),
                                geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    geom_by_uid = stops.drop_duplicates("stop_uid").set_index("stop_uid")["geometry"]
    links = stop_links[
        stop_links["stop_uid_a"].isin(geom_by_uid.index)
        & stop_links["stop_uid_b"].isin(geom_by_uid.index)
    ].copy()
    lines = [
        LineString([geom_by_uid[a], geom_by_uid[b]])
        for a, b in zip(links["stop_uid_a"], links["stop_uid_b"])
    ]
    return gpd.GeoDataFrame(links[columns].reset_index(drop=True), geometry=lines,
                            crs=stops.crs or "EPSG:4326")
