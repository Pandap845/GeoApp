"""Service-date-aware feed loading.

partridge resolves which service_ids are active on a calendar date by
combining ``calendar.txt`` (weekday pattern + start/end range) with
``calendar_dates.txt`` exceptions (type 1 = added, type 2 = removed) - both
are honored, including feeds that ship only ``calendar_dates.txt``.

Loading with a partridge *view* on ``trips.txt`` cascades through the feed's
dependency graph: ``feed.trips``, ``feed.stop_times``, etc. only contain
rows reachable from trips active on the chosen date. Times are converted to
**seconds since service-day midnight** and GTFS times past 24:00 (e.g.
``25:10:00`` -> 90600) are preserved as-is, not wrapped.
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
    """Which service day was analyzed for one feed.

    Attributes:
        service_date: the resolved calendar date.
        service_ids: service_ids active on that date (may be empty when an
            explicit date has no service in this feed).
        source: ``"explicit"`` if the date was user-supplied, ``"busiest"``
            if it was auto-picked as the feed's busiest date.
    """

    service_date: date
    service_ids: frozenset[str]
    source: Literal["explicit", "busiest", "common"]


def common_service_date(paths: Sequence[str]) -> Optional[date]:
    """Latest calendar date on which EVERY feed has non-empty service.

    One ``read_service_ids_by_date`` call per feed - no feed loads.

    Args:
        paths: feed zip/directory paths.

    Returns:
        The latest shared served date, or ``None`` when the feeds' service
        windows share no such date (or *paths* is empty).
    """
    common: Optional[set[date]] = None
    for path in paths:
        served = {d for d, ids in ptg.read_service_ids_by_date(path).items() if ids}
        common = served if common is None else common & served
        if not common:
            return None
    return max(common) if common else None


def service_ids_in_range(path: str, start: date, end: date) -> dict[date, frozenset[str]]:
    """Served dates within ``[start, end]`` mapped to their service_ids.

    Args:
        path: feed zip/directory path.
        start / end: inclusive window bounds.

    Returns:
        Ordered ``{date: service_ids}`` for every date in the window with
        non-empty service; empty dict when the feed serves none of it.
    """
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
    """Load a feed filtered to explicit service_ids (range-run day loads).

    Skips the per-call calendar re-read that :func:`load_service_feed`
    performs - the caller already resolved the dates via
    :func:`service_ids_in_range`.
    """
    view: dict[str, dict[str, Any]] = {"trips.txt": {"service_id": service_ids}}
    if route_ids is not None:
        view["trips.txt"]["route_id"] = route_ids
    return ptg.load_feed(str(source.path), view=view)


def resolve_service(
    path: str,
    service_date: Optional[date],
    label: Literal["explicit", "common"] = "explicit",
) -> ServiceSelection:
    """Resolve the service date and its active service_ids for a feed.

    Args:
        path: path to the feed zip/directory.
        service_date: explicit date to analyze, or ``None`` to auto-select
            the feed's busiest date (the date with the most active trips,
            as computed by partridge).
        label: source label recorded when *service_date* is given -
            ``"common"`` when it came from :func:`common_service_date`.

    Returns:
        The resolved :class:`ServiceSelection`. With an explicit date that
        has no service, ``service_ids`` is empty (callers report "no
        service" rather than failing).

    Raises:
        Exception: partridge errors on feeds whose calendar files are
            unreadable propagate; the pipeline isolates them per feed.
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
    """Load a feed filtered to trips active on the (resolved) service date.

    Args:
        source: discovered feed (Phase 1 ``loader.discover_feeds``).
        service_date: explicit date, or ``None`` for the busiest date.
        route_ids: optional route restriction (``--mode`` / ``--route``);
            when given, the partridge view also filters ``trips.route_id``,
            so stop_times/shapes/routes cascade to just those routes.
            ``None`` (the default) analyzes every route.
        label: source label for explicit dates (``"common"`` when resolved
            by :func:`common_service_date`).

    Returns:
        ``(feed, selection)`` where *feed* is a typed partridge feed whose
        ``trips``/``stop_times`` contain only the active service day and
        whose times are float seconds (post-24:00 values intact).

    Raises:
        Exception: any partridge/parse failure propagates so the Phase 2
            pipeline can record it as a per-feed failure and continue.
    """
    path = str(source.path)
    selection = resolve_service(path, service_date, label=label)
    view: dict[str, dict[str, Any]] = {"trips.txt": {"service_id": selection.service_ids}}
    if route_ids is not None:
        view["trips.txt"]["route_id"] = route_ids
    feed = ptg.load_feed(path, view=view)
    return feed, selection
