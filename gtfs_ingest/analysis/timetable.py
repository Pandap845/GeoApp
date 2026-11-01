"""Timetable exports for CSA, RAPTOR and time-dependent Dijkstra.

connections.csv.gz has one row per vehicle hop between consecutive stops, with
frequencies.txt expanded into runs ("{trip_id}@HH:MM:SS" of the run start).
footpaths.csv has the walk links both ways, the dedupe alias links, and a
minimum change time per stop written as a stop -> same-stop footpath.
Footpaths are one hop, not transitively closed.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

import pandas as pd

from .headways import frequency_runs
from .near_stations import walk_minutes
from .route_geometry import direction_series, ordered_stop_times

log = logging.getLogger(__name__)

#: Minimum change time at a stop and between alias stops; CLI default too.
MIN_TRANSFER_S = 120

CONNECTIONS_NAME = "connections.csv.gz"
FOOTPATHS_NAME = "footpaths.csv"
CONNECTION_COLUMNS = [
    "trip_id",
    "route_uid",
    "direction_id",
    "from_stop",
    "to_stop",
    "dep_s",
    "arr_s",
]
FOOTPATH_COLUMNS = ["from_stop", "to_stop", "walk_s", "kind"]


def _hhmmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _hops(stop_times: pd.DataFrame) -> pd.DataFrame:
    """(trip_id, from, to, dep, arr) between consecutive timed stops of each trip."""
    columns = ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]
    ordered, _ = ordered_stop_times(stop_times.reindex(columns=columns))
    # A stop with only one of the two times uses it for both.
    ordered["departure_time"] = ordered["departure_time"].fillna(ordered["arrival_time"])
    ordered["arrival_time"] = ordered["arrival_time"].fillna(ordered["departure_time"])
    timed = ordered.dropna(subset=["departure_time"])
    if len(timed) < len(ordered):
        log.info("Timetable: %d untimed stop_times row(s) skipped", len(ordered) - len(timed))
    nxt = timed.groupby("trip_id")
    hops = pd.DataFrame(
        {
            "trip_id": timed["trip_id"],
            "from_stop": timed["stop_id"],
            "to_stop": nxt["stop_id"].shift(-1),
            "dep_s": timed["departure_time"],
            "arr_s": nxt["arrival_time"].shift(-1),
        }
    )
    return hops.dropna(subset=["to_stop"])


def _expand_runs(hops: pd.DataFrame, frequencies: pd.DataFrame | None) -> pd.DataFrame:
    """Replace frequency trips' template hops by one copy per run ("trip@HH:MM:SS")."""
    if frequencies is None or frequencies.empty:
        return hops
    # Overlapping frequency rows can repeat a run; keep it once.
    runs = frequency_runs(frequencies).drop_duplicates()
    runs["label"] = "@" + runs["run_start"].map(_hhmmss)
    is_template = hops["trip_id"].isin(set(runs["trip_id"]))
    template = hops[is_template].copy()
    first = template.groupby("trip_id")["dep_s"].transform("min")
    template["dep_off"] = template["dep_s"] - first
    template["arr_off"] = template["arr_s"] - first
    expanded = runs.merge(template.drop(columns=["dep_s", "arr_s"]), on="trip_id")
    expanded["dep_s"] = expanded["run_start"] + expanded["dep_off"]
    expanded["arr_s"] = expanded["run_start"] + expanded["arr_off"]
    expanded["trip_id"] = expanded["trip_id"] + expanded["label"]
    return pd.concat([hops[~is_template], expanded[hops.columns]], ignore_index=True)


def feed_connections(feed: Any, feed_id: str) -> pd.DataFrame:
    """Every vehicle hop of one service day, frequencies expanded, sorted by departure."""
    stop_times, trips = feed.stop_times, feed.trips
    if stop_times.empty or trips.empty:
        return pd.DataFrame(columns=CONNECTION_COLUMNS)
    meta = pd.DataFrame(
        {
            "trip_id": trips["trip_id"].astype(str),
            "route_uid": feed_id + ":" + trips["route_id"].astype(str),
            "direction_id": direction_series(trips).to_numpy(),
        }
    )
    # Ids are prefixed on the template hops, before frequencies multiply them.
    hops = _hops(stop_times).merge(meta, on="trip_id")
    hops["from_stop"] = feed_id + ":" + hops["from_stop"]
    hops["to_stop"] = feed_id + ":" + hops["to_stop"]
    frequencies = getattr(feed, "frequencies", None)
    if frequencies is not None and not frequencies.empty:
        frequencies = frequencies.assign(trip_id=feed_id + ":" + frequencies["trip_id"].astype(str))
    hops["trip_id"] = feed_id + ":" + hops["trip_id"]
    connections = _expand_runs(hops, frequencies)

    backwards = connections["arr_s"] < connections["dep_s"]
    if backwards.any():
        log.warning(
            "Feed %s: %d hop(s) arrive before they depart; skipped", feed_id, backwards.sum()
        )
        connections = connections[~backwards]
    connections = connections.assign(
        dep_s=connections["dep_s"].round().astype("int64"),
        arr_s=connections["arr_s"].round().astype("int64"),
    )
    return sort_connections(connections[CONNECTION_COLUMNS])


def sort_connections(connections: pd.DataFrame) -> pd.DataFrame:
    """The CSA order: by departure, then arrival, trip and stop (stable)."""
    return connections.sort_values(
        ["dep_s", "arr_s", "trip_id", "from_stop"], kind="mergesort"
    ).reset_index(drop=True)


def footpaths(
    served_stops: Collection[str],
    stop_pairs: pd.DataFrame,
    equivalences: pd.DataFrame,
    min_transfer_s: int = MIN_TRANSFER_S,
) -> pd.DataFrame:
    """Footpaths between *served_stops*: self change time, alias links and walk links."""
    served = sorted(set(served_stops))
    frames = [
        pd.DataFrame(
            {"from_stop": served, "to_stop": served, "walk_s": min_transfer_s, "kind": "self"}
        )
    ]
    if not equivalences.empty:
        frames.append(_both_ways(equivalences, min_transfer_s, "same_station"))
    if not stop_pairs.empty:
        frames.append(_both_ways(stop_pairs, walk_minutes(stop_pairs["distance_m"]) * 60, "walk"))
    paths = pd.concat(frames, ignore_index=True)
    keep = paths["from_stop"].isin(served) & paths["to_stop"].isin(served)
    paths = paths[keep].astype({"walk_s": "int64"})
    return paths.sort_values(["from_stop", "to_stop", "kind"], kind="mergesort").reset_index(
        drop=True
    )


def _both_ways(pairs: pd.DataFrame, walk_s: Any, kind: str) -> pd.DataFrame:
    """Two footpaths per stop pair (a -> b and b -> a)."""
    one = pd.DataFrame(
        {"from_stop": pairs["stop_uid_a"], "to_stop": pairs["stop_uid_b"], "walk_s": walk_s}
    )
    other = one.rename(columns={"from_stop": "to_stop", "to_stop": "from_stop"})
    return pd.concat([one, other], ignore_index=True).assign(kind=kind)[FOOTPATH_COLUMNS]
