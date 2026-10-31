"""Optional --transport-mode / --route filtering for Phase 2.

A route is kept when it matches either filter (union): "--mode rail --route C98"
means every rail route plus C98. Only trip-derived data is filtered; stops are
never dropped, so stops served only by excluded routes just appear unserved.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from ..conflation.clustering import MODE_NAMES, mode_name, normalize_route_type

log = logging.getLogger(__name__)

_NAME_TO_CODE: dict[str, int] = {name: code for code, name in MODE_NAMES.items()}


def _tokens(specs: Iterable[str]) -> list[str]:
    """Split repeated and comma-separated flag values into clean tokens."""
    return [t.strip() for spec in specs for t in str(spec).split(",") if t.strip()]


def parse_modes(specs: Iterable[str]) -> frozenset[int]:
    """Turn mode names (bus, tram, ...) or route_type codes into basic 0-12 codes.

    Raises:
        ValueError: on an unknown name or code.
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
    """Parse --route values (route_id or route_short_name)."""
    return frozenset(_tokens(specs))


def mode_labels(codes: Optional[Iterable[int]]) -> list[str]:
    """Sorted mode names for the report; empty when there is no filter."""
    return sorted(mode_name(c) for c in codes) if codes else []


def allowed_route_ids(
    routes: Optional[pd.DataFrame],
    mode_codes: Optional[frozenset[int]],
    route_specs: Optional[frozenset[str]],
) -> Optional[set[str]]:
    """Route ids of this feed selected by the filters.

    Returns None when no filter is set (analyze everything), otherwise the
    matching ids, possibly an empty set.
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
