"""Station deduplication: find stop records that are the same physical station.

Two rules:

- cross-feed: stops of different feeds within 30 m, with a similar name
  (>= 0.75) or a shared route short name;
- within-feed: stops of the same feed within 5 m that share a route. Names
  don't count here, and the radius is tight so the opposite-direction stop
  across the street (20-40 m away, same routes) stays separate.

Accepted pairs then join stations closest first. A pair is rejected when it
would put two stops of the same feed more than the within-feed radius apart
into one station; this keeps opposite platforms apart even when each has a twin
in the other feed. The kept pairs are grouped by connected components; the
smallest stop_uid of a group is its canonical_uid. match_kind records which
rule matched.
Stops-only sources (MiBici) are excluded: a dock is never a transit station.
Records are never deleted; persistence happens in pipeline_phase2.
"""

from __future__ import annotations

import difflib
import logging
import unicodedata
from collections.abc import Collection
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

from . import connectivity

log = logging.getLogger(__name__)

#: Cross-feed candidate radius (meters).
DEDUPE_DISTANCE_M = 30.0

#: Within-feed candidate radius used by the pipelines; None in a config turns it off.
WITHIN_FEED_DISTANCE_M = 5.0

#: Minimum name similarity to accept a cross-feed pair on names alone.
NAME_SIM_THRESHOLD = 0.75

EQUIVALENCES_NAME = "stop_equivalences.csv"

REJECTED_NAME = "stop_equivalences_rejected.csv"

#: One row per accepted pair, before grouping.
PAIR_COLUMNS = [
    "stop_uid_a",
    "stop_uid_b",
    "distance_m",
    "name_score",
    "shared_routes",
    "score",
    "match_kind",
]

#: A rejected pair plus the two stops of one feed it would have put in one station.
REJECTED_COLUMNS = [*PAIR_COLUMNS, "conflict_uid_a", "conflict_uid_b", "conflict_m"]

#: One row per accepted pair, tagged with its group.
EQUIVALENCE_COLUMNS = [
    "group_id",
    "canonical_uid",
    "stop_uid_a",
    "stop_uid_b",
    "distance_m",
    "name_score",
    "shared_routes",
    "score",
    "match_kind",
]

#: match_kind values.
CROSS_FEED = "cross_feed"
WITHIN_FEED = "within_feed"

#: One row per stop that belongs to a group.
CANONICAL_COLUMNS = ["stop_uid", "canonical_uid", "group_id"]


def normalize_name(name: object) -> str:
    """Name without accents or punctuation, lowercased, words sorted; "" when missing."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = str(name)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() else " " for c in stripped.casefold())
    return " ".join(sorted(cleaned.split()))


def name_similarity(a: object, b: object) -> float:
    """Similarity 0-1 of two normalized names; 0.0 if either is missing."""
    norm_a, norm_b = normalize_name(a), normalize_name(b)
    if not norm_a or not norm_b:
        return 0.0
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio()


def _route_name_sets(route_stops: pd.DataFrame, routes: pd.DataFrame) -> pd.Series:
    """Per stop_uid, the set of normalized route short names serving it."""
    if route_stops.empty or routes.empty or "route_short_name" not in routes.columns:
        return pd.Series(dtype=object)
    labels = routes[["route_uid"]].copy()
    labels["short"] = routes["route_short_name"].map(normalize_name)
    labels = labels[labels["short"] != ""]
    if labels.empty:
        return pd.Series(dtype=object)
    served = route_stops[["stop_uid", "route_uid"]].merge(labels, on="route_uid")
    return served.groupby("stop_uid")["short"].agg(set)


def find_equivalences(
    stops: gpd.GeoDataFrame,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
    distance_m: float = DEDUPE_DISTANCE_M,
    name_threshold: float = NAME_SIM_THRESHOLD,
    within_feed_distance_m: float | None = None,
    exclude_feeds: Collection[str] = (),
) -> pd.DataFrame:
    """Accepted pairs (stop_uid_a < stop_uid_b), one row each, sorted.

    within_feed_distance_m=None turns the within-feed rule off. Stops of
    *exclude_feeds* are never matched. score (half closeness, half name) is
    for review only, never a filter.
    """
    columns = PAIR_COLUMNS
    if exclude_feeds:
        stops = stops[~stops["feed_id"].isin(list(exclude_feeds))]
    if len(stops) < 2:
        return pd.DataFrame(columns=columns)

    # One search at the larger radius; each rule applies its own radius below.
    search_m = max(distance_m, within_feed_distance_m or 0.0)
    candidates = connectivity.proximity_pairs_local(stops, search_m)
    if candidates.empty:
        return pd.DataFrame(columns=columns)

    meta = pd.DataFrame(stops[["stop_uid", "feed_id", "stop_name"]])
    candidates = candidates.merge(
        meta.rename(columns={"stop_uid": "stop_uid_a", "feed_id": "feed_a", "stop_name": "name_a"}),
        on="stop_uid_a",
    ).merge(
        meta.rename(columns={"stop_uid": "stop_uid_b", "feed_id": "feed_b", "stop_name": "name_b"}),
        on="stop_uid_b",
    )
    same_feed = candidates["feed_a"] == candidates["feed_b"]
    distance = candidates["distance_m"].astype(float)
    in_scope = ~same_feed & (distance <= distance_m)
    if within_feed_distance_m is not None:
        in_scope |= same_feed & (distance <= float(within_feed_distance_m))
    candidates = candidates[in_scope]
    if candidates.empty:
        return pd.DataFrame(columns=columns)
    same_feed = candidates["feed_a"] == candidates["feed_b"]

    candidates["match_kind"] = np.where(same_feed, WITHIN_FEED, CROSS_FEED)
    candidates["name_score"] = [
        name_similarity(a, b) for a, b in zip(candidates["name_a"], candidates["name_b"])
    ]
    name_sets = _route_name_sets(route_stops, routes)
    sets_a = candidates["stop_uid_a"].map(name_sets)
    sets_b = candidates["stop_uid_b"].map(name_sets)
    candidates["shared_routes"] = [
        len(sa & sb) if isinstance(sa, set) and isinstance(sb, set) else 0
        for sa, sb in zip(sets_a, sets_b)
    ]

    # Cross-feed: name or shared route. Within-feed: shared route only.
    has_route = candidates["shared_routes"] >= 1
    accept = np.where(
        same_feed, has_route, (candidates["name_score"] >= name_threshold) | has_route
    )
    accepted = candidates[accept].copy()
    if accepted.empty:
        return pd.DataFrame(columns=columns)

    accepted["name_score"] = accepted["name_score"].round(3)
    accepted["distance_m"] = accepted["distance_m"].astype(float).round(2)
    # Closeness is measured against the radius of the rule that accepted the pair.
    radius = np.where(
        accepted["match_kind"] == WITHIN_FEED,
        float(within_feed_distance_m or distance_m),
        float(distance_m),
    )
    accepted["score"] = (
        0.5 * (1.0 - accepted["distance_m"] / radius) + 0.5 * accepted["name_score"]
    ).round(3)
    result = (
        accepted[columns]
        .sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")
        .reset_index(drop=True)
    )
    n_within = int((result["match_kind"] == WITHIN_FEED).sum())
    log.info(
        "Dedupe: %d alias pair(s) accepted - %d cross-feed within %.0f m, "
        "%d within-feed within %.0f m",
        len(result),
        len(result) - n_within,
        distance_m,
        n_within,
        within_feed_distance_m or 0.0,
    )
    return result


def _same_feed_conflict(
    group_a: list[str], group_b: list[str], feed: dict, xy: dict, limit_m: float
) -> tuple[str, str, float] | None:
    """First pair of same-feed stops, one from each group, farther apart than *limit_m*."""
    for uid_a in group_a:
        for uid_b in group_b:
            if feed[uid_a] != feed[uid_b]:
                continue
            (xa, ya), (xb, yb) = xy[uid_a], xy[uid_b]
            meters = float(np.hypot(xa - xb, ya - yb))
            if meters > limit_m:
                low, high = sorted((uid_a, uid_b))
                return low, high, round(meters, 2)
    return None


def drop_conflicting_pairs(
    accepted: pd.DataFrame, stops: gpd.GeoDataFrame, same_feed_limit_m: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split accepted pairs into (kept, rejected) so no station mixes far-apart stops of one feed.

    Pairs join stations closest first. A pair that would put two stops of the
    same feed more than *same_feed_limit_m* apart into one station is rejected;
    the rejected row names that stop pair. Without this, chaining merges the
    opposite platforms of a street through their twins in the other feed.
    """
    if accepted.empty:
        return accepted, pd.DataFrame(columns=REJECTED_COLUMNS)
    uids = set(accepted["stop_uid_a"]) | set(accepted["stop_uid_b"])
    members_frame = stops[stops["stop_uid"].isin(uids)]
    projected = members_frame.to_crs(members_frame.estimate_utm_crs())
    xy = dict(zip(members_frame["stop_uid"], zip(projected.geometry.x, projected.geometry.y)))
    feed = dict(zip(members_frame["stop_uid"], members_frame["feed_id"]))

    root = {uid: uid for uid in uids}
    members = {uid: [uid] for uid in uids}

    def find(uid: str) -> str:
        while root[uid] != uid:
            root[uid] = root[root[uid]]
            uid = root[uid]
        return uid

    order = accepted.sort_values(["distance_m", "stop_uid_a", "stop_uid_b"], kind="mergesort")
    kept_index: list = []
    conflicts: dict = {}
    for index, uid_a, uid_b in zip(order.index, order["stop_uid_a"], order["stop_uid_b"]):
        root_a, root_b = find(uid_a), find(uid_b)
        if root_a != root_b:
            conflict = _same_feed_conflict(
                members[root_a], members[root_b], feed, xy, same_feed_limit_m
            )
            if conflict is not None:
                conflicts[index] = conflict
                continue
            root[root_b] = root_a
            members[root_a] = sorted(members[root_a] + members.pop(root_b))
        kept_index.append(index)

    kept = accepted.loc[accepted.index.isin(kept_index)].reset_index(drop=True)
    if not conflicts:
        return kept, pd.DataFrame(columns=REJECTED_COLUMNS)
    rejected = accepted.loc[list(conflicts)].copy()
    rejected[["conflict_uid_a", "conflict_uid_b", "conflict_m"]] = list(conflicts.values())
    rejected = (
        rejected[REJECTED_COLUMNS]
        .sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")
        .reset_index(drop=True)
    )
    if len(rejected):
        log.info(
            "Dedupe: %d pair(s) rejected - they would merge stops of one feed more than %g m apart",
            len(rejected),
            same_feed_limit_m,
        )
    return kept, rejected


def canonical_map(equivalences: pd.DataFrame) -> pd.DataFrame:
    """Group stops by connected components; canonical_uid = smallest member."""
    if equivalences.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)
    graph = nx.Graph()
    graph.add_edges_from(zip(equivalences["stop_uid_a"], equivalences["stop_uid_b"]))
    components = sorted(nx.connected_components(graph), key=min)
    records = [
        {"stop_uid": uid, "canonical_uid": min(component), "group_id": group_id}
        for group_id, component in enumerate(components)
        for uid in sorted(component)
    ]
    return pd.DataFrame(records, columns=CANONICAL_COLUMNS)


def attach_groups(equivalences: pd.DataFrame, canonical: pd.DataFrame) -> pd.DataFrame:
    """Add group_id and canonical_uid to each pair (the stop_equivalences output)."""
    if equivalences.empty or canonical.empty:
        return pd.DataFrame(columns=EQUIVALENCE_COLUMNS)
    tagged = equivalences.merge(
        canonical.rename(columns={"stop_uid": "stop_uid_a"}), on="stop_uid_a"
    )
    return (
        tagged[EQUIVALENCE_COLUMNS]
        .sort_values(["group_id", "stop_uid_a", "stop_uid_b"], kind="mergesort")
        .reset_index(drop=True)
    )


def filter_stop_pairs(stop_pairs: pd.DataFrame, canonical: pd.DataFrame) -> pd.DataFrame:
    """Remove proximity pairs whose two stops belong to the same group."""
    if stop_pairs.empty or canonical.empty:
        return stop_pairs
    lookup = canonical.set_index("stop_uid")["canonical_uid"]
    canon_a = stop_pairs["stop_uid_a"].map(lookup)
    canon_b = stop_pairs["stop_uid_b"].map(lookup)
    alias = canon_a.notna() & (canon_a == canon_b)
    kept = stop_pairs[~alias].reset_index(drop=True)
    if alias.any():
        log.info("Dedupe: %d alias stop pair(s) removed from proximity set", int(alias.sum()))
    return kept


def write_rejected(rejected: pd.DataFrame, out_dir: Path) -> Path:
    """Write stop_equivalences_rejected.csv to *out_dir* and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / REJECTED_NAME
    rejected.to_csv(path, index=False, encoding="utf-8")
    log.info("Rejected alias pairs written: %s (%d pairs)", path, len(rejected))
    return path


def write_equivalences(equivalences: pd.DataFrame, out_dir: Path) -> Path:
    """Write stop_equivalences.csv to *out_dir* and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / EQUIVALENCES_NAME
    equivalences.to_csv(path, index=False, encoding="utf-8")
    log.info("Stop equivalences written: %s (%d pairs)", path, len(equivalences))
    return path
