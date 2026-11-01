"""Normalize one feed's raw stops.txt into the common stops schema (EPSG:4326 points).

Rows without a stop_id or a valid coordinate are dropped, and so are repeated
stop_ids (first one wins). Every drop is counted in a WARNING finding, so the
report explains why the loaded count differs from the source.
stop_uid = "{feed_id}:{stop_id}" is unique across feeds.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import pandas as pd

from .models import STOP_COLUMNS, Finding, LoadedFeed, Severity, numeric_stop_id

log = logging.getLogger(__name__)

#: WGS84, the coordinate system GTFS uses.
CRS = "EPSG:4326"

#: Leave out stops that no trip uses (they can't be boarded); CLI default too.
DROP_UNSERVED_STOPS = True

_OPTIONAL_STR_COLUMNS = ("stop_name", "parent_station")


def empty_stops_frame() -> gpd.GeoDataFrame:
    """An empty GeoDataFrame with the exact normalized stops schema."""
    df = pd.DataFrame(columns=list(STOP_COLUMNS))
    return gpd.GeoDataFrame(df, geometry=gpd.GeoSeries([], crs=CRS))


def normalize_stops(loaded: LoadedFeed) -> tuple[gpd.GeoDataFrame, list[Finding]]:
    """Return (stops GeoDataFrame, findings for dropped rows). Never raises."""
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

    # Same columns for every feed.
    for col in ("stop_lat", "stop_lon", "location_type", *_OPTIONAL_STR_COLUMNS):
        if col not in df.columns:
            df[col] = pd.NA

    # Strip strings; blank becomes NULL.
    for col in ("stop_id", *_OPTIONAL_STR_COLUMNS):
        df[col] = df[col].astype("string").str.strip().replace("", pd.NA)

    # Unparseable coordinates become NaN and are dropped below.
    for col in ("stop_lat", "stop_lon"):
        df[col] = pd.to_numeric(df[col].astype("string").str.strip(), errors="coerce")

    # Blank location_type means 0 (stop/platform) in GTFS.
    df["location_type"] = pd.to_numeric(df["location_type"], errors="coerce").fillna(0).astype(int)

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

    _drop(
        df["stop_id"].duplicated(keep="first"),
        "dropped_duplicate_stop_id",
        "duplicate stop_id (first occurrence kept)",
    )

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


def unserved_stops(stops: gpd.GeoDataFrame, stop_times: pd.DataFrame | None) -> pd.Series:
    """Mask of stops (location_type 0) that no stop_times row uses.

    Such a stop can't be boarded on any day. Stations, entrances and other
    location types are never unserved: trips don't stop at them by design.
    Without a readable stop_times table nothing is marked: we can't tell.
    """
    no_times = stop_times is None or stop_times.empty or "stop_id" not in stop_times.columns
    if stops.empty or no_times:
        return pd.Series(False, index=stops.index)
    served = set(stop_times["stop_id"].astype("string").str.strip().dropna())
    return (stops["location_type"] == 0) & ~stops["stop_id"].astype("string").isin(served)
