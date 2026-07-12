"""Phase 3 orchestration: detect-interchanges and conflate-osm.

``run_interchanges`` (Part A): feeds -> normalized stops + route/stop maps
(Phase 1/2 code reused) -> DBSCAN + parent_station clusters -> mode sets ->
transfers.txt cross-check -> ``interchanges`` table + report. Clustering
uses the *full* feed (all trips regardless of service day): an interchange
is infrastructure, not a schedule property.

``run_conflation`` (Parts B + C): stops already in PostGIS + the OSM layers
in the same database -> nearest transit node match with confidence, and
nearest highway centerline with side-of-street. A database is required
even with ``--dry-run`` (OSM is the reference source); dry-run only skips
the table writes.

Both follow the same failure contract as earlier phases: per-feed problems
are recorded and skipped; only run-level problems (bad DB, missing OSM
layers, unwritable report dir) abort.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from .. import db, loader, normalizer
from ..analysis import dedupe
from ..analysis.dedupe import DEDUPE_DISTANCE_M, NAME_SIM_THRESHOLD
from ..analysis.connectivity import route_stop_map
from . import clustering, coverage, osm_match, side_of_street, storage
from .transfers_check import TransferCheck, check_transfers

log = logging.getLogger(__name__)

INTERCHANGE_JSON = "interchanges_report.json"
INTERCHANGE_MD = "interchanges_report.md"
CONFLATION_JSON = "conflation_report.json"
CONFLATION_MD = "conflation_report.md"


# --------------------------------------------------------------------------
# Configs
# --------------------------------------------------------------------------

class InterchangeConfig(BaseModel):
    """Configuration of one detect-interchanges run."""

    feed_paths: list[Path]
    database_url: Optional[str] = None
    report_dir: Path = Path("out")
    eps_m: float = Field(default=120.0, gt=0)
    min_samples: int = Field(default=2, ge=1)
    metric_crs: Optional[str] = None
    dedupe: bool = True
    dedupe_distance_m: float = Field(default=DEDUPE_DISTANCE_M, gt=0)
    dedupe_name_threshold: float = Field(default=NAME_SIM_THRESHOLD, ge=0.0, le=1.0)
    dry_run: bool = False


class ConflationConfig(BaseModel):
    """Configuration of one conflate-osm run."""

    database_url: Optional[str] = None
    report_dir: Path = Path("out")
    match_threshold_m: float = Field(default=50.0, gt=0)
    street_threshold_m: float = Field(default=50.0, gt=0)
    osm_line_table: str = "planet_osm_line"
    osm_point_table: str = "planet_osm_point"
    osm_geom_column: str = "way"
    metric_crs: Optional[str] = None
    coverage_radius_m: float = Field(default=100.0, gt=0)
    coverage_buffer_m: float = Field(default=500.0, gt=0)
    skip_coverage: bool = False
    dry_run: bool = False


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

class FeedNote(BaseModel):
    """Per-feed outcome in the interchange report."""

    feed_id: str
    status: str  # "ok" | "failed"
    stops: int = 0
    routes: int = 0
    has_transfers_txt: bool = False
    notes: list[str] = Field(default_factory=list)


class InterchangeReport(BaseModel):
    """Top-level detect-interchanges report (serialized to JSON as-is)."""

    generated_at: str
    dry_run: bool
    eps_m: float
    min_samples: int
    dedupe: bool = True
    equivalence_groups: int = 0       # cross-feed alias groups found
    stops_deduplicated: int = 0       # stops belonging to some alias group
    single_physical_station_clusters: int = 0  # clusters that are one station's aliases
    feeds: list[FeedNote]
    clusters_total: int = 0
    multimodal_count: int = 0
    single_mode_count: int = 0
    mode_combinations: dict[str, int] = Field(default_factory=dict)
    transfers: TransferCheck = Field(default_factory=TransferCheck)


class ConflationReport(BaseModel):
    """Top-level conflate-osm report (serialized to JSON as-is)."""

    generated_at: str
    dry_run: bool
    match_threshold_m: float
    street_threshold_m: float
    osm_line_table: str
    osm_point_table: str
    stops_total: int = 0
    matched: int = 0
    match_rate: float = 0.0
    needs_review: int = 0
    side_distribution: dict[str, int] = Field(default_factory=dict)
    side_low_confidence: int = 0
    stops_without_street: int = 0
    coverage_radius_m: Optional[float] = None
    coverage_buffer_m: Optional[float] = None
    osm_only_stops: int = 0
    osm_only_by_kind: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Part A - interchanges
# --------------------------------------------------------------------------

def _collect_feed(source: Any) -> tuple[FeedNote, dict[str, pd.DataFrame | None]]:
    """Load one feed's stops/route maps/transfers with failure isolation."""
    note = FeedNote(feed_id=source.feed_id, status="ok")
    data: dict[str, pd.DataFrame | None] = {"stops": None, "route_stops": None, "routes": None, "transfers": None}
    try:
        loaded = loader.load_feed(source)
        if loaded.load_error:
            raise RuntimeError(loaded.load_error)
        stops, _ = normalizer.normalize_stops(loaded)
        data["stops"] = stops
        data["route_stops"] = route_stop_map(loaded.feed, source.feed_id)

        routes_raw = loaded.table("routes.txt")
        if routes_raw is not None and not routes_raw.empty and "route_id" in routes_raw.columns:
            routes = routes_raw.drop_duplicates("route_id").copy()
            routes["route_uid"] = source.feed_id + ":" + routes["route_id"].astype(str)
            if "route_type" not in routes.columns:
                routes["route_type"] = None
                note.notes.append("routes.txt lacks route_type; modes unknown")
            # route_short_name feeds the dedupe shared-route evidence.
            keep = ["route_uid", "route_type"]
            if "route_short_name" in routes.columns:
                keep.append("route_short_name")
            data["routes"] = routes[keep]

        transfers = loaded.table("transfers.txt")
        data["transfers"] = transfers if "transfers.txt" in loaded.files_present else None
        note.has_transfers_txt = data["transfers"] is not None
        note.stops = int(len(stops))
        note.routes = int(len(data["routes"])) if data["routes"] is not None else 0
    except Exception as exc:  # noqa: BLE001 - per-feed isolation
        log.exception("Feed %s: interchange collection failed", source.feed_id)
        note.status = "failed"
        note.notes.append(f"{type(exc).__name__}: {exc}")
    return note, data


def run_interchanges(config: InterchangeConfig) -> InterchangeReport:
    """Execute Part A end to end and write outputs.

    Raises:
        ValueError: non-dry run without a database URL.
        RuntimeError / sqlalchemy errors / OSError: run-level problems.
    """
    sources = loader.discover_feeds(config.feed_paths)
    engine: Optional[Engine] = None
    if not config.dry_run:
        if not config.database_url:
            raise ValueError("No database URL configured; pass --db-url / DATABASE_URL or use --dry-run")
        engine = db.make_engine(config.database_url)
        storage.ensure_conflation_schema(engine)

    notes: list[FeedNote] = []
    all_data: list[dict] = []
    for source in sources:
        note, data = _collect_feed(source)
        notes.append(note)
        all_data.append(data)

    def _concat(key: str, columns: list[str]) -> pd.DataFrame:
        frames = [d[key] for d in all_data if d[key] is not None and len(d[key])]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)

    stops = _concat("stops", ["stop_uid", "feed_id", "stop_id", "parent_station"])
    route_stops = _concat("route_stops", ["feed_id", "route_id", "route_uid", "stop_id", "stop_uid"])
    routes = _concat("routes", ["route_uid", "route_type"])

    # Cross-feed station dedupe: recomputed here (not read from the Phase 2
    # table) so the pipelines stay independently runnable; distance + name
    # evidence is schedule-independent, so results match Phase 2 except for
    # pairs rescued only by shared routes (full feed here vs service-day
    # filtered there).
    canonical = pd.DataFrame(columns=dedupe.CANONICAL_COLUMNS)
    if config.dedupe and len(stops):
        accepted = dedupe.find_equivalences(
            stops, route_stops, routes,
            config.dedupe_distance_m, config.dedupe_name_threshold,
        )
        canonical = dedupe.canonical_map(accepted)

    labels = clustering.cluster_stops(
        stops, config.eps_m, config.min_samples, config.metric_crs, canonical
    ) if len(stops) else pd.Series(dtype=int).to_numpy()
    clusters = clustering.build_interchanges(stops, labels, route_stops, routes, canonical)

    transfers_by_feed = {
        note.feed_id: data["transfers"] for note, data in zip(notes, all_data) if note.status == "ok"
    }
    transfers = check_transfers(transfers_by_feed, list(clusters["member_stop_uids"]) if not clusters.empty else [])

    if engine is not None:
        storage.load_interchanges(engine, clusters)

    combos: dict[str, int] = {}
    if not clusters.empty:
        combos = clusters["mode_names"].map(lambda names: "+".join(sorted(names)) or "(unknown)") \
            .value_counts().to_dict()
    report = InterchangeReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=config.dry_run,
        eps_m=config.eps_m,
        min_samples=config.min_samples,
        dedupe=config.dedupe,
        equivalence_groups=int(canonical["group_id"].nunique()) if not canonical.empty else 0,
        stops_deduplicated=int(len(canonical)),
        single_physical_station_clusters=(
            int((clusters["physical_stop_count"] == 1).sum()) if not clusters.empty else 0
        ),
        feeds=notes,
        clusters_total=int(len(clusters)),
        multimodal_count=int(clusters["is_multimodal"].sum()) if not clusters.empty else 0,
        single_mode_count=int((~clusters["is_multimodal"]).sum()) if not clusters.empty else 0,
        mode_combinations={k: int(v) for k, v in combos.items()},
        transfers=transfers,
    )
    _write(report, config.report_dir, INTERCHANGE_JSON, INTERCHANGE_MD, _interchange_markdown)
    log.info(
        "Interchange detection complete: %d clusters (%d multimodal), transfers agreement %d/%d",
        report.clusters_total, report.multimodal_count,
        transfers.declared_co_clustered, transfers.declared_pairs,
    )
    return report


# --------------------------------------------------------------------------
# Parts B + C - conflation
# --------------------------------------------------------------------------

def run_conflation(config: ConflationConfig) -> ConflationReport:
    """Execute Parts B and C end to end and write outputs.

    Raises:
        ValueError: no database URL (a DB is required even for dry runs -
            OSM in the database is the reference source).
        RuntimeError: missing/unusable OSM layers (fail fast, with a
            message naming exactly what is missing).
        sqlalchemy errors / OSError: run-level problems.
    """
    if not config.database_url:
        raise ValueError(
            "conflate-osm requires a database (OSM is read from PostGIS even "
            "with --dry-run); pass --db-url or set DATABASE_URL"
        )
    engine = db.make_engine(config.database_url)
    schema = osm_match.validate_osm_schema(
        engine, config.osm_line_table, config.osm_point_table, config.osm_geom_column
    )
    if not config.dry_run:
        storage.ensure_conflation_schema(engine)

    matches = osm_match.score_matches(
        osm_match.nearest_transit_nodes(engine, schema), config.match_threshold_m
    )
    sides = side_of_street.compute_sides(
        side_of_street.nearest_streets(engine, schema),
        config.street_threshold_m,
        config.metric_crs,
    )

    if not config.dry_run:
        storage.load_osm_matches(engine, matches)
        storage.load_sides(engine, sides)

    # Part D - reverse coverage: OSM transit nodes with no GTFS stop nearby.
    gaps = coverage.rows_to_frame([])
    if not config.skip_coverage:
        gaps = coverage.osm_only_stops(
            engine, schema, config.coverage_radius_m, config.coverage_buffer_m
        )
        coverage.write_coverage_gpkg(gaps, config.report_dir)

    matched = int((~matches["needs_review"]).sum()) if not matches.empty else 0
    total = int(len(matches))
    side_counts = sides["side"].fillna("(no street)").value_counts().to_dict() if not sides.empty else {}
    report = ConflationReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=config.dry_run,
        match_threshold_m=config.match_threshold_m,
        street_threshold_m=config.street_threshold_m,
        osm_line_table=config.osm_line_table,
        osm_point_table=config.osm_point_table,
        stops_total=total,
        matched=matched,
        match_rate=round(matched / total, 3) if total else 0.0,
        needs_review=int(matches["needs_review"].sum()) if not matches.empty else 0,
        side_distribution={str(k): int(v) for k, v in side_counts.items()},
        side_low_confidence=int((sides["side_confidence"] == "low").sum()) if not sides.empty else 0,
        stops_without_street=int(sides["side"].isna().sum()) if not sides.empty else 0,
        coverage_radius_m=None if config.skip_coverage else config.coverage_radius_m,
        coverage_buffer_m=None if config.skip_coverage else config.coverage_buffer_m,
        osm_only_stops=int(len(gaps)),
        osm_only_by_kind={str(k): int(v) for k, v in gaps["kind"].fillna("(untagged)").value_counts().items()} if not gaps.empty else {},
    )
    _write(report, config.report_dir, CONFLATION_JSON, CONFLATION_MD, _conflation_markdown)
    log.info(
        "Conflation complete: %d/%d stops matched (%d need review); side resolved for %d stops",
        matched, total, report.needs_review, total - report.stops_without_street,
    )
    return report


# --------------------------------------------------------------------------
# Report writers
# --------------------------------------------------------------------------

def _write(report: BaseModel, out_dir: Path, json_name: str, md_name: str, renderer) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / json_name).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / md_name).write_text(renderer(report), encoding="utf-8")
    log.info("Reports written: %s, %s", out_dir / json_name, out_dir / md_name)


def _interchange_markdown(report: InterchangeReport) -> str:
    t = report.transfers
    lines = [
        "# Multimodal Interchange Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Mode: {'dry run (no DB writes)' if report.dry_run else 'full run'}  ",
        f"DBSCAN: eps = {report.eps_m:g} m, min_samples = {report.min_samples} "
        "(parent_station groups always merged)  ",
        "Station deduplication: "
        + (
            f"on - {report.equivalence_groups} cross-feed group(s), "
            f"{report.stops_deduplicated} stop(s) merged"
            if report.dedupe
            else "off (--no-dedupe)"
        ),
        "",
        "## Feeds",
        "",
        "| Feed | Status | Stops | Routes | transfers.txt | Notes |",
        "| --- | --- | ---: | ---: | :-: | --- |",
        *[
            f"| {f.feed_id} | {f.status} | {f.stops} | {f.routes} "
            f"| {'Y' if f.has_transfers_txt else '-'} | {'; '.join(f.notes) or '-'} |"
            for f in report.feeds
        ],
        "",
        "## Clusters",
        "",
        f"Total: {report.clusters_total} · Multimodal: {report.multimodal_count} · "
        f"Single-mode: {report.single_mode_count} · "
        f"Single physical station (alias-only): {report.single_physical_station_clusters}",
        "",
        "| Mode combination | Clusters |",
        "| --- | ---: |",
        *[f"| {combo} | {count} |" for combo, count in sorted(report.mode_combinations.items())],
        "",
        "## Inferred vs declared transfers",
        "",
        f"- Feeds with transfers.txt: {', '.join(t.feeds_with_transfers) or '(none)'}",
        f"- Feeds without transfers.txt: {', '.join(t.feeds_without_transfers) or '(none)'}",
        f"- Declared transfer pairs: {t.declared_pairs}",
        f"- Declared AND inferred (same cluster): {t.declared_co_clustered}",
        f"- Declared but NOT inferred: {t.declared_not_co_clustered}",
        f"- Inferred clusters backed by a declared transfer: {t.clusters_with_declared}",
        f"- Inferred clusters with no declared transfer: {t.clusters_without_declared}",
    ]
    if t.not_inferred_examples:
        lines += ["", "Declared-but-not-inferred examples:", ""]
        lines += [f"- `{example}`" for example in t.not_inferred_examples]
    lines.append("")
    return "\n".join(lines)


def _conflation_markdown(report: ConflationReport) -> str:
    lines = [
        "# OSM Conflation Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Mode: {'dry run (no DB writes)' if report.dry_run else 'full run'}  ",
        f"OSM layers: `{report.osm_line_table}` / `{report.osm_point_table}`  ",
        f"Thresholds: match {report.match_threshold_m:g} m · street {report.street_threshold_m:g} m",
        "",
        "## Stop -> OSM transit node matching",
        "",
        f"- Stops: {report.stops_total}",
        f"- Matched (confidence >= 0.5, within threshold): {report.matched} "
        f"({report.match_rate:.1%})",
        f"- Needs review: {report.needs_review}",
        "",
        "## Side of street",
        "",
        "| Side | Stops |",
        "| --- | ---: |",
        *[f"| {side} | {count} |" for side, count in sorted(report.side_distribution.items())],
        "",
        f"Low confidence: {report.side_low_confidence} · "
        f"No highway within threshold: {report.stops_without_street}",
        "",
    ]
    if report.coverage_radius_m is not None:
        lines += [
            "## Reverse coverage (OSM transit nodes without a GTFS stop)",
            "",
            f"Service area: stops hull + {report.coverage_buffer_m:g} m buffer · "
            f"covered when a stop is within {report.coverage_radius_m:g} m",
            "",
            f"- OSM-only transit nodes: **{report.osm_only_stops}**",
        ]
        for kind, count in sorted(report.osm_only_by_kind.items()):
            lines.append(f"  - {kind}: {count}")
        if report.osm_only_stops:
            lines.append(
                f"- Reviewable layer: `{coverage.COVERAGE_GPKG}` "
                f"(layer `{coverage.COVERAGE_LAYER}`) - coverage gaps in the "
                "feeds, or OSM noise."
            )
        lines.append("")
    return "\n".join(lines)
