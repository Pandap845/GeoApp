"""Compare inferred interchange clusters with the transfers declared in transfers.txt.

Counts declared transfers that were inferred, declared ones that were not,
and inferred clusters that no feed declares. Feeds without transfers.txt are
simply listed.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import pandas as pd
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_SAMPLE = 10


class TransferCheck(BaseModel):
    """Agreement statistics between inferred clusters and transfers.txt."""

    feeds_with_transfers: list[str] = Field(default_factory=list)
    feeds_without_transfers: list[str] = Field(default_factory=list)
    declared_pairs: int = 0
    declared_co_clustered: int = 0        # declared and inferred (agreement)
    declared_not_co_clustered: int = 0    # declared but NOT inferred
    clusters_total: int = 0
    clusters_with_declared: int = 0       # inferred and backed by transfers.txt
    clusters_without_declared: int = 0    # inferred only, not declared
    not_inferred_examples: list[str] = Field(default_factory=list)


def declared_pairs(feed_id: str, transfers: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Declared transfer pairs as stop_uid_a < stop_uid_b, deduplicated, no self-transfers."""
    columns = ["stop_uid_a", "stop_uid_b"]
    if (
        transfers is None
        or transfers.empty
        or not {"from_stop_id", "to_stop_id"} <= set(transfers.columns)
    ):
        return pd.DataFrame(columns=columns)
    pairs = pd.DataFrame(
        {
            "a": transfers["from_stop_id"].astype("string").str.strip(),
            "b": transfers["to_stop_id"].astype("string").str.strip(),
        }
    ).dropna()
    pairs = pairs[(pairs["a"] != "") & (pairs["b"] != "") & (pairs["a"] != pairs["b"])]
    if pairs.empty:
        return pd.DataFrame(columns=columns)
    uid_a = feed_id + ":" + pairs.min(axis=1)
    uid_b = feed_id + ":" + pairs.max(axis=1)
    return (
        pd.DataFrame({"stop_uid_a": uid_a, "stop_uid_b": uid_b})
        .drop_duplicates()
        .reset_index(drop=True)
    )


def check_transfers(
    transfers_by_feed: dict[str, Optional[pd.DataFrame]],
    cluster_members: Sequence[Sequence[str]],
) -> TransferCheck:
    """Compare declared transfers (feed_id -> table or None) with the cluster member lists."""
    result = TransferCheck(clusters_total=len(cluster_members))
    all_pairs: list[pd.DataFrame] = []
    for feed_id, table in sorted(transfers_by_feed.items()):
        pairs = declared_pairs(feed_id, table)
        if table is None:
            result.feeds_without_transfers.append(feed_id)
        else:
            result.feeds_with_transfers.append(feed_id)
            all_pairs.append(pairs)

    declared = (
        pd.concat(all_pairs, ignore_index=True).drop_duplicates()
        if all_pairs
        else pd.DataFrame(columns=["stop_uid_a", "stop_uid_b"])
    )
    result.declared_pairs = int(len(declared))

    cluster_of: dict[str, int] = {}
    for idx, members in enumerate(cluster_members):
        for uid in members:
            cluster_of[uid] = idx

    clusters_backed: set[int] = set()
    for row in declared.to_dict("records"):
        ca, cb = cluster_of.get(row["stop_uid_a"]), cluster_of.get(row["stop_uid_b"])
        if ca is not None and ca == cb:
            result.declared_co_clustered += 1
            clusters_backed.add(ca)
        else:
            result.declared_not_co_clustered += 1
            if len(result.not_inferred_examples) < _SAMPLE:
                result.not_inferred_examples.append(
                    f"{row['stop_uid_a']} <-> {row['stop_uid_b']}"
                )

    result.clusters_with_declared = len(clusters_backed)
    result.clusters_without_declared = result.clusters_total - len(clusters_backed)
    return result
