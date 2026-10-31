"""near_stations.csv: for every stop, the stops within walking distance and their lines.

Each row lists neighbours as [(ID, station, linea, minutes), ...] sorted by
walking time, one tuple per (neighbour, line). Conventions:

- ID is a sequential 1..N row id used by every relation column; stop_uid is
  the durable key.
- linea is route_short_name, else route_id. A neighbour served by no line
  that day (a dock, a parent station) still appears, with linea "-".
- Walking minutes = straight-line distance / 83 m/min (or the street
  distance with --network-walk), at least 1.
- same_line_stations lists the previous and next stop on each line and
  direction, with scheduled minutes (25 km/h estimate if untimed).

near_stations_quality.csv has the same rows and says how each value was
obtained (walk_source, travel_source, coord_confidence, station_dedup).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .walk_network import MAX_SNAP_M

log = logging.getLogger(__name__)

#: ~5 km/h walking speed.
WALK_M_PER_MIN = 83.0

NEAR_STATIONS_NAME = "near_stations.csv"
QUALITY_NAME = "near_stations_quality.csv"

#: linea shown for a neighbour served by no line ("" would read badly, a word could clash).
UNSERVED_LINE = "-"

#: Quality sidecar columns. walk_source: network/straight_line/mixed/unsnapped/none;
#: travel_source: observed/estimated/mixed/none; coord_confidence: score/needs_review/no_match/unavailable.
QUALITY_COLUMNS = [
    "ID", "stop_uid", "walk_source", "travel_source", "travel_n_obs",
    "coord_confidence", "station_dedup",
]

#: Exported columns (feed_id is the prefix of stop_uid, so it isn't repeated).
COLUMNS = [
    "ID", "stop_uid", "station", "system", "stop_id",
    "stop_lat", "stop_lon", "stop_line_ids", "near_stations", "same_line_stations",
]

#: Mean earth radius in meters.
_EARTH_RADIUS_M = 6_371_000.0

#: Vehicle speed (~25 km/h) used when a segment has no scheduled times.
VEHICLE_M_PER_MIN = 25_000.0 / 60.0


def _line_labels(routes: pd.DataFrame) -> pd.DataFrame:
    """Map route_uid to its label (short name, else route_id)."""
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
    """Walking minutes for straight-line meters (at least 1)."""
    return (distance_m / WALK_M_PER_MIN).round().clip(lower=1).astype(int)


def build_near_stations(
    stops: pd.DataFrame,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    stop_links: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per stop with its near_stations and same_line_stations lists."""
    base = stops[
        ["stop_uid", "stop_id", "stop_name", "stop_lat", "stop_lon"]
    ].copy()
    base = base.rename(columns={"stop_name": "station"})
    base["station"] = base["station"].fillna("")
    # Sequential id, 1..N in row order.
    base.insert(0, "ID", range(1, len(base) + 1))

    lines_at = _lines_at_stops(route_stops, routes)
    base = base.merge(_systems(route_stops, routes), on="stop_uid", how="left")
    base["system"] = base["system"].fillna("")

    # "{ID}-{line}" for each line serving this stop.
    own = base[["stop_uid", "ID"]].merge(lines_at, on="stop_uid")
    own["combo"] = own["ID"].astype(str) + "-" + own["combo_line"]
    combos = (
        own.sort_values(["stop_uid", "combo"])
        .groupby("stop_uid")["combo"]
        .agg(", ".join)
        .rename("stop_line_ids")
        .reset_index()
    )
    base = base.merge(combos, on="stop_uid", how="left")
    base["stop_line_ids"] = base["stop_line_ids"].fillna("")

    # Attached to base so both return paths below include it.
    base = _attach_same_line(base, stop_links)

    entries = _near_entries(base, lines_at, stop_pairs)
    if entries.empty:
        base["near_stations"] = "[]"
        return base[COLUMNS]

    entries = entries.sort_values(["stop_uid", "minutes", "near_station", "line"])
    entries["tuple"] = (
        "(" + entries["near_num_id"].astype(str) + ", " + entries["near_station"] + ", "
        + entries["line"].astype(str) + ", " + entries["minutes"].astype(str) + ")"
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


def _systems(route_stops: pd.DataFrame, routes: pd.DataFrame) -> pd.DataFrame:
    """Per stop, the agency names (modalidad) of the lines serving it."""
    columns = ["stop_uid", "system"]
    if route_stops.empty or routes.empty or "modalidad" not in routes.columns:
        return pd.DataFrame(columns=columns)
    served = (
        route_stops[["stop_uid", "route_uid"]]
        .merge(routes[["route_uid", "modalidad"]], on="route_uid")
        .dropna(subset=["modalidad"])
    )
    served["modalidad"] = served["modalidad"].astype("string").str.strip()
    served = served[served["modalidad"] != ""].drop_duplicates(["stop_uid", "modalidad"])
    if served.empty:
        return pd.DataFrame(columns=columns)
    return (
        served.sort_values(["stop_uid", "modalidad"])
        .groupby("stop_uid")["modalidad"]
        .agg(", ".join)
        .rename("system")
        .reset_index()
    )


def _lines_at_stops(route_stops: pd.DataFrame, routes: pd.DataFrame) -> pd.DataFrame:
    """Distinct (stop_uid, line label) pairs."""
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
    """One row per (stop, neighbour, line) before rendering; carries walk_source."""
    columns = ["stop_uid", "near_num_id", "near_station", "line", "minutes", "walk_source"]
    # Unserved neighbours still get listed, so only stop_pairs is required.
    if stop_pairs.empty:
        return pd.DataFrame(columns=columns)

    # Pairs are stored once; add the reverse direction.
    both = pd.concat(
        [
            stop_pairs.rename(columns={"stop_uid_a": "stop_uid", "stop_uid_b": "near_uid"}),
            stop_pairs.rename(columns={"stop_uid_b": "stop_uid", "stop_uid_a": "near_uid"}),
        ],
        ignore_index=True,
    )
    if "walk_source" not in both.columns:
        both["walk_source"] = "straight_line"
    # Left join: a neighbour without a line keeps its row and gets UNSERVED_LINE.
    served = (
        lines_at.rename(columns={"stop_uid": "near_uid", "combo_line": "line"})
        if not lines_at.empty
        else pd.DataFrame(columns=["near_uid", "line"], dtype=object)
    )
    entries = both.merge(served, on="near_uid", how="left")
    entries["line"] = entries["line"].fillna(UNSERVED_LINE)
    if entries.empty:
        return pd.DataFrame(columns=columns)
    entries = entries.merge(
        base[["stop_uid", "ID", "station"]].rename(
            columns={"stop_uid": "near_uid", "ID": "near_num_id", "station": "near_station"}
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
    """Great-circle meters between two point series."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    meters = 2 * _EARTH_RADIUS_M * np.arcsin(np.sqrt(a))
    return pd.Series(np.asarray(meters), index=lat1.index)


def _same_line_entries(base: pd.DataFrame, stop_links: pd.DataFrame | None) -> pd.DataFrame:
    """Previous and next stop per line and direction, with scheduled (or estimated) minutes."""
    columns = ["stop_uid", "near_num_id", "near_station", "linea", "direction_id",
               "minutes", "travel_min", "n_obs"]
    if stop_links is None or stop_links.empty:
        return pd.DataFrame(columns=columns)
    keep = ["stop_uid", "near_uid", "linea", "direction_id"]
    links = stop_links.copy()
    if "travel_min" not in links.columns:
        links["travel_min"] = pd.NA
    if "n_obs" not in links.columns:
        links["n_obs"] = 0
    keep_t = keep + ["travel_min", "n_obs"]
    forward = links.rename(columns={"stop_uid_a": "stop_uid", "stop_uid_b": "near_uid"})[keep_t]
    backward = links.rename(columns={"stop_uid_b": "stop_uid", "stop_uid_a": "near_uid"})[keep_t]
    both = pd.concat([forward, backward], ignore_index=True).drop_duplicates(keep)

    near_attr = base[["stop_uid", "ID", "station", "stop_lat", "stop_lon"]].rename(
        columns={
            "stop_uid": "near_uid", "ID": "near_num_id",
            "station": "near_station", "stop_lat": "near_lat", "stop_lon": "near_lon",
        }
    )
    entries = both.merge(near_attr, on="near_uid", how="inner").merge(
        base[["stop_uid", "stop_lat", "stop_lon"]], on="stop_uid", how="left"
    )
    if entries.empty:
        return pd.DataFrame(columns=columns)
    entries["near_station"] = entries["near_station"].fillna("")
    estimate = (
        _haversine_m(entries["stop_lat"], entries["stop_lon"],
                     entries["near_lat"], entries["near_lon"]) / VEHICLE_M_PER_MIN
    ).round(1)
    entries["minutes"] = pd.to_numeric(entries["travel_min"], errors="coerce").fillna(estimate)
    return entries[columns]


def _attach_same_line(base: pd.DataFrame, stop_links: pd.DataFrame | None) -> pd.DataFrame:
    """Add the rendered same_line_stations column to *base*."""
    entries = _same_line_entries(base, stop_links)
    if entries.empty:
        base["same_line_stations"] = "[]"
        return base
    entries = entries.sort_values(
        ["stop_uid", "linea", "direction_id", "minutes", "near_num_id"]
    )
    entries["tuple"] = (
        "(" + entries["near_num_id"].astype(str) + ", " + entries["near_station"] + ", "
        + entries["linea"].astype(str) + ", dir" + entries["direction_id"].astype(str) + ", "
        + entries["minutes"].astype(str) + ")"
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
    """Write near_stations.csv to *out_dir* and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / NEAR_STATIONS_NAME
    near.to_csv(path, index=False, encoding="utf-8")
    log.info("Near-stations export written: %s (%d stops)", path, len(near))
    return path


def _stop_snap_m(stop_pairs: pd.DataFrame) -> pd.Series:
    """Per-stop distance to the walk graph, or empty if --network-walk didn't run."""
    if stop_pairs.empty or "snap_m_a" not in stop_pairs.columns:
        return pd.Series(dtype=float)
    snaps = pd.concat(
        [
            stop_pairs[["stop_uid_a", "snap_m_a"]].rename(
                columns={"stop_uid_a": "stop_uid", "snap_m_a": "snap_m"}),
            stop_pairs[["stop_uid_b", "snap_m_b"]].rename(
                columns={"stop_uid_b": "stop_uid", "snap_m_b": "snap_m"}),
        ],
        ignore_index=True,
    ).dropna(subset=["snap_m"])
    if snaps.empty:
        return pd.Series(dtype=float)
    return snaps.groupby("stop_uid")["snap_m"].first()


def _summarize_source(entries: pd.DataFrame, column: str) -> pd.Series:
    """The stop's single source value, or "mixed" when its rows disagree."""
    if entries.empty:
        return pd.Series(dtype=object)
    return entries.groupby("stop_uid")[column].agg(
        lambda s: s.iloc[0] if s.nunique() == 1 else "mixed"
    )


def build_quality(
    near: pd.DataFrame,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    stop_links: pd.DataFrame | None = None,
    osm_matches: pd.DataFrame | None = None,
    canonical: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-stop provenance table, same rows and IDs as near_stations.

    Uses the same entry builders as the main file, so labels always match what
    the CSV shows. *osm_matches* is None when conflate-osm hasn't run.
    """
    if near.empty:
        return pd.DataFrame(columns=QUALITY_COLUMNS)
    out = near[["ID", "stop_uid"]].copy()

    # walk_source: how the near_stations minutes were obtained.
    entries = _near_entries(near, _lines_at_stops(route_stops, routes), stop_pairs)
    walk = _summarize_source(entries, "walk_source")
    out["walk_source"] = out["stop_uid"].map(walk).fillna("none")
    snap_m = _stop_snap_m(stop_pairs)
    if not snap_m.empty:
        far = out["stop_uid"].map(snap_m) > MAX_SNAP_M
        out.loc[far & (out["walk_source"] != "none"), "walk_source"] = "unsnapped"

    # travel_source / travel_n_obs: how the same_line_stations minutes were obtained.
    sl = _same_line_entries(near, stop_links)
    if not sl.empty:
        sl = sl.copy()
        sl["travel_source"] = np.where(
            pd.to_numeric(sl["travel_min"], errors="coerce").notna(), "observed", "estimated"
        )
        # Estimated rows have 0 observations, so the minimum reflects them.
        sl["n_obs"] = pd.to_numeric(sl["n_obs"], errors="coerce").fillna(0).astype(int)
        sl.loc[sl["travel_source"] == "estimated", "n_obs"] = 0
        travel = _summarize_source(sl, "travel_source")
        n_obs = sl.groupby("stop_uid")["n_obs"].min()
    else:
        travel, n_obs = pd.Series(dtype=object), pd.Series(dtype=int)
    out["travel_source"] = out["stop_uid"].map(travel).fillna("none")
    out["travel_n_obs"] = out["stop_uid"].map(n_obs).fillna(0).astype(int)

    # coord_confidence: the OSM match verdict.
    if osm_matches is None or osm_matches.empty:
        out["coord_confidence"] = "unavailable"
    else:
        m = osm_matches.set_index("stop_uid")
        conf = pd.Series("unavailable", index=out.index)
        matched = out["stop_uid"].isin(m.index)
        sub = m.reindex(out.loc[matched, "stop_uid"])
        verdict = np.where(
            sub["osm_id"].isna(), "no_match",
            np.where(sub["needs_review"], "needs_review",
                     sub["match_confidence"].map(lambda c: f"{c:.3f}")),
        )
        conf.loc[matched] = verdict
        out["coord_confidence"] = conf

    # station_dedup: the stop's dedupe group.
    if canonical is not None and not canonical.empty:
        canon = canonical.set_index("stop_uid")["canonical_uid"]
        out["station_dedup"] = out["stop_uid"].map(canon).fillna("")
    else:
        out["station_dedup"] = ""

    result = out.sort_values("ID", kind="mergesort")[QUALITY_COLUMNS].reset_index(drop=True)
    log.info(
        "Quality sidecar: walk %s | travel %s",
        result["walk_source"].value_counts().to_dict(),
        result["travel_source"].value_counts().to_dict(),
    )
    return result


def write_quality(quality: pd.DataFrame, out_dir: Path) -> Path:
    """Write near_stations_quality.csv to *out_dir* and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / QUALITY_NAME
    quality.to_csv(path, index=False, encoding="utf-8")
    log.info("Quality sidecar written: %s (%d stops)", path, len(quality))
    return path
