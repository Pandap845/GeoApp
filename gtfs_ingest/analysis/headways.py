"""Headway computation per (route, direction, stop) and time window.

Definition used throughout (documented in the README): a **headway** is the
gap between two consecutive scheduled *departures* at the same stop, for the
same (feed, route, direction), within one time window. Statistics are over
the gaps of a window, in **seconds**:

- a window containing N departures yields N-1 gaps;
- fewer than 2 departures -> headway undefined: the row is kept with its
  ``trip_count`` and NULL statistics (marked, never crashed on);
- windows are half-open ``[start, end)`` over seconds since service-day
  midnight and are NOT wrapped at 24:00 - a departure at 25:10 (90600 s)
  only matches a window that extends past 24:00 (e.g. ``23:00-26:00``),
  which keeps owl service attributable to the service date being analyzed.

Feeds are never mixed: all functions here operate on a single feed's data;
the pipeline adds the ``feed_id`` provenance column to every result.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import pandas as pd
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

#: Sentinel for trips whose optional direction_id is absent/blank.
UNKNOWN_DIRECTION = -1

#: Grouping key of one headway series.
GROUP_KEY = ["route_id", "direction_id", "stop_id"]

#: Statistic columns produced per group and window.
STAT_COLUMNS = ["mean_headway_s", "median_headway_s", "min_headway_s", "max_headway_s"]


class TimeWindow(BaseModel):
    """A named half-open time window ``[start_s, end_s)`` on the service day.

    Attributes:
        name: window identifier used in tables/reports (e.g. ``am_peak``).
        start_s: inclusive start, seconds since service-day midnight.
        end_s: exclusive end; may exceed 86400 (24:00) to capture GTFS
            post-midnight times (e.g. ``23:00-26:00``).
    """

    name: str = Field(min_length=1)
    start_s: int = Field(ge=0)
    end_s: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "TimeWindow":
        if self.end_s <= self.start_s:
            raise ValueError(
                f"window {self.name!r}: end must be after start "
                f"(express past-midnight windows as e.g. 23:00-26:00)"
            )
        return self

    @classmethod
    def parse(cls, name: str, spec: str) -> "TimeWindow":
        """Build a window from ``"HH:MM-HH:MM"`` (hours may exceed 23).

        Args:
            name: window name.
            spec: e.g. ``"06:00-09:00"`` or ``"23:00-26:00"``.

        Raises:
            ValueError: on malformed specs or end <= start.
        """
        try:
            start, end = spec.split("-")
            return cls(name=name, start_s=parse_hhmm(start), end_s=parse_hhmm(end))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"window {name!r}: cannot parse {spec!r} (want HH:MM-HH:MM)") from exc

    @property
    def label(self) -> str:
        """Human-readable ``HH:MM-HH:MM`` form."""
        return f"{_fmt(self.start_s)}-{_fmt(self.end_s)}"


def parse_hhmm(text: str) -> int:
    """``"HH:MM"`` -> seconds since midnight; hours may exceed 23 (GTFS).

    Raises:
        ValueError: if the text is not H+:MM with minutes in [0, 60).
    """
    hours_str, _, minutes_str = text.strip().partition(":")
    hours, minutes = int(hours_str), int(minutes_str or 0)
    if hours < 0 or not 0 <= minutes < 60:
        raise ValueError(f"invalid time {text!r}")
    return hours * 3600 + minutes * 60


def _fmt(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


#: Default windows (spec): AM peak, midday, PM peak, evening.
DEFAULT_WINDOWS: tuple[TimeWindow, ...] = (
    TimeWindow.parse("am_peak", "06:00-09:00"),
    TimeWindow.parse("midday", "09:00-15:00"),
    TimeWindow.parse("pm_peak", "15:00-19:00"),
    TimeWindow.parse("evening", "19:00-23:00"),
)


def stop_departures(feed: Any) -> pd.DataFrame:
    """Flatten one filtered feed into per-stop departure events.

    Args:
        feed: a *typed* partridge feed already filtered to one service date
            (``service_filter.load_service_feed``), so departure_time is
            float seconds since midnight.

    Returns:
        DataFrame with columns ``route_id, direction_id, stop_id,
        departure_s`` - one row per (trip, stop) departure. Rows without a
        departure_time (non-timepoints) are excluded. ``direction_id`` is
        coerced to int with :data:`UNKNOWN_DIRECTION` for blanks. Empty
        inputs yield an empty, correctly-typed frame.
    """
    columns = ["route_id", "direction_id", "stop_id", "departure_s"]
    stop_times = feed.stop_times
    trips = feed.trips
    if stop_times.empty or trips.empty:
        return pd.DataFrame(columns=columns)

    st = stop_times[["trip_id", "stop_id", "departure_time"]].dropna(subset=["departure_time"])
    tr = trips[["trip_id", "route_id"]].copy()
    if "direction_id" in trips.columns:
        tr["direction_id"] = (
            pd.to_numeric(trips["direction_id"], errors="coerce")
            .fillna(UNKNOWN_DIRECTION)
            .astype(int)
        )
    else:
        tr["direction_id"] = UNKNOWN_DIRECTION

    merged = st.merge(tr, on="trip_id", how="inner")
    merged = merged.rename(columns={"departure_time": "departure_s"})
    return merged[columns]


def compute_stop_headways(
    departures: pd.DataFrame, windows: Sequence[TimeWindow]
) -> pd.DataFrame:
    """Headway statistics per (route, direction, stop) and time window.

    Args:
        departures: frame from :func:`stop_departures` (single feed).
        windows: time windows to evaluate.

    Returns:
        One row per (route_id, direction_id, stop_id, time_window) that has
        at least one departure, with ``trip_count`` and the
        :data:`STAT_COLUMNS` in seconds. Groups with a single departure keep
        ``trip_count=1`` and NaN statistics (headway undefined). Groups with
        zero departures in a window are omitted entirely.
    """
    out_columns = GROUP_KEY + ["time_window", "trip_count"] + STAT_COLUMNS
    frames: list[pd.DataFrame] = []
    for window in windows:
        in_window = departures[
            (departures["departure_s"] >= window.start_s)
            & (departures["departure_s"] < window.end_s)
        ]
        if in_window.empty:
            continue
        ordered = in_window.sort_values("departure_s")
        gaps = ordered.groupby(GROUP_KEY, sort=False)["departure_s"].diff()
        stats = (
            ordered.assign(gap_s=gaps)
            .groupby(GROUP_KEY, sort=False)
            .agg(
                trip_count=("departure_s", "size"),
                mean_headway_s=("gap_s", "mean"),
                median_headway_s=("gap_s", "median"),
                min_headway_s=("gap_s", "min"),
                max_headway_s=("gap_s", "max"),
            )
            .reset_index()
        )
        stats.insert(len(GROUP_KEY), "time_window", window.name)
        frames.append(stats)

    if not frames:
        return pd.DataFrame(columns=out_columns)
    return pd.concat(frames, ignore_index=True)[out_columns]


def route_rollup(stop_headways: pd.DataFrame) -> pd.DataFrame:
    """Route-level service-frequency overview per window.

    Definition (documented in the README): for each (route_id,
    direction_id, time_window), the rollup averages the *stop-level mean
    headways* across the route's stops that have a defined headway in that
    window. ``trip_count`` is the maximum stop-level departure count (the
    best proxy for "runs through the corridor" without double counting).

    Args:
        stop_headways: output of :func:`compute_stop_headways`.

    Returns:
        Columns ``route_id, direction_id, time_window, mean_headway_s,
        stop_count, stops_with_headway, trip_count``. ``mean_headway_s`` is
        NaN when no stop of the route has >= 2 departures in the window.
    """
    columns = [
        "route_id", "direction_id", "time_window",
        "mean_headway_s", "stop_count", "stops_with_headway", "trip_count",
    ]
    if stop_headways.empty:
        return pd.DataFrame(columns=columns)
    rollup = (
        stop_headways.groupby(["route_id", "direction_id", "time_window"], sort=False)
        .agg(
            mean_headway_s=("mean_headway_s", "mean"),
            stop_count=("stop_id", "nunique"),
            stops_with_headway=("mean_headway_s", "count"),
            trip_count=("trip_count", "max"),
        )
        .reset_index()
    )
    return rollup[columns]
