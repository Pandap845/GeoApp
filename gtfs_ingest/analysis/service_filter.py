"""Pick a service date and load a feed filtered to the trips running that day.

partridge combines calendar.txt with calendar_dates.txt exceptions. Filtering
trips cascades to stop_times, shapes and routes. Times are seconds after
midnight; values past 24:00 are kept as-is (25:10:00 -> 90600).

Without an explicit date the default rule is the **typical weekday**: the
most common Monday-Friday service pattern on days without calendar
exceptions. "Busiest" is kept as an option, but in feeds that stack Sunday
service on holidays it picks a holiday.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import partridge as ptg

from ..models import FeedSource

log = logging.getLogger(__name__)

DateRule = Literal["typical-weekday", "busiest"]
DATE_RULES: tuple[str, ...] = ("typical-weekday", "busiest")
#: Default rule when no date is given; the CLI uses it too.
DEFAULT_DATE_RULE: DateRule = "typical-weekday"

#: Tuesday-Thursday: avoids long-weekend bridges on Mondays and Fridays.
_MIDWEEK = (1, 2, 3)


@dataclass(frozen=True)
class ServiceSelection:
    """The service day analyzed for one feed and how it was chosen."""

    service_date: date
    service_ids: frozenset[str]
    # who picked the date
    source: Literal["explicit", "busiest", "common", "typical_weekday"]
    note: str = ""  # why this date, for the report


def common_service_date(paths: Sequence[str]) -> date | None:
    """Latest date on which every feed has service, or None if there is none."""
    common: set[date] | None = None
    for path in paths:
        served = {d for d, ids in ptg.read_service_ids_by_date(path).items() if ids}
        common = served if common is None else common & served
        if not common:
            return None
    return max(common) if common else None


def service_ids_in_range(path: str, start: date, end: date) -> dict[date, frozenset[str]]:
    """Dates in [start, end] that have service, mapped to their service_ids (sorted)."""
    by_date = ptg.read_service_ids_by_date(path)
    return {d: frozenset(ids) for d, ids in sorted(by_date.items()) if start <= d <= end and ids}


def _exception_dates(raw: Any) -> set[date]:
    """Dates listed in calendar_dates.txt (holidays and other exceptions)."""
    table = raw.calendar_dates
    if table is None or table.empty or "date" not in table.columns:
        return set()
    # GTFS dates are YYYYMMDD, which date.fromisoformat reads (Python 3.11+).
    return {date.fromisoformat(str(d).strip()) for d in table["date"].dropna()}


def _trips_per_service(raw: Any) -> Counter:
    trips = raw.trips
    if trips is None or trips.empty or "service_id" not in trips.columns:
        return Counter()
    return Counter(trips["service_id"].astype(str))


def _most_common_pattern(candidates: dict[date, frozenset[str]], trips: Counter) -> frozenset[str]:
    """The service_id set seen on most candidate days; ties: more trips, then sorted ids."""
    counts = Counter(candidates.values())

    def key(pattern: frozenset[str]) -> tuple:
        return (-counts[pattern], -sum(trips[s] for s in pattern), sorted(pattern))

    return min(counts, key=key)


def typical_weekday(path: str) -> ServiceSelection | None:
    """The middle Tue-Thu date with the most common weekday service; None if no weekday.

    Candidates are Monday-Friday dates with service that calendar_dates.txt
    doesn't touch, so holidays never count.
    """
    raw = ptg.load_raw_feed(path)
    exceptions = _exception_dates(raw)
    candidates = {
        d: frozenset(ids)
        for d, ids in ptg.read_service_ids_by_date(path).items()
        if ids and d.weekday() < 5 and d not in exceptions
    }
    if not candidates:
        return None
    pattern = _most_common_pattern(candidates, _trips_per_service(raw))
    matching = sorted(d for d, ids in candidates.items() if ids == pattern)
    midweek = [d for d in matching if d.weekday() in _MIDWEEK] or matching
    picked = midweek[(len(midweek) - 1) // 2]
    note = (
        f"typical weekday: services {'+'.join(sorted(pattern))} run on "
        f"{len(matching)} of {len(candidates)} regular weekdays; middle Tue-Thu date picked"
    )
    return ServiceSelection(picked, pattern, "typical_weekday", note)


def load_feed_for_service_ids(
    source: FeedSource,
    service_ids: frozenset[str],
    route_ids: set[str] | None = None,
) -> Any:
    """Load a feed filtered to given service_ids (used for each day of a date range)."""
    view: dict[str, dict[str, Any]] = {"trips.txt": {"service_id": service_ids}}
    if route_ids is not None:
        view["trips.txt"]["route_id"] = route_ids
    return ptg.load_feed(str(source.path), view=view)


def resolve_service(
    path: str,
    service_date: date | None,
    label: Literal["explicit", "common"] = "explicit",
    rule: DateRule = DEFAULT_DATE_RULE,
) -> ServiceSelection:
    """Service date and active service_ids; None picks one with *rule*.

    An explicit date with no service returns empty service_ids instead of failing.
    """
    if service_date is not None:
        ids = ptg.read_service_ids_by_date(path).get(service_date, frozenset())
        if not ids:
            log.warning("%s: no service runs on %s", path, service_date)
        return ServiceSelection(service_date, frozenset(ids), label)
    if rule == "typical-weekday":
        selection = typical_weekday(path)
        if selection is not None:
            log.info("%s: auto-selected typical weekday %s", path, selection.service_date)
            return selection
        log.warning("%s: no regular weekday with service; using the busiest date", path)
    busiest, ids = ptg.read_busiest_date(path)
    log.info("%s: auto-selected busiest service date %s", path, busiest)
    note = "busiest date" if rule == "busiest" else "no regular weekday; busiest date used"
    return ServiceSelection(busiest, frozenset(ids), "busiest", note)


def load_service_feed(
    source: FeedSource,
    service_date: date | None,
    route_ids: set[str] | None = None,
    label: Literal["explicit", "common"] = "explicit",
    rule: DateRule = DEFAULT_DATE_RULE,
) -> tuple[Any, ServiceSelection]:
    """Load the feed filtered to one service day (and optionally to *route_ids*).

    Returns (feed, selection). Parse errors propagate; Phase 2 isolates them per feed.
    """
    path = str(source.path)
    selection = resolve_service(path, service_date, label=label, rule=rule)
    view: dict[str, dict[str, Any]] = {"trips.txt": {"service_id": selection.service_ids}}
    if route_ids is not None:
        view["trips.txt"]["route_id"] = route_ids
    feed = ptg.load_feed(path, view=view)
    return feed, selection
