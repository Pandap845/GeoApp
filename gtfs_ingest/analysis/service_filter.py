"""Pick a service date and load a feed filtered to the trips running that day.

partridge combines calendar.txt with calendar_dates.txt exceptions. Filtering
trips cascades to stop_times, shapes and routes. Times are seconds after
midnight; values past 24:00 are kept as-is (25:10:00 -> 90600).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Optional, Sequence

import partridge as ptg

from ..models import FeedSource

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceSelection:
    """The service day analyzed for one feed and how it was chosen."""

    service_date: date
    service_ids: frozenset[str]
    source: Literal["explicit", "busiest", "common"]  # who picked the date


def common_service_date(paths: Sequence[str]) -> Optional[date]:
    """Latest date on which every feed has service, or None if there is none."""
    common: Optional[set[date]] = None
    for path in paths:
        served = {d for d, ids in ptg.read_service_ids_by_date(path).items() if ids}
        common = served if common is None else common & served
        if not common:
            return None
    return max(common) if common else None


def service_ids_in_range(path: str, start: date, end: date) -> dict[date, frozenset[str]]:
    """Dates in [start, end] that have service, mapped to their service_ids (sorted)."""
    by_date = ptg.read_service_ids_by_date(path)
    return {
        d: frozenset(ids)
        for d, ids in sorted(by_date.items())
        if start <= d <= end and ids
    }


def load_feed_for_service_ids(
    source: FeedSource,
    service_ids: frozenset[str],
    route_ids: Optional[set[str]] = None,
) -> Any:
    """Load a feed filtered to given service_ids (used for each day of a date range)."""
    view: dict[str, dict[str, Any]] = {"trips.txt": {"service_id": service_ids}}
    if route_ids is not None:
        view["trips.txt"]["route_id"] = route_ids
    return ptg.load_feed(str(source.path), view=view)


def resolve_service(
    path: str,
    service_date: Optional[date],
    label: Literal["explicit", "common"] = "explicit",
) -> ServiceSelection:
    """Service date and active service_ids; None picks the feed's busiest date.

    An explicit date with no service returns empty service_ids instead of failing.
    """
    if service_date is not None:
        ids = ptg.read_service_ids_by_date(path).get(service_date, frozenset())
        if not ids:
            log.warning("%s: no service runs on %s", path, service_date)
        return ServiceSelection(service_date, frozenset(ids), label)
    busiest, ids = ptg.read_busiest_date(path)
    log.info("%s: auto-selected busiest service date %s", path, busiest)
    return ServiceSelection(busiest, frozenset(ids), "busiest")


def load_service_feed(
    source: FeedSource,
    service_date: Optional[date],
    route_ids: Optional[set[str]] = None,
    label: Literal["explicit", "common"] = "explicit",
) -> tuple[Any, ServiceSelection]:
    """Load the feed filtered to one service day (and optionally to *route_ids*).

    Returns (feed, selection). Parse errors propagate; Phase 2 isolates them per feed.
    """
    path = str(source.path)
    selection = resolve_service(path, service_date, label=label)
    view: dict[str, dict[str, Any]] = {"trips.txt": {"service_id": selection.service_ids}}
    if route_ids is not None:
        view["trips.txt"]["route_id"] = route_ids
    feed = ptg.load_feed(path, view=view)
    return feed, selection
