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

#: A "busiest" date older than this many days gets a staleness note.
STALE_BUSIEST_DAYS = 30


class AnalysisConfig(BaseModel):
    """Configuration of one Phase 2 run.

    service_date None picks each feed's busiest date. mode_codes/route_specs
    keep routes matching either filter. dedupe_within_feed_distance_m defaults
    to 5 m like the CLI; None turns that rule off. dry_run skips the database.
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
    """Phase 2 report (written to JSON as-is)."""

    generated_at: str        # wall-clock timestamp (run metadata)
    analysis_date: str = ""  # the run's input clock (expiry/staleness reference)
    dry_run: bool
    proximity_m: float
    mode_filter: list[str] = Field(default_factory=list)   # mode names; [] = all
    route_filter: list[str] = Field(default_factory=list)  # route specs; [] = all
    dedupe: bool = True
    dedupe_distance_m: float = DEDUPE_DISTANCE_M
    # None = within-feed dedupe was off (or the report predates this field).
    dedupe_within_feed_distance_m: Optional[float] = None
    # stop_equivalences.match_kind tells the two rules apart.
    station_groups: int = 0
    stops_deduplicated: int = 0    # stops belonging to some alias group
    windows: dict[str, str]  # name -> "HH:MM-HH:MM"
    # False when feeds were analyzed on different days (outputs then mix schedules).
    service_dates_coherent: bool = True
    service_dates: list[str] = Field(default_factory=list)
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


class _FeedData:
    """Per-feed frames passed between steps (not serialized)."""

    def __init__(self) -> None:
        self.stop_headways = pd.DataFrame()
        self.rollup = pd.DataFrame()
        self.route_stops = pd.DataFrame()
        self.routes = pd.DataFrame()
        self.stops: Optional[gpd.GeoDataFrame] = None
        self.route_shapes = gpd.GeoDataFrame()
        self.stop_links = pd.DataFrame()


def _route_metadata(feed: Any, feed_id: str, route_ids: pd.Series) -> pd.DataFrame:
    """Route attributes for the graph; modalidad is the agency name (NULL if unknown)."""
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
    """Frames for one service day; returns (day_data, frequency_trips, trips)."""
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
    """Add feed_id and service_date to one day's headway frames."""
    for frame in (day.stop_headways, day.rollup):
        if not frame.empty:
            frame.insert(0, "feed_id", feed_id)
            frame.insert(1, "service_date", service_date)


def _union_days(days: list[_FeedData], data: _FeedData) -> None:
    """Merge per-day frames for a date range; links keep the median travel time."""
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
    """Analyze one feed. Never raises; failures go into the result's notes."""
    data = _FeedData()
    result = FeedAnalysis(feed_id=source.feed_id, source_path=str(source.path), status="analyzed")
    try:
        raw = loader.load_feed(source)

        # Stops-only sources have no calendar: handle them before any service-date
        # logic, and set data.stops here so they are never dropped from the outputs.
        if source.is_stops_only:
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
                result.notes.append(
                    "--transport-mode/--route not applicable to a stops-only source"
                )
            if engine is not None:
                db.load_feed_stops(engine, source.feed_id, data.stops)
            return result, data

        # Optional --transport-mode/--route filter; None means every route.
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

        # Flag expired feeds whatever day is analyzed.
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
            # Date range: analyze each served day, then merge.
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
            # Single day: explicit, busiest, or --common-date.
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

        # Stops come from the raw feed, so filters never drop them.
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
    """Run Phase 2 and write every output.

    Raises:
        ValueError: a non-dry run without a database URL.
    """
    sources = loader.discover_feeds(config.feed_paths)
    if not sources:
        log.warning("No feeds discovered under: %s", ", ".join(map(str, config.feed_paths)))

    # --common-date: one shared day for every schedule feed, or fail.
    # Stops-only sources have no calendar, so they are left out here.
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

    # Warn when feeds were analyzed on different days.
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

    route_stops = pd.concat([d.route_stops for d in feed_data if not d.route_stops.empty], ignore_index=True) \
        if any(not d.route_stops.empty for d in feed_data) else pd.DataFrame(columns=["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"])
    routes = pd.concat([d.routes for d in feed_data if not d.routes.empty], ignore_index=True) \
        if any(not d.routes.empty for d in feed_data) else pd.DataFrame(columns=["route_uid", "feed_id", "route_id"])
    stops_frames = [d.stops for d in feed_data if d.stops is not None and not d.stops.empty]
    all_stops = pd.concat(stops_frames, ignore_index=True) if stops_frames else normalizer.empty_stops_frame()

    stations = connectivity.station_map(all_stops)
    # Every feed with stops, schedule or not; the PostGIS query filters on this
    # list, so stops-only feeds must be in it (dry runs can't catch a mistake).
    geographic_feed_ids = [
        r.feed_id for r in feed_results if r.status in ("analyzed", "stops_only")
    ]
    if engine is not None and geographic_feed_ids:
        stop_pairs = connectivity.proximity_pairs_postgis(engine, geographic_feed_ids, config.proximity_m)
    else:
        stop_pairs = connectivity.proximity_pairs_local(all_stops, config.proximity_m)

    # Stops-only sources are kept out of dedupe and station-network links.
    stops_only_ids = [r.feed_id for r in feed_results if r.status == "stops_only"]

    # Aliases of one station are not each other's neighbours; their routes
    # connect as same_station_dedup instead.
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

    # --network-walk: after connectivity (which stays straight-line), before
    # near_stations and the GeoPackage, which use the walking times.
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

    # Quality sidecar: same rows and IDs as near_stations.csv.
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

    # Routable station network built from the same frames.
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
    # Headways as CSV too, so dry runs keep them.
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
        # With --no-dedupe the table is emptied, so it always matches the last run.
        storage.replace_all_rows(
            engine, "stop_equivalences", equivalences[dedupe.EQUIVALENCE_COLUMNS]
        )

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
