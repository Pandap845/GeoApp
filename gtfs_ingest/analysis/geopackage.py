"""GeoPackage export of the analysis outputs (QGIS/GDAL-ready).

Writes ``transit_analysis.gpkg`` with two layers, both EPSG:4326:

- ``stops`` (POINT): every analyzed stop with ``stop_num_id``, ``stop_uid``,
  feed/stop ids, name, ``location_type``, ``parent_station``, plus the
  ``stop_line_ids`` and ``near_stations`` strings from the CSV export - the
  layer is self-contained for map use.
- ``near_links`` (LINESTRING): one line per near-stop pair (within the
  proximity threshold), carrying both numeric ids, the geodesic distance,
  and the estimated walking minutes - the node relations as geometry.

GeoPackage supports 64-bit integers, so the numeric ids survive intact
(unlike e.g. shapefiles, which would truncate them).
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from ..models import numeric_stop_id
from .near_stations import walk_minutes

log = logging.getLogger(__name__)

GEOPACKAGE_NAME = "transit_analysis.gpkg"

#: Attribute columns of the stops layer (geometry column excluded).
STOPS_LAYER_COLUMNS = [
    "stop_num_id", "stop_uid", "feed_id", "stop_id", "station",
    "location_type", "parent_station", "stop_line_ids", "near_stations",
]


def write_geopackage(
    stops: gpd.GeoDataFrame,
    near: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    out_dir: Path,
) -> Path:
    """Write the analysis GeoPackage.

    Args:
        stops: normalized stops of all feeds (EPSG:4326 geometry).
        near: ``near_stations.build_near_stations`` output (provides the
            ``station``/``stop_line_ids``/``near_stations`` attributes).
        stop_pairs: proximity stop pairs (``stop_uid_a, stop_uid_b,
            distance_m``) used to draw the ``near_links`` lines.
        out_dir: report directory (created if needed).

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

    log.info(
        "GeoPackage written: %s (stops: %d, near_links: %d)",
        path, len(stops_layer), len(links_layer),
    )
    return path


def _stops_layer(stops: gpd.GeoDataFrame, near: pd.DataFrame) -> gpd.GeoDataFrame:
    """Stops as a self-contained point layer (near attributes merged in)."""
    layer = stops[
        ["stop_num_id", "stop_uid", "feed_id", "stop_id",
         "location_type", "parent_station", "geometry"]
    ].copy()
    extras = near[["stop_uid", "station", "stop_line_ids", "near_stations"]] \
        if not near.empty else pd.DataFrame(columns=["stop_uid", "station", "stop_line_ids", "near_stations"])
    layer = layer.merge(extras, on="stop_uid", how="left")
    layer["station"] = layer["station"].fillna("")
    layer["stop_line_ids"] = layer["stop_line_ids"].fillna("")
    layer["near_stations"] = layer["near_stations"].fillna("[]")
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
