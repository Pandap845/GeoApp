"""OSM schema validation + nearest-transit-node matching (Part B).

OSM schema assumptions (validated at startup, configurable, never hardcoded
at call sites - see ``config.Settings`` / the conflate-osm CLI flags):

- an osm2pgsql-classic-style **line table** (default ``planet_osm_line``)
  with LINESTRING geometries in some SRID, a ``highway`` tag column, and an
  ``oneway`` column;
- a **point table** (default ``planet_osm_point``) with POINT geometries
  and the tag columns ``highway``, ``railway``, ``public_transport``;
- the geometry column (default ``way``) may be in any SRID - geometries are
  transformed on the fly and its SRID is read from the data;
- a ``name`` column is used when present, silently skipped when not.

``validate_osm_schema`` fails fast with one clear message listing every
missing table/column, so a mislabeled import surfaces before any work runs.

Matching: for each GTFS stop, the nearest OSM node carrying a compatible
transit tag (``highway=bus_stop``, ``public_transport in (platform,
stop_position, station)``, ``railway in (station, halt, tram_stop)``) is
found with the PostGIS KNN operator (``<->``, index-assisted) in a LATERAL
join; the true geodesic distance is then measured on ``geography``.

Confidence formula (documented in the README)::

    distance_score = max(0, 1 - distance_m / threshold_m)
    confidence     = round(distance_score * type_score, 3)

where ``type_score`` reflects tag/type compatibility between the stop's
GTFS ``location_type`` and the OSM element kind::

    location_type = 1 (station):  station-ish OSM kind -> 1.0, else 0.6
    location_type = 0/other:      platform-ish kind    -> 1.0,
                                  station-ish kind     -> 0.8,
                                  anything else        -> 0.7

``needs_review`` is set when there is no match at all, the distance exceeds
the threshold, or confidence < 0.5.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import Engine, bindparam, text

log = logging.getLogger(__name__)

#: Conservative identifier check - table/column names are interpolated into
#: SQL, so anything unusual is rejected outright.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$", re.IGNORECASE)

#: OSM tag values accepted as transit nodes.
PT_VALUES = ("platform", "stop_position", "station")
RAILWAY_VALUES = ("station", "halt", "tram_stop")

_STATION_KINDS = {"station"}
_PLATFORM_KINDS = {"bus_stop", "platform", "stop_position", "tram_stop", "halt"}

MATCH_COLUMNS = [
    "stop_uid", "feed_id", "stop_id", "location_type",
    "osm_id", "osm_kind", "osm_name", "distance_m", "match_confidence", "needs_review",
]


@dataclass(frozen=True)
class OsmSchema:
    """Validated OSM layer facts used to build queries."""

    line_table: str
    point_table: str
    geom_column: str
    line_srid: int
    point_srid: int
    line_has_name: bool
    point_has_name: bool


def _check_identifier(name: str, what: str) -> None:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{what} {name!r} is not a valid SQL identifier")


def validate_osm_schema(
    engine: Engine, line_table: str, point_table: str, geom_column: str
) -> OsmSchema:
    """Validate that the configured OSM layers exist and are usable.

    Args:
        engine: database engine.
        line_table / point_table / geom_column: configured layer names.

    Returns:
        An :class:`OsmSchema` with the detected SRIDs and optional-column
        availability.

    Raises:
        ValueError: on names that are not plain SQL identifiers.
        RuntimeError: with a single message listing every missing
            table/column, or when a table is empty (SRID undeterminable) -
            fail fast, before any per-stop work.
    """
    for name, what in ((line_table, "OSM line table"), (point_table, "OSM point table"),
                       (geom_column, "OSM geometry column")):
        _check_identifier(name, what)

    problems: list[str] = []
    columns: dict[str, set[str]] = {}
    with engine.connect() as conn:
        for table in (line_table, point_table):
            exists = conn.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}
            ).scalar_one()
            if not exists:
                problems.append(f"table {table!r} does not exist")
                continue
            cols = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t"
                    ),
                    {"t": table.split(".")[-1]},
                )
            }
            columns[table] = cols
            if geom_column not in cols:
                problems.append(f"{table} lacks geometry column {geom_column!r}")

        required = {line_table: ("highway", "oneway"), point_table: ("highway", "railway", "public_transport")}
        for table, needed in required.items():
            for col in needed:
                if table in columns and col not in columns[table]:
                    problems.append(f"{table} lacks required tag column {col!r}")

        srids: dict[str, int] = {}
        if not problems:
            for table in (line_table, point_table):
                srid = conn.execute(
                    text(f"SELECT ST_SRID({geom_column}) FROM {table} "
                         f"WHERE {geom_column} IS NOT NULL LIMIT 1")
                ).scalar()
                if srid is None:
                    problems.append(f"{table} contains no geometries (empty OSM import?)")
                else:
                    srids[table] = int(srid)

    if problems:
        raise RuntimeError(
            "OSM layers are not usable - fix the import or point the config "
            "(--osm-line-table/--osm-point-table/--osm-geom-column, or the "
            "GTFS_OSM_* env vars) at the right layers:\n  - "
            + "\n  - ".join(problems)
        )
    schema = OsmSchema(
        line_table=line_table,
        point_table=point_table,
        geom_column=geom_column,
        line_srid=srids[line_table],
        point_srid=srids[point_table],
        line_has_name="name" in columns[line_table],
        point_has_name="name" in columns[point_table],
    )
    log.info(
        "OSM schema validated: %s (SRID %d), %s (SRID %d), geom column %r",
        line_table, schema.line_srid, point_table, schema.point_srid, geom_column,
    )
    return schema


def nearest_transit_nodes(engine: Engine, schema: OsmSchema) -> pd.DataFrame:
    """Nearest compatible OSM transit node for every stop in ``stops``.

    Args:
        engine: database engine.
        schema: validated OSM layer facts.

    Returns:
        One row per GTFS stop with ``osm_id / osm_kind / osm_name /
        distance_m`` (all None/NaN when no transit node exists in the OSM
        extract at all). No threshold is applied here - scoring decides
        what counts as matched.
    """
    g = schema.geom_column
    name_expr = "p.name" if schema.point_has_name else "NULL"
    sql = text(
        f"""
        SELECT s.stop_uid, s.feed_id, s.stop_id, s.location_type,
               o.osm_id, o.kind AS osm_kind, o.osm_name, o.distance_m
        FROM stops s
        LEFT JOIN LATERAL (
            SELECT p.osm_id,
                   CASE
                       WHEN p.highway = 'bus_stop' THEN 'bus_stop'
                       WHEN p.public_transport IN :pt_values THEN p.public_transport
                       ELSE p.railway
                   END AS kind,
                   {name_expr} AS osm_name,
                   ST_Distance(s.geom::geography,
                               ST_Transform(p.{g}, 4326)::geography) AS distance_m
            FROM {schema.point_table} p
            WHERE p.highway = 'bus_stop'
               OR p.public_transport IN :pt_values
               OR p.railway IN :railway_values
            ORDER BY p.{g} <-> ST_Transform(s.geom, :point_srid)
            LIMIT 1
        ) o ON TRUE
        """
    ).bindparams(
        # expanding=True renders IN :param as IN (v1, v2, ...) - psycopg3
        # does not expand a plain tuple parameter itself.
        bindparam("pt_values", value=list(PT_VALUES), expanding=True),
        bindparam("railway_values", value=list(RAILWAY_VALUES), expanding=True),
        bindparam("point_srid", value=schema.point_srid),
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    frame = pd.DataFrame(
        rows,
        columns=["stop_uid", "feed_id", "stop_id", "location_type",
                 "osm_id", "osm_kind", "osm_name", "distance_m"],
    )
    log.info("OSM match: %d stops queried, %d with a candidate node",
             len(frame), int(frame["osm_id"].notna().sum()))
    return frame


def type_score(location_type: object, osm_kind: object) -> float:
    """Tag/type compatibility component of the confidence formula."""
    kind = str(osm_kind) if pd.notna(osm_kind) else ""
    try:
        loc = int(location_type)
    except (TypeError, ValueError):
        loc = 0
    if loc == 1:
        return 1.0 if kind in _STATION_KINDS else 0.6
    if kind in _PLATFORM_KINDS:
        return 1.0
    if kind in _STATION_KINDS:
        return 0.8
    return 0.7


def score_matches(matches: pd.DataFrame, threshold_m: float) -> pd.DataFrame:
    """Apply the documented confidence formula and the review flag.

    Args:
        matches: output of :func:`nearest_transit_nodes` (or an equivalent
            frame in tests).
        threshold_m: match threshold in meters.

    Returns:
        The frame with ``match_confidence`` and ``needs_review`` added, in
        :data:`MATCH_COLUMNS` order. Unmatched stops get confidence 0.0 and
        ``needs_review = True``.
    """
    result = matches.copy()
    distance = pd.to_numeric(result["distance_m"], errors="coerce")
    distance_score = (1 - distance / threshold_m).clip(lower=0.0, upper=1.0)
    compat = result.apply(lambda r: type_score(r["location_type"], r["osm_kind"]), axis=1) \
        if not result.empty else pd.Series(dtype=float)
    confidence = (distance_score * compat).round(3)
    result["match_confidence"] = confidence.fillna(0.0)
    result["needs_review"] = (
        result["osm_id"].isna()
        | distance.isna()
        | (distance > threshold_m)
        | (result["match_confidence"] < 0.5)
    )
    result["needs_review"] = result["needs_review"].astype(bool)
    return result[MATCH_COLUMNS] if not result.empty else pd.DataFrame(columns=MATCH_COLUMNS)
