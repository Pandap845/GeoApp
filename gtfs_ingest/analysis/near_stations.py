
"""Per-station "near stations" export.

For every normalized stop, lists the other stops within the proximity
threshold together with the transit lines serving them and an estimated
walking time, rendered exactly as::

    [(ID, station, linea, time to reach), (ID2, station, linea, time to reach)]

one tuple per (near station, line) combination, sorted by walking time.

Identifier conventions (documented in the README):

- ``ID`` in the tuples is the stop's **numeric id** (``stop_num_id`` -
  first 48 bits of ``sha256(stop_uid)``; see ``models.numeric_stop_id``).
  It is deterministic and stable across runs, and matches the
  ``stop_num_id`` column of the PostGIS ``stops`` table, so the node
  relations in this file join directly against the database.
- ``stop_line_ids`` is the **combined stop+line identifier** set of the
  row's own stop: ``"{stop_num_id}-{line}"`` per line serving it (e.g.
  ``"128733941436-L10"``), for consumers that model a node per
  (station, line) pair.
- ``linea`` is the route's ``route_short_name`` when present, otherwise its
  ``route_id``; only lines active on the analyzed service date appear, so a
  near stop served by no route that day (e.g. a bare parent-station node)
  is omitted from neighbour lists.
- ``time to reach`` is straight-line distance divided by a 5 km/h walking
  speed (~83 m/min), rounded to whole minutes with a 1-minute floor. It is
  an estimate - no street network is consulted (that is Phase 3 territory).
- "near" reuses the connectivity proximity threshold (``--proximity-m``,
  default 150 m) and the same distance engine (PostGIS geography when a
  database is available, local UTM math otherwise).

A second relation, ``same_line_stations``, lists the stop's **adjacent
stations along each line it serves** - the actual previous/next stop in
route order (``stop_times`` sequence), per line and direction, rendered as::

    [(ID, station, linea, dir{d}, {dist}m), ...]

Unlike ``near_stations`` (any line, geographic proximity only), this is the
route topology: it comes from the consecutive-stop pairs of
``route_geometry.route_stop_links`` and includes both the previous and next
stop of every (line, direction) serving the stop, even when they are farther
than the proximity threshold. ``dist`` is the straight-line (haversine)
distance between the two consecutive stops in meters.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

#: ~5 km/h walking speed.
WALK_M_PER_MIN = 83.0

NEAR_STATIONS_NAME = "near_stations.csv"

#: Columns of the exported file.
COLUMNS = [
    "stop_num_id", "stop_uid", "feed_id", "stop_id", "station",
    "stop_lat", "stop_lon", "stop_line_ids", "near_stations", "same_line_stations",
]

#: Mean earth radius (meters) for the straight-line inter-stop distance.
_EARTH_RADIUS_M = 6_371_000.0


def _line_labels(routes: pd.DataFrame) -> pd.DataFrame:
    """Map route_uid -> display label (short name, falling back to route_id)."""
    if routes.empty:
        return pd.DataFrame(columns=["route_uid", "line"])
    labels = routes[["route_uid", "route_id"]].copy()
    short = routes.get("route_short_name")
    if short is not None:
        short = short.astype("string").str.strip()
        labels["line"] = short.where(short.notna() & (short != ""), routes["route_id"])
    else:
        labels["line"] = routes["route_id"]
    return labels[["route_uid", "line"]]


def walk_minutes(distance_m: pd.Series) -> pd.Series:
    """Estimated walking minutes for straight-line distances (>= 1 min)."""
    return np.maximum(1, (distance_m / WALK_M_PER_MIN).round()).astype(int)


def build_near_stations(
    stops: pd.DataFrame,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    stop_links: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Assemble the near-stations table for all analyzed stops.

    Args:
        stops: Phase 1 normalized stops (all feeds; needs ``stop_uid``,
            ``stop_num_id``, ``feed_id``, ``stop_id``, ``stop_name``,
            ``stop_lat``, ``stop_lon``).
        route_stops: ``connectivity.route_stop_map`` frames (all feeds).
        routes: route metadata with ``route_uid``/``route_id`` and optional
            ``route_short_name``.
        stop_pairs: proximity stop pairs (``stop_uid_a, stop_uid_b,
            distance_m``) from either ``proximity_pairs_*`` function.
        stop_links: consecutive-stop pairs per route/direction
            (``route_geometry.route_stop_links``, all feeds) used to build
            the ``same_line_stations`` column; ``None``/empty yields ``"[]"``.

    Returns:
        One row per stop with :data:`COLUMNS`; ``near_stations`` holds the
        formatted proximity tuple list (``"[]"`` when nothing is within the
        threshold), ``same_line_stations`` the adjacent same-line stations
        in route order, and ``stop_line_ids`` the combined ``num_id-line``
        identifiers of the stop's own lines (``""`` when unserved). Never
        raises on empty inputs.
    """
    base = stops[
        ["stop_num_id", "stop_uid", "feed_id", "stop_id", "stop_name", "stop_lat", "stop_lon"]
    ].copy()
    base = base.rename(columns={"stop_name": "station"})
    base["station"] = base["station"].fillna("")

    lines_at = _lines_at_stops(route_stops, routes)

    # Combined stop+line ids for the row's own stop.
    own = base[["stop_uid", "stop_num_id"]].merge(lines_at, on="stop_uid")
    own["combo"] = own["stop_num_id"].astype(str) + "-" + own["combo_line"]
    combos = (
        own.sort_values(["stop_uid", "combo"])
        .groupby("stop_uid")["combo"]
        .agg(", ".join)
        .rename("stop_line_ids")
        .reset_index()
    )
    base = base.merge(combos, on="stop_uid", how="left")
    base["stop_line_ids"] = base["stop_line_ids"].fillna("")

    # Adjacent same-line stations (route order); carried through both the
    # early-return and normal paths because it is attached to `base`.
    base = _attach_same_line(base, stop_links)

    entries = _near_entries(base, lines_at, stop_pairs)
    if entries.empty:
        base["near_stations"] = "[]"
        return base[COLUMNS]

    entries = entries.sort_values(["stop_uid", "minutes", "near_station", "line"])
    entries["tuple"] = (
        "(" + entries["near_num_id"].astype(str) + ", " + entries["near_station"] + ", "
        + entries["line"].astype(str) + ", " + entries["minutes"].astype(str) + " min)"
    )
    rendered = (
        entries.groupby("stop_uid")["tuple"]
        .agg(lambda parts: "[" + ", ".join(parts) + "]")
        .rename("near_stations")
        .reset_index()
    )
    result = base.merge(rendered, on="stop_uid", how="left")
    result["near_stations"] = result["near_stations"].fillna("[]")
    return result[COLUMNS]


def _lines_at_stops(route_stops: pd.DataFrame, routes: pd.DataFrame) -> pd.DataFrame:
    """Distinct (stop_uid, line label) pairs; ``combo_line`` is the label."""
    if route_stops.empty:
        return pd.DataFrame(columns=["stop_uid", "combo_line"])
    lines = (
        route_stops[["stop_uid", "route_uid"]]
        .merge(_line_labels(routes), on="route_uid")
        [["stop_uid", "line"]]
        .drop_duplicates()
        .rename(columns={"line": "combo_line"})
    )
    return lines


def _near_entries(
    base: pd.DataFrame,
    lines_at: pd.DataFrame,
    stop_pairs: pd.DataFrame,
) -> pd.DataFrame:
    """One row per (stop, near stop, line): the tuples before rendering."""
    columns = ["stop_uid", "near_num_id", "near_station", "line", "minutes"]
    if stop_pairs.empty or lines_at.empty:
        return pd.DataFrame(columns=columns)

    # Pairs are stored once per unordered pair; neighbourhood is symmetric.
    both = pd.concat(
        [
            stop_pairs.rename(columns={"stop_uid_a": "stop_uid", "stop_uid_b": "near_uid"}),
            stop_pairs.rename(columns={"stop_uid_b": "stop_uid", "stop_uid_a": "near_uid"}),
        ],
        ignore_index=True,
    )
    entries = both.merge(
        lines_at.rename(columns={"stop_uid": "near_uid", "combo_line": "line"}), on="near_uid"
    )
    if entries.empty:
        return pd.DataFrame(columns=columns)
    entries = entries.merge(
        base[["stop_uid", "stop_num_id", "station"]].rename(
            columns={"stop_uid": "near_uid", "stop_num_id": "near_num_id", "station": "near_station"}
        ),
        on="near_uid",
        how="left",
    )
    entries["near_station"] = entries["near_station"].fillna("")
    entries["minutes"] = walk_minutes(entries["distance_m"])
    return entries[columns]


def _haversine_m(
    lat1: pd.Series, lon1: pd.Series, lat2: pd.Series, lon2: pd.Series
) -> pd.Series:
    """Vectorized great-circle distance in meters between two point series."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    meters = 2 * _EARTH_RADIUS_M * np.arcsin(np.sqrt(a))
    return pd.Series(np.asarray(meters), index=lat1.index)


def _same_line_entries(base: pd.DataFrame, stop_links: pd.DataFrame | None) -> pd.DataFrame:
    """One row per (stop, adjacent same-line stop, line, direction).

    Both the next stop (this stop is ``stop_uid_a`` of a link) and the
    previous stop (this stop is ``stop_uid_b``) are collected, so each
    (line, direction) contributes up to two neighbours. Distance is the
    straight-line distance between the two consecutive stops.
    """
    columns = ["stop_uid", "near_num_id", "near_station", "linea", "direction_id", "distance_m"]
    if stop_links is None or stop_links.empty:
        return pd.DataFrame(columns=columns)
    keep = ["stop_uid", "near_uid", "linea", "direction_id"]
    forward = stop_links.rename(columns={"stop_uid_a": "stop_uid", "stop_uid_b": "near_uid"})[keep]
    backward = stop_links.rename(columns={"stop_uid_b": "stop_uid", "stop_uid_a": "near_uid"})[keep]
    both = pd.concat([forward, backward], ignore_index=True).drop_duplicates(keep)

    near_attr = base[["stop_uid", "stop_num_id", "station", "stop_lat", "stop_lon"]].rename(
        columns={
            "stop_uid": "near_uid", "stop_num_id": "near_num_id",
            "station": "near_station", "stop_lat": "near_lat", "stop_lon": "near_lon",
        }
    )
    entries = both.merge(near_attr, on="near_uid", how="inner").merge(
        base[["stop_uid", "stop_lat", "stop_lon"]], on="stop_uid", how="left"
    )
    if entries.empty:
        return pd.DataFrame(columns=columns)
    entries["near_station"] = entries["near_station"].fillna("")
    entries["distance_m"] = _haversine_m(
        entries["stop_lat"], entries["stop_lon"], entries["near_lat"], entries["near_lon"]
    ).round(1)
    return entries[columns]


def _attach_same_line(base: pd.DataFrame, stop_links: pd.DataFrame | None) -> pd.DataFrame:
    """Add the rendered ``same_line_stations`` column to *base* (in place)."""
    entries = _same_line_entries(base, stop_links)
    if entries.empty:
        base["same_line_stations"] = "[]"
        return base
    entries = entries.sort_values(
        ["stop_uid", "linea", "direction_id", "distance_m", "near_num_id"]
    )
    entries["tuple"] = (
        "(" + entries["near_num_id"].astype(str) + ", " + entries["near_station"] + ", "
        + entries["linea"].astype(str) + ", dir" + entries["direction_id"].astype(str) + ", "
        + entries["distance_m"].astype(str) + "m)"
    )
    rendered = (
        entries.groupby("stop_uid")["tuple"]
        .agg(lambda parts: "[" + ", ".join(parts) + "]")
        .rename("same_line_stations")
        .reset_index()
    )
    base = base.merge(rendered, on="stop_uid", how="left")
    base["same_line_stations"] = base["same_line_stations"].fillna("[]")
    return base


def write_near_stations(near: pd.DataFrame, out_dir: Path) -> Path:
    """Write the table as ``near_stations.csv`` in *out_dir*.

    Args:
        near: frame from :func:`build_near_stations`.
        out_dir: report directory (created if needed).

    Returns:
        The written path.

    Raises:
        OSError: if the file cannot be written (run-level problem).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / NEAR_STATIONS_NAME
    near.to_csv(path, index=False, encoding="utf-8")
    log.info("Near-stations export written: %s (%d stops)", path, len(near))
    return path
