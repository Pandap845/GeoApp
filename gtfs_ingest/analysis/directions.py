"""Relabel direction_id where a feed puts trips of both senses under one direction.

Headways group by (route, direction, stop), so a route with both senses under
one direction_id counts both ways together. A trip's sense is the sign of the
rank correlation between its stop order and the route's longest trip. Routes
whose labels already agree with their senses are left as they are.
"""

from __future__ import annotations

import logging
from collections import Counter

import numpy as np
import pandas as pd

from .route_geometry import direction_series, ordered_stop_times

log = logging.getLogger(__name__)

#: Relabel mixed-sense routes unless asked not to; CLI default too.
FIX_DIRECTIONS = True

FIX_COLUMNS = ["route_id", "trip_id", "direction_id_feed", "direction_id_used"]

#: Fewer shared stops than this give no sense.
MIN_SHARED_STOPS = 2


def _stop_orders(stop_times: pd.DataFrame) -> dict[str, dict[str, int]]:
    """{trip_id: {stop_id: position of its first visit}}."""
    ordered, _ = ordered_stop_times(stop_times[["trip_id", "stop_id", "stop_sequence"]])
    ordered["position"] = ordered.groupby("trip_id").cumcount()
    first = ordered.drop_duplicates(["trip_id", "stop_id"])
    return {
        str(trip): dict(zip(group["stop_id"], group["position"]))
        for trip, group in first.groupby("trip_id", sort=True)
    }


def _same_sense(order: dict[str, int], reference: dict[str, int]) -> bool | None:
    """True if *order* runs the way *reference* does, False if reversed, None if unclear."""
    shared = sorted(set(order) & set(reference))
    if len(shared) < MIN_SHARED_STOPS:
        return None
    corr = np.corrcoef([reference[s] for s in shared], [order[s] for s in shared])[0, 1]
    if not np.isfinite(corr) or corr == 0:
        return None
    return bool(corr > 0)


def _is_mixed(senses: dict[str, bool | None], labels: pd.Series) -> bool:
    """True if one direction_id holds trips of both senses."""
    by_label: dict[int, set[bool]] = {}
    for trip, same in senses.items():
        if same is not None:
            by_label.setdefault(int(labels[trip]), set()).add(same)
    return any(len(found) > 1 for found in by_label.values())


def _reference_label(senses: dict[str, bool | None], labels: pd.Series) -> int:
    """The label most same-sense trips already carry (0 when none is usable)."""
    same = Counter(int(labels[t]) for t, s in senses.items() if s is True and labels[t] in (0, 1))
    if same:
        return min(same, key=lambda label: (-same[label], label))
    other = Counter(int(labels[t]) for t, s in senses.items() if s is False and labels[t] in (0, 1))
    if other:
        return 1 - min(other, key=lambda label: (-other[label], label))
    return 0


def fix_directions(
    stop_times: pd.DataFrame, trips: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trips with direction_id from stop order on mixed routes; returns (trips, changed rows)."""
    if trips.empty or stop_times.empty:
        return trips, pd.DataFrame(columns=FIX_COLUMNS)
    orders = _stop_orders(stop_times)
    labels = pd.Series(direction_series(trips).to_numpy(), index=trips["trip_id"].astype(str))
    used = labels.copy()
    route_of = pd.Series(trips["route_id"].astype(str).to_numpy(), index=labels.index)
    for route_id in sorted(route_of.unique()):
        ids = sorted(t for t in route_of.index[route_of == route_id] if t in orders)
        if len(ids) < 2:  # a single trip has nothing to disagree with
            continue
        reference = min(ids, key=lambda t: (-len(orders[t]), t))
        senses = {t: _same_sense(orders[t], orders[reference]) for t in ids}
        if not _is_mixed(senses, labels):
            continue
        forward = _reference_label(senses, labels)
        for trip, same in senses.items():
            if same is not None:
                used[trip] = forward if same else 1 - forward

    changed = used != labels
    fixes = pd.DataFrame(
        {
            "route_id": route_of[changed].to_numpy(),
            "trip_id": used.index[changed],
            "direction_id_feed": labels[changed].to_numpy(),
            "direction_id_used": used[changed].to_numpy(),
        },
        columns=FIX_COLUMNS,
    )
    if fixes.empty:
        return trips, fixes
    fixed = trips.copy()
    fixed["direction_id"] = used.to_numpy()
    log.info(
        "Directions: %d trip(s) of %d route(s) relabelled from stop order",
        len(fixes),
        fixes["route_id"].nunique(),
    )
    return fixed, fixes.sort_values(["route_id", "trip_id"], kind="mergesort").reset_index(
        drop=True
    )
