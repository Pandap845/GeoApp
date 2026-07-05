"""Optional route/mode filtering for Phase 2 (``--mode`` / ``--route``).

By default the analysis covers every route of every feed. These helpers
implement the opt-in restriction:

- ``--mode`` accepts mode *names* (``bus``, ``tram``, ``metro``, ``rail``,
  ``ferry``, ``cable_tram``, ``aerial_lift``, ``funicular``,
  ``trolleybus``, ``monorail``) or GTFS ``route_type`` codes - basic or
  extended (``700`` normalizes to bus, ``109`` to rail, ...).
- ``--route`` accepts ``route_id`` or ``route_short_name`` values.

A route is selected when it matches **either** filter (union), so
``--mode rail --route C98`` means "all rail routes plus the C98 bus".
The selection happens per feed against its raw ``routes.txt``; the chosen
``route_id`` set is then pushed into the partridge view, so trips,
stop_times, shapes - everything trip-derived - is filtered at the source.
Stops are NOT filtered: the stop inventory is Phase 1 data and stays
complete; stops served only by excluded routes simply appear unserved.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from ..conflation.clustering import MODE_NAMES, mode_name, normalize_route_type

log = logging.getLogger(__name__)

_NAME_TO_CODE: dict[str, int] = {name: code for code, name in MODE_NAMES.items()}


def _tokens(specs: Iterable[str]) -> list[str]:
    """Split repeatable/comma-separated flag values into clean tokens."""
    return [t.strip() for spec in specs for t in str(spec).split(",") if t.strip()]


def parse_modes(specs: Iterable[str]) -> frozenset[int]:
    """Parse ``--mode`` values into basic route_type codes.

    Args:
        specs: raw flag values (repeatable and/or comma-separated).

    Returns:
        The set of basic 0-12 codes.

    Raises:
        ValueError: on an unknown mode name or an unmapped numeric code,
            listing the accepted names.
    """
    codes: set[int] = set()
    for token in _tokens(specs):
        if token.lstrip("-").isdigit():
            basic = normalize_route_type(token)
            if basic is None:
                raise ValueError(f"--mode {token!r}: not a known GTFS route_type code")
            codes.add(basic)
        else:
            code = _NAME_TO_CODE.get(token.lower())
            if code is None:
                raise ValueError(
                    f"--mode {token!r}: unknown mode name "
                    f"(accepted: {', '.join(sorted(_NAME_TO_CODE))}, or a route_type code)"
                )
            codes.add(code)
    return frozenset(codes)


def parse_routes(specs: Iterable[str]) -> frozenset[str]:
    """Parse ``--route`` values (route_id / route_short_name tokens)."""
    return frozenset(_tokens(specs))


def mode_labels(codes: Optional[Iterable[int]]) -> list[str]:
    """Human labels for the report (sorted; empty when no filter)."""
    return sorted(mode_name(c) for c in codes) if codes else []


def allowed_route_ids(
    routes: Optional[pd.DataFrame],
    mode_codes: Optional[frozenset[int]],
    route_specs: Optional[frozenset[str]],
) -> Optional[set[str]]:
    """The feed's route_ids selected by the filters.

    Args:
        routes: the feed's raw ``routes.txt`` table (all-string dtypes).
        mode_codes: basic route_type codes from :func:`parse_modes`.
        route_specs: route_id/route_short_name tokens from :func:`parse_routes`.

    Returns:
        ``None`` when no filter is configured (analyze everything - the
        default); otherwise the matching route_id set, possibly empty
        (the feed has nothing the caller asked for).
    """
    if not mode_codes and not route_specs:
        return None
    if routes is None or routes.empty or "route_id" not in routes.columns:
        return set()

    route_id = routes["route_id"].astype("string").str.strip()
    selected = pd.Series(False, index=routes.index)

    if mode_codes and "route_type" in routes.columns:
        basic = routes["route_type"].map(normalize_route_type)
        selected |= basic.isin(mode_codes).fillna(False)

    if route_specs:
        selected |= route_id.isin(route_specs).fillna(False)
        if "route_short_name" in routes.columns:
            short = routes["route_short_name"].astype("string").str.strip()
            selected |= short.isin(route_specs).fillna(False)

    return set(route_id[selected & route_id.notna() & (route_id != "")])
