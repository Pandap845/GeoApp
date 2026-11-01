"""One source per physical line: cross-feed route twins and duplicate trips inside a feed.

Two routes of different feeds are the same line when their stop sets, mapped
to dedupe stations, have a Jaccard index of at least ROUTE_MATCH_MIN_JACCARD.
Pairs are accepted best first and one-to-one; the route of the feed with the
later calendar is kept. Short names are not compared to decide: "L1" names
both Mi Tren and Sitren. Inside one feed, two trips of a route with the same
times and frequencies over stops that are pairwise within a few metres are
one trip listed twice; the smallest trip_id is kept. Only routing inputs use
the result; per-feed outputs keep every route and trip.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from .near_stations import haversine_m
from .route_geometry import ordered_stop_times

log = logging.getLogger(__name__)

#: Keep one source per line in the routing inputs unless asked not to; CLI default too.
ROUTE_DEDUPE = True
#: True twins score >= 0.91 in the Guadalajara feeds, every other pair <= 0.53.
ROUTE_MATCH_MIN_JACCARD = 0.8

ROUTE_EQUIVALENCES_NAME = "route_equivalences.csv"
DUPLICATE_TRIPS_NAME = "duplicate_trips.csv"
EQUIVALENCE_COLUMNS = [
    "kept_route_uid",
    "dropped_route_uid",
    "stop_jaccard",
    "same_short_name",
    "rule",
]
DUPLICATE_TRIP_COLUMNS = ["feed_id", "route_uid", "kept_trip_id", "dropped_trip_id"]


def _route_stations(route_stops: pd.DataFrame, canonical: pd.DataFrame) -> pd.DataFrame:
    """Distinct (route_uid, feed_id, station) rows; a stop without alias is its own station."""
    station = (
        canonical.set_index("stop_uid")["canonical_uid"]
        if not canonical.empty
        else pd.Series(dtype=object)
    )
    frame = route_stops[["route_uid", "feed_id", "stop_uid"]].copy()
    frame["station"] = frame["stop_uid"].map(station).fillna(frame["stop_uid"])
    return frame.drop_duplicates(["route_uid", "station"])[["route_uid", "feed_id", "station"]]


def _candidate_pairs(stations: pd.DataFrame) -> pd.DataFrame:
    """Cross-feed route pairs (route_a < route_b) with their stop-set Jaccard index."""
    sizes = stations.groupby("route_uid").size()
    joined = stations.merge(stations, on="station", suffixes=("_a", "_b"))
    joined = joined[
        (joined["feed_id_a"] != joined["feed_id_b"])
        & (joined["route_uid_a"] < joined["route_uid_b"])
    ]
    pairs = joined.groupby(["route_uid_a", "route_uid_b"], as_index=False).size()
    pairs = pairs.rename(columns={"size": "shared"})
    union = pairs["route_uid_a"].map(sizes) + pairs["route_uid_b"].map(sizes) - pairs["shared"]
    pairs["stop_jaccard"] = pairs["shared"] / union
    return pairs


def _newest_first(feeds: list[str], feed_rank: Mapping[str, tuple[str, str]]) -> list[str]:
    """Later calendar end first, then later service date, then smaller feed_id."""
    by_id = sorted(feeds)
    return sorted(by_id, key=lambda feed: feed_rank.get(feed, ("", "")), reverse=True)


def _rule(kept: str, dropped: str, feed_rank: Mapping[str, tuple[str, str]]) -> str:
    kept_end, kept_date = feed_rank.get(kept, ("", ""))
    dropped_end, dropped_date = feed_rank.get(dropped, ("", ""))
    if kept_end != dropped_end:
        return "newer_calendar"
    if kept_date != dropped_date:
        return "later_service_date"
    return "feed_id"


def match_routes(
    route_stops: pd.DataFrame,
    canonical: pd.DataFrame,
    routes: pd.DataFrame,
    feed_rank: Mapping[str, tuple[str, str]],
    min_jaccard: float = ROUTE_MATCH_MIN_JACCARD,
) -> pd.DataFrame:
    """One row per matched line: the kept route, the dropped twin and why.

    feed_rank maps feed_id to (calendar_end, service_date) ISO strings.
    """
    if route_stops.empty:
        return pd.DataFrame(columns=EQUIVALENCE_COLUMNS)
    pairs = _candidate_pairs(_route_stations(route_stops, canonical))
    pairs = pairs[pairs["stop_jaccard"] >= min_jaccard].sort_values(
        ["stop_jaccard", "route_uid_a", "route_uid_b"],
        ascending=[False, True, True],
        kind="mergesort",
    )
    feed_of = route_stops.drop_duplicates("route_uid").set_index("route_uid")["feed_id"]
    short = _short_names(routes)
    matched: set[str] = set()
    rows = []
    for a, b, jaccard in zip(pairs["route_uid_a"], pairs["route_uid_b"], pairs["stop_jaccard"]):
        if a in matched or b in matched:
            continue
        matched.update((a, b))
        newer = _newest_first([feed_of[a], feed_of[b]], feed_rank)[0]
        kept, dropped = (a, b) if feed_of[a] == newer else (b, a)
        rows.append(
            {
                "kept_route_uid": kept,
                "dropped_route_uid": dropped,
                "stop_jaccard": round(float(jaccard), 4),
                "same_short_name": bool(short.get(a) and short.get(a) == short.get(b)),
                "rule": _rule(feed_of[kept], feed_of[dropped], feed_rank),
            }
        )
    result = pd.DataFrame(rows, columns=EQUIVALENCE_COLUMNS)
    log.info("Route dedupe: %d line(s) present in two feeds", len(result))
    return result.sort_values("kept_route_uid", kind="mergesort").reset_index(drop=True)


def _short_names(routes: pd.DataFrame) -> dict[str, str]:
    if routes.empty or "route_short_name" not in routes.columns:
        return {}
    names = routes["route_short_name"].astype(str).str.strip().replace("nan", "")
    return dict(zip(routes["route_uid"], names))


def _trip_signatures(feed: Any) -> pd.DataFrame:
    """Per trip: route, times, frequencies (the key of a duplicate) and its ordered stops."""
    columns = ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]
    stop_times = feed.stop_times
    ordered, _ = ordered_stop_times(stop_times[[c for c in columns if c in stop_times.columns]])
    times = ordered.reindex(columns=columns).fillna({"arrival_time": -1, "departure_time": -1})
    per_trip = times.groupby("trip_id", sort=True).agg(
        stops=("stop_id", tuple),
        arrivals=("arrival_time", tuple),
        departures=("departure_time", tuple),
    )
    runs: dict[str, tuple] = {}
    frequencies = getattr(feed, "frequencies", None)
    if frequencies is not None and not frequencies.empty:
        rows = frequencies.sort_values(["trip_id", "start_time"], kind="mergesort")
        for trip, group in rows.groupby("trip_id", sort=True):
            runs[str(trip)] = tuple(
                zip(group["start_time"], group["end_time"], group["headway_secs"])
            )
    per_trip["frequencies"] = [runs.get(str(trip), ()) for trip in per_trip.index]
    route = feed.trips.assign(trip_id=feed.trips["trip_id"].astype(str)).set_index("trip_id")
    per_trip["route_id"] = route["route_id"].astype(str).reindex(per_trip.index)
    return per_trip.dropna(subset=["route_id"])


def _same_places(stops_a: tuple, stops_b: tuple, coords: pd.DataFrame, radius_m: float) -> bool:
    """True if the two stop lists are equally long and every pair is within radius_m."""
    if len(stops_a) != len(stops_b):
        return False
    a = coords.reindex(list(stops_a))
    b = coords.reindex(list(stops_b))
    meters = haversine_m(
        a["stop_lat"].reset_index(drop=True),
        a["stop_lon"].reset_index(drop=True),
        b["stop_lat"].reset_index(drop=True),
        b["stop_lon"].reset_index(drop=True),
    )
    return bool(np.all(meters.to_numpy() <= radius_m))


def duplicate_trips(feed: Any, feed_id: str, radius_m: float) -> pd.DataFrame:
    """Trips that repeat another trip of their route over alias stops; one row per dropped trip."""
    if feed.stop_times.empty or feed.trips.empty:
        return pd.DataFrame(columns=DUPLICATE_TRIP_COLUMNS)
    signatures = _trip_signatures(feed)
    coords = feed.stops.assign(stop_id=feed.stops["stop_id"].astype(str)).set_index("stop_id")
    rows = []
    key = ["route_id", "arrivals", "departures", "frequencies"]
    for (route_id, *_), group in signatures.groupby(key, sort=True):
        kept: list[str] = []
        for trip in sorted(group.index):
            stops = group.at[trip, "stops"]
            twin = next(
                (k for k in kept if _same_places(group.at[k, "stops"], stops, coords, radius_m)),
                None,
            )
            if twin is None:
                kept.append(trip)
            else:
                rows.append((feed_id, f"{feed_id}:{route_id}", twin, trip))
    result = pd.DataFrame(rows, columns=DUPLICATE_TRIP_COLUMNS)
    if not result.empty:
        log.info("Feed %s: %d trip(s) listed twice over alias stops", feed_id, len(result))
    return result.sort_values(["route_uid", "dropped_trip_id"], kind="mergesort").reset_index(
        drop=True
    )


def without_trips(feed: Any, trip_ids: set[str]) -> Any:
    """A read-only view of *feed* without *trip_ids* (stop_times, trips, frequencies)."""
    if not trip_ids:
        return feed
    keep_st = ~feed.stop_times["trip_id"].astype(str).isin(trip_ids)
    keep_tr = ~feed.trips["trip_id"].astype(str).isin(trip_ids)
    frequencies = getattr(feed, "frequencies", None)
    if frequencies is not None and not frequencies.empty:
        frequencies = frequencies[~frequencies["trip_id"].astype(str).isin(trip_ids)]
    return SimpleNamespace(
        stop_times=feed.stop_times[keep_st].reset_index(drop=True),
        trips=feed.trips[keep_tr].reset_index(drop=True),
        frequencies=frequencies,
        routes=feed.routes,
        stops=feed.stops,
    )
