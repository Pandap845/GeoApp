"""Station deduplication (Phase 2, also reused by Phase 3).

The same physical station is often described more than once, under different
stop ids. Two distinct cases, with deliberately different evidence rules:

**Cross-feed** (always on) - different feeds describe one station differently
(e.g. ``improved:SITEUR_x`` vs ``mdb:TL_x``):

* candidate pairs: stops of **different feeds** within ``distance_m``
  (default 30 m), found with :func:`connectivity.proximity_pairs_local`;
* accept rule: normalized-name similarity >= ``name_threshold`` **or** at
  least one shared normalized ``route_short_name``.

**Within-feed** (``within_feed_distance_m``; the pipelines default to
:data:`WITHIN_FEED_DISTANCE_M`, the function itself is opt-in) - ONE feed
lists a station twice under two id schemes, both scheduled. Observed in the
wild: a feed publishing its light-rail line as both ``SITEUR_001_025`` and
``ST_01_025``, so the route renders as two disjoint chains.

* candidate pairs: stops of the **same feed** within ``within_feed_distance_m``;
* accept rule: **at least one shared route, and nothing else.** Name
  similarity deliberately cannot rescue a within-feed pair. Two stops of one
  feed can share a name for ordinary reasons (a corridor, a plaza), whereas
  two stops of the *same route* at the same spot are a duplicate record.
* the radius is deliberately **tight** (5 m). Duplicate records sit at
  0 m, while the same route's stop for the *opposite direction* - across the
  street, or the other platform of a median station - is typically 20-40 m
  away and shares every route. A 50 m radius merged those direction pairs
  (2,697 within-feed pairs on the Guadalajara feeds, median 28.9 m), which
  turns two platforms into one "station" and gives routing a free U-turn.

Both kinds feed one grouping step:

* groups: connected components over accepted pairs (the same networkx idiom
  as ``conflation.clustering.cluster_stops``), deterministically ordered by
  each group's smallest ``stop_uid``, which also becomes the group's
  ``canonical_uid``.

Each accepted pair records which rule matched it in ``match_kind``, so
``stop_equivalences.csv`` stays auditable: the two kinds carry different
evidence and should be reviewable separately.

Stops of a stops-only source (``exclude_feeds``, e.g. MiBici docks) are left
out of both rules. The within-feed rule could never fire for them anyway (a
stops CSV has no routes), but the cross-feed *name* rule could: a bike dock
named after a corner, within 30 m of a bus stop named after the same corner,
would otherwise become an alias of it. A dock is never the same station as a
transit stop, so such stops are excluded up front rather than left to chance.

Name matching is stdlib only: NFKD accent stripping + ``casefold`` +
token-sorted ``difflib.SequenceMatcher`` ratio. Stops without a usable name
score 0.0 and can only match through shared routes.

Everything here is pure/in-memory; persistence (``stop_equivalences.csv`` and
the ``stop_equivalences`` table) is wired in ``pipeline_phase2``.
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

#: Maximum separation for two stops to be alias candidates.
DEDUPE_DISTANCE_M = 30.0

#: Pipeline default for the within-feed radius (see the module docstring for
#: why it is this tight). ``None`` in the configs disables within-feed dedupe.
WITHIN_FEED_DISTANCE_M = 5.0

#: Minimum token-sort name similarity to accept on names alone.
NAME_SIM_THRESHOLD = 0.75

EQUIVALENCES_NAME = "stop_equivalences.csv"

#: Audit payload - one row per accepted alias pair, tagged with its group.
#: ``match_kind`` is ``cross_feed`` or ``within_feed``; the two are accepted
#: on different evidence, so keep them distinguishable in the audit trail.
EQUIVALENCE_COLUMNS = [
    "group_id", "canonical_uid", "stop_uid_a", "stop_uid_b",
    "distance_m", "name_score", "shared_routes", "score", "match_kind",
]

#: ``match_kind`` values.
CROSS_FEED = "cross_feed"
WITHIN_FEED = "within_feed"

#: Membership map - one row per stop that belongs to some alias group.
CANONICAL_COLUMNS = ["stop_uid", "canonical_uid", "group_id"]


def normalize_name(name: object) -> str:
    """Accent-stripped, casefolded, token-sorted form of a stop/route name.

    ``None``/NaN/empty inputs normalize to ``""`` so they can never match on
    name evidence alone.
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = str(name)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() else " " for c in stripped.casefold())
    return " ".join(sorted(cleaned.split()))


def name_similarity(a: object, b: object) -> float:
    """Similarity ratio of two names after :func:`normalize_name` (0.0-1.0).

    Either side normalizing to ``""`` yields 0.0 (missing names are not
    evidence of anything).
    """
    norm_a, norm_b = normalize_name(a), normalize_name(b)
    if not norm_a or not norm_b:
        return 0.0
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio()


def _route_name_sets(route_stops: pd.DataFrame, routes: pd.DataFrame) -> pd.Series:
    """Per ``stop_uid``: the set of normalized route short names serving it.

    Routes without a usable ``route_short_name`` contribute nothing (route
    ids are feed-scoped, so they can never provide cross-feed evidence).
    """
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
    """Accepted alias pairs among *stops*.

    Args:
        stops: Phase 1 normalized stops of all feeds (EPSG:4326 GeoDataFrame
            with ``stop_uid``, ``feed_id``, ``stop_name``, ``geometry``).
        route_stops: concatenated ``connectivity.route_stop_map`` frames.
        routes: route metadata with ``route_uid`` and (optionally)
            ``route_short_name`` - the shared-route evidence source.
        distance_m: candidate radius in meters for CROSS-feed pairs.
        name_threshold: minimum :func:`name_similarity` to accept on names
            (cross-feed only).
        within_feed_distance_m: candidate radius for pairs of the SAME feed.
            ``None`` (the default) disables within-feed matching entirely, so
            the result is exactly the cross-feed behavior. Within-feed pairs
            are accepted on shared routes alone - see the module docstring.
        exclude_feeds: feed ids whose stops can never be an alias of anything
            (stops-only sources). They are dropped before the spatial pass.

    Returns:
        :data:`EQUIVALENCE_COLUMNS` minus the group fields, i.e. ``stop_uid_a,
        stop_uid_b, distance_m, name_score, shared_routes, score,
        match_kind`` with ``stop_uid_a < stop_uid_b``, sorted by the pair key.
        ``score`` is an audit blend (0.5 distance closeness + 0.5 name score)
        against the radius that admitted the pair, not a gate. Empty inputs
        yield an empty frame.
    """
    columns = ["stop_uid_a", "stop_uid_b", "distance_m", "name_score",
               "shared_routes", "score", "match_kind"]
    if exclude_feeds:
        stops = stops[~stops["feed_id"].isin(list(exclude_feeds))]
    if len(stops) < 2:
        return pd.DataFrame(columns=columns)

    # One spatial pass at the wider of the two radii; each branch re-gates
    # below on the radius that applies to it.
    search_m = max(distance_m, within_feed_distance_m or 0.0)
    candidates = connectivity.proximity_pairs_local(stops, search_m)
    if candidates.empty:
        return pd.DataFrame(columns=columns)

    meta = pd.DataFrame(stops[["stop_uid", "feed_id", "stop_name"]])
    candidates = (
        candidates
        .merge(meta.rename(columns={
            "stop_uid": "stop_uid_a", "feed_id": "feed_a", "stop_name": "name_a"}), on="stop_uid_a")
        .merge(meta.rename(columns={
            "stop_uid": "stop_uid_b", "feed_id": "feed_b", "stop_name": "name_b"}), on="stop_uid_b")
    )
    same_feed = candidates["feed_a"] == candidates["feed_b"]
    distance = candidates["distance_m"].astype(float)
    in_scope = (~same_feed & (distance <= distance_m))
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

    # Cross-feed: name OR shared route. Within-feed: shared route ONLY - a
    # shared name between two stops of one feed is not evidence of a duplicate.
    has_route = candidates["shared_routes"] >= 1
    accept = np.where(
        same_feed, has_route, (candidates["name_score"] >= name_threshold) | has_route
    )
    accepted = candidates[accept].copy()
    if accepted.empty:
        return pd.DataFrame(columns=columns)

    accepted["name_score"] = accepted["name_score"].round(3)
    accepted["distance_m"] = accepted["distance_m"].astype(float).round(2)
    # Closeness is relative to the radius that admitted the pair, or a
    # within-feed pair past 30 m would score negative against the narrower one.
    radius = np.where(
        accepted["match_kind"] == WITHIN_FEED,
        float(within_feed_distance_m or distance_m),
        float(distance_m),
    )
    accepted["score"] = (
        0.5 * (1.0 - accepted["distance_m"] / radius) + 0.5 * accepted["name_score"]
    ).round(3)
    result = (
        accepted[columns].sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")
        .reset_index(drop=True)
    )
    n_within = int((result["match_kind"] == WITHIN_FEED).sum())
    log.info(
        "Dedupe: %d alias pair(s) accepted - %d cross-feed within %.0f m, "
        "%d within-feed within %.0f m",
        len(result), len(result) - n_within, distance_m,
        n_within, within_feed_distance_m or 0.0,
    )
    return result


def canonical_map(equivalences: pd.DataFrame) -> pd.DataFrame:
    """Group membership derived from accepted pairs via connected components.

    Args:
        equivalences: accepted pairs from :func:`find_equivalences` (only
            ``stop_uid_a``/``stop_uid_b`` are used).

    Returns:
        :data:`CANONICAL_COLUMNS` - one row per stop belonging to a group.
        ``group_id`` enumerates components ordered by their smallest
        ``stop_uid`` (deterministic); ``canonical_uid`` is that smallest
        member. Empty input yields an empty frame.
    """
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
    """Tag each accepted pair with its group - the CSV/DB audit payload.

    Args:
        equivalences: accepted pairs from :func:`find_equivalences`.
        canonical: membership map from :func:`canonical_map`.

    Returns:
        :data:`EQUIVALENCE_COLUMNS`, sorted by ``(group_id, stop_uid_a,
        stop_uid_b)``. Empty inputs yield an empty frame.
    """
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
    """Drop proximity pairs whose two endpoints are the same physical station.

    Args:
        stop_pairs: proximity pairs (``stop_uid_a, stop_uid_b, distance_m``
            and possibly extra columns) from either ``proximity_pairs_*``.
        canonical: membership map from :func:`canonical_map`.

    Returns:
        *stop_pairs* minus rows where both endpoints map to one
        ``canonical_uid``; column set and order preserved.
    """
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


def write_equivalences(equivalences: pd.DataFrame, out_dir: Path) -> Path:
    """Write the audit table as ``stop_equivalences.csv`` in *out_dir*.

    Args:
        equivalences: frame from :func:`attach_groups`.
        out_dir: report directory (created if needed).

    Returns:
        The written path.

    Raises:
        OSError: if the file cannot be written (run-level problem).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / EQUIVALENCES_NAME
    equivalences.to_csv(path, index=False, encoding="utf-8")
    log.info("Stop equivalences written: %s (%d pairs)", path, len(equivalences))
    return path
