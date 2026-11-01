"""Phase 2 orchestration (analyze-frequency).

1. Per feed (a broken feed never stops the run): pick the service day,
   compute headways, build route/stop maps, normalize stops.
2. Across feeds: proximity pairs, station dedupe, route connectivity,
   optional street walking distances.
3. Outputs: near_stations (+ quality sidecar), station network, GeoPackage,
   GraphML, headway CSVs, database tables, JSON/Markdown report.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from .. import db, loader, normalizer
from ..models import FeedSource
from ..validators import calendar_range
from . import (
    connectivity,
    dedupe,
    directions,
    geopackage,
    graph_export,
    near_stations,
    route_dedupe,
    route_filter,
    route_geometry,
    station_network,
    storage,
    timetable,
)
from .dedupe import DEDUPE_DISTANCE_M, NAME_SIM_THRESHOLD, WITHIN_FEED_DISTANCE_M
from .headways import (
    DEFAULT_WINDOWS,
    TimeWindow,
    compute_stop_headways,
    route_rollup,
    stop_departures,
)
from .service_filter import (
    DEFAULT_DATE_RULE,
    DateRule,
    common_service_date,
    load_feed_for_service_ids,
    load_service_feed,
    service_ids_in_range,
)

log = logging.getLogger(__name__)

GRAPHML_NAME = "route_connectivity.graphml"
ROUTE_CONNECTIONS_NAME = "route_connections.csv"
ROUTE_CONNECTION_STOPS_NAME = "route_connection_stops.csv"
ROUTES_NAME = "routes.csv"
ROUTE_STOPS_NAME = "route_stops.csv"
DIRECTION_FIXES_NAME = "direction_fixes.csv"

#: routes.csv columns (missing metadata stays empty).
ROUTE_COLUMNS = [
    "route_uid",
    "feed_id",
    "route_id",
    "route_short_name",
    "route_long_name",
    "route_type",
    "modalidad",
    "duplicate_of",  # the kept twin when route dedupe left this route out of routing
]
JSON_NAME = "analysis_report.json"
MARKDOWN_NAME = "analysis_report.md"

#: An auto-picked date older than this many days gets a staleness note.
STALE_AUTO_DATE_DAYS = 30

#: Columns of an empty route_stops frame (no feed was analyzed).
ROUTE_STOP_COLUMNS = ["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"]


class AnalysisConfig(BaseModel):
    """Configuration of one Phase 2 run.

    service_date None picks each feed's date with date_rule (typical weekday by
    default); feed_service_dates pins single feeds. mode_codes/route_specs
    keep routes matching either filter. dedupe_within_feed_distance_m defaults
    to 5 m like the CLI; None turns that rule off. drop_unserved_stops leaves
    out stops no trip uses. fix_directions relabels routes whose direction_id
    holds trips of both senses. route_dedupe keeps one source per line in the
    routing inputs. min_transfer_s is the timetable's change time at a stop.
    dry_run skips the database.
    """

    feed_paths: list[Path]
    database_url: str | None = None
    report_dir: Path = Path("out")
    service_date: date | None = None
    service_date_range: tuple[date, date] | None = None
    common_date: bool = False
    date_rule: DateRule = DEFAULT_DATE_RULE
    feed_service_dates: dict[str, date] = Field(default_factory=dict)
    analysis_date: date = Field(default_factory=date.today)
    proximity_m: float = Field(default=150.0, gt=0)
    windows: list[TimeWindow] = Field(default_factory=lambda: list(DEFAULT_WINDOWS))
    mode_codes: frozenset[int] | None = None
    route_specs: frozenset[str] | None = None
    network_walk: bool = False
    walk_cache: bool = True
    walk_cache_dir: Path = Path(".gtfs_cache")
    osm_line_table: str = "planet_osm_line"
    osm_geom_column: str = "way"
    dedupe: bool = True
    dedupe_distance_m: float = Field(default=DEDUPE_DISTANCE_M, gt=0)
    dedupe_name_threshold: float = Field(default=NAME_SIM_THRESHOLD, ge=0.0, le=1.0)
    # Same default as the CLI; None = off (cross-feed aliases only).
    dedupe_within_feed_distance_m: float | None = Field(default=WITHIN_FEED_DISTANCE_M, gt=0)
    drop_unserved_stops: bool = normalizer.DROP_UNSERVED_STOPS
    prune_station_network: bool = station_network.PRUNE_STATION_NETWORK
    fix_directions: bool = directions.FIX_DIRECTIONS
    route_dedupe: bool = route_dedupe.ROUTE_DEDUPE
    min_transfer_s: int = Field(default=timetable.MIN_TRANSFER_S, ge=0)
    dry_run: bool = False


class FeedAnalysis(BaseModel):
    """Per-feed outcome recorded in the analysis report."""

    feed_id: str
    source_path: str
    status: str  # "analyzed" | "stops_only" | "no_service" | "filtered_out" | "failed"
    service_date: str | None = None  # ISO date, or "START..END" for ranges
    # "explicit" | "typical_weekday" | "busiest" | "common" | "range"
    service_date_source: str | None = None
    days_analyzed: int | None = None  # served days (range runs only)
    routes_analyzed: int = 0
    trips_analyzed: int = 0
    stop_headway_rows: int = 0
    frequency_trips_expanded: int = 0  # trips from frequencies.txt
    calendar_end: str | None = None  # ISO date, latest declared service
    calendar_status: str = "unknown"  # "current" | "expired" | "unknown"
    unserved_stops: int = 0  # location_type 0 stops that no trip uses
    directions_fixed: int = 0  # trips relabelled from their stop order
    duplicate_sequence_rows: int = 0  # stop_times rows sharing a trip's stop_sequence
    duplicate_trips: int = 0  # trips listed twice, left out of the routing inputs
    notes: list[str] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    """Phase 2 report (written to JSON as-is)."""

    generated_at: str  # wall-clock timestamp (run metadata)
    analysis_date: str = ""  # the run's input clock (expiry/staleness reference)
    dry_run: bool
    proximity_m: float
    mode_filter: list[str] = Field(default_factory=list)  # mode names; [] = all
    route_filter: list[str] = Field(default_factory=list)  # route specs; [] = all
    dedupe: bool = True
    dedupe_distance_m: float = DEDUPE_DISTANCE_M
    # None = within-feed dedupe was off (or the report predates this field).
    dedupe_within_feed_distance_m: float | None = None
    # stop_equivalences.match_kind tells the two rules apart.
    station_groups: int = 0
    stops_deduplicated: int = 0  # stops belonging to some alias group
    # Pairs dropped because they would merge far-apart stops of one feed.
    dedupe_rejected_pairs: int = 0
    drop_unserved_stops: bool = normalizer.DROP_UNSERVED_STOPS
    unserved_stops_dropped: int = 0  # left out of every output of this run
    windows: dict[str, str]  # name -> "HH:MM-HH:MM"
    # False when feeds were analyzed on different days (outputs then mix schedules).
    service_dates_coherent: bool = True
    service_dates: list[str] = Field(default_factory=list)
    # Why the dates differ, when they do (empty when coherent).
    service_dates_note: str = ""
    # Stops-only CSV sources (geometry, no schedule).
    stops_only_feeds: list[str] = Field(default_factory=list)
    stops_only_stops: int = 0
    feeds: list[FeedAnalysis]
    connections_by_type: dict[str, int]
    isolated_routes: list[str]
    graph: dict[str, Any]  # n_routes / n_edges / n_components / component_sizes
    # Station network stats; weak components count transit stops only.
    station_graph: dict[str, Any] = Field(default_factory=dict)
    # Stop counts per walk_source / travel_source label.
    quality_summary: dict[str, dict[str, int]] = Field(default_factory=dict)
    # Lines present in two feeds and trips listed twice, left out of routing inputs.
    route_dedupe: dict[str, Any] = Field(default_factory=dict)
    # connections.csv.gz / footpaths.csv counts (single-day runs only).
    timetable: dict[str, Any] = Field(default_factory=dict)


class _FeedData:
    """Per-feed frames passed between steps (not serialized)."""

    def __init__(self) -> None:
        self.stop_headways = pd.DataFrame()
        self.rollup = pd.DataFrame()
        self.route_stops = pd.DataFrame()
        self.routes = pd.DataFrame()
        self.stops: gpd.GeoDataFrame | None = None
        self.route_shapes = gpd.GeoDataFrame()
        self.stop_links = pd.DataFrame()
        self.trip_minutes = pd.DataFrame()
        self.direction_fixes = pd.DataFrame(columns=["feed_id", *directions.FIX_COLUMNS])
        self.duplicate_sequences = pd.DataFrame()
        self.duplicate_trips = pd.DataFrame(columns=route_dedupe.DUPLICATE_TRIP_COLUMNS)
        # Without duplicate trips: the station network, route graph and timetable use these.
        self.routing_route_stops = pd.DataFrame()
        self.routing_links = pd.DataFrame()
        self.connections = pd.DataFrame(columns=timetable.CONNECTION_COLUMNS)


def _route_metadata(feed: Any, feed_id: str, route_ids: pd.Series) -> pd.DataFrame:
    """Route attributes for the graph; modalidad is the agency name (NULL if unknown)."""
    routes = pd.DataFrame({"route_id": route_ids.astype(str).unique()})
    routes["feed_id"] = feed_id
    routes["route_uid"] = feed_id + ":" + routes["route_id"]
    try:
        meta = feed.routes
        keep = [
            c
            for c in ("route_id", "route_short_name", "route_long_name", "route_type", "agency_id")
            if c in meta.columns
        ]
        meta = meta[keep].drop_duplicates("route_id").copy()
        meta["route_id"] = meta["route_id"].astype(str)
        routes = routes.merge(meta, on="route_id", how="left")

        agency = feed.agency
        if agency is not None and not agency.empty and "agency_name" in agency.columns:
            if "agency_id" in routes.columns and "agency_id" in agency.columns:
                names = agency[["agency_id", "agency_name"]].drop_duplicates("agency_id")
                routes = routes.merge(
                    names.rename(columns={"agency_name": "modalidad"}), on="agency_id", how="left"
                )
            elif len(agency) == 1:
                routes["modalidad"] = agency["agency_name"].iloc[0]
    except Exception:  # noqa: BLE001 - metadata is cosmetic, never fatal
        log.warning("Feed %s: could not read routes.txt/agency.txt metadata", feed_id)
    if "modalidad" not in routes.columns:
        routes["modalidad"] = None
    extra = [
        c
        for c in routes.columns
        if (c.startswith("route_") or c == "modalidad") and c not in ("route_uid", "route_id")
    ]
    return routes[["route_uid", "feed_id", "route_id"] + extra]


def _analyze_service_day(
    feed: Any, source: FeedSource, config: AnalysisConfig
) -> tuple[_FeedData, int, int]:
    """Frames for one service day; returns (day_data, frequency_trips, trips)."""
    day = _FeedData()
    if config.fix_directions:
        trips, fixes = directions.fix_directions(feed.stop_times, feed.trips)
        if not fixes.empty:
            feed.set("trips.txt", trips)
            fixes.insert(0, "feed_id", source.feed_id)
            day.direction_fixes = fixes
    frequencies = getattr(feed, "frequencies", None)
    n_freq = (
        int(frequencies["trip_id"].nunique())
        if frequencies is not None and not frequencies.empty
        else 0
    )
    departures = stop_departures(feed)
    n_trips = int(feed.trips["trip_id"].nunique()) if not feed.trips.empty else 0

    day.stop_headways = compute_stop_headways(departures, config.windows)
    day.rollup = route_rollup(day.stop_headways)
    day.route_stops = connectivity.route_stop_map(feed, source.feed_id)
    day.routes = _route_metadata(feed, source.feed_id, day.route_stops["route_id"])
    day.route_shapes = route_geometry.route_shapes(feed, source.feed_id)
    day.stop_links = route_geometry.route_stop_links(feed, source.feed_id)
    day.trip_minutes = route_geometry.route_trip_minutes(feed, source.feed_id)
    day.duplicate_sequences = _duplicate_sequences(feed)
    _routing_frames(feed, source, config, day)
    return day, n_freq, n_trips


def _routing_frames(feed: Any, source: FeedSource, config: AnalysisConfig, day: _FeedData) -> None:
    """Route stops, links and timetable without the trips this feed lists twice."""
    day.routing_route_stops, day.routing_links = day.route_stops, day.stop_links
    routing = feed
    if config.route_dedupe:
        radius = config.dedupe_within_feed_distance_m or WITHIN_FEED_DISTANCE_M
        day.duplicate_trips = route_dedupe.duplicate_trips(feed, source.feed_id, radius)
    if not day.duplicate_trips.empty:
        routing = route_dedupe.without_trips(feed, set(day.duplicate_trips["dropped_trip_id"]))
        day.routing_route_stops = connectivity.route_stop_map(routing, source.feed_id)
        day.routing_links = route_geometry.route_stop_links(routing, source.feed_id)
    # A timetable is one day; ranges merge days, so they get none.
    if config.service_date_range is None:
        day.connections = timetable.feed_connections(routing, source.feed_id)


def _duplicate_sequences(feed: Any) -> pd.DataFrame:
    """stop_times rows left out because another stop has their stop_sequence (+ distance_m)."""
    stop_times = feed.stop_times
    if stop_times.empty:
        return pd.DataFrame()
    _, dropped = route_geometry.ordered_stop_times(
        stop_times[["trip_id", "stop_id", "stop_sequence"]]
    )
    if dropped.empty:
        return dropped
    coords = feed.stops.assign(stop_id=feed.stops["stop_id"].astype(str)).set_index("stop_id")
    a = coords.reindex(dropped["stop_id"]).set_index(dropped.index)
    b = coords.reindex(dropped["kept_stop_id"]).set_index(dropped.index)
    dropped["distance_m"] = near_stations.haversine_m(
        a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"]
    )
    routes = feed.trips[["trip_id", "route_id"]].astype(str)
    return dropped.merge(routes, on="trip_id", how="left")


def _record_trip_fixes(day: _FeedData, result: FeedAnalysis) -> None:
    """Count and explain this feed's direction and stop-sequence fixes."""
    result.directions_fixed = len(day.direction_fixes)
    if result.directions_fixed:
        routes = sorted(day.direction_fixes["route_id"].unique())
        result.notes.append(
            f"{result.directions_fixed} trip(s) of {len(routes)} route(s) had both senses "
            f"under one direction_id and were relabelled from their stop order "
            f"({', '.join(routes)}; see {DIRECTION_FIXES_NAME})"
        )
    result.duplicate_trips = len(day.duplicate_trips)
    if result.duplicate_trips:
        routes = sorted(day.duplicate_trips["route_uid"].str.split(":", n=1).str[1].unique())
        result.notes.append(
            f"{result.duplicate_trips} trip(s) repeat another trip of their route over alias "
            f"stops ({', '.join(routes)}); left out of the routing inputs "
            f"(see {route_dedupe.DUPLICATE_TRIPS_NAME})"
        )
    dropped = day.duplicate_sequences
    result.duplicate_sequence_rows = len(dropped)
    if result.duplicate_sequence_rows:
        routes = sorted(dropped["route_id"].dropna().unique())
        result.notes.append(
            f"{len(dropped)} stop_times row(s) share a stop_sequence with another stop of "
            f"the trip ({', '.join(routes)}); one stop kept per sequence, the pairs are at "
            f"most {dropped['distance_m'].max():.0f} m apart"
        )


def _copy_day(day: _FeedData) -> _FeedData:
    """Independent copy, so tagging one day never changes another."""
    copy = _FeedData()
    for name, frame in vars(day).items():
        setattr(copy, name, None if frame is None else frame.copy())
    return copy


def _tag_headways(day: _FeedData, feed_id: str, service_date: date) -> None:
    """Add feed_id and service_date to one day's headway frames."""
    for frame in (day.stop_headways, day.rollup):
        if not frame.empty:
            frame.insert(0, "feed_id", feed_id)
            frame.insert(1, "service_date", service_date)


def _union_days(days: list[_FeedData], data: _FeedData) -> None:
    """Merge per-day frames for a date range; links keep the median travel time."""
    data.stop_headways = _concat([d.stop_headways for d in days])
    data.rollup = _concat([d.rollup for d in days])
    data.route_stops = _union_route_stops([d.route_stops for d in days])
    data.routing_route_stops = _union_route_stops([d.routing_route_stops for d in days])
    trips = _concat([d.duplicate_trips for d in days])
    if not trips.empty:
        data.duplicate_trips = trips.drop_duplicates().reset_index(drop=True)
    routes = _concat([d.routes for d in days])
    data.routes = (
        routes.drop_duplicates("route_uid").reset_index(drop=True) if not routes.empty else routes
    )
    shape_frames = [d.route_shapes for d in days if not d.route_shapes.empty]
    if shape_frames:
        shapes = pd.concat(shape_frames, ignore_index=True)
        data.route_shapes = gpd.GeoDataFrame(
            shapes.drop_duplicates(["route_uid", "direction_id", "shape_id"]).reset_index(
                drop=True
            ),
            crs=shape_frames[0].crs,
        )
    fixes = _concat([d.direction_fixes for d in days])
    if not fixes.empty:
        data.direction_fixes = fixes.drop_duplicates().reset_index(drop=True)
    sequences = _concat([d.duplicate_sequences for d in days])
    if not sequences.empty:
        data.duplicate_sequences = sequences.drop_duplicates(
            ["trip_id", "stop_sequence", "stop_id"]
        ).reset_index(drop=True)
    minutes = _concat([d.trip_minutes for d in days])
    if not minutes.empty:
        data.trip_minutes = minutes.groupby(["route_uid", "direction_id"], as_index=False)[
            "trip_min"
        ].median()
    data.stop_links = _union_links([d.stop_links for d in days])
    data.routing_links = _union_links([d.routing_links for d in days])


def _union_route_stops(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Distinct (route, stop) pairs over several days."""
    route_stops = _concat(frames)
    if route_stops.empty:
        return route_stops
    return route_stops.drop_duplicates(["route_uid", "stop_uid"]).reset_index(drop=True)


def _union_links(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Links over several days; each keeps the median travel time."""
    links = _concat(frames)
    if links.empty:
        return links
    return links.groupby(
        ["route_uid", "feed_id", "route_id", "linea", "direction_id", "stop_uid_a", "stop_uid_b"],
        as_index=False,
        dropna=False,
    ).agg(travel_min=("travel_min", "median"), n_obs=("n_obs", "max"))[route_geometry.LINK_COLUMNS]


def _concat(frames: Iterable[pd.DataFrame], columns: Sequence[str] | None = None) -> pd.DataFrame:
    """Concatenate the non-empty frames; an empty frame with *columns* if there are none."""
    keep = [f for f in frames if not f.empty]
    return pd.concat(keep, ignore_index=True) if keep else pd.DataFrame(columns=columns)


def _analyze_stops_only(
    raw: Any,
    source: FeedSource,
    config: AnalysisConfig,
    engine: Engine | None,
    result: FeedAnalysis,
    data: _FeedData,
) -> None:
    """Stops-only sources have no calendar: keep their stops, skip all schedule work."""
    data.stops, _ = normalizer.normalize_stops(raw)
    result.status = "stops_only"
    # No service_date, so it stays out of the date-coherence check.
    result.notes.append(
        f"stops-only CSV source: {len(data.stops)} stop(s) contribute geometry "
        "(proximity, near-stations, GeoPackage, unlinked station-network nodes); "
        "no routes, trips, headways, connectivity edges, dedupe or walk links"
    )
    if config.mode_codes or config.route_specs:
        # Route filters never remove stops.
        result.notes.append("--transport-mode/--route not applicable to a stops-only source")
    if engine is not None:
        db.load_feed_stops(engine, source.feed_id, data.stops)


def _allowed_routes(
    raw: Any, source: FeedSource, config: AnalysisConfig, result: FeedAnalysis
) -> set[str] | None:
    """Routes kept by --transport-mode/--route; None means every route.

    Sets status "filtered_out" when the filter keeps nothing.
    """
    if not (config.mode_codes or config.route_specs):
        return None
    routes_raw = raw.table("routes.txt")
    allowed = route_filter.allowed_route_ids(routes_raw, config.mode_codes, config.route_specs)
    has_ids = routes_raw is not None and "route_id" in getattr(routes_raw, "columns", [])
    total = int(routes_raw["route_id"].nunique()) if has_ids else 0
    if not allowed:
        result.status = "filtered_out"
        result.notes.append(
            f"no route matches the --mode/--route filter ({total} route(s) in feed)"
        )
        log.info("Feed %s: excluded by route/mode filter", source.feed_id)
        return allowed
    result.notes.append(f"route filter: {len(allowed)} of {total} route(s) selected")
    return allowed


def _check_calendar(
    raw: Any, config: AnalysisConfig, result: FeedAnalysis
) -> tuple[date | None, date | None]:
    """Record the calendar end and flag expired feeds; returns (start, end)."""
    cal_start, cal_end = calendar_range(raw.table("calendar.txt"), raw.table("calendar_dates.txt"))
    today = config.analysis_date  # injectable clock: reproducible notes
    if cal_end is not None:
        result.calendar_end = cal_end.isoformat()
        result.calendar_status = "expired" if cal_end < today else "current"
        if cal_end < today:
            result.notes.append(
                f"feed calendar ended {cal_end.isoformat()} "
                f"({(today - cal_end).days} day(s) before today)"
            )
    return cal_start, cal_end


def _analyze_range(
    source: FeedSource,
    config: AnalysisConfig,
    allowed: set[str] | None,
    result: FeedAnalysis,
) -> _FeedData | None:
    """Analyze every served day of --service-date-range and merge them; None if none."""
    start, end = config.service_date_range
    result.service_date = f"{start.isoformat()}..{end.isoformat()}"
    result.service_date_source = "range"
    served = service_ids_in_range(str(source.path), start, end)
    total_days = (end - start).days + 1
    if not served:
        result.status = "no_service"
        result.notes.append(f"no service between {start} and {end}")
        return None
    result.days_analyzed = len(served)
    if len(served) < total_days:
        result.notes.append(f"{len(served)} of {total_days} day(s) in the range have service")

    days: list[_FeedData] = []
    n_freq_max, trips_total = 0, 0
    # Days with the same service_ids give the same frames: load each set once.
    by_ids: dict[frozenset[str], tuple[_FeedData, int, int]] = {}
    for day_date, ids in served.items():
        key = frozenset(ids)
        if key not in by_ids:
            feed = load_feed_for_service_ids(source, ids, route_ids=allowed)
            by_ids[key] = _analyze_service_day(feed, source, config)
        template, n_freq, n_trips = by_ids[key]
        day = _copy_day(template)
        _tag_headways(day, source.feed_id, day_date)
        days.append(day)
        n_freq_max = max(n_freq_max, n_freq)
        trips_total += n_trips

    data = _FeedData()
    _union_days(days, data)
    _record_trip_fixes(data, result)
    result.frequency_trips_expanded = n_freq_max
    if n_freq_max:
        result.notes.append(
            f"frequencies.txt: up to {n_freq_max} trip(s)/day expanded into timetable runs"
        )
    result.trips_analyzed = trips_total  # trip-days across the range
    return data


def _analyze_single_day(
    source: FeedSource,
    config: AnalysisConfig,
    allowed: set[str] | None,
    calendar: tuple[date | None, date | None],
    result: FeedAnalysis,
) -> _FeedData | None:
    """Analyze one day (pinned, explicit, rule-picked or common); None if nothing runs."""
    pinned = config.feed_service_dates.get(source.feed_id)
    feed, selection = load_service_feed(
        source,
        pinned or config.service_date,
        route_ids=allowed,
        label="common" if config.common_date and pinned is None else "explicit",
        rule=config.date_rule,
    )
    day_date = selection.service_date
    result.service_date = day_date.isoformat()
    result.service_date_source = selection.source
    cal_start, cal_end = calendar
    if cal_start is not None and cal_end is not None and not cal_start <= day_date <= cal_end:
        result.notes.append(
            f"analyzed service date {day_date} is outside the feed's calendar validity"
        )
    if selection.note:
        result.notes.append(selection.note)
    days_ago = (config.analysis_date - day_date).days
    auto_picked = selection.source in ("typical_weekday", "busiest")
    if auto_picked and days_ago > STALE_AUTO_DATE_DAYS:
        result.notes.append(f"auto-picked service date {day_date} is {days_ago} day(s) in the past")
    if not selection.service_ids:
        result.status = "no_service"
        result.notes.append(f"no service runs on {day_date}")
        return None

    day, n_freq, n_trips = _analyze_service_day(feed, source, config)
    _tag_headways(day, source.feed_id, day_date)
    _record_trip_fixes(day, result)
    result.frequency_trips_expanded = n_freq
    if n_freq:
        result.notes.append(f"frequencies.txt: {n_freq} trip(s) expanded into timetable runs")
    result.trips_analyzed = n_trips
    return day


def _store_feed(engine: Engine, feed_id: str, data: _FeedData) -> None:
    """Replace this feed's stops and headway rows in PostGIS."""
    db.load_feed_stops(engine, feed_id, data.stops)
    storage.replace_feed_rows(engine, "route_stop_headways", feed_id, data.stop_headways)
    storage.replace_feed_rows(engine, "route_headways", feed_id, data.rollup)


def _handle_unserved(
    raw: Any, config: AnalysisConfig, result: FeedAnalysis, data: _FeedData
) -> None:
    """Count stops no trip uses; with drop_unserved_stops, leave them out of the run."""
    unserved = normalizer.unserved_stops(data.stops, raw.table("stop_times.txt"))
    result.unserved_stops = int(unserved.sum())
    if not result.unserved_stops:
        return
    if config.drop_unserved_stops:
        data.stops = data.stops[~unserved].reset_index(drop=True)
        result.notes.append(
            f"{result.unserved_stops} stop(s) used by no trip left out "
            "(--no-drop-unserved-stops keeps them)"
        )
    else:
        result.notes.append(f"{result.unserved_stops} stop(s) used by no trip, kept")


def _analyze_feed(
    source: FeedSource, config: AnalysisConfig, engine: Engine | None
) -> tuple[FeedAnalysis, _FeedData]:
    """Analyze one feed. Never raises; failures go into the result's notes."""
    data = _FeedData()
    result = FeedAnalysis(feed_id=source.feed_id, source_path=str(source.path), status="analyzed")
    try:
        raw = loader.load_feed(source)
        if source.is_stops_only:
            _analyze_stops_only(raw, source, config, engine, result, data)
            return result, data

        allowed = _allowed_routes(raw, source, config, result)
        if result.status == "filtered_out":
            return result, data
        calendar = _check_calendar(raw, config, result)
        if config.service_date_range is not None:
            analyzed = _analyze_range(source, config, allowed, result)
        else:
            analyzed = _analyze_single_day(source, config, allowed, calendar, result)
        if analyzed is None:
            return result, data
        data = analyzed

        result.routes_analyzed = len(data.routes)
        result.stop_headway_rows = len(data.stop_headways)
        # Stops come from the raw feed, so filters never drop them.
        data.stops, _ = normalizer.normalize_stops(raw)
        if engine is not None:
            _store_feed(engine, source.feed_id, data)
        # After the DB load: PostGIS keeps the whole inventory, the run drops
        # stops that can't be boarded.
        _handle_unserved(raw, config, result, data)
    except Exception as exc:  # per-feed isolation
        log.exception("Feed %s: analysis failed", source.feed_id)
        result.status = "failed"
        result.notes.append(f"{type(exc).__name__}: {exc}")
    return result, data


def _resolve_common_date(config: AnalysisConfig, sources: list[FeedSource]) -> AnalysisConfig:
    """--common-date: one shared day for every schedule feed, or fail.

    Stops-only sources have no calendar, so they are left out here.
    """
    if not config.common_date:
        return config
    schedule_sources = [s for s in sources if not s.is_stops_only]
    if sources and not schedule_sources:
        log.warning("--common-date ignored: every discovered source is stops-only")
        return config
    if not schedule_sources:
        return config
    common = common_service_date([str(s.path) for s in schedule_sources])
    if common is None:
        raise ValueError(
            "--common-date: the discovered feeds share no calendar date "
            "with active service in every feed (their validity windows do "
            "not overlap). Analyze with per-feed busiest dates (default) "
            "or pass an explicit --service-date."
        )
    log.info("Common service date resolved: %s", common)
    return config.model_copy(update={"service_date": common})


def _open_engine(config: AnalysisConfig) -> Engine | None:
    """Database engine with the schema in place; None for a dry run."""
    if config.dry_run:
        return None
    if not config.database_url:
        raise ValueError(
            "No database URL configured; set DATABASE_URL or pass --db-url "
            "(or use --dry-run to analyze without a database)"
        )
    engine = db.make_engine(config.database_url)
    db.ensure_schema(engine)
    storage.ensure_analysis_schema(engine)
    return engine


def _dates_note(config: AnalysisConfig, sources: list[FeedSource], dates: list[str]) -> str:
    """Why the feeds were analyzed on different days ("" when they weren't)."""
    if len(dates) <= 1:
        return ""
    schedule_paths = [str(s.path) for s in sources if not s.is_stops_only]
    if common_service_date(schedule_paths) is None:
        return (
            "the feeds' calendars share no date with service, so each feed uses its "
            f"own date ({config.date_rule} unless pinned); cross-feed outputs combine "
            "schedules from different periods"
        )
    return (
        "the feeds were analyzed on different days although their calendars overlap; "
        "use --common-date or one --service-date for a shared day"
    )


def _analyzed_dates(feed_results: list[FeedAnalysis]) -> list[str]:
    """Distinct service dates of the analyzed feeds; warns when there are several."""
    dates = sorted(
        {r.service_date for r in feed_results if r.status == "analyzed" and r.service_date}
    )
    if len(dates) > 1:
        log.warning(
            "Feeds were analyzed on DIFFERENT service dates (%s): cross-feed "
            "connectivity and near-stations mix non-contemporaneous "
            "schedules. Use --service-date, or --common-date if the "
            "calendars overlap.",
            ", ".join(dates),
        )
    return dates


def _proximity_pairs(
    engine: Engine | None,
    all_stops: gpd.GeoDataFrame,
    feed_ids: list[str],
    proximity_m: float,
) -> pd.DataFrame:
    """Stop pairs within *proximity_m*, from PostGIS when there is a database."""
    if engine is not None and feed_ids:
        pairs = connectivity.proximity_pairs_postgis(engine, feed_ids, proximity_m)
        # The table holds every stop; keep pairs of stops this run uses.
        known = set(all_stops["stop_uid"])
        used = pairs["stop_uid_a"].isin(known) & pairs["stop_uid_b"].isin(known)
        return pairs[used].reset_index(drop=True)
    return connectivity.proximity_pairs_local(all_stops, proximity_m)


def _apply_dedupe(
    config: AnalysisConfig,
    all_stops: gpd.GeoDataFrame,
    route_stops: pd.DataFrame,
    routes: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    stops_only_ids: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Station dedupe; returns (equivalences, canonical, rejected, filtered stop_pairs).

    Aliases of one station are not each other's neighbours; their routes
    connect as same_station_dedup instead.
    """
    equivalences = pd.DataFrame(columns=dedupe.EQUIVALENCE_COLUMNS)
    canonical = pd.DataFrame(columns=dedupe.CANONICAL_COLUMNS)
    rejected = pd.DataFrame(columns=dedupe.REJECTED_COLUMNS)
    if not config.dedupe:
        return equivalences, canonical, rejected, stop_pairs
    accepted = dedupe.find_equivalences(
        all_stops,
        route_stops,
        routes,
        config.dedupe_distance_m,
        config.dedupe_name_threshold,
        within_feed_distance_m=config.dedupe_within_feed_distance_m,
        exclude_feeds=stops_only_ids,
    )
    # Two stops of one feed share a station only within the within-feed radius.
    kept, rejected = dedupe.drop_conflicting_pairs(
        accepted, all_stops, config.dedupe_within_feed_distance_m or 0.0
    )
    canonical = dedupe.canonical_map(kept)
    equivalences = dedupe.attach_groups(kept, canonical)
    return equivalences, canonical, rejected, dedupe.filter_stop_pairs(stop_pairs, canonical)


def _network_walk_pairs(
    config: AnalysisConfig,
    engine: Engine,
    all_stops: gpd.GeoDataFrame,
    stop_pairs: pd.DataFrame,
    feed_ids: list[str],
) -> pd.DataFrame:
    """Street-network walking distances; keeps the straight-line pairs on failure."""
    from . import walk_network

    try:
        return walk_network.network_distances(
            engine,
            all_stops,
            stop_pairs,
            feed_ids,
            metric_crs=None,
            line_table=config.osm_line_table,
            geom_column=config.osm_geom_column,
            cache_dir=config.walk_cache_dir if config.walk_cache else None,
        )
    except Exception:  # refinement only, never fatal
        log.exception("Network walk distances failed; keeping straight-line values")
        return stop_pairs


def _write_station_network(
    all_stops: gpd.GeoDataFrame,
    near: pd.DataFrame,
    all_links: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    equivalences: pd.DataFrame,
    canonical: pd.DataFrame,
    stops_only_ids: list[str],
    out_dir: Path,
    prune: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Build and write the routable station network; returns (graph, stats).

    Redundant walk/same_station pairs are always counted; with *prune* they
    are also left out of the written network (no shortest time changes).
    """
    graph = station_network.build_station_graph(
        all_stops,
        near,
        all_links,
        stop_pairs,
        equivalences,
        canonical=canonical,
        exclude_feeds=stops_only_ids,
    )
    redundant_walk, redundant_same = station_network.redundant_pairs(graph)
    if prune:
        graph = station_network.prune_station_graph(graph, redundant_walk, redundant_same)
    station_network.write_station_graphml(graph, out_dir / station_network.STATION_GRAPHML_NAME)
    stats = station_network.station_graph_stats(graph)
    stats.update(
        redundant_walk_pairs=len(redundant_walk),
        redundant_same_station_pairs=len(redundant_same),
        pruned=prune,
    )
    _write_csv(station_network.station_nodes_frame(graph), out_dir / "station_network_nodes.csv")
    _write_csv(station_network.station_edges_frame(graph), out_dir / "station_network_edges.csv")
    return graph, stats


def _write_timetable(
    config: AnalysisConfig,
    connections: pd.DataFrame,
    route_matches: pd.DataFrame,
    routing_links: pd.DataFrame,
    stop_pairs: pd.DataFrame,
    equivalences: pd.DataFrame,
) -> dict[str, Any]:
    """Write connections.csv.gz and footpaths.csv; returns the report block."""
    if config.service_date_range is not None:
        return {"written": False, "note": "a timetable is one day; not written for date ranges"}
    dropped = set(route_matches["dropped_route_uid"])
    if dropped:
        connections = connections[~connections["route_uid"].isin(dropped)]
    connections = timetable.sort_connections(_with_time_quality(connections, routing_links))
    served = set(connections["from_stop"]) | set(connections["to_stop"])
    paths = timetable.footpaths(served, stop_pairs, equivalences, config.min_transfer_s)

    out_dir = config.report_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the gzip bytes reproducible.
    connections.to_csv(
        out_dir / timetable.CONNECTIONS_NAME,
        index=False,
        encoding="utf-8",
        compression={"method": "gzip", "mtime": 0},
    )
    log.info("Timetable written: %d connection(s)", len(connections))
    _write_csv(paths, out_dir / timetable.FOOTPATHS_NAME)
    return {
        "written": True,
        "connections": len(connections),
        "runs": int(connections["trip_id"].nunique()),
        "stops_served": len(served),
        "min_transfer_s": config.min_transfer_s,
        "footpaths_by_kind": {k: int(v) for k, v in paths["kind"].value_counts().items()},
        "connections_by_time_quality": {
            k: int(v) for k, v in connections["time_quality"].value_counts().sort_index().items()
        },
    }


def _with_time_quality(connections: pd.DataFrame, routing_links: pd.DataFrame) -> pd.DataFrame:
    """Each connection gets the time_quality of its route-direction stop pair."""
    if connections.empty or routing_links.empty:
        return connections.assign(time_quality=pd.Series(dtype=object))
    quality = routing_links[
        ["route_uid", "direction_id", "stop_uid_a", "stop_uid_b", "time_quality"]
    ].rename(columns={"stop_uid_a": "from_stop", "stop_uid_b": "to_stop"})
    quality = quality.astype({"direction_id": "int64"})
    keys = ["route_uid", "direction_id", "from_stop", "to_stop"]
    return connections.astype({"direction_id": "int64"}).merge(quality, on=keys, how="left")


def _write_headways(feed_data: list[_FeedData], out_dir: Path) -> None:
    """Headways as CSV too, so dry runs keep them."""
    _write_csv(_concat(d.stop_headways for d in feed_data), out_dir / "route_stop_headways.csv")
    _write_csv(_concat(d.rollup for d in feed_data), out_dir / "route_headways.csv")


def _build_report(
    config: AnalysisConfig,
    feed_results: list[FeedAnalysis],
    analyzed_dates: list[str],
    dates_note: str,
    stops_only_ids: list[str],
    all_stops: gpd.GeoDataFrame,
    canonical: pd.DataFrame,
    rejected: pd.DataFrame,
    edges: pd.DataFrame,
    metrics: dict[str, Any],
    station_stats: dict[str, Any],
    quality: pd.DataFrame,
    route_summary: dict[str, Any],
) -> AnalysisReport:
    """Assemble the analysis report from the run's results."""
    by_type = edges["connection_type"].value_counts().to_dict() if not edges.empty else {}
    stops_only_stops = (
        int(all_stops["feed_id"].isin(stops_only_ids).sum())
        if stops_only_ids and not all_stops.empty
        else 0
    )
    quality_summary = (
        {
            col: {k: int(v) for k, v in quality[col].value_counts().items()}
            for col in ("walk_source", "travel_source")
        }
        if not quality.empty
        else {}
    )
    return AnalysisReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        analysis_date=config.analysis_date.isoformat(),
        dry_run=config.dry_run,
        proximity_m=config.proximity_m,
        mode_filter=route_filter.mode_labels(config.mode_codes),
        route_filter=sorted(config.route_specs) if config.route_specs else [],
        dedupe=config.dedupe,
        dedupe_distance_m=config.dedupe_distance_m,
        dedupe_within_feed_distance_m=(
            config.dedupe_within_feed_distance_m if config.dedupe else None
        ),
        station_groups=int(canonical["group_id"].nunique()) if not canonical.empty else 0,
        stops_deduplicated=len(canonical),
        dedupe_rejected_pairs=len(rejected),
        drop_unserved_stops=config.drop_unserved_stops,
        unserved_stops_dropped=(
            sum(r.unserved_stops for r in feed_results) if config.drop_unserved_stops else 0
        ),
        windows={w.name: w.label for w in config.windows},
        service_dates_coherent=len(analyzed_dates) <= 1,
        service_dates=analyzed_dates,
        service_dates_note=dates_note,
        stops_only_feeds=stops_only_ids,
        stops_only_stops=stops_only_stops,
        feeds=feed_results,
        connections_by_type={k: int(v) for k, v in by_type.items()},
        isolated_routes=metrics["isolated_routes"],
        graph={k: metrics[k] for k in ("n_routes", "n_edges", "n_components", "component_sizes")},
        station_graph=station_stats,
        quality_summary=quality_summary,
        route_dedupe=route_summary,
    )


@dataclass
class _AllFeeds:
    """Per-feed frames concatenated across feeds."""

    route_stops: pd.DataFrame
    routes: pd.DataFrame
    stops: gpd.GeoDataFrame
    shapes: gpd.GeoDataFrame
    links: pd.DataFrame
    trip_minutes: pd.DataFrame
    direction_fixes: pd.DataFrame
    routing_route_stops: pd.DataFrame
    routing_links: pd.DataFrame
    duplicate_trips: pd.DataFrame
    connections: pd.DataFrame


def _combine_feeds(feed_data: list[_FeedData]) -> _AllFeeds:
    """Concatenate every feed's frames (empty frames keep their columns)."""
    stops_frames = [d.stops for d in feed_data if d.stops is not None and not d.stops.empty]
    shape_frames = [d.route_shapes for d in feed_data if not d.route_shapes.empty]
    return _AllFeeds(
        route_stops=_concat((d.route_stops for d in feed_data), ROUTE_STOP_COLUMNS),
        routes=_concat((d.routes for d in feed_data), ["route_uid", "feed_id", "route_id"]),
        stops=(
            pd.concat(stops_frames, ignore_index=True)
            if stops_frames
            else normalizer.empty_stops_frame()
        ),
        shapes=(
            gpd.GeoDataFrame(pd.concat(shape_frames, ignore_index=True), crs="EPSG:4326")
            if shape_frames
            else gpd.GeoDataFrame()
        ),
        links=_concat(d.stop_links for d in feed_data),
        trip_minutes=_concat(d.trip_minutes for d in feed_data),
        direction_fixes=_concat(
            (d.direction_fixes for d in feed_data), ["feed_id", *directions.FIX_COLUMNS]
        ),
        routing_route_stops=_concat((d.routing_route_stops for d in feed_data), ROUTE_STOP_COLUMNS),
        routing_links=_concat(d.routing_links for d in feed_data),
        duplicate_trips=_concat(
            (d.duplicate_trips for d in feed_data), route_dedupe.DUPLICATE_TRIP_COLUMNS
        ),
        connections=_concat((d.connections for d in feed_data), timetable.CONNECTION_COLUMNS),
    )


def _write_near_outputs(
    config: AnalysisConfig,
    engine: Engine | None,
    feed_ids: list[str],
    feeds: _AllFeeds,
    stop_pairs: pd.DataFrame,
    equivalences: pd.DataFrame,
    canonical: pd.DataFrame,
    rejected: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Write near_stations, its quality sidecar and the dedupe pairs; returns (near, quality)."""
    out_dir = config.report_dir
    near = near_stations.build_near_stations(
        feeds.stops, feeds.route_stops, feeds.routes, stop_pairs, stop_links=feeds.links
    )
    near_stations.write_near_stations(near, out_dir)

    # Quality sidecar: same rows and IDs as near_stations.csv.
    osm_matches = None
    if engine is not None and feed_ids:
        osm_matches = storage.fetch_osm_matches(engine, feed_ids)
    quality = near_stations.build_quality(
        near,
        feeds.route_stops,
        feeds.routes,
        stop_pairs,
        stop_links=feeds.links,
        osm_matches=osm_matches,
        canonical=canonical,
    )
    near_stations.write_quality(quality, out_dir)
    if config.dedupe:
        dedupe.write_equivalences(equivalences, out_dir)
        dedupe.write_rejected(rejected, out_dir)
    return near, quality


def _match_routes(
    config: AnalysisConfig,
    feeds: _AllFeeds,
    canonical: pd.DataFrame,
    feed_results: list[FeedAnalysis],
) -> pd.DataFrame:
    """Lines present in two feeds (empty with --no-route-dedupe)."""
    if not config.route_dedupe:
        return pd.DataFrame(columns=route_dedupe.EQUIVALENCE_COLUMNS)
    feed_rank = {
        r.feed_id: (r.calendar_end or "", r.service_date or "")
        for r in feed_results
        if r.status == "analyzed"
    }
    return route_dedupe.match_routes(feeds.route_stops, canonical, feeds.routes, feed_rank)


def _routing_inputs(
    feeds: _AllFeeds, route_matches: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(route_stops, links) for routing: no dropped twin routes; links get time_quality."""
    dropped = set(route_matches["dropped_route_uid"])
    route_stops = feeds.routing_route_stops
    links = feeds.routing_links
    if dropped:
        route_stops = route_stops[~route_stops["route_uid"].isin(dropped)].reset_index(drop=True)
        links = links[~links["route_uid"].isin(dropped)].reset_index(drop=True)
    if not links.empty:
        links = links.assign(
            time_quality=route_geometry.link_time_quality(
                links, feeds.stops, feeds.shapes, feeds.trip_minutes
            )
        )
    return route_stops, links


def _route_dedupe_summary(
    config: AnalysisConfig, route_matches: pd.DataFrame, duplicate_trips: pd.DataFrame
) -> dict[str, Any]:
    """Report block for the route dedupe."""
    if not config.route_dedupe:
        return {"enabled": False}
    dropped_feeds = route_matches["dropped_route_uid"].str.split(":", n=1).str[0]
    return {
        "enabled": True,
        "min_jaccard": route_dedupe.ROUTE_MATCH_MIN_JACCARD,
        "matched_lines": len(route_matches),
        "lowest_match_jaccard": (
            float(route_matches["stop_jaccard"].min()) if not route_matches.empty else None
        ),
        "dropped_routes_by_feed": {k: int(v) for k, v in dropped_feeds.value_counts().items()},
        "duplicate_trips_by_feed": {
            k: int(v) for k, v in duplicate_trips["feed_id"].value_counts().items()
        },
    }


def _write_route_outputs(
    feeds: _AllFeeds,
    edges: pd.DataFrame,
    connection_rows: pd.DataFrame,
    route_matches: pd.DataFrame,
    out_dir: Path,
) -> dict[str, Any]:
    """Write the route graph (GraphML) and its CSVs; returns the graph metrics.

    The graph leaves out the twins route dedupe dropped; routes.csv keeps them
    with duplicate_of.
    """
    twin_of = route_matches.set_index("dropped_route_uid")["kept_route_uid"]
    routes = feeds.routes.assign(duplicate_of=feeds.routes["route_uid"].map(twin_of))
    graph = graph_export.build_graph(
        routes[routes["duplicate_of"].isna()].drop(columns="duplicate_of"), edges
    )
    metrics = graph_export.graph_metrics(graph)
    graph_export.write_graphml(graph, out_dir / GRAPHML_NAME)
    _write_csv(edges, out_dir / ROUTE_CONNECTIONS_NAME)
    _write_csv(connection_rows, out_dir / ROUTE_CONNECTION_STOPS_NAME)
    routes = routes.reindex(columns=ROUTE_COLUMNS).sort_values("route_uid")
    _write_csv(routes, out_dir / ROUTES_NAME)
    served = feeds.route_stops[["route_uid", "stop_uid"]].drop_duplicates()
    _write_csv(
        served.sort_values(["route_uid", "stop_uid"], kind="mergesort"),
        out_dir / ROUTE_STOPS_NAME,
    )
    return metrics


def _store_cross_feed(engine: Engine, edges: pd.DataFrame, equivalences: pd.DataFrame) -> None:
    """Replace the cross-feed tables in PostGIS."""
    storage.replace_all_rows(engine, "route_connections", edges)
    # With --no-dedupe the table is emptied, so it always matches the last run.
    storage.replace_all_rows(engine, "stop_equivalences", equivalences[dedupe.EQUIVALENCE_COLUMNS])


def run_analysis(config: AnalysisConfig) -> AnalysisReport:
    """Run Phase 2 and write every output.

    Raises:
        ValueError: a non-dry run without a database URL, or --common-date
            with no shared service date.
    """
    sources = loader.discover_feeds(config.feed_paths)
    if not sources:
        log.warning("No feeds discovered under: %s", ", ".join(map(str, config.feed_paths)))
    config = _resolve_common_date(config, sources)
    engine = _open_engine(config)
    out_dir = config.report_dir

    analyzed = [_analyze_feed(source, config, engine) for source in sources]
    feed_results = [result for result, _ in analyzed]
    feed_data = [data for _, data in analyzed]
    analyzed_dates = _analyzed_dates(feed_results)
    dates_note = _dates_note(config, sources, analyzed_dates)
    unknown_pins = sorted(set(config.feed_service_dates) - {s.feed_id for s in sources})
    if unknown_pins:
        log.warning("--feed-service-date for unknown feed(s): %s", ", ".join(unknown_pins))
    feeds = _combine_feeds(feed_data)

    # Every feed with stops, schedule or not; the PostGIS query filters on this
    # list, so stops-only feeds must be in it (dry runs can't catch a mistake).
    geographic_feed_ids = [
        r.feed_id for r in feed_results if r.status in ("analyzed", "stops_only")
    ]
    # Stops-only sources are kept out of dedupe and station-network links.
    stops_only_ids = [r.feed_id for r in feed_results if r.status == "stops_only"]

    stations = connectivity.station_map(feeds.stops)
    stop_pairs = _proximity_pairs(engine, feeds.stops, geographic_feed_ids, config.proximity_m)
    equivalences, canonical, rejected, stop_pairs = _apply_dedupe(
        config, feeds.stops, feeds.route_stops, feeds.routes, stop_pairs, stops_only_ids
    )
    route_matches = _match_routes(config, feeds, canonical, feed_results)
    routing_route_stops, routing_links = _routing_inputs(feeds, route_matches)
    edges = connectivity.build_edges(routing_route_stops, stations, stop_pairs, canonical)
    # Same evidence as the edges, per stop pair: where the routes connect.
    connection_rows = connectivity.connection_stops(
        routing_route_stops, stations, stop_pairs, canonical, feeds.stops
    )

    # --network-walk: after connectivity (which stays straight-line), before
    # near_stations and the GeoPackage, which use the walking times.
    if config.network_walk and engine is not None and not stop_pairs.empty:
        stop_pairs = _network_walk_pairs(
            config, engine, feeds.stops, stop_pairs, geographic_feed_ids
        )

    near, quality = _write_near_outputs(
        config, engine, geographic_feed_ids, feeds, stop_pairs, equivalences, canonical, rejected
    )
    station_graph, station_stats = _write_station_network(
        feeds.stops,
        near,
        routing_links,
        stop_pairs,
        equivalences,
        canonical,
        stops_only_ids,
        out_dir,
        prune=config.prune_station_network,
    )
    timetable_stats = _write_timetable(
        config, feeds.connections, route_matches, routing_links, stop_pairs, equivalences
    )
    _write_headways(feed_data, out_dir)
    _write_csv(
        feeds.direction_fixes.sort_values(["feed_id", "route_id", "trip_id"], kind="mergesort"),
        out_dir / DIRECTION_FIXES_NAME,
    )
    geopackage.write_geopackage(
        feeds.stops,
        near,
        stop_pairs,
        out_dir,
        route_shapes=feeds.shapes,
        stop_links=feeds.links,
        station_edges=station_network.station_edges_layer(station_graph, feeds.stops),
    )
    metrics = _write_route_outputs(feeds, edges, connection_rows, route_matches, out_dir)
    _write_csv(route_matches, out_dir / route_dedupe.ROUTE_EQUIVALENCES_NAME)
    _write_csv(
        feeds.duplicate_trips.sort_values(["route_uid", "dropped_trip_id"], kind="mergesort"),
        out_dir / route_dedupe.DUPLICATE_TRIPS_NAME,
    )
    if engine is not None:
        _store_cross_feed(engine, edges, equivalences)

    report = _build_report(
        config,
        feed_results,
        analyzed_dates,
        dates_note,
        stops_only_ids,
        feeds.stops,
        canonical,
        rejected,
        edges,
        metrics,
        station_stats,
        quality,
        _route_dedupe_summary(config, route_matches, feeds.duplicate_trips),
    )
    report.timetable = timetable_stats
    _write_reports(report, out_dir)
    log.info(
        "Analysis complete: %d feed(s), %d route(s), %d connection edge(s), %d isolated route(s)",
        len(feed_results),
        metrics["n_routes"],
        metrics["n_edges"],
        len(metrics["isolated_routes"]),
    )
    return report


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    """Write a frame as CSV (header only when empty)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    log.info("CSV export written: %s (%d rows)", path, len(frame))
    return path


def _write_reports(report: AnalysisReport, out_dir: Path) -> None:
    """Write analysis_report.json and analysis_report.md into *out_dir*."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / JSON_NAME).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / MARKDOWN_NAME).write_text(_render_markdown(report), encoding="utf-8")
    log.info("Analysis reports written to %s", out_dir)


def _render_markdown(report: AnalysisReport) -> str:
    """Markdown version of the report."""
    lines = [
        *_md_header(report),
        *_md_feeds(report),
        *_md_warnings(report),
        *_md_connectivity(report),
        *_md_quality(report),
        *_md_isolated_routes(report),
        *_md_exports(),
    ]
    return "\n".join(lines)


def _md_dedupe_line(report: AnalysisReport) -> str:
    if not report.dedupe:
        return "Station deduplication: off (--no-dedupe)"
    within = (
        f"{report.dedupe_within_feed_distance_m:g} m"
        if report.dedupe_within_feed_distance_m is not None
        else "off"
    )
    return (
        f"Station deduplication: on (cross-feed {report.dedupe_distance_m:g} m, "
        f"within-feed {within}) - {report.station_groups} group(s), "
        f"{report.stops_deduplicated} stop(s) merged, {report.dedupe_rejected_pairs} "
        f"pair(s) rejected (same-feed conflict); see `{dedupe.EQUIVALENCES_NAME}` "
        f"and `{dedupe.REJECTED_NAME}`"
    )


def _md_route_dedupe_line(report: AnalysisReport) -> str:
    summary = report.route_dedupe
    if not summary.get("enabled"):
        return "Route deduplication: off (--no-route-dedupe)  "
    dropped = sum(summary.get("dropped_routes_by_feed", {}).values())
    trips = sum(summary.get("duplicate_trips_by_feed", {}).values())
    return (
        f"Route deduplication: on - {summary.get('matched_lines', 0)} line(s) in two feeds "
        f"({dropped} twin route(s) left out of routing, newer calendar kept), {trips} trip(s) "
        f"listed twice left out; see `{route_dedupe.ROUTE_EQUIVALENCES_NAME}` and "
        f"`{route_dedupe.DUPLICATE_TRIPS_NAME}`  "
    )


def _md_unserved_line(report: AnalysisReport) -> str:
    total = sum(f.unserved_stops for f in report.feeds)
    if report.drop_unserved_stops:
        return f"Unserved stops (no trip uses them): {total} left out of every output  "
    return f"Unserved stops (no trip uses them): {total} kept (--no-drop-unserved-stops)  "


def _md_header(report: AnalysisReport) -> list[str]:
    modes = ", ".join(report.mode_filter) if report.mode_filter else "all (no filter)"
    routes = ", ".join(report.route_filter) if report.route_filter else "all (no filter)"
    return [
        "# GTFS Headway & Connectivity Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Analysis date: {report.analysis_date}  ",
        f"Mode: {'dry run (no DB writes)' if report.dry_run else 'full run'}  ",
        f"Proximity threshold: {report.proximity_m:g} m  ",
        f"Transport modes: {modes}  ",
        f"Routes: {routes}  ",
        _md_dedupe_line(report),
        _md_route_dedupe_line(report),
        _md_unserved_line(report),
        "",
        "## Time windows",
        "",
        "| Window | Range |",
        "| --- | --- |",
        *[f"| {name} | {label} |" for name, label in report.windows.items()],
        "",
    ]


def _md_feeds(report: AnalysisReport) -> list[str]:
    lines = [
        "## Feeds",
        "",
        "| Feed | Status | Service date | Picked | Routes | Trips | Stop-headway rows | Notes |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for f in report.feeds:
        notes = "; ".join(f.notes).replace("|", "\\|") or "-"
        lines.append(
            f"| {f.feed_id} | {f.status} | {f.service_date or '-'} | {f.service_date_source or '-'} "
            f"| {f.routes_analyzed} | {f.trips_analyzed} | {f.stop_headway_rows} | {notes} |"
        )
    return lines


def _md_warnings(report: AnalysisReport) -> list[str]:
    lines: list[str] = []
    if report.stops_only_feeds:
        lines += [
            "",
            (
                f"> **Stops-only sources:** `{'`, `'.join(report.stops_only_feeds)}` - "
                f"{report.stops_only_stops} stop(s) contributing geometry only. They appear in "
                "`near_stations.csv`, `near_stations_quality.csv` and `transit_analysis.gpkg`, "
                "and can be neighbours of scheduled stops (rendered with line `-`). They are "
                "never deduplicated, sit in the station network as unlinked nodes (no walk "
                "arcs, so no route passes through them), and contribute no route connectivity "
                "edges, headways or same-line topology."
            ),
        ]
    if not report.service_dates_coherent:
        lines += [
            "",
            (
                "> **Warning:** feeds were analyzed on DIFFERENT service dates "
                f"({', '.join(report.service_dates)}): "
                f"{report.service_dates_note or 'cross-feed outputs mix schedules'}."
            ),
        ]
    return lines


def _md_connectivity(report: AnalysisReport) -> list[str]:
    graph, stations = report.graph, report.station_graph
    edge_types = ", ".join(f"{k}={v}" for k, v in stations.get("edges_by_type", {}).items())
    stops_only = (
        f" · {stations['n_stops_only']} unlinked stops-only node(s)"
        if stations.get("n_stops_only")
        else ""
    )
    redundancy = (
        f" · redundant pairs (no shortest time changes): "
        f"{stations['redundant_walk_pairs']} walk, "
        f"{stations['redundant_same_station_pairs']} same_station "
        f"({'pruned' if stations.get('pruned') else 'kept'})"
        if "redundant_walk_pairs" in stations
        else ""
    )
    quality = stations.get("in_vehicle_time_quality", {})
    time_quality = (
        " · in_vehicle time_quality: " + ", ".join(f"{k}={v}" for k, v in quality.items())
        if quality
        else ""
    )
    table = report.timetable
    timetable_line = (
        f"Timetable: {table['connections']} connection(s) in {table['runs']} run(s) over "
        f"{table['stops_served']} stop(s); footpaths "
        + ", ".join(f"{k}={v}" for k, v in table["footpaths_by_kind"].items())
        + f" (change time {table['min_transfer_s']} s); see `{timetable.CONNECTIONS_NAME}` "
        f"and `{timetable.FOOTPATHS_NAME}`"
        if table.get("written")
        else "Timetable: not written (date-range run)"
    )
    lines = [
        "",
        "## Route connectivity",
        "",
        (
            f"Routes: {graph['n_routes']} · Edges: {graph['n_edges']} · "
            f"Connected components: {graph['n_components']} "
            f"(sizes: {', '.join(map(str, graph['component_sizes'])) or '-'})"
        ),
        "",
        f"Station network: {stations.get('n_stops', 0)} stops · "
        f"{stations.get('n_edges', 0)} edges ({edge_types or 'none'}) · "
        f"{stations.get('n_weak_components', 0)} weak component(s) among transit stops"
        + stops_only
        + redundancy
        + time_quality,
        "",
        timetable_line,
        "",
        "| Connection type | Route pairs |",
        "| --- | ---: |",
    ]
    for kind in ("same_stop", "same_parent_station", "same_station_dedup", "proximity"):
        lines.append(f"| {kind} | {report.connections_by_type.get(kind, 0)} |")
    return lines


def _md_quality(report: AnalysisReport) -> list[str]:
    if not report.quality_summary:
        return []
    walk = report.quality_summary.get("walk_source", {})
    travel = report.quality_summary.get("travel_source", {})
    lines = [
        "",
        "## Estimate provenance (near_stations_quality.csv)",
        "",
        "| walk_source | stops | | travel_source | stops |",
        "| --- | ---: | --- | --- | ---: |",
    ]
    walk_kinds = ("network", "straight_line", "mixed", "unsnapped", "none")
    travel_kinds = ("observed", "estimated", "mixed", "none")
    for i in range(max(len(walk_kinds), len(travel_kinds))):
        w = walk_kinds[i] if i < len(walk_kinds) else ""
        t = travel_kinds[i] if i < len(travel_kinds) else ""
        lines.append(
            f"| {w} | {walk.get(w, 0) if w else ''} | | {t} | {travel.get(t, 0) if t else ''} |"
        )
    return lines


def _md_isolated_routes(report: AnalysisReport) -> list[str]:
    lines = ["", "### Isolated routes (no connections)", ""]
    if report.isolated_routes:
        return lines + [f"- `{r}`" for r in report.isolated_routes]
    return lines + ["None - every analyzed route connects to at least one other."]


def _md_exports() -> list[str]:
    return [
        "",
        f"GraphML export: `{GRAPHML_NAME}` (same directory).  ",
        (
            f"Near-stations export: `{near_stations.NEAR_STATIONS_NAME}` "
            "(per stop: numeric ID, name, lat, lon, combined stop+line ids, and "
            "nearby stations as `[(ID, station, linea, time to reach), ...]`).  "
        ),
        (
            f"Stop equivalences export: `{dedupe.EQUIVALENCES_NAME}` "
            "(cross-feed alias pairs merged by station deduplication; only "
            "written when dedupe is on).  "
        ),
        (
            f"Quality sidecar: `{near_stations.QUALITY_NAME}` "
            "(per stop, same ID key: walk/travel provenance, worst-case "
            "observation count, OSM coord confidence, dedupe canonical id).  "
        ),
        (
            f"Station network: `{station_network.STATION_GRAPHML_NAME}` + "
            "`station_network_nodes.csv`/`station_network_edges.csv` + the "
            "`station_network` GeoPackage layer - the ROUTABLE stop-level graph "
            "(directed in_vehicle edges weighted by scheduled minutes, walk "
            "edges by walking minutes, 0-min same_station alias links; "
            "`nx.shortest_path(..., weight='time_min')` works directly).  "
        ),
        (
            "Headway tables: `route_stop_headways.csv` / `route_headways.csv` "
            "(same rows as the DB tables, written on every run incl. dry runs).  "
        ),
        (
            f"GeoPackage export: `{geopackage.GEOPACKAGE_NAME}` "
            "(layers: `stops` points, `near_links` walking lines, `route_shapes` "
            "real route paths, `route_stop_links` consecutive-stop segments - open in QGIS)."
        ),
        "",
    ]
