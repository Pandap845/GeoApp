"""Phase 2 orchestration: headways + connectivity for a set of feeds.

Flow per run:

1. Discover feeds (Phase 1 loader — reused, not re-implemented).
2. Per feed, inside a failure boundary (one broken feed never aborts the run):

   a. service-date filtering (calendar + calendar_dates via partridge),
   b. headway statistics per (route, direction, stop) and window + rollup,
   c. route/stop maps for connectivity,
   d. Phase 1 raw load + normalize of stops (parent_station links, local
      proximity fallback, and — on non-dry runs — a refresh of the PostGIS
      ``stops`` table so proximity queries match the analyzed feeds).

3. Cross-feed station deduplication (``dedupe``, on by default): aliases of
   one physical station across feeds are merged, removed from the proximity
   pairs, and connected as ``same_station_dedup`` edges.
4. Cross-feed connectivity edges (PostGIS geography when a DB is available,
   local UTM math otherwise), networkx graph, GraphML export.
5. Near-stations export + its provenance sidecar
   (``near_stations_quality.csv``: walk/travel sources, observation counts,
   OSM coord confidence, dedupe canonical ids).
6. Idempotent DB loads (skipped with --dry-run) and JSON + Markdown report.

Feeds are never mixed in headway math; ``feed_id`` is carried through every
table. Only run-level problems (bad DB, unwritable report dir) abort.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from .. import db, loader, normalizer
from ..models import FeedSource
from ..validators import calendar_range
from . import connectivity, dedupe, geopackage, graph_export, near_stations, route_filter, route_geometry, station_network, storage
from .dedupe import DEDUPE_DISTANCE_M, NAME_SIM_THRESHOLD, WITHIN_FEED_DISTANCE_M
from .headways import DEFAULT_WINDOWS, TimeWindow, compute_stop_headways, route_rollup, stop_departures
from .service_filter import (
    common_service_date,
    load_feed_for_service_ids,
    load_service_feed,
    service_ids_in_range,
)

log = logging.getLogger(__name__)

GRAPHML_NAME = "route_connectivity.graphml"
JSON_NAME = "analysis_report.json"
MARKDOWN_NAME = "analysis_report.md"

#: A "busiest" auto-selected date older than this gets a staleness note
#: (a healthy current feed's busiest date sits near today).
STALE_BUSIEST_DAYS = 30


class AnalysisConfig(BaseModel):
    """Configuration of one Phase 2 run.

    Attributes:
        feed_paths: same inputs as Phase 1 ingest.
        database_url: PostGIS URL; may be ``None`` only for dry runs.
        report_dir: output directory (report + GraphML).
        service_date: explicit date to analyze; ``None`` auto-selects each
            feed's busiest date.
        proximity_m: connectivity proximity threshold in meters.
        windows: time windows for headway statistics.
        mode_codes: restrict the analysis to these basic route_type codes
            (``route_filter.parse_modes`` output); ``None`` = all modes.
        route_specs: restrict to these route_id/route_short_name values;
            ``None`` = all routes. A route passes when it matches either
            filter (union with ``mode_codes``).
        dedupe: merge duplicate stations (``analysis.dedupe``) before
            connectivity / near-stations; on by default.
        dedupe_distance_m: cross-feed alias candidate radius in meters.
        dedupe_name_threshold: minimum name similarity (0-1) to accept a
            cross-feed alias pair on names alone.
        dedupe_within_feed_distance_m: candidate radius for aliases inside a
            SINGLE feed (one station published under two id schemes),
            default 5 m - same as the CLI. ``None`` disables it; such pairs
            are accepted on shared routes alone, never on names. Stops-only
            sources take part in neither dedupe rule.
        dry_run: skip all database writes (and use local proximity math).
    """

    feed_paths: list[Path]
    database_url: Optional[str] = None
    report_dir: Path = Path("out")
    service_date: Optional[date] = None
    service_date_range: Optional[tuple[date, date]] = None
    common_date: bool = False
    analysis_date: date = Field(default_factory=date.today)
    proximity_m: float = Field(default=150.0, gt=0)
    windows: list[TimeWindow] = Field(default_factory=lambda: list(DEFAULT_WINDOWS))
    mode_codes: Optional[frozenset[int]] = None
    route_specs: Optional[frozenset[str]] = None
    network_walk: bool = False
    walk_cache: bool = True
    walk_cache_dir: Path = Path(".gtfs_cache")
    osm_line_table: str = "planet_osm_line"
    osm_geom_column: str = "way"
    dedupe: bool = True
    dedupe_distance_m: float = Field(default=DEDUPE_DISTANCE_M, gt=0)
    dedupe_name_threshold: float = Field(default=NAME_SIM_THRESHOLD, ge=0.0, le=1.0)
    # Same default as the CLI; None = off (cross-feed aliases only).
    dedupe_within_feed_distance_m: Optional[float] = Field(default=WITHIN_FEED_DISTANCE_M, gt=0)
    dry_run: bool = False


class FeedAnalysis(BaseModel):
    """Per-feed outcome recorded in the analysis report."""

    feed_id: str
    source_path: str
    status: str  # "analyzed" | "stops_only" | "no_service" | "filtered_out" | "failed"
    service_date: Optional[str] = None          # ISO date, or "START..END" for ranges
    service_date_source: Optional[str] = None   # "explicit" | "busiest" | "common" | "range"
    days_analyzed: Optional[int] = None         # served days (range runs only)
    routes_analyzed: int = 0
    trips_analyzed: int = 0
    stop_headway_rows: int = 0
    frequency_trips_expanded: int = 0     # trips from frequencies.txt
    calendar_end: Optional[str] = None    # ISO date, latest declared service
    calendar_status: str = "unknown"      # "current" | "expired" | "unknown"
    notes: list[str] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    """Top-level Phase 2 report (serialized to JSON as-is)."""

    generated_at: str        # wall-clock timestamp (run metadata)
    analysis_date: str = ""  # the run's input clock (expiry/staleness reference)
    dry_run: bool
    proximity_m: float
    mode_filter: list[str] = Field(default_factory=list)   # mode names; [] = all
    route_filter: list[str] = Field(default_factory=list)  # route specs; [] = all
    dedupe: bool = True
    dedupe_distance_m: float = DEDUPE_DISTANCE_M
    # None = within-feed dedupe was off (and in reports written before the
    # field existed, whose radius was not recorded).
    dedupe_within_feed_distance_m: Optional[float] = None
    # Alias groups found - cross-feed, plus within-feed when
    # dedupe_within_feed_distance_m is set. stop_equivalences.match_kind splits them.
    station_groups: int = 0
    stops_deduplicated: int = 0    # stops belonging to some alias group
    windows: dict[str, str]  # name -> "HH:MM-HH:MM"
    # False when analyzed feeds resolved to different service days - the
    # cross-feed outputs then mix non-contemporaneous schedules.
    service_dates_coherent: bool = True
    service_dates: list[str] = Field(default_factory=list)
    # Sources that contributed stop geometry but no schedule (stops-only CSVs).
    stops_only_feeds: list[str] = Field(default_factory=list)
    stops_only_stops: int = 0
    feeds: list[FeedAnalysis]
    connections_by_type: dict[str, int]
    isolated_routes: list[str]
    graph: dict[str, Any]  # n_routes / n_edges / n_components / component_sizes
    # Routable stop-level network (station_network.graphml):
    # n_stops / n_stops_only / n_edges / edges_by_type / n_weak_components
    # (components counted over transit stops; stops-only nodes are unlinked)
    station_graph: dict[str, Any] = Field(default_factory=dict)
    # Stop counts by provenance label (near_stations_quality.csv):
    # {"walk_source": {...}, "travel_source": {...}}
    quality_summary: dict[str, dict[str, int]] = Field(default_factory=dict)


class _FeedData:
    """In-memory per-feed artifacts passed between stages (not serialized)."""

    def __init__(self) -> None:
        self.stop_headways = pd.DataFrame()
        self.rollup = pd.DataFrame()
        self.route_stops = pd.DataFrame()
        self.routes = pd.DataFrame()
        self.stops: Optional[gpd.GeoDataFrame] = None
        self.route_shapes = gpd.GeoDataFrame()
        self.stop_links = pd.DataFrame()


def _route_metadata(feed: Any, feed_id: str, route_ids: pd.Series) -> pd.DataFrame:
    """Node attributes for analyzed routes (best effort from routes.txt).

    ``modalidad`` is the serving system's name from ``agency.txt``
    (routes.agency_id -> agency_name; single-agency feeds map every route
    to that agency, per the GTFS default). Missing agency data leaves the
    column NULL - metadata is cosmetic, never fatal.
    """
    routes = pd.DataFrame({"route_id": route_ids.astype(str).unique()})
    routes["feed_id"] = feed_id
    routes["route_uid"] = feed_id + ":" + routes["route_id"]
    try:
        meta = feed.routes
        keep = [c for c in ("route_id", "route_short_name", "route_long_name",
                            "route_type", "agency_id") if c in meta.columns]
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
    extra = [c for c in routes.columns
             if (c.startswith("route_") or c == "modalidad") and c not in ("route_uid", "route_id")]
    return routes[["route_uid", "feed_id", "route_id"] + extra]


def _analyze_service_day(
    feed: Any, source: FeedSource, config: AnalysisConfig
) -> tuple[_FeedData, int, int]:
    """One service day's frames from an already-filtered feed (untagged).

    Returns:
        ``(day_data, frequency_trips, trips)`` - the day's frames without
        feed_id/service_date provenance columns (the caller tags them).
    """
    day = _FeedData()
    frequencies = getattr(feed, "frequencies", None)
    n_freq = (
        int(frequencies["trip_id"].nunique())
        if frequencies is not None and not frequencies.empty else 0
    )
    departures = stop_departures(feed)
    n_trips = int(feed.trips["trip_id"].nunique()) if not feed.trips.empty else 0

    day.stop_headways = compute_stop_headways(departures, config.windows)
    day.rollup = route_rollup(day.stop_headways)
    day.route_stops = connectivity.route_stop_map(feed, source.feed_id)
    day.routes = _route_metadata(feed, source.feed_id, day.route_stops["route_id"])
    day.route_shapes = route_geometry.route_shapes(feed, source.feed_id)
    day.stop_links = route_geometry.route_stop_links(feed, source.feed_id)
    return day, n_freq, n_trips


def _tag_headways(day: _FeedData, feed_id: str, service_date: date) -> None:
    """Insert the DB provenance columns into one day's headway frames."""
    for frame in (day.stop_headways, day.rollup):
        if not frame.empty:
            frame.insert(0, "feed_id", feed_id)
            frame.insert(1, "service_date", service_date)


def _union_days(days: list[_FeedData], data: _FeedData) -> None:
    """Merge per-day frames into range-wide unions (headways stay per-day).

    Connectivity/near-stations/geometry consume the union of served days;
    ``stop_links`` collapses to one row per segment with the median
    ``travel_min`` and the max ``n_obs`` across days.
    """
    def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        keep = [f for f in frames if not f.empty]
        return pd.concat(keep, ignore_index=True) if keep else pd.DataFrame()

    data.stop_headways = _concat([d.stop_headways for d in days])
    data.rollup = _concat([d.rollup for d in days])
    route_stops = _concat([d.route_stops for d in days])
    data.route_stops = (
        route_stops.drop_duplicates(["route_uid", "stop_uid"]).reset_index(drop=True)
        if not route_stops.empty else route_stops
    )
    routes = _concat([d.routes for d in days])
    data.routes = (
        routes.drop_duplicates("route_uid").reset_index(drop=True)
        if not routes.empty else routes
    )
    shape_frames = [d.route_shapes for d in days if not d.route_shapes.empty]
    if shape_frames:
        shapes = pd.concat(shape_frames, ignore_index=True)
        data.route_shapes = gpd.GeoDataFrame(
            shapes.drop_duplicates(["route_uid", "direction_id", "shape_id"]).reset_index(drop=True),
            crs=shape_frames[0].crs,
        )
    links = _concat([d.stop_links for d in days])
    if not links.empty:
        data.stop_links = (
            links.groupby(
                ["route_uid", "feed_id", "route_id", "linea", "direction_id",
                 "stop_uid_a", "stop_uid_b"],
                as_index=False, dropna=False,
            )
            .agg(travel_min=("travel_min", "median"), n_obs=("n_obs", "max"))
            [route_geometry.LINK_COLUMNS]
        )


def _analyze_feed(
    source: FeedSource, config: AnalysisConfig, engine: Optional[Engine]
) -> tuple[FeedAnalysis, _FeedData]:
    """Run stages 2a-2d for one feed. Never raises (failure boundary)."""
    data = _FeedData()
    result = FeedAnalysis(feed_id=source.feed_id, source_path=str(source.path), status="analyzed")
    try:
        raw = loader.load_feed(source)

        # A stops-only source has no calendar and no trips, so every
        # service-date path below would raise inside partridge. Branch out
        # here, BEFORE the route filter and the service resolution, so the
        # stops are assigned unconditionally: `data.stops` is otherwise set
        # near the end of this function, downstream of every early return and
        # inside the try, and a feed that never reaches it is silently dropped
        # from all_stops and therefore from PostGIS, near_stations, the
        # GeoPackage, the station network and the Neo4j inputs.
        if source.is_stops_only:
            data.stops, _ = normalizer.normalize_stops(raw)
            result.status = "stops_only"
            # service_date stays None: this feed has no schedule, so it must
            # not participate in the cross-feed service-date coherence check.
            result.notes.append(
                f"stops-only CSV source: {len(data.stops)} stop(s) contribute geometry "
                "(proximity, near-stations, GeoPackage, unlinked station-network nodes); "
                "no routes, trips, headways, connectivity edges, dedupe or walk links"
            )
            if config.mode_codes or config.route_specs:
                # Filtering on route attributes a stops-only source cannot have
                # would exclude it entirely and delete its stops from every
                # output. Stops are never filtered - see route_filter's docstring.
                result.notes.append(
                    "--transport-mode/--route not applicable to a stops-only source"
                )
            if engine is not None:
                db.load_feed_stops(engine, source.feed_id, data.stops)
            return result, data

        # Optional --mode/--route restriction, resolved against raw routes.txt
        # and pushed into the partridge view so everything trip-derived
        # (stop_times, shapes, routes) cascades. None = analyze everything.
        allowed: Optional[set[str]] = None
        if config.mode_codes or config.route_specs:
            routes_raw = raw.table("routes.txt")
            allowed = route_filter.allowed_route_ids(
                routes_raw, config.mode_codes, config.route_specs
            )
            total = int(routes_raw["route_id"].nunique()) if routes_raw is not None and "route_id" in getattr(routes_raw, "columns", []) else 0
            if not allowed:
                result.status = "filtered_out"
                result.notes.append(
                    f"no route matches the --mode/--route filter ({total} route(s) in feed)"
                )
                log.info("Feed %s: excluded by route/mode filter", source.feed_id)
                return result, data
            result.notes.append(f"route filter: {len(allowed)} of {total} route(s) selected")

        # Calendar honesty (selection-independent): expired feeds must be
        # visible regardless of which day(s) get analyzed.
        cal_start, cal_end = calendar_range(
            raw.table("calendar.txt"), raw.table("calendar_dates.txt")
        )
        today = config.analysis_date  # injectable clock: reproducible notes
        if cal_end is not None:
            result.calendar_end = cal_end.isoformat()
            result.calendar_status = "expired" if cal_end < today else "current"
            if cal_end < today:
                result.notes.append(
                    f"feed calendar ended {cal_end.isoformat()} "
                    f"({(today - cal_end).days} day(s) before today)"
                )

        if config.service_date_range is not None:
            # ---- range path: one analysis per served day, unions downstream.
            start, end = config.service_date_range
            result.service_date = f"{start.isoformat()}..{end.isoformat()}"
            result.service_date_source = "range"
            served = service_ids_in_range(str(source.path), start, end)
            total_days = (end - start).days + 1
            if not served:
                result.status = "no_service"
                result.notes.append(f"no service between {start} and {end}")
                return result, data
            result.days_analyzed = len(served)
            if len(served) < total_days:
                result.notes.append(
                    f"{len(served)} of {total_days} day(s) in the range have service"
                )

            days: list[_FeedData] = []
            n_freq_max, trips_total = 0, 0
            for day_date, ids in served.items():
                feed = load_feed_for_service_ids(source, ids, route_ids=allowed)
                day, n_freq, n_trips = _analyze_service_day(feed, source, config)
                _tag_headways(day, source.feed_id, day_date)
                days.append(day)
                n_freq_max = max(n_freq_max, n_freq)
                trips_total += n_trips
            _union_days(days, data)
            result.frequency_trips_expanded = n_freq_max
            if n_freq_max:
                result.notes.append(
                    f"frequencies.txt: up to {n_freq_max} trip(s)/day expanded "
                    "into timetable runs"
                )
            result.trips_analyzed = trips_total  # trip-days across the range
        else:
            # ---- single-date path (explicit, busiest, or --common-date).
            feed, selection = load_service_feed(
                source, config.service_date, route_ids=allowed,
                label="common" if config.common_date else "explicit",
            )
            result.service_date = selection.service_date.isoformat()
            result.service_date_source = selection.source
            if cal_start is not None and cal_end is not None and not (
                cal_start <= selection.service_date <= cal_end
            ):
                result.notes.append(
                    f"analyzed service date {selection.service_date} is outside "
                    f"the feed's calendar validity"
                )
            if (
                selection.source == "busiest"
                and (today - selection.service_date).days > STALE_BUSIEST_DAYS
            ):
                result.notes.append(
                    f"busiest service date {selection.service_date} is "
                    f"{(today - selection.service_date).days} day(s) in the past"
                )
            if not selection.service_ids:
                result.status = "no_service"
                result.notes.append(f"no service runs on {selection.service_date}")
                return result, data

            day, n_freq, n_trips = _analyze_service_day(feed, source, config)
            _tag_headways(day, source.feed_id, selection.service_date)
            data = day
            result.frequency_trips_expanded = n_freq
            if n_freq:
                result.notes.append(
                    f"frequencies.txt: {n_freq} trip(s) expanded into timetable runs"
                )
            result.trips_analyzed = n_trips

        result.routes_analyzed = int(len(data.routes))
        result.stop_headway_rows = int(len(data.stop_headways))

        # Stops via the Phase 1 path: parent_station links + proximity input.
        # Deliberately from the RAW feed - the stop inventory stays complete
        # even under a route/mode filter (filtered-out stops appear unserved).
        data.stops, _ = normalizer.normalize_stops(raw)

        if engine is not None:
            db.load_feed_stops(engine, source.feed_id, data.stops)  # keep PostGIS stops fresh
            storage.replace_feed_rows(engine, "route_stop_headways", source.feed_id, data.stop_headways)
            storage.replace_feed_rows(engine, "route_headways", source.feed_id, data.rollup)
    except Exception as exc:  # noqa: BLE001 - per-feed isolation
        log.exception("Feed %s: analysis failed", source.feed_id)
        result.status = "failed"
        result.notes.append(f"{type(exc).__name__}: {exc}")
    return result, data


def run_analysis(config: AnalysisConfig) -> AnalysisReport:
    """Execute one full Phase 2 run and write all outputs.

    Args:
        config: resolved configuration.

    Returns:
        The analysis report (also written to ``config.report_dir`` as JSON
        and Markdown, next to the GraphML export).

    Raises:
        ValueError: non-dry run without a database URL.
        RuntimeError / sqlalchemy errors: unreachable database or missing
            PostGIS (run-level).
        OSError: unwritable report directory.
    """
    sources = loader.discover_feeds(config.feed_paths)
    if not sources:
        log.warning("No feeds discovered under: %s", ", ".join(map(str, config.feed_paths)))

    # --common-date: one shared service day for every feed, or fail loudly.
    # Stops-only sources are excluded first: they have no calendar, so
    # resolving a service date against them raises inside partridge - and this
    # call sits outside every per-feed failure boundary, so one extra CSV in
    # --feeds would abort the whole run.
    schedule_sources = [s for s in sources if not s.is_stops_only]
    if config.common_date and sources and not schedule_sources:
        log.warning("--common-date ignored: every discovered source is stops-only")
    elif config.common_date and schedule_sources:
        common = common_service_date([str(s.path) for s in schedule_sources])
        if common is None:
            raise ValueError(
                "--common-date: the discovered feeds share no calendar date "
                "with active service in every feed (their validity windows do "
                "not overlap). Analyze with per-feed busiest dates (default) "
                "or pass an explicit --service-date."
            )
        config = config.model_copy(update={"service_date": common})
        log.info("Common service date resolved: %s", common)

    engine: Optional[Engine] = None
    if not config.dry_run:
        if not config.database_url:
            raise ValueError(
                "No database URL configured; set DATABASE_URL or pass --db-url "
                "(or use --dry-run to analyze without a database)"
            )
        engine = db.make_engine(config.database_url)
        db.ensure_schema(engine)
        storage.ensure_analysis_schema(engine)

    feed_results: list[FeedAnalysis] = []
    feed_data: list[_FeedData] = []
    for source in sources:
        result, data = _analyze_feed(source, config, engine)
        feed_results.append(result)
        feed_data.append(data)

    # Service-date coherence: cross-feed outputs (connectivity, near
    # stations) mix every analyzed feed into one picture - if the feeds
    # resolved to different service days, that picture mixes
    # non-contemporaneous schedules and the reader must know.
    analyzed_dates = sorted({
        r.service_date for r in feed_results
        if r.status == "analyzed" and r.service_date
    })
    service_dates_coherent = len(analyzed_dates) <= 1
    if not service_dates_coherent:
        log.warning(
            "Feeds were analyzed on DIFFERENT service dates (%s): cross-feed "
            "connectivity and near-stations mix non-contemporaneous "
            "schedules. Use --service-date, or --common-date if the "
            "calendars overlap.",
            ", ".join(analyzed_dates),
        )

    # ---- cross-feed connectivity -----------------------------------------
    route_stops = pd.concat([d.route_stops for d in feed_data if not d.route_stops.empty], ignore_index=True) \
        if any(not d.route_stops.empty for d in feed_data) else pd.DataFrame(columns=["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"])
    routes = pd.concat([d.routes for d in feed_data if not d.routes.empty], ignore_index=True) \
        if any(not d.routes.empty for d in feed_data) else pd.DataFrame(columns=["route_uid", "feed_id", "route_id"])
    stops_frames = [d.stops for d in feed_data if d.stops is not None and not d.stops.empty]
    all_stops = pd.concat(stops_frames, ignore_index=True) if stops_frames else normalizer.empty_stops_frame()

    stations = connectivity.station_map(all_stops)
    # Every feed that contributed STOPS, which is a superset of the feeds that
    # contributed a schedule. The spatial queries below must use this one:
    # keyed on "analyzed" alone, a stops-only feed's rows sit in PostGIS but
    # match no proximity pair, so its stops end up with no neighbours at all.
    # Dry runs cannot catch that - they take the local branch below, which
    # works off all_stops and ignores the feed-id list entirely.
    geographic_feed_ids = [
        r.feed_id for r in feed_results if r.status in ("analyzed", "stops_only")
    ]
    if engine is not None and geographic_feed_ids:
        stop_pairs = connectivity.proximity_pairs_postgis(engine, geographic_feed_ids, config.proximity_m)
    else:
        stop_pairs = connectivity.proximity_pairs_local(all_stops, config.proximity_m)

    # Stops-only sources contribute geometry, never station identity or
    # routing: they are kept out of dedupe and of the station network.
    stops_only_ids = [r.feed_id for r in feed_results if r.status == "stops_only"]

    # Station deduplication: aliases of one physical station must not count
    # as "near" each other, and their routes connect via the canonical
    # station rather than mere proximity.
    equivalences = pd.DataFrame(columns=dedupe.EQUIVALENCE_COLUMNS)
    canonical = pd.DataFrame(columns=dedupe.CANONICAL_COLUMNS)
    if config.dedupe:
        accepted = dedupe.find_equivalences(
            all_stops, route_stops, routes,
            config.dedupe_distance_m, config.dedupe_name_threshold,
            within_feed_distance_m=config.dedupe_within_feed_distance_m,
            exclude_feeds=stops_only_ids,
        )
        canonical = dedupe.canonical_map(accepted)
        equivalences = dedupe.attach_groups(accepted, canonical)
        stop_pairs = dedupe.filter_stop_pairs(stop_pairs, canonical)

    edges = connectivity.build_edges(route_stops, stations, stop_pairs, canonical)

    # Optional OSM street-network walking distances (--network-walk): applied
    # AFTER the connectivity edges (min_stop_distance_m stays geodesic) and
    # BEFORE near-stations/GeoPackage, whose walking minutes they refine.
    if config.network_walk and engine is not None and not stop_pairs.empty:
        from . import walk_network

        try:
            stop_pairs = walk_network.network_distances(
                engine, all_stops, stop_pairs, geographic_feed_ids,
                metric_crs=None,
                line_table=config.osm_line_table,
                geom_column=config.osm_geom_column,
                cache_dir=config.walk_cache_dir if config.walk_cache else None,
            )
        except Exception:  # noqa: BLE001 - refinement only, never fatal
            log.exception("Network walk distances failed; keeping straight-line values")

    shape_frames = [d.route_shapes for d in feed_data if not d.route_shapes.empty]
    all_shapes = gpd.GeoDataFrame(
        pd.concat(shape_frames, ignore_index=True), crs="EPSG:4326"
    ) if shape_frames else gpd.GeoDataFrame()
    link_frames = [d.stop_links for d in feed_data if not d.stop_links.empty]
    all_links = pd.concat(link_frames, ignore_index=True) if link_frames else pd.DataFrame()

    near = near_stations.build_near_stations(
        all_stops, route_stops, routes, stop_pairs, stop_links=all_links
    )
    near_stations.write_near_stations(near, config.report_dir)

    # Quality sidecar: same rows/IDs as near_stations.csv, provenance only.
    osm_matches = None
    if engine is not None and geographic_feed_ids:
        osm_matches = storage.fetch_osm_matches(engine, geographic_feed_ids)
    quality = near_stations.build_quality(
        near, route_stops, routes, stop_pairs,
        stop_links=all_links, osm_matches=osm_matches, canonical=canonical,
    )
    near_stations.write_quality(quality, config.report_dir)

    if config.dedupe:
        dedupe.write_equivalences(equivalences, config.report_dir)

    # Routable station network: the same frames, exposed as a graph.
    station_graph = station_network.build_station_graph(
        all_stops, near, all_links, stop_pairs, equivalences, canonical=canonical,
        exclude_feeds=stops_only_ids,
    )
    station_network.write_station_graphml(
        station_graph, config.report_dir / station_network.STATION_GRAPHML_NAME
    )
    station_stats = station_network.station_graph_stats(station_graph)
    _write_csv(station_network.station_nodes_frame(station_graph),
               config.report_dir / "station_network_nodes.csv")
    _write_csv(station_network.station_edges_frame(station_graph),
               config.report_dir / "station_network_edges.csv")
    # Headway tables as CSVs too - dry runs otherwise lose them entirely.
    all_headways = pd.concat(
        [d.stop_headways for d in feed_data if not d.stop_headways.empty],
        ignore_index=True,
    ) if any(not d.stop_headways.empty for d in feed_data) else pd.DataFrame()
    all_rollups = pd.concat(
        [d.rollup for d in feed_data if not d.rollup.empty], ignore_index=True
    ) if any(not d.rollup.empty for d in feed_data) else pd.DataFrame()
    _write_csv(all_headways, config.report_dir / "route_stop_headways.csv")
    _write_csv(all_rollups, config.report_dir / "route_headways.csv")

    geopackage.write_geopackage(
        all_stops, near, stop_pairs, config.report_dir,
        route_shapes=all_shapes, stop_links=all_links,
        station_edges=station_network.station_edges_layer(station_graph, all_stops),
    )

    graph = graph_export.build_graph(routes, edges)
    metrics = graph_export.graph_metrics(graph)
    graph_export.write_graphml(graph, config.report_dir / GRAPHML_NAME)

    if engine is not None:
        storage.replace_all_rows(engine, "route_connections", edges)
        # With --no-dedupe the table is reloaded to empty so it always
        # reflects the last run.
        storage.replace_all_rows(
            engine, "stop_equivalences", equivalences[dedupe.EQUIVALENCE_COLUMNS]
        )

    # ---- report ------------------------------------------------------------
    by_type = edges["connection_type"].value_counts().to_dict() if not edges.empty else {}
    stops_only_stops = (
        int(all_stops["feed_id"].isin(stops_only_ids).sum())
        if stops_only_ids and not all_stops.empty
        else 0
    )
    report = AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        stops_deduplicated=int(len(canonical)),
        windows={w.name: w.label for w in config.windows},
        service_dates_coherent=service_dates_coherent,
        service_dates=analyzed_dates,
        stops_only_feeds=stops_only_ids,
        stops_only_stops=stops_only_stops,
        feeds=feed_results,
        connections_by_type={k: int(v) for k, v in by_type.items()},
        isolated_routes=metrics["isolated_routes"],
        graph={k: metrics[k] for k in ("n_routes", "n_edges", "n_components", "component_sizes")},
        station_graph=station_stats,
        quality_summary={
            col: {k: int(v) for k, v in quality[col].value_counts().items()}
            for col in ("walk_source", "travel_source")
        } if not quality.empty else {},
    )
    _write_reports(report, config.report_dir)
    log.info(
        "Analysis complete: %d feed(s), %d route(s), %d connection edge(s), %d isolated route(s)",
        len(feed_results), metrics["n_routes"], metrics["n_edges"], len(metrics["isolated_routes"]),
    )
    return report


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    """Write an analysis frame as CSV (utf-8, no index; header-only if empty)."""
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
    """Human-readable Markdown rendering of the analysis report."""
    lines = [
        "# GTFS Headway & Connectivity Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Analysis date: {report.analysis_date}  ",
        f"Mode: {'dry run (no DB writes)' if report.dry_run else 'full run'}  ",
        f"Proximity threshold: {report.proximity_m:g} m  ",
        "Transport modes: "
        + (", ".join(report.mode_filter) if report.mode_filter else "all (no filter)")
        + "  ",
        "Routes: "
        + (", ".join(report.route_filter) if report.route_filter else "all (no filter)")
        + "  ",
        "Station deduplication: "
        + (
            f"on (cross-feed {report.dedupe_distance_m:g} m, within-feed "
            + (
                f"{report.dedupe_within_feed_distance_m:g} m"
                if report.dedupe_within_feed_distance_m is not None
                else "off"
            )
            + f") - {report.station_groups} group(s), "
            f"{report.stops_deduplicated} stop(s) merged; see `{dedupe.EQUIVALENCES_NAME}`"
            if report.dedupe
            else "off (--no-dedupe)"
        ),
        "",
        "## Time windows",
        "",
        "| Window | Range |",
        "| --- | --- |",
        *[f"| {name} | {label} |" for name, label in report.windows.items()],
        "",
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

    if report.stops_only_feeds:
        lines += [
            "",
            f"> **Stops-only sources:** `{'`, `'.join(report.stops_only_feeds)}` - "
            f"{report.stops_only_stops} stop(s) contributing geometry only. They appear in "
            "`near_stations.csv`, `near_stations_quality.csv` and `transit_analysis.gpkg`, "
            "and can be neighbours of scheduled stops (rendered with line `-`). They are "
            "never deduplicated, sit in the station network as unlinked nodes (no walk "
            "arcs, so no route passes through them), and contribute no route connectivity "
            "edges, headways or same-line topology.",
        ]

    if not report.service_dates_coherent:
        lines += [
            "",
            "> **Warning:** feeds were analyzed on DIFFERENT service dates "
            f"({', '.join(report.service_dates)}). Cross-feed connectivity and "
            "near-stations mix non-contemporaneous schedules. Use "
            "`--service-date`, or `--common-date` if the calendars overlap.",
        ]

    lines += [
        "",
        "## Route connectivity",
        "",
        f"Routes: {report.graph['n_routes']} · Edges: {report.graph['n_edges']} · "
        f"Connected components: {report.graph['n_components']} "
        f"(sizes: {', '.join(map(str, report.graph['component_sizes'])) or '-'})",
        "",
        f"Station network: {report.station_graph.get('n_stops', 0)} stops · "
        f"{report.station_graph.get('n_edges', 0)} edges "
        f"({', '.join(f'{k}={v}' for k, v in report.station_graph.get('edges_by_type', {}).items()) or 'none'}) · "
        f"{report.station_graph.get('n_weak_components', 0)} weak component(s) among transit stops"
        + (
            f" · {report.station_graph['n_stops_only']} unlinked stops-only node(s)"
            if report.station_graph.get("n_stops_only")
            else ""
        ),
        "",
        "| Connection type | Route pairs |",
        "| --- | ---: |",
    ]
    for kind in ("same_stop", "same_parent_station", "same_station_dedup", "proximity"):
        lines.append(f"| {kind} | {report.connections_by_type.get(kind, 0)} |")

    if report.quality_summary:
        walk = report.quality_summary.get("walk_source", {})
        travel = report.quality_summary.get("travel_source", {})
        lines += [
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
                f"| {w} | {walk.get(w, 0) if w else ''} | "
                f"| {t} | {travel.get(t, 0) if t else ''} |"
            )

    lines += ["", "### Isolated routes (no connections)", ""]
    if report.isolated_routes:
        lines += [f"- `{r}`" for r in report.isolated_routes]
    else:
        lines.append("None - every analyzed route connects to at least one other.")
    lines += [
        "",
        f"GraphML export: `{GRAPHML_NAME}` (same directory).  ",
        f"Near-stations export: `{near_stations.NEAR_STATIONS_NAME}` "
        "(per stop: numeric ID, name, lat, lon, combined stop+line ids, and "
        "nearby stations as `[(ID, station, linea, time to reach), ...]`).  ",
        f"Stop equivalences export: `{dedupe.EQUIVALENCES_NAME}` "
        "(cross-feed alias pairs merged by station deduplication; only "
        "written when dedupe is on).  ",
        f"Quality sidecar: `{near_stations.QUALITY_NAME}` "
        "(per stop, same ID key: walk/travel provenance, worst-case "
        "observation count, OSM coord confidence, dedupe canonical id).  ",
        f"Station network: `{station_network.STATION_GRAPHML_NAME}` + "
        "`station_network_nodes.csv`/`station_network_edges.csv` + the "
        "`station_network` GeoPackage layer - the ROUTABLE stop-level graph "
        "(directed in_vehicle edges weighted by scheduled minutes, walk "
        "edges by walking minutes, 0-min same_station alias links; "
        "`nx.shortest_path(..., weight='time_min')` works directly).  ",
        "Headway tables: `route_stop_headways.csv` / `route_headways.csv` "
        "(same rows as the DB tables, written on every run incl. dry runs).  ",
        f"GeoPackage export: `{geopackage.GEOPACKAGE_NAME}` "
        "(layers: `stops` points, `near_links` walking lines, `route_shapes` "
        "real route paths, `route_stop_links` consecutive-stop segments - open in QGIS).",
        "",
    ]
    return "\n".join(lines)
