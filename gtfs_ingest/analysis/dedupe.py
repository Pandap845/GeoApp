"""Cross-feed station deduplication (Phase 2, also reused by Phase 3).

Different feeds describe the same physical station under different stop ids
(e.g. ``improved:SITEUR_x`` vs ``mdb:TL_x``). This module detects those
aliases and derives a *canonical station* per group so downstream stages can
stop treating an alias pair as two distinct places:

* candidate pairs: stops of **different feeds** within ``distance_m``
  (default 30 m), found with :func:`connectivity.proximity_pairs_local`;
* accept rule: normalized-name similarity >= ``name_threshold`` **or** at
  least one shared normalized ``route_short_name`` between the routes
  serving each stop;
* groups: connected components over accepted pairs (the same networkx idiom
  as ``conflation.clustering.cluster_stops``), deterministically ordered by
  each group's smallest ``stop_uid``, which also becomes the group's
  ``canonical_uid``.

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
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd

from . import connectivity

log = logging.getLogger(__name__)

#: Maximum separation for two stops to be alias candidates.
DEDUPE_DISTANCE_M = 30.0

#: Minimum token-sort name similarity to accept on names alone.
NAME_SIM_THRESHOLD = 0.75

EQUIVALENCES_NAME = "stop_equivalences.csv"

#: Audit payload: one row per accepted alias pair, tagged with its group.
EQUIVALENCE_COLUMNS = [
    "group_id", "canonical_uid", "stop_uid_a", "stop_uid_b",
    "distance_m", "name_score", "shared_routes", "score",
]

#: Membership map: one row per stop that belongs to some alias group.
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
) -> pd.DataFrame:
    """Accepted cross-feed alias pairs among *stops*.

    Args:
        stops: Phase 1 normalized stops of all feeds (EPSG:4326 GeoDataFrame
            with ``stop_uid``, ``feed_id``, ``stop_name``, ``geometry``).
        route_stops: concatenated ``connectivity.route_stop_map`` frames.
        routes: route metadata with ``route_uid`` and (optionally)
            ``route_short_name`` - the shared-route evidence source.
        distance_m: candidate radius in meters.
        name_threshold: minimum :func:`name_similarity` to accept on names.

    Returns:
        Columns ``stop_uid_a, stop_uid_b, distance_m, name_score,
        shared_routes, score`` with ``stop_uid_a < stop_uid_b``, sorted by
        the pair key. ``score`` is an audit blend (0.5 distance closeness +
        0.5 name score), not a gate. Empty inputs yield an empty frame.
    """
    columns = ["stop_uid_a", "stop_uid_b", "distance_m", "name_score", "shared_routes", "score"]
    if len(stops) < 2:
        return pd.DataFrame(columns=columns)

    candidates = connectivity.proximity_pairs_local(stops, distance_m)
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
    candidates = candidates[candidates["feed_a"] != candidates["feed_b"]]
    if candidates.empty:
        return pd.DataFrame(columns=columns)

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

    accepted = candidates[
        (candidates["name_score"] >= name_threshold) | (candidates["shared_routes"] >= 1)
    ].copy()
    if accepted.empty:
        return pd.DataFrame(columns=columns)

    accepted["name_score"] = accepted["name_score"].round(3)
    accepted["distance_m"] = accepted["distance_m"].astype(float).round(2)
    accepted["score"] = (
        0.5 * (1.0 - accepted["distance_m"] / float(distance_m)) + 0.5 * accepted["name_score"]
    ).round(3)
    result = (
        accepted[columns].sort_values(["stop_uid_a", "stop_uid_b"], kind="mergesort")
        .reset_index(drop=True)
    )
    log.info("Dedupe: %d alias pair(s) accepted within %.0f m", len(result), distance_m)
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
