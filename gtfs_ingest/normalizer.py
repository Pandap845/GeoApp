"""Normalization of per-feed stops into the common cross-feed schema.

Input: the raw ``stops.txt`` table of one feed (all-string dtypes, as read
by ``partridge.load_raw_feed``). Output: a GeoDataFrame matching the
:class:`~gtfs_ingest.models.StopRecord` contract plus a shapely POINT
geometry column in EPSG:4326, ready for the PostGIS loader.

Transformations applied, in order (documented contract - see README):
  1. Missing optional columns (stop_name, location_type, parent_station)
     are added as nulls so every feed yields the same column set.
  2. All string columns are whitespace-stripped; empty strings become NULL.
  3. stop_lat/stop_lon are coerced to float (unparseable -> NaN).
  4. location_type is coerced to int; blank/unparseable values become 0,
     the GTFS default (= stop/platform).
  5. Rows are DROPPED (and counted in a WARNING finding) when:
       - stop_id is null/blank (no usable key), or
       - stop_lat/stop_lon is missing, unparseable, or outside
         [-90, 90] / [-180, 180] (no usable geometry). This intentionally
         also drops coordinate-less location_type 3/4 rows: the stops table
         is a geometry table, and a row without a point cannot live in it.
  6. Duplicate stop_id: first occurrence wins, the rest are dropped and
     counted in a WARNING finding (deterministic: file order).
  7. stop_uid = "{feed_id}:{stop_id}" - globally unique across feeds because
     feed_ids are unique per run and ':' never appears in a feed_id slug.

The dropped-row counts intentionally mirror what ``validators.py`` flags as
ERRORs: validation *reports*, normalization *repairs* (by exclusion), so the
report always explains exactly why the loaded count differs from the source
row count.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import pandas as pd

from .models import Finding, LoadedFeed, STOP_COLUMNS, Severity, numeric_stop_id

log = logging.getLogger(__name__)

#: CRS of all normalized geometry (WGS84 - the coordinate system GTFS mandates).
CRS = "EPSG:4326"

_OPTIONAL_STR_COLUMNS = ("stop_name", "parent_station")


def empty_stops_frame() -> gpd.GeoDataFrame:
    """An empty GeoDataFrame with the exact normalized stops schema."""
    df = pd.DataFrame(columns=list(STOP_COLUMNS))
    return gpd.GeoDataFrame(df, geometry=gpd.GeoSeries([], crs=CRS))


def normalize_stops(loaded: LoadedFeed) -> tuple[gpd.GeoDataFrame, list[Finding]]:
    """Normalize one feed's stops into the common schema.

    Args:
        loaded: feed as returned by ``loader.load_feed``.

    Returns:
        ``(stops, findings)`` where *stops* is a GeoDataFrame with columns
        ``stop_uid, feed_id, stop_id, stop_name, stop_lat, stop_lon,
        location_type, parent_station, geometry`` (POINT, EPSG:4326) and
        *findings* records every row dropped during normalization. If
        stops.txt is absent/unreadable/empty the frame is empty and a
        WARNING finding explains why. Never raises.
    """
    findings: list[Finding] = []
    raw = loaded.table("stops.txt")
    if raw is None or raw.empty or "stop_id" not in raw.columns:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="no_stops_normalized",
                message="stops.txt is absent, unreadable, empty, or lacks stop_id; "
                "no stops from this feed will be loaded",
                file="stops.txt",
            )
        )
        return empty_stops_frame(), findings

    df = raw.copy()

    # 1. Ensure a uniform column set across feeds.
    for col in ("stop_lat", "stop_lon", "location_type", *_OPTIONAL_STR_COLUMNS):
        if col not in df.columns:
            df[col] = pd.NA

    # 2. Strings: strip whitespace, standardize blanks to NULL.
    for col in ("stop_id", *_OPTIONAL_STR_COLUMNS):
        df[col] = df[col].astype("string").str.strip().replace("", pd.NA)

    # 3. Coordinates to float (unparseable -> NaN, handled by the row filter).
    for col in ("stop_lat", "stop_lon"):
        df[col] = pd.to_numeric(
            df[col].astype("string").str.strip(), errors="coerce"
        )

    # 4. location_type to int, blank/unparseable -> 0 (GTFS default).
    df["location_type"] = (
        pd.to_numeric(df["location_type"], errors="coerce").fillna(0).astype(int)
    )

    # 5. Drop rows without a usable key or geometry.
    def _drop(mask: pd.Series, code: str, reason: str) -> None:
        nonlocal df
        n = int(mask.sum())
        if n:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=code,
                    message=f"{n} stop row(s) dropped during normalization: {reason}",
                    file="stops.txt",
                    count=n,
                )
            )
            df = df[~mask]

    _drop(df["stop_id"].isna(), "dropped_null_stop_id", "missing/blank stop_id")
    bad_coords = (
        df["stop_lat"].isna()
        | df["stop_lon"].isna()
        | ~df["stop_lat"].between(-90.0, 90.0)
        | ~df["stop_lon"].between(-180.0, 180.0)
    )
    _drop(
        bad_coords,
        "dropped_invalid_coordinates",
        "missing, unparseable, or out-of-range stop_lat/stop_lon",
    )

    # 6. Duplicate stop_id: keep the first occurrence (file order).
    _drop(
        df["stop_id"].duplicated(keep="first"),
        "dropped_duplicate_stop_id",
        "duplicate stop_id (first occurrence kept)",
    )

    # 7. Provenance + globally unique key (+ its numeric alias).
    df["feed_id"] = loaded.feed_id
    df["stop_uid"] = loaded.feed_id + ":" + df["stop_id"].astype(str)
    df["stop_num_id"] = df["stop_uid"].map(numeric_stop_id)

    df = df[list(STOP_COLUMNS)].reset_index(drop=True)
    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["stop_lon"], df["stop_lat"]),
        crs=CRS,
    )
    log.info(
        "Feed %s: normalized %d stops (%d dropped)",
        loaded.feed_id,
        len(gdf),
        len(raw) - len(gdf),
    )
    return gdf, findings
