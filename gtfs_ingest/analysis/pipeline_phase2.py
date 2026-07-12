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
from . import connectivity, dedupe, geopackage, graph_export, near_stations, route_filter, route_geometry, storage
from .dedupe import DEDUPE_DISTANCE_M, NAME_SIM_THRESHOLD
from .headways import DEFAULT_WINDOWS, TimeWindow, compute_stop_headways, route_rollup, stop_departures
from .service_filter import load_service_feed

log = logging.getLogger(__name__)

GRAPHML_NAME = "route_connectivity.graphml"
JSON_NAME = "analysis_report.json"
MARKDOWN_NAME = "analysis_report.md"


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
        dedupe: merge cross-feed duplicate stations (``analysis.dedupe``)
            before connectivity / near-stations; on by default.
        dedupe_distance_m: alias candidate radius in meters.
        dedupe_name_threshold: minimum name similarity (0-1) to accept an
            alias pair on names alone.
        dry_run: skip all database writes (and use local proximity math).
    """

    feed_paths: list[Path]
    database_url: Optional[str] = None
    report_dir: Path = Path("out")
    service_date: Optional[date] = None
    proximity_m: float = Field(default=150.0, gt=0)
    windows: list[TimeWindow] = Field(default_factory=lambda: list(DEFAULT_WINDOWS))
    mode_codes: Optional[frozenset[int]] = None
    route_specs: Optional[frozenset[str]] = None
    network_walk: bool = False
    osm_line_table: str = "planet_osm_line"
    osm_geom_column: str = "way"
    dedupe: bool = True
    dedupe_distance_m: float = Field(default=DEDUPE_DISTANCE_M, gt=0)
    dedupe_name_threshold: float = Field(default=NAME_SIM_THRESHOLD, ge=0.0, le=1.0)
    dry_run: bool = False


class FeedAnalysis(BaseModel):
    """Per-feed outcome recorded in the analysis report."""

    feed_id: str
    source_path: str
    status: str  # "analyzed" | "no_service" | "filtered_out" | "failed"
    service_date: Optional[str] = None
    service_date_source: Optional[str] = None  # "explicit" | "busiest"
    routes_analyzed: int = 0
    trips_analyzed: int = 0
    stop_headway_rows: int = 0
    notes: list[str] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    """Top-level Phase 2 report (serialized to JSON as-is)."""

    generated_at: str
    dry_run: bool
    proximity_m: float
    mode_filter: list[str] = Field(default_factory=list)   # mode names; [] = all
    route_filter: list[str] = Field(default_factory=list)  # route specs; [] = all
    dedupe: bool = True
    dedupe_distance_m: float = DEDUPE_DISTANCE_M
    station_groups: int = 0        # cross-feed alias groups found
    stops_deduplicated: int = 0    # stops belonging to some alias group
    windows: dict[str, str]  # name -> "HH:MM-HH:MM"
    feeds: list[FeedAnalysis]
    connections_by_type: dict[str, int]
    isolated_routes: list[str]
    graph: dict[str, Any]  # n_routes / n_edges / n_components / component_sizes
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


def _analyze_feed(
    source: FeedSource, config: AnalysisConfig, engine: Optional[Engine]
) -> tuple[FeedAnalysis, _FeedData]:
    """Run stages 2a-2d for one feed. Never raises (failure boundary)."""
    data = _FeedData()
    result = FeedAnalysis(feed_id=source.feed_id, source_path=str(source.path), status="analyzed")
    try:
        raw = loader.load_feed(source)

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

        feed, selection = load_service_feed(source, config.service_date, route_ids=allowed)
        result.service_date = selection.service_date.isoformat()
        result.service_date_source = selection.source
        if not selection.service_ids:
            result.status = "no_service"
            result.notes.append(f"no service runs on {selection.service_date}")
            return result, data

        departures = stop_departures(feed)
        result.trips_analyzed = int(feed.trips["trip_id"].nunique()) if not feed.trips.empty else 0

        data.stop_headways = compute_stop_headways(departures, config.windows)
        data.rollup = route_rollup(data.stop_headways)
        data.route_stops = connectivity.route_stop_map(feed, source.feed_id)
        data.routes = _route_metadata(feed, source.feed_id, data.route_stops["route_id"])
        data.route_shapes = route_geometry.route_shapes(feed, source.feed_id)
        data.stop_links = route_geometry.route_stop_links(feed, source.feed_id)
        result.routes_analyzed = int(len(data.routes))
        result.stop_headway_rows = int(len(data.stop_headways))

        # Provenance columns for the DB tables.
        for frame in (data.stop_headways, data.rollup):
            if not frame.empty:
                frame.insert(0, "feed_id", source.feed_id)
                frame.insert(1, "service_date", selection.service_date)

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

    # ---- cross-feed connectivity -----------------------------------------
    route_stops = pd.concat([d.route_stops for d in feed_data if not d.route_stops.empty], ignore_index=True) \
        if any(not d.route_stops.empty for d in feed_data) else pd.DataFrame(columns=["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"])
    routes = pd.concat([d.routes for d in feed_data if not d.routes.empty], ignore_index=True) \
        if any(not d.routes.empty for d in feed_data) else pd.DataFrame(columns=["route_uid", "feed_id", "route_id"])
    stops_frames = [d.stops for d in feed_data if d.stops is not None and not d.stops.empty]
    all_stops = pd.concat(stops_frames, ignore_index=True) if stops_frames else normalizer.empty_stops_frame()

    stations = connectivity.station_map(all_stops)
    analyzed_feed_ids = [r.feed_id for r in feed_results if r.status == "analyzed"]
    if engine is not None and analyzed_feed_ids:
        stop_pairs = connectivity.proximity_pairs_postgis(engine, analyzed_feed_ids, config.proximity_m)
    else:
        stop_pairs = connectivity.proximity_pairs_local(all_stops, config.proximity_m)

    # Cross-feed station deduplication: aliases of one physical station must
    # not count as "near" each other, and their routes connect via the
    # canonical station rather than mere proximity.
    equivalences = pd.DataFrame(columns=dedupe.EQUIVALENCE_COLUMNS)
    canonical = pd.DataFrame(columns=dedupe.CANONICAL_COLUMNS)
    if config.dedupe:
        accepted = dedupe.find_equivalences(
            all_stops, route_stops, routes,
            config.dedupe_distance_m, config.dedupe_name_threshold,
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
                engine, all_stops, stop_pairs, analyzed_feed_ids,
                metric_crs=None,
                line_table=config.osm_line_table,
                geom_column=config.osm_geom_column,
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
    if engine is not None and analyzed_feed_ids:
        osm_matches = storage.fetch_osm_matches(engine, analyzed_feed_ids)
    quality = near_stations.build_quality(
        near, route_stops, routes, stop_pairs,
        stop_links=all_links, osm_matches=osm_matches, canonical=canonical,
    )
    near_stations.write_quality(quality, config.report_dir)

    if config.dedupe:
        dedupe.write_equivalences(equivalences, config.report_dir)
    geopackage.write_geopackage(all_stops, near, stop_pairs, config.report_dir,
                                route_shapes=all_shapes, stop_links=all_links)

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
    report = AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=config.dry_run,
        proximity_m=config.proximity_m,
        mode_filter=route_filter.mode_labels(config.mode_codes),
        route_filter=sorted(config.route_specs) if config.route_specs else [],
        dedupe=config.dedupe,
        dedupe_distance_m=config.dedupe_distance_m,
        station_groups=int(canonical["group_id"].nunique()) if not canonical.empty else 0,
        stops_deduplicated=int(len(canonical)),
        windows={w.name: w.label for w in config.windows},
        feeds=feed_results,
        connections_by_type={k: int(v) for k, v in by_type.items()},
        isolated_routes=metrics["isolated_routes"],
        graph={k: metrics[k] for k in ("n_routes", "n_edges", "n_components", "component_sizes")},
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
            f"on ({report.dedupe_distance_m:g} m) - {report.station_groups} group(s), "
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

    lines += [
        "",
        "## Route connectivity",
        "",
        f"Routes: {report.graph['n_routes']} · Edges: {report.graph['n_edges']} · "
        f"Connected components: {report.graph['n_components']} "
        f"(sizes: {', '.join(map(str, report.graph['component_sizes'])) or '-'})",
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
        f"GeoPackage export: `{geopackage.GEOPACKAGE_NAME}` "
        "(layers: `stops` points, `near_links` walking lines, `route_shapes` "
        "real route paths, `route_stop_links` consecutive-stop segments - open in QGIS).",
        "",
    ]
    return "\n".join(lines)
