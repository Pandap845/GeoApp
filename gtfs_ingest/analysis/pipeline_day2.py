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
3. Cross-feed connectivity edges (PostGIS geography when a DB is available,
   local UTM math otherwise), networkx graph, GraphML export.
4. Idempotent DB loads (skipped with --dry-run) and JSON + Markdown report.

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
from . import connectivity, graph_export, near_stations, storage
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
        dry_run: skip all database writes (and use local proximity math).
    """

    feed_paths: list[Path]
    database_url: Optional[str] = None
    report_dir: Path = Path("out")
    service_date: Optional[date] = None
    proximity_m: float = Field(default=150.0, gt=0)
    windows: list[TimeWindow] = Field(default_factory=lambda: list(DEFAULT_WINDOWS))
    dry_run: bool = False


class FeedAnalysis(BaseModel):
    """Per-feed outcome recorded in the analysis report."""

    feed_id: str
    source_path: str
    status: str  # "analyzed" | "no_service" | "failed"
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
    windows: dict[str, str]  # name -> "HH:MM-HH:MM"
    feeds: list[FeedAnalysis]
    connections_by_type: dict[str, int]
    isolated_routes: list[str]
    graph: dict[str, Any]  # n_routes / n_edges / n_components / component_sizes


class _FeedData:
    """In-memory per-feed artifacts passed between stages (not serialized)."""

    def __init__(self) -> None:
        self.stop_headways = pd.DataFrame()
        self.rollup = pd.DataFrame()
        self.route_stops = pd.DataFrame()
        self.routes = pd.DataFrame()
        self.stops: Optional[gpd.GeoDataFrame] = None


def _route_metadata(feed: Any, feed_id: str, route_ids: pd.Series) -> pd.DataFrame:
    """Node attributes for analyzed routes (best effort from routes.txt)."""
    routes = pd.DataFrame({"route_id": route_ids.astype(str).unique()})
    routes["feed_id"] = feed_id
    routes["route_uid"] = feed_id + ":" + routes["route_id"]
    try:
        meta = feed.routes
        keep = [c for c in ("route_id", "route_short_name", "route_long_name", "route_type") if c in meta.columns]
        meta = meta[keep].drop_duplicates("route_id").copy()
        meta["route_id"] = meta["route_id"].astype(str)
        routes = routes.merge(meta, on="route_id", how="left")
    except Exception:  # noqa: BLE001 - metadata is cosmetic, never fatal
        log.warning("Feed %s: could not read routes.txt metadata", feed_id)
    return routes[["route_uid", "feed_id", "route_id"] + [c for c in routes.columns if c.startswith("route_") and c not in ("route_uid", "route_id")]]


def _analyze_feed(
    source: FeedSource, config: AnalysisConfig, engine: Optional[Engine]
) -> tuple[FeedAnalysis, _FeedData]:
    """Run stages 2a-2d for one feed. Never raises (failure boundary)."""
    data = _FeedData()
    result = FeedAnalysis(feed_id=source.feed_id, source_path=str(source.path), status="analyzed")
    try:
        feed, selection = load_service_feed(source, config.service_date)
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
        result.routes_analyzed = int(len(data.routes))
        result.stop_headway_rows = int(len(data.stop_headways))

        # Provenance columns for the DB tables.
        for frame in (data.stop_headways, data.rollup):
            if not frame.empty:
                frame.insert(0, "feed_id", source.feed_id)
                frame.insert(1, "service_date", selection.service_date)

        # Stops via the Phase 1 path: parent_station links + proximity input.
        raw = loader.load_feed(source)
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
    edges = connectivity.build_edges(route_stops, stations, stop_pairs)

    near = near_stations.build_near_stations(all_stops, route_stops, routes, stop_pairs)
    near_stations.write_near_stations(near, config.report_dir)

    graph = graph_export.build_graph(routes, edges)
    metrics = graph_export.graph_metrics(graph)
    graph_export.write_graphml(graph, config.report_dir / GRAPHML_NAME)

    if engine is not None:
        storage.replace_all_rows(engine, "route_connections", edges)

    # ---- report ------------------------------------------------------------
    by_type = edges["connection_type"].value_counts().to_dict() if not edges.empty else {}
    report = AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=config.dry_run,
        proximity_m=config.proximity_m,
        windows={w.name: w.label for w in config.windows},
        feeds=feed_results,
        connections_by_type={k: int(v) for k, v in by_type.items()},
        isolated_routes=metrics["isolated_routes"],
        graph={k: metrics[k] for k in ("n_routes", "n_edges", "n_components", "component_sizes")},
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
        f"Proximity threshold: {report.proximity_m:g} m",
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
    for kind in ("same_stop", "same_parent_station", "proximity"):
        lines.append(f"| {kind} | {report.connections_by_type.get(kind, 0)} |")

    lines += ["", "### Isolated routes (no connections)", ""]
    if report.isolated_routes:
        lines += [f"- `{r}`" for r in report.isolated_routes]
    else:
        lines.append("None - every analyzed route connects to at least one other.")
    lines += [
        "",
        f"GraphML export: `{GRAPHML_NAME}` (same directory).  ",
        f"Near-stations export: `{near_stations.NEAR_STATIONS_NAME}` "
        "(per stop: ID, name, lat, lon, and nearby stations as "
        "`[(ID, station, linea, time to reach), ...]`).",
        "",
    ]
    return "\n".join(lines)
