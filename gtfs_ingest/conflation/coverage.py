"""Reverse coverage check: OSM transit nodes with no GTFS stop nearby.

Service area = convex hull of all stops plus a buffer. An OSM transit node
inside it with no stop within the radius is a gap: either a missing system in
the feeds or OSM noise. Results go to osm_coverage_gaps.gpkg for review.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import Engine, bindparam, text

from .osm_match import PT_VALUES, RAILWAY_VALUES, OsmSchema

log = logging.getLogger(__name__)

COVERAGE_GPKG = "osm_coverage_gaps.gpkg"
COVERAGE_LAYER = "osm_only_stops"

GAP_COLUMNS = ["osm_id", "kind", "osm_name", "nearest_stop_m", "lat", "lon"]


def osm_only_stops(
    engine: Engine,
    schema: OsmSchema,
    coverage_radius_m: float,
    coverage_buffer_m: float,
) -> gpd.GeoDataFrame:
    """OSM transit nodes in the service area farther than *coverage_radius_m* from any stop."""
    g = schema.geom_column
    name_expr = "p.name" if schema.point_has_name else "NULL"
    sql = text(
        f"""
        WITH area AS (
            SELECT ST_Buffer(ST_ConvexHull(ST_Collect(geom))::geography,
                             :buffer_m)::geometry AS hull
            FROM stops
        )
        SELECT p.osm_id,
               CASE
                   WHEN p.highway = 'bus_stop' THEN 'bus_stop'
                   WHEN p.public_transport IN :pt_values THEN p.public_transport
                   ELSE p.railway
               END AS kind,
               {name_expr} AS osm_name,
               n.distance_m AS nearest_stop_m,
               ST_Y(ST_Transform(p.{g}, 4326)) AS lat,
               ST_X(ST_Transform(p.{g}, 4326)) AS lon
        FROM {schema.point_table} p
        JOIN area a ON ST_Intersects(ST_Transform(p.{g}, 4326), a.hull)
        LEFT JOIN LATERAL (
            SELECT ST_Distance(s.geom::geography,
                               ST_Transform(p.{g}, 4326)::geography) AS distance_m
            FROM stops s
            ORDER BY s.geom <-> ST_Transform(p.{g}, 4326)
            LIMIT 1
        ) n ON TRUE
        WHERE (p.highway = 'bus_stop'
               OR p.public_transport IN :pt_values
               OR p.railway IN :railway_values)
          AND (n.distance_m IS NULL OR n.distance_m > :radius_m)
        ORDER BY n.distance_m DESC NULLS FIRST
        """
    ).bindparams(
        bindparam("pt_values", value=list(PT_VALUES), expanding=True),
        bindparam("railway_values", value=list(RAILWAY_VALUES), expanding=True),
        bindparam("buffer_m", value=float(coverage_buffer_m)),
        bindparam("radius_m", value=float(coverage_radius_m)),
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return rows_to_frame(rows)


def rows_to_frame(rows: list) -> gpd.GeoDataFrame:
    """Turn query rows into a point GeoDataFrame."""
    frame = pd.DataFrame(rows, columns=GAP_COLUMNS)
    if frame.empty:
        return gpd.GeoDataFrame(frame, geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    frame["nearest_stop_m"] = pd.to_numeric(frame["nearest_stop_m"], errors="coerce").round(1)
    geometry = gpd.points_from_xy(frame["lon"], frame["lat"])
    return gpd.GeoDataFrame(frame, geometry=geometry, crs="EPSG:4326")


def write_coverage_gpkg(gaps: gpd.GeoDataFrame, out_dir: Path) -> Path:
    """Write the gaps layer to ``osm_coverage_gaps.gpkg`` (overwritten per run)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / COVERAGE_GPKG
    if path.exists():
        path.unlink()
    if gaps.empty:
        log.info("OSM coverage: no uncovered transit nodes; %s not written", COVERAGE_GPKG)
        return path
    gaps.to_file(path, layer=COVERAGE_LAYER, driver="GPKG")
    log.info("OSM coverage gaps written: %s (%d nodes)", path, len(gaps))
    return path
