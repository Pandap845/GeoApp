"""Side-of-street disambiguation (Part C - nearest centerline + sign math).

For each GTFS stop the nearest OSM highway centerline is fetched with the
PostGIS KNN operator; the left/right decision itself is pure planar
geometry, computed in a metric CRS.

Sign convention (documented in the README with a worked example):

    Take the segment of the centerline nearest to the stop. Let ``d`` be
    the segment's direction vector *in travel direction* and ``v`` the
    vector from the stop's perpendicular foot on the segment to the stop.
    The z component of the 2D cross product::

        cross = d.x * v.y - d.y * v.x

    is positive when the stop lies LEFT of the direction of travel and
    negative when it lies RIGHT ("driver's view": travelling along ``d``,
    left hand side = left).

    Travel direction is the way's digitized direction (coordinate order),
    reversed when ``oneway`` is ``-1``/``reverse`` (OSM's convention for
    "one way against digitization"). Two-way and untagged ways use the
    digitized direction as-is - the reported side is then relative to
    digitization, which is stable but arbitrary; consumers must combine it
    with the direction they care about.

    Worked example: way digitized (0,0) -> (100,0) (pointing +x), stop at
    (50, 10): d = (1, 0), v = (0, 10), cross = 1*10 - 0*0 = +10 -> left.
    With ``oneway = -1`` travel is -x, so the same stop is right.

Confidence: ``low`` when the answer is geometrically fragile -

- the stop is within ``on_tolerance_m`` (1 m) of the centerline
  (side reported as ``"on"``),
- the perpendicular foot lies within ``endpoint_tolerance_m`` (10 m) of
  either way end (typically an intersection, where "which street?" itself
  is ambiguous), or
- the perpendicular distance is under 2 m (nearly collinear digitization
  noise dominates).

Otherwise ``high``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import geopandas as gpd
import pandas as pd
from shapely import wkt as shapely_wkt
from shapely.geometry import LineString, Point
from sqlalchemy import Engine, text

from .osm_match import OsmSchema

log = logging.getLogger(__name__)

#: ``oneway`` values meaning "traffic flows against digitized direction".
REVERSED_ONEWAY = {"-1", "reverse"}

ON_TOLERANCE_M = 1.0
ENDPOINT_TOLERANCE_M = 10.0
NEARLY_COLLINEAR_M = 2.0

SIDE_COLUMNS = [
    "stop_uid", "feed_id", "stop_id", "osm_way_id", "highway", "oneway",
    "perpendicular_distance_m", "side", "side_confidence", "near_endpoint",
]


@dataclass(frozen=True)
class SideResult:
    """Outcome of the side computation for one stop/way pair.

    Attributes:
        side: ``"left"`` / ``"right"`` relative to travel direction (see
            module docstring), or ``"on"`` within the on-line tolerance.
        perpendicular_distance_m: distance from the stop to its nearest
            point on the centerline, in the metric CRS.
        near_endpoint: nearest point lies within the endpoint tolerance of
            a way end (intersection proxy).
        confidence: ``"high"`` or ``"low"`` (see module docstring).
    """

    side: str
    perpendicular_distance_m: float
    near_endpoint: bool
    confidence: str


def side_of_line(
    line: LineString,
    point: Point,
    oneway: Optional[str] = None,
    on_tolerance_m: float = ON_TOLERANCE_M,
    endpoint_tolerance_m: float = ENDPOINT_TOLERANCE_M,
) -> SideResult:
    """Which side of *line* does *point* lie on? (planar/metric coordinates)

    Args:
        line: highway centerline, already projected to a metric CRS.
        point: stop location in the same CRS.
        oneway: raw OSM ``oneway`` value; ``-1``/``reverse`` flips travel
            direction relative to digitization, anything else (including
            ``None``) keeps it.
        on_tolerance_m: distances under this report side ``"on"``.
        endpoint_tolerance_m: proximity to a way end that flags low
            confidence (intersection proxy).

    Returns:
        A :class:`SideResult`. Never raises for degenerate geometry: a
        zero-length segment yields side ``"on"`` with low confidence.
    """
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
    """The line segment containing chainage *along* (last segment at the end)."""
    coords = list(line.coords)
    cumulative = 0.0
    for start, end in zip(coords, coords[1:]):
        length = math.dist(start, end)
        if along <= cumulative + length or end == coords[-1]:
            return (start[0], start[1]), (end[0], end[1])
        cumulative += length
    first = (coords[0][0], coords[0][1])
    return first, first  # degenerate single-point line


def nearest_streets(engine: Engine, schema: OsmSchema) -> pd.DataFrame:
    """Nearest OSM highway centerline for every stop (KNN, no threshold).

    Args:
        engine: database engine.
        schema: validated OSM layer facts.

    Returns:
        One row per stop with the stop WKT, the way's ``osm_id`` /
        ``highway`` / ``oneway``, the way geometry as EPSG:4326 WKT, and
        the geodesic ``distance_m`` (None when the extract has no highway
        lines at all).
    """
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
            SELECT l.osm_id, l.highway, l.oneway, l.{g}
            FROM {schema.line_table} l
            WHERE l.highway IS NOT NULL
            ORDER BY l.{g} <-> ST_Transform(s.geom, :line_srid)
            LIMIT 1
        ) w ON TRUE
        """
    ).bindparams(line_srid=schema.line_srid)
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return pd.DataFrame(
        rows,
        columns=["stop_uid", "feed_id", "stop_id", "stop_wkt",
                 "osm_id", "highway", "oneway", "way_wkt", "distance_m"],
    )


def compute_sides(
    streets: pd.DataFrame,
    threshold_m: float,
    metric_crs: Optional[str] = None,
) -> pd.DataFrame:
    """Apply the side math to the nearest-street rows.

    Args:
        streets: output of :func:`nearest_streets`.
        threshold_m: max centerline distance; farther (or missing) streets
            yield a NULL-way row (recorded, never a crash).
        metric_crs: metric CRS override; default estimates a UTM zone from
            the stop extent.

    Returns:
        One row per stop with :data:`SIDE_COLUMNS`; stops without a usable
        street keep NULL ``osm_way_id``/``side`` and low confidence.
    """
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
    points = gpd.GeoSeries(subset["stop_wkt"].map(shapely_wkt.loads).to_list(),
                           index=subset.index, crs="EPSG:4326")
    lines = gpd.GeoSeries(subset["way_wkt"].map(shapely_wkt.loads).to_list(),
                          index=subset.index, crs="EPSG:4326")
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
            subset.at[idx, "osm_id"], subset.at[idx, "highway"], subset.at[idx, "oneway"],
        )
        base.loc[idx, "perpendicular_distance_m"] = round(result.perpendicular_distance_m, 2)
        base.loc[idx, "side"] = result.side
        base.loc[idx, "side_confidence"] = result.confidence
        base.loc[idx, "near_endpoint"] = result.near_endpoint

    log.info(
        "Side-of-street: %d/%d stops resolved (%d low confidence)",
        int(base["side"].notna().sum()), len(base),
        int((base["side_confidence"] == "low").sum()),
    )
    return base[SIDE_COLUMNS]
