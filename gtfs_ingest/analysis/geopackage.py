"""Write transit_analysis.gpkg (EPSG:4326) for QGIS.

Layers: stops (points with the near_stations attributes), near_links
(walkable stop pairs), route_shapes (real paths), route_stop_links (next stop
on the same route) and station_network (routable edges).
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

#: ID matches near_stations.csv; stop_num_id is the database join key.
STOPS_LAYER_COLUMNS = [
    "ID", "stop_num_id", "stop_uid", "feed_id", "stop_id", "station",
    "system", "location_type", "parent_station", "stop_line_ids",
    "near_stations", "same_line_stations",
]


def write_geopackage(
    stops: gpd.GeoDataFrame,
    near: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    out_dir: Path,
    route_shapes: Optional[gpd.GeoDataFrame] = None,
    stop_links: Optional[pd.DataFrame] = None,
    station_edges: Optional[gpd.GeoDataFrame] = None,
) -> Path:
    """Write the GeoPackage, replacing any previous one. Optional layers are skipped when empty."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / GEOPACKAGE_NAME
    if path.exists():
        path.unlink()  # start fresh instead of appending to old layers

    stops_layer = _stops_layer(stops, near)
    stops_layer.to_file(path, layer="stops", driver="GPKG")

    links_layer = _near_links_layer(stops, stop_pairs)
    links_layer.to_file(path, layer="near_links", driver="GPKG", mode="a")

    n_shapes = n_route_links = n_station_edges = 0
    if route_shapes is not None and not route_shapes.empty:
        route_shapes.to_file(path, layer="route_shapes", driver="GPKG", mode="a")
        n_shapes = len(route_shapes)
    route_links_layer = _route_stop_links_layer(stops, stop_links)
    if not route_links_layer.empty:
        route_links_layer.to_file(path, layer="route_stop_links", driver="GPKG", mode="a")
        n_route_links = len(route_links_layer)
    if station_edges is not None and not station_edges.empty:
        station_edges.to_file(path, layer="station_network", driver="GPKG", mode="a")
        n_station_edges = len(station_edges)

    log.info(
        "GeoPackage written: %s (stops: %d, near_links: %d, route_shapes: %d, "
        "route_stop_links: %d, station_network: %d)",
        path, len(stops_layer), len(links_layer), n_shapes, n_route_links, n_station_edges,
    )
    return path


def _stops_layer(stops: gpd.GeoDataFrame, near: pd.DataFrame) -> gpd.GeoDataFrame:
    """Stop points with the near_stations columns merged in."""
    layer = stops[
        ["stop_num_id", "stop_uid", "feed_id", "stop_id",
         "location_type", "parent_station", "geometry"]
    ].copy()
    extra_cols = ["stop_uid", "ID", "station", "system", "stop_line_ids",
                  "near_stations", "same_line_stations"]
    extras = near[extra_cols] if not near.empty else pd.DataFrame(columns=extra_cols)
    layer = layer.merge(extras, on="stop_uid", how="left")
    layer["ID"] = pd.to_numeric(layer["ID"], errors="coerce").fillna(0).astype(int)
    layer["station"] = layer["station"].fillna("")
    layer["system"] = layer["system"].fillna("")
    layer["stop_line_ids"] = layer["stop_line_ids"].fillna("")
    layer["near_stations"] = layer["near_stations"].fillna("[]")
    layer["same_line_stations"] = layer["same_line_stations"].fillna("[]")
    # GeoPackage strings can't be NaN; use None.
    layer["parent_station"] = layer["parent_station"].where(layer["parent_station"].notna(), None)
    gdf = gpd.GeoDataFrame(layer[STOPS_LAYER_COLUMNS + ["geometry"]],
                           geometry="geometry", crs=stops.crs or "EPSG:4326")
    return gdf


def _near_links_layer(stops: gpd.GeoDataFrame, stop_pairs: pd.DataFrame) -> gpd.GeoDataFrame:
    """One line per walkable stop pair, with distance and walking minutes."""
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
    """One line per consecutive stop pair; pairs with a stop lacking geometry are skipped."""
    columns = ["route_uid", "feed_id", "linea", "direction_id",
               "stop_uid_a", "stop_uid_b", "travel_min"]
    if stop_links is None or stop_links.empty:
        return gpd.GeoDataFrame(pd.DataFrame(columns=columns),
                                geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    geom_by_uid = stops.drop_duplicates("stop_uid").set_index("stop_uid")["geometry"]
    links = stop_links.copy()
    if "travel_min" not in links.columns:
        links["travel_min"] = None
    links = links[
        links["stop_uid_a"].isin(geom_by_uid.index)
        & links["stop_uid_b"].isin(geom_by_uid.index)
    ].copy()
    lines = [
        LineString([geom_by_uid[a], geom_by_uid[b]])
        for a, b in zip(links["stop_uid_a"], links["stop_uid_b"])
    ]
    return gpd.GeoDataFrame(links[columns].reset_index(drop=True), geometry=lines,
                            crs=stops.crs or "EPSG:4326")
