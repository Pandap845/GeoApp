"""Headways: gaps between consecutive departures at a stop, per route, direction and window.

N departures in a window give N-1 gaps (seconds). With fewer than 2 the row
keeps its trip_count and NULL stats. Windows are [start, end) and not wrapped
at 24:00, so 25:10 only falls in a window like 23:00-26:00. frequencies.txt
trips are expanded into one run per headway, replacing their template times.
Everything here works on a single feed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

#: Max runs from one frequencies.txt row, so a corrupt row can't blow up memory.
MAX_FREQUENCY_RUNS = 1000

#: direction_id used when it is missing.
UNKNOWN_DIRECTION = -1

GROUP_KEY = ["route_id", "direction_id", "stop_id"]

STAT_COLUMNS = ["mean_headway_s", "median_headway_s", "min_headway_s", "max_headway_s"]


class TimeWindow(BaseModel):
    """Named window [start_s, end_s) in seconds after midnight; end may pass 24:00."""

    name: str = Field(min_length=1)
    start_s: int = Field(ge=0)
    end_s: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end_s <= self.start_s:
            raise ValueError(
                f"window {self.name!r}: end must be after start "
                f"(express past-midnight windows as e.g. 23:00-26:00)"
            )
        return self

    @classmethod
    def parse(cls, name: str, spec: str) -> TimeWindow:
        """Window from "HH:MM-HH:MM" (e.g. "23:00-26:00"); raises ValueError if invalid."""
        try:
            start, end = spec.split("-")
            return cls(name=name, start_s=parse_hhmm(start), end_s=parse_hhmm(end))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"window {name!r}: cannot parse {spec!r} (want HH:MM-HH:MM)") from exc

    @property
    def label(self) -> str:
        """The window as "HH:MM-HH:MM"."""
        return f"{_fmt(self.start_s)}-{_fmt(self.end_s)}"


def parse_hhmm(text: str) -> int:
    """Seconds after midnight for "HH:MM" (hours may exceed 23); raises ValueError."""
    hours_str, _, minutes_str = text.strip().partition(":")
    hours, minutes = int(hours_str), int(minutes_str or 0)
    if hours < 0 or not 0 <= minutes < 60:
        raise ValueError(f"invalid time {text!r}")
    return hours * 3600 + minutes * 60


def _fmt(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


#: AM peak, midday, PM peak, evening.
DEFAULT_WINDOWS: tuple[TimeWindow, ...] = (
    TimeWindow.parse("am_peak", "06:00-09:00"),
    TimeWindow.parse("midday", "09:00-15:00"),
    TimeWindow.parse("pm_peak", "15:00-19:00"),
    TimeWindow.parse("evening", "19:00-23:00"),
)


def frequency_runs(frequencies: pd.DataFrame, max_runs: int = MAX_FREQUENCY_RUNS) -> pd.DataFrame:
    """(trip_id, run_start) per run of frequencies.txt: one every headway_secs until end_time."""
    columns = ["trip_id", "run_start"]
    if frequencies.empty:
        return pd.DataFrame(columns=columns)
    freq = frequencies[["trip_id", "start_time", "end_time", "headway_secs"]].copy()
    for col in ("start_time", "end_time", "headway_secs"):
        freq[col] = pd.to_numeric(freq[col], errors="coerce")
    valid = (
        freq["start_time"].notna()
        & freq["end_time"].notna()
        & freq["headway_secs"].notna()
        & (freq["headway_secs"] > 0)
        & (freq["end_time"] > freq["start_time"])
    )
    if (~valid).any():
        log.warning("frequencies.txt: %d unusable row(s) skipped", int((~valid).sum()))
    freq = freq[valid]
    if freq.empty:
        return pd.DataFrame(columns=columns)
    n_runs = np.ceil((freq["end_time"] - freq["start_time"]) / freq["headway_secs"]).astype(int)
    if (n_runs > max_runs).any():
        log.warning(
            "frequencies.txt: %d row(s) capped at %d run(s) each",
            int((n_runs > max_runs).sum()),
            max_runs,
        )
        n_runs = n_runs.clip(upper=max_runs)

    # run_start = start_time + k * headway, one row per run.
    return pd.DataFrame(
        {
            "trip_id": np.repeat(freq["trip_id"].to_numpy(), n_runs),
            "run_start": np.repeat(freq["start_time"].to_numpy(), n_runs)
            + np.concatenate([np.arange(n) for n in n_runs])
            * np.repeat(freq["headway_secs"].to_numpy(), n_runs),
        }
    )


def expand_frequencies(
    stop_times: pd.DataFrame,
    frequencies: pd.DataFrame,
    max_runs: int = MAX_FREQUENCY_RUNS,
) -> pd.DataFrame:
    """Departure rows for frequency-based trips: one run every headway_secs until end_time.

    Each run repeats the trip's stop offsets from its first departure. Only
    those trips are returned; the caller must drop their template rows.
    """
    columns = ["trip_id", "stop_id", "departure_time"]
    if frequencies.empty or stop_times.empty:
        return pd.DataFrame(columns=columns)
    runs = frequency_runs(frequencies, max_runs)
    if runs.empty:
        return pd.DataFrame(columns=columns)

    # Offsets of each stop from the trip's first departure.
    template = stop_times[stop_times["trip_id"].isin(set(runs["trip_id"]))][
        ["trip_id", "stop_id", "departure_time"]
    ].dropna(subset=["departure_time"])
    if template.empty:
        return pd.DataFrame(columns=columns)
    template = template.copy()
    template["offset_s"] = template["departure_time"] - template.groupby("trip_id")[
        "departure_time"
    ].transform("min")

    expanded = runs.merge(template[["trip_id", "stop_id", "offset_s"]], on="trip_id")
    expanded["departure_time"] = expanded["run_start"] + expanded["offset_s"]
    # Drop duplicates from overlapping ranges; they would create zero gaps.
    result = (
        expanded[columns]
        .drop_duplicates(columns)
        .sort_values(columns, kind="mergesort")
        .reset_index(drop=True)
    )
    return result


def stop_departures(feed: Any) -> pd.DataFrame:
    """One row per (trip, stop) departure for a single-day feed, frequencies expanded."""
    columns = ["route_id", "direction_id", "stop_id", "departure_s"]
    stop_times = feed.stop_times
    trips = feed.trips
    if stop_times.empty or trips.empty:
        return pd.DataFrame(columns=columns)

    st = stop_times[["trip_id", "stop_id", "departure_time"]].dropna(subset=["departure_time"])

    frequencies = getattr(feed, "frequencies", None)
    if frequencies is not None and not frequencies.empty:
        synthetic = expand_frequencies(stop_times, frequencies)
        freq_trips = set(frequencies["trip_id"].astype(str))
        # Replace the template rows with the expanded runs.
        st = st[~st["trip_id"].astype(str).isin(freq_trips)]
        st = pd.concat([st, synthetic], ignore_index=True)
        log.info(
            "Frequencies: %d trip(s) expanded into %d departure row(s)",
            synthetic["trip_id"].nunique() if not synthetic.empty else 0,
            len(synthetic),
        )
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


def compute_stop_headways(departures: pd.DataFrame, windows: Sequence[TimeWindow]) -> pd.DataFrame:
    """Headway stats per (route, direction, stop, window) with at least one departure."""
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
    """Per route, direction and window: average of the stop mean headways, max trip count."""
    columns = [
        "route_id",
        "direction_id",
        "time_window",
        "mean_headway_s",
        "stop_count",
        "stops_with_headway",
        "trip_count",
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
