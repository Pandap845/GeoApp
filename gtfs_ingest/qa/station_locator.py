"""Station Locator: route geocoding + travel times (GetlocationApiGoogle.md).

Re-implements the Station Locator flow on this package's Phase 4 rails:
read a list of station names from a TXT/CSV file, resolve each name to
coordinates with **Text Search** (anchored to the previously resolved
station to disambiguate homonyms), fall back to the **Geocoding API**,
compute the travel time between consecutive stations with the **Distance
Matrix API** (or a Haversine estimate), and export the resulting directed
route to an Excel file plus the usual JSON + Markdown report.

Rails kept from the QA cross-check (same client, same guarantees):

- OFF by default: needs the explicit enable flag AND ``GOOGLE_MAPS_API_KEY``
  in the environment (no CLI flag for the key); otherwise a clean no-op.
- Hard ``--max-calls`` budget, rate limit, backoff, ``--dry-run`` (no API
  calls at all), and never crashing on API errors (``None`` sentinels keep
  the fallback chain going).
- NO database writes, ever. Unlike the QA verdict tables, this tool's
  entire purpose is exporting resolved names/coordinates for the user's
  route file — that Google-derived content goes to the export file ONLY,
  never to PostGIS (see the README licensing note before enabling).

Anchoring (duplicate-name disambiguation): the coordinates of the previous
successfully resolved station are passed as a Text Search location bias
circle of ``search_radius_m`` so Google ranks results near the route first.
The first station of a run has no anchor.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .google_client import CallBudgetExhausted, GooglePlacesClient

log = logging.getLogger(__name__)

LOCATOR_JSON = "station_locator_report.json"
LOCATOR_MD = "station_locator_report.md"

#: Text Search mask - exactly what the locator consumes, nothing more.
TEXT_SEARCH_FIELD_MASK: tuple[str, ...] = (
    "places.id",
    "places.displayName",
    "places.location",
)

#: GUI transport type -> (Distance Matrix mode, transit_mode).
TRANSPORT_MODE_MAP: dict[str, tuple[str, str]] = {
    "SITREN": ("transit", "train"),
    "MIMACRO": ("transit", "bus"),
    "TRONCAL": ("transit", "bus"),
    "ALIMENTADOR": ("transit", "bus"),
    "EXPRESO": ("transit", "bus"),
}
DEFAULT_TRANSIT_MODE: tuple[str, str] = ("transit", "bus")

EXCEL_HEADER: tuple[str, ...] = (
    "ID", "Nombre_Estación", "Modalidad", "Línea", "Latitud", "Longitud", "Estacion_Proxima",
)


class LocatorConfig(BaseModel):
    """Configuration of one locate-stations run.

    ``api_key`` is excluded from reprs; it must come from the environment
    (the CLI has no flag for it, by design — same rule as the QA phase).
    """

    stations_file: Optional[Path] = None
    report_dir: Path = Path("out")
    enabled: bool = False
    api_key: Optional[str] = Field(default=None, repr=False)
    transport_type: str = "SITREN"
    line: Optional[str] = None
    language: str = "es"
    region: str = "mx"
    search_radius_m: float = Field(default=3000.0, gt=0)
    use_distance_matrix: bool = True
    avg_speed_kmh: float = Field(default=25.0, gt=0)
    max_route_minutes: float = Field(default=60.0, gt=0)
    max_calls: int = Field(default=200, ge=1)
    rate_per_s: float = Field(default=5.0, ge=0)
    excel_name: str = "stations_route.xlsx"
    dry_run: bool = False


class StationRow(BaseModel):
    """One resolved (or unresolved) station of the route, in order."""

    index: int
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    source: Optional[str] = None        # "text_search" | "geocoding" | None
    next_name: Optional[str] = None     # directed edge: this -> next station
    next_minutes: Optional[float] = None
    next_source: Optional[str] = None   # "real" | "estimado" | "generico"


class LocatorReport(BaseModel):
    """Run summary: counts, call accounting, and unresolved names."""

    generated_at: str
    status: str  # "ran" | "dry_run" | "skipped_disabled" | "skipped_no_key"
    max_calls: int = 0
    calls_made: int = 0
    budget_remaining: int = 0
    stations_total: int = 0
    resolved_text_search: int = 0
    resolved_geocoding: int = 0
    unresolved: list[str] = Field(default_factory=list)
    edges_real: int = 0
    edges_estimated: int = 0
    edges_generic: int = 0
    excel_file: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Pure helpers (no I/O, no API)
# --------------------------------------------------------------------------

def read_station_list(path: Path) -> list[str]:
    """Station names from a TXT/CSV file (comma- and/or newline-separated).

    Order is preserved (it defines the route); blanks are dropped,
    duplicates are kept (a route may legitimately revisit a name).

    Raises:
        OSError: unreadable file.
    """
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        names.extend(part.strip() for part in line.split(",") if part.strip())
    return names


def build_query(station: str, transport_type: str, line: Optional[str]) -> str:
    """The Spanish Text Search query, exactly as the original app builds it."""
    query = f"Estación {transport_type} {station}".strip()
    if line:
        query += f" línea {line}"
    return query


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers."""
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def estimate_minutes(a: tuple[float, float], b: tuple[float, float], avg_speed_kmh: float) -> float:
    """Straight-line travel time estimate at the configured average speed."""
    return round(haversine_km(a[0], a[1], b[0], b[1]) / avg_speed_kmh * 60, 1)


def transit_mode_for(transport_type: str) -> tuple[str, str]:
    """Map the transport type to Distance Matrix ``(mode, transit_mode)``."""
    return TRANSPORT_MODE_MAP.get(transport_type.upper(), DEFAULT_TRANSIT_MODE)


# --------------------------------------------------------------------------
# Resolution (Text Search with anchor -> Geocoding fallback)
# --------------------------------------------------------------------------

def _resolve_station(
    client: GooglePlacesClient,
    config: LocatorConfig,
    station: str,
    anchor: Optional[tuple[float, float]],
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """One station -> ``(lat, lon, source)``; Nones when both APIs failed.

    Takes the FIRST Text Search result — the anchor bias is what makes the
    first result trustworthy. The Geocoding fallback prefers the name Text
    Search recommended (if any) over the raw constructed query.

    Raises:
        CallBudgetExhausted: budget ceiling reached mid-resolution.
    """
    query = build_query(station, config.transport_type, config.line)
    places = client.search_text(
        query,
        field_mask=TEXT_SEARCH_FIELD_MASK,
        lat=anchor[0] if anchor else None,
        lon=anchor[1] if anchor else None,
        radius_m=config.search_radius_m if anchor else None,
        language=config.language,
        region=config.region,
    )
    recommended: Optional[str] = None
    if places:
        recommended = places[0].name
        coords = places[0].coords
        if coords:
            return coords[0], coords[1], "text_search"
    coords = client.geocode(recommended or query, config.language, config.region)
    if coords:
        return coords[0], coords[1], "geocoding"
    return None, None, None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_station_locator(config: LocatorConfig) -> LocatorReport:
    """Execute (or cleanly skip) a locate-stations run.

    Returns:
        The report (also written to ``config.report_dir``). Skips are
        reported with a clear status, never raised.

    Raises:
        ValueError: enabled run without a readable stations file.
        OSError: run-level problems (report dir, export file).
    """
    now = datetime.now(timezone.utc)
    report = LocatorReport(
        generated_at=now.isoformat(timespec="seconds"),
        status="ran",
        max_calls=config.max_calls,
        budget_remaining=config.max_calls,
    )

    # ---- gating: OFF unless flag AND key are both present -----------------
    if not config.enabled:
        report.status = "skipped_disabled"
        report.notes.append("locate-stations is OFF by default; pass --enable-google-api to run it")
        log.info("Station locator skipped: not enabled (--enable-google-api absent)")
        _write(report, config.report_dir)
        return report
    if not config.api_key:
        report.status = "skipped_no_key"
        report.notes.append("GOOGLE_MAPS_API_KEY is not set in the environment; nothing was called")
        log.info("Station locator skipped: GOOGLE_MAPS_API_KEY not set")
        _write(report, config.report_dir)
        return report

    if not config.stations_file or not config.stations_file.is_file():
        raise ValueError("locate-stations requires an existing stations file (--stations)")
    stations = read_station_list(config.stations_file)
    if not stations:
        raise ValueError(f"no station names found in {config.stations_file}")
    report.stations_total = len(stations)

    if config.dry_run:
        report.status = "dry_run"
        pairs = max(0, len(stations) - 1) if config.use_distance_matrix else 0
        worst = 2 * len(stations) + pairs  # text search + geocode fallback + matrix
        report.notes.append(
            f"dry run: {len(stations)} station(s) read; worst case {worst} API call(s) "
            f"({len(stations)} Text Search + up to {len(stations)} Geocoding fallback(s)"
            f"{f' + {pairs} Distance Matrix' if pairs else ''}) against a budget of "
            f"{config.max_calls}; no API calls were made"
        )
        _write(report, config.report_dir)
        return report

    client = GooglePlacesClient(
        api_key=config.api_key, max_calls=config.max_calls, rate_per_s=config.rate_per_s
    )
    rows = [StationRow(index=i + 1, name=name) for i, name in enumerate(stations)]
    budget_hit = False
    try:
        # ---- (1) geocode every station first, updating the anchor ---------
        anchor: Optional[tuple[float, float]] = None  # reset at run start
        for row in rows:
            try:
                lat, lon, source = _resolve_station(client, config, row.name, anchor)
            except CallBudgetExhausted:
                budget_hit = True
                break
            row.lat, row.lon, row.source = lat, lon, source
            if lat is not None and lon is not None:
                anchor = (lat, lon)

        # ---- (2) directed edges between consecutive stations --------------
        use_matrix = config.use_distance_matrix and not budget_hit
        mode, transit_mode = transit_mode_for(config.transport_type)
        generic = round(config.max_route_minutes / len(stations), 1)
        for current, nxt in zip(rows, rows[1:]):
            current.next_name = nxt.name
            if None in (current.lat, current.lon, nxt.lat, nxt.lon):
                current.next_minutes, current.next_source = generic, "generico"
                continue
            origin = (current.lat, current.lon)
            destination = (nxt.lat, nxt.lon)
            minutes: Optional[float] = None
            if use_matrix:
                try:
                    minutes = client.travel_time_minutes(
                        origin, destination, mode, transit_mode,
                        config.language, config.region,
                    )
                except CallBudgetExhausted:
                    budget_hit = True
                    use_matrix = False  # estimate the remaining pairs
            if minutes is not None:
                current.next_minutes, current.next_source = minutes, "real"
            else:
                current.next_minutes = estimate_minutes(origin, destination, config.avg_speed_kmh)
                current.next_source = "estimado"
    finally:
        report.calls_made = client.calls_made
        report.budget_remaining = client.budget_remaining
        client.close()

    report.resolved_text_search = sum(1 for r in rows if r.source == "text_search")
    report.resolved_geocoding = sum(1 for r in rows if r.source == "geocoding")
    report.unresolved = [r.name for r in rows if r.source is None]
    report.edges_real = sum(1 for r in rows if r.next_source == "real")
    report.edges_estimated = sum(1 for r in rows if r.next_source == "estimado")
    report.edges_generic = sum(1 for r in rows if r.next_source == "generico")
    if budget_hit:
        report.notes.append(
            f"call budget of {config.max_calls} reached; unresolved stations and "
            "remaining pairs fell back to estimates"
        )
        log.warning("Station locator: %s", report.notes[-1])

    excel_path = config.report_dir / config.excel_name
    config.report_dir.mkdir(parents=True, exist_ok=True)
    _write_excel(rows, config, excel_path)
    report.excel_file = str(excel_path)
    report.notes.append(
        "the Excel export contains Google-resolved coordinates; it is a user "
        "export only and must not be loaded into the database (README licensing note)"
    )

    _write(report, config.report_dir)
    log.info(
        "Station locator complete: %d/%d station(s) resolved with %d call(s); "
        "%d real / %d estimated / %d generic edge time(s)",
        report.stations_total - len(report.unresolved), report.stations_total,
        report.calls_made, report.edges_real, report.edges_estimated, report.edges_generic,
    )
    return report


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------

def _write_excel(rows: list[StationRow], config: LocatorConfig, path: Path) -> None:
    """Excel export with the original app's columns; last station gets N/A."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Estaciones"
    sheet.append(list(EXCEL_HEADER))
    for row in rows:
        if row.next_name is not None and row.next_minutes is not None:
            proxima = f"({row.next_name}, {config.line or '-'}, {row.next_minutes}min)"
        else:
            proxima = "N/A"
        sheet.append(
            [row.index, row.name, config.transport_type, config.line or "", row.lat, row.lon, proxima]
        )
    workbook.save(path)
    log.info("Route exported to %s", path)


def _write(report: LocatorReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / LOCATOR_JSON).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / LOCATOR_MD).write_text(_markdown(report), encoding="utf-8")
    log.info("Station locator reports written to %s", out_dir)


def _markdown(report: LocatorReport) -> str:
    lines = [
        "# Station Locator Report",
        "",
        f"Generated: {report.generated_at}  ",
        f"Status: **{report.status}**  ",
        f"API calls: {report.calls_made} of {report.max_calls} "
        f"(remaining budget: {report.budget_remaining})",
        "",
        "## Resolution",
        "",
        f"- Stations in route: {report.stations_total}",
        f"- Resolved via Text Search: {report.resolved_text_search}",
        f"- Resolved via Geocoding fallback: {report.resolved_geocoding}",
        f"- Unresolved: {len(report.unresolved)}",
        "",
        "## Travel times (directed, consecutive pairs)",
        "",
        f"- Real (Distance Matrix): {report.edges_real}",
        f"- Estimated (Haversine / avg speed): {report.edges_estimated}",
        f"- Generic (missing coordinates): {report.edges_generic}",
        "",
    ]
    if report.excel_file:
        lines += [f"Route export: `{report.excel_file}`", ""]
    if report.unresolved:
        lines += ["## Unresolved stations", ""]
        lines += [f"- {name}" for name in report.unresolved]
        lines.append("")
    if report.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in report.notes]
    lines.append("")
    return "\n".join(lines)
