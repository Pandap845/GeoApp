"""Station locator: turn a list of station names into a route with coordinates and times.

Each name is resolved with Text Search, biased toward the previous station so
namesakes elsewhere lose; Geocoding is the fallback. Travel times come from
Distance Matrix, else a straight-line estimate. Output: an Excel route plus
JSON/Markdown reports. Same rules as Google QA (opt-in, budget, dry run), and
nothing is ever written to the database. See docs/guide/google-apis.md.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, Field

from .google_client import CallBudgetExhausted, GooglePlacesClient

log = logging.getLogger(__name__)

LOCATOR_JSON = "station_locator_report.json"
LOCATOR_MD = "station_locator_report.md"

#: Only the fields the locator uses.
TEXT_SEARCH_FIELD_MASK: tuple[str, ...] = (
    "places.id",
    "places.displayName",
    "places.location",
)

#: Transport type -> (Distance Matrix mode, transit_mode).
TRANSPORT_MODE_MAP: dict[str, tuple[str, str]] = {
    "SITREN": ("transit", "train"),
    "MIMACRO": ("transit", "bus"),
    "TRONCAL": ("transit", "bus"),
    "ALIMENTADOR": ("transit", "bus"),
    "EXPRESO": ("transit", "bus"),
}
DEFAULT_TRANSIT_MODE: tuple[str, str] = ("transit", "bus")

EXCEL_HEADER: tuple[str, ...] = (
    "ID",
    "Nombre_Estación",
    "Modalidad",
    "Línea",
    "Latitud",
    "Longitud",
    "Estacion_Proxima",
)


class LocatorConfig(BaseModel):
    """Configuration of one locate-stations run (api_key comes from the environment only)."""

    stations_file: Path | None = None
    report_dir: Path = Path("out")
    enabled: bool = False
    api_key: str | None = Field(default=None, repr=False)
    transport_type: str = "SITREN"
    line: str | None = None
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
    lat: float | None = None
    lon: float | None = None
    source: str | None = None  # "text_search" | "geocoding" | None
    next_name: str | None = None  # directed edge: this -> next station
    next_minutes: float | None = None
    next_source: str | None = None  # "real" | "estimado" | "generico"


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
    excel_file: str | None = None
    notes: list[str] = Field(default_factory=list)


def read_station_list(path: Path) -> list[str]:
    """Station names in file order (comma or newline separated); repeats are kept."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        names.extend(part.strip() for part in line.split(",") if part.strip())
    return names


def build_query(station: str, transport_type: str, line: str | None) -> str:
    """Spanish Text Search query, as the original app builds it."""
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
    """Minutes at *avg_speed_kmh* along the straight line."""
    return round(haversine_km(a[0], a[1], b[0], b[1]) / avg_speed_kmh * 60, 1)


def transit_mode_for(transport_type: str) -> tuple[str, str]:
    """Distance Matrix (mode, transit_mode) for a transport type."""
    return TRANSPORT_MODE_MAP.get(transport_type.upper(), DEFAULT_TRANSIT_MODE)


def _resolve_station(
    client: GooglePlacesClient,
    config: LocatorConfig,
    station: str,
    anchor: tuple[float, float] | None,
) -> tuple[float | None, float | None, str | None]:
    """Return (lat, lon, source) for one station, or Nones if both APIs fail.

    Uses the first Text Search hit (the bias toward the anchor makes it reliable).
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
    recommended: str | None = None
    if places:
        recommended = places[0].name
        coords = places[0].coords
        if coords:
            return coords[0], coords[1], "text_search"
    coords = client.geocode(recommended or query, config.language, config.region)
    if coords:
        return coords[0], coords[1], "geocoding"
    return None, None, None


def run_station_locator(config: LocatorConfig) -> LocatorReport:
    """Run or skip the locator and write its reports.

    Raises:
        ValueError: enabled without a readable stations file.
    """
    now = datetime.now(UTC)
    report = LocatorReport(
        generated_at=now.isoformat(timespec="seconds"),
        status="ran",
        max_calls=config.max_calls,
        budget_remaining=config.max_calls,
    )

    # Off unless both the flag and the key are present.
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
        # Resolve every station; each success becomes the next anchor.
        anchor: tuple[float, float] | None = None
        for row in rows:
            try:
                lat, lon, source = _resolve_station(client, config, row.name, anchor)
            except CallBudgetExhausted:
                budget_hit = True
                break
            row.lat, row.lon, row.source = lat, lon, source
            if lat is not None and lon is not None:
                anchor = (lat, lon)

        # Travel time from each station to the next.
        use_matrix = config.use_distance_matrix and not budget_hit
        mode, transit_mode = transit_mode_for(config.transport_type)
        generic = round(config.max_route_minutes / len(stations), 1)
        for current, nxt in pairwise(rows):
            current.next_name = nxt.name
            if None in (current.lat, current.lon, nxt.lat, nxt.lon):
                current.next_minutes, current.next_source = generic, "generico"
                continue
            origin = (current.lat, current.lon)
            destination = (nxt.lat, nxt.lon)
            minutes: float | None = None
            if use_matrix:
                try:
                    minutes = client.travel_time_minutes(
                        origin,
                        destination,
                        mode,
                        transit_mode,
                        config.language,
                        config.region,
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
        report.stations_total - len(report.unresolved),
        report.stations_total,
        report.calls_made,
        report.edges_real,
        report.edges_estimated,
        report.edges_generic,
    )
    return report


def _write_excel(rows: list[StationRow], config: LocatorConfig, path: Path) -> None:
    """Excel route with the original app's columns; the last station gets N/A."""
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
            [
                row.index,
                row.name,
                config.transport_type,
                config.line or "",
                row.lat,
                row.lon,
                proxima,
            ]
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
        (
            f"API calls: {report.calls_made} of {report.max_calls} "
            f"(remaining budget: {report.budget_remaining})"
        ),
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
