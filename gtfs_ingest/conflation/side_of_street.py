"""Which side of the nearest OSM street each stop is on.

With d = street direction and v = vector from the closest point on the street
to the stop, cross = d.x * v.y - d.y * v.x is positive for left, negative for
right. Direction is the drawing order, reversed for oneway=-1/reverse; for
two-way streets the side is relative to the drawing order.
Example: street (0,0)->(100,0), stop (50,10): cross = +10 -> left.

Confidence is low when the stop is within 1 m of the line ("on"), within 10 m
of a street end (likely an intersection), or under 2 m away.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from itertools import pairwise

import geopandas as gpd
import pandas as pd
from shapely import wkt as shapely_wkt
from shapely.geometry import LineString, Point
from sqlalchemy import Engine, text

from .osm_match import KNN_CANDIDATES, OsmSchema

log = logging.getLogger(__name__)

#: oneway values meaning traffic runs against the drawing order.
REVERSED_ONEWAY = {"-1", "reverse"}

ON_TOLERANCE_M = 1.0
ENDPOINT_TOLERANCE_M = 10.0
NEARLY_COLLINEAR_M = 2.0

SIDE_COLUMNS = [
    "stop_uid",
    "feed_id",
    "stop_id",
    "osm_way_id",
    "highway",
    "oneway",
    "perpendicular_distance_m",
    "side",
    "side_confidence",
    "near_endpoint",
]


@dataclass(frozen=True)
class SideResult:
    """Side of one stop relative to one street."""

    side: str  # "left", "right" or "on"
    perpendicular_distance_m: float
    near_endpoint: bool
    confidence: str  # "high" or "low"


def side_of_line(
    line: LineString,
    point: Point,
    oneway: str | None = None,
    on_tolerance_m: float = ON_TOLERANCE_M,
    endpoint_tolerance_m: float = ENDPOINT_TOLERANCE_M,
) -> SideResult:
    """Side of *point* relative to *line* (both in a metric CRS); see the module rules."""
    along = line.project(point)
    foot = line.interpolate(along)
    perp = point.distance(foot)

    segment = _segment_at(line, along)
    dx, dy = segment[1][0] - segment[0][0], segment[1][1] - segment[0][1]
    if oneway is not None and str(oneway).strip().lower() in REVERSED_ONEWAY:
        dx, dy = -dx, -dy
    vx, vy = point.x - foot.x, point.y - foot.y
    cross = dx * vy - dy * vx

    near_endpoint = along <= endpoint_tolerance_m or (line.length - along) <= endpoint_tolerance_m
    if perp <= on_tolerance_m or (dx == 0 and dy == 0):
        side = "on"
    else:
        side = "left" if cross > 0 else "right"
    low = side == "on" or near_endpoint or perp < NEARLY_COLLINEAR_M
    return SideResult(
        side=side,
        perpendicular_distance_m=float(perp),
        near_endpoint=bool(near_endpoint),
        confidence="low" if low else "high",
    )


def _segment_at(line: LineString, along: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """The segment that contains the point at distance *along* on the line."""
    coords = list(line.coords)
    cumulative = 0.0
    for start, end in pairwise(coords):
        length = math.dist(start, end)
        if along <= cumulative + length or end == coords[-1]:
            return (start[0], start[1]), (end[0], end[1])
        cumulative += length
    first = (coords[0][0], coords[0][1])
    return first, first  # degenerate single-point line


def nearest_streets(engine: Engine, schema: OsmSchema) -> pd.DataFrame:
    """Nearest OSM street for every stop, with its geometry and distance (no limit)."""
    g = schema.geom_column
    sql = text(
        f"""
        SELECT s.stop_uid, s.feed_id, s.stop_id,
               ST_AsText(s.geom) AS stop_wkt,
               w.osm_id, w.highway, w.oneway,
               ST_AsText(ST_Transform(w.{g}, 4326)) AS way_wkt,
               ST_Distance(s.geom::geography,
                           ST_Transform(w.{g}, 4326)::geography) AS distance_m
        FROM stops s
        LEFT JOIN LATERAL (
            SELECT osm_id, highway, oneway, {g} FROM (
                SELECT l.osm_id, l.highway, l.oneway, l.{g},
                       l.{g} <-> ST_Transform(s.geom, :line_srid) AS knn_d
                FROM {schema.line_table} l
                WHERE l.highway IS NOT NULL
                ORDER BY l.{g} <-> ST_Transform(s.geom, :line_srid)
                LIMIT {KNN_CANDIDATES}
            ) candidates
            ORDER BY knn_d, osm_id
            LIMIT 1
        ) w ON TRUE
        ORDER BY s.stop_uid
        """
    ).bindparams(line_srid=schema.line_srid)
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return pd.DataFrame(
        rows,
        columns=[
            "stop_uid",
            "feed_id",
            "stop_id",
            "stop_wkt",
            "osm_id",
            "highway",
            "oneway",
            "way_wkt",
            "distance_m",
        ],
    )


def compute_sides(
    streets: pd.DataFrame,
    threshold_m: float,
    metric_crs: str | None = None,
) -> pd.DataFrame:
    """Side of street per stop; streets farther than *threshold_m* leave the row empty."""
    if streets.empty:
        return pd.DataFrame(columns=SIDE_COLUMNS)

    base = streets[["stop_uid", "feed_id", "stop_id"]].copy()
    for col in ("osm_way_id", "highway", "oneway", "perpendicular_distance_m", "side"):
        base[col] = None
    base["side_confidence"] = "low"
    base["near_endpoint"] = False

    distance = pd.to_numeric(streets["distance_m"], errors="coerce")
    usable = streets["osm_id"].notna() & distance.notna() & (distance <= threshold_m)
    if not usable.any():
        log.warning("Side-of-street: no stop has a highway centerline within %.0f m", threshold_m)
        return base[SIDE_COLUMNS]

    subset = streets[usable]
    points = gpd.GeoSeries(
        subset["stop_wkt"].map(shapely_wkt.loads).to_list(), index=subset.index, crs="EPSG:4326"
    )
    lines = gpd.GeoSeries(
        subset["way_wkt"].map(shapely_wkt.loads).to_list(), index=subset.index, crs="EPSG:4326"
    )
    crs = metric_crs or points.estimate_utm_crs()
    points, lines = points.to_crs(crs), lines.to_crs(crs)

    for idx in subset.index:
        line = lines.loc[idx]
        if line.geom_type == "MultiLineString":  # rare with osm2pgsql, but possible
            line = min(line.geoms, key=lambda part: part.distance(points.loc[idx]))
        if line.geom_type != "LineString" or line.length == 0:
            continue  # keep the NULL "needs data" row
        result = side_of_line(line, points.loc[idx], subset.at[idx, "oneway"])
        base.loc[idx, ["osm_way_id", "highway", "oneway"]] = (
            subset.at[idx, "osm_id"],
            subset.at[idx, "highway"],
            subset.at[idx, "oneway"],
        )
        base.loc[idx, "perpendicular_distance_m"] = round(result.perpendicular_distance_m, 2)
        base.loc[idx, "side"] = result.side
        base.loc[idx, "side_confidence"] = result.confidence
        base.loc[idx, "near_endpoint"] = result.near_endpoint

    log.info(
        "Side-of-street: %d/%d stops resolved (%d low confidence)",
        int(base["side"].notna().sum()),
        len(base),
        int((base["side_confidence"] == "low").sum()),
    )
    return base[SIDE_COLUMNS]
