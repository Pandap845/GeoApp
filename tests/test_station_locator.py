"""Station locator flow + legacy client endpoints. ALL responses are
mocked - this suite performs no live API calls."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gtfs_ingest.qa.google_client import CallBudgetExhausted, GooglePlacesClient
from gtfs_ingest.qa.station_locator import (
    LocatorConfig,
    build_query,
    estimate_minutes,
    haversine_km,
    read_station_list,
    run_station_locator,
    transit_mode_for,
)

API_KEY = "test-key"


def make_client(handler, max_calls=20) -> GooglePlacesClient:
    return GooglePlacesClient(
        api_key=API_KEY,
        max_calls=max_calls,
        rate_per_s=0,           # no throttling in tests
        backoff_base=0,         # no backoff sleeps in tests
        transport=httpx.MockTransport(handler),
    )


def text_search_response(name="Estación Central", lat=20.67, lng=-103.35, pid="p1"):
    return {
        "places": [
            {
                "id": pid,
                "displayName": {"text": name, "languageCode": "es"},
                "location": {"latitude": lat, "longitude": lng},
            }
        ]
    }


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_read_station_list_mixes_commas_and_newlines(tmp_path: Path) -> None:
    stations = tmp_path / "route.txt"
    stations.write_text("Central, Juárez\n\n  Periférico Sur \nCentral", encoding="utf-8")
    assert read_station_list(stations) == ["Central", "Juárez", "Periférico Sur", "Central"]


def test_build_query_matches_original_app() -> None:
    assert build_query("Central", "SITREN", None) == "Estación SITREN Central"
    assert build_query("Central", "SITREN", "2") == "Estación SITREN Central línea 2"


def test_transit_mode_mapping() -> None:
    assert transit_mode_for("SITREN") == ("transit", "train")
    assert transit_mode_for("MiMacro") == ("transit", "bus")
    assert transit_mode_for("unknown-system") == ("transit", "bus")


def test_haversine_and_estimate() -> None:
    # ~1 degree of latitude is ~111 km; at 25 km/h that's ~267 minutes.
    km = haversine_km(20.0, -103.0, 21.0, -103.0)
    assert 110 < km < 112
    minutes = estimate_minutes((20.0, -103.0), (21.0, -103.0), 25.0)
    assert 260 < minutes < 270


# --------------------------------------------------------------------------
# Client: legacy endpoints share the budget and redaction rules
# --------------------------------------------------------------------------

def test_geocode_parses_first_result_and_sends_key_param() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [{"geometry": {"location": {"lat": 20.5, "lng": -103.4}}}],
            },
        )

    client = make_client(handler)
    assert client.geocode("Estación Central", "es", "mx") == (20.5, -103.4)
    assert seen["params"]["key"] == API_KEY
    assert seen["params"]["language"] == "es"
    assert seen["params"]["region"] == "mx"
    assert client.calls_made == 1


def test_geocode_non_ok_status_returns_none() -> None:
    def handler(request):
        return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})

    assert make_client(handler).geocode("nowhere") is None


def test_travel_time_transit_sends_departure_time_and_rounds_minutes() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "rows": [{"elements": [{"status": "OK", "duration": {"value": 330}}]}],
            },
        )

    client = make_client(handler)
    minutes = client.travel_time_minutes((20.0, -103.0), (20.1, -103.1), "transit", "train")
    assert minutes == 5.5
    assert seen["params"]["transit_mode"] == "train"
    assert seen["params"]["departure_time"] == "now"


def test_travel_time_element_not_ok_returns_none() -> None:
    def handler(request):
        return httpx.Response(
            200, json={"status": "OK", "rows": [{"elements": [{"status": "ZERO_RESULTS"}]}]}
        )

    assert make_client(handler).travel_time_minutes((0, 0), (1, 1)) is None


def test_legacy_calls_respect_the_shared_budget() -> None:
    def handler(request):
        return httpx.Response(200, json={"status": "OK", "results": []})

    client = make_client(handler, max_calls=1)
    client.geocode("one")
    with pytest.raises(CallBudgetExhausted):
        client.geocode("two")
    assert client.calls_made == 1


# --------------------------------------------------------------------------
# End-to-end flow (patched client transport - no network)
# --------------------------------------------------------------------------

def run_locator(tmp_path, handler, stations="Central, Juárez, Periférico", **overrides):
    """Run the locator against a MockTransport by patching the client class."""
    stations_file = tmp_path / "route.txt"
    stations_file.write_text(stations, encoding="utf-8")
    config = LocatorConfig(
        stations_file=stations_file,
        report_dir=tmp_path,
        enabled=True,
        api_key=API_KEY,
        rate_per_s=0,
        **overrides,
    )
    import gtfs_ingest.qa.station_locator as sl

    original = sl.GooglePlacesClient

    def patched(**kwargs):
        return original(
            **kwargs, backoff_base=0, transport=httpx.MockTransport(handler)
        )

    sl.GooglePlacesClient = patched
    try:
        return run_station_locator(config)
    finally:
        sl.GooglePlacesClient = original


def test_full_run_anchoring_and_real_times(tmp_path: Path) -> None:
    requests_seen: list[dict] = []
    coords = iter([(20.60, -103.30), (20.61, -103.31), (20.62, -103.32)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":searchText"):
            body = json.loads(request.content)
            requests_seen.append(body)
            lat, lng = next(coords)
            return httpx.Response(200, json=text_search_response(lat=lat, lng=lng))
        if "/distancematrix/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "rows": [{"elements": [{"status": "OK", "duration": {"value": 300}}]}],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    report = run_locator(tmp_path, handler, transport_type="SITREN", line="2")

    # First query is unanchored; the next ones carry the previous coords.
    assert "locationBias" not in requests_seen[0]
    assert requests_seen[0]["textQuery"] == "Estación SITREN Central línea 2"
    bias = requests_seen[1]["locationBias"]["circle"]
    assert bias["center"] == {"latitude": 20.60, "longitude": -103.30}
    assert bias["radius"] == 3000.0

    assert report.status == "ran"
    assert report.resolved_text_search == 3
    assert report.unresolved == []
    assert report.edges_real == 2
    assert report.calls_made == 5  # 3 text searches + 2 matrix elements

    from openpyxl import load_workbook

    sheet = load_workbook(tmp_path / "stations_route.xlsx").active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0] == ("ID", "Nombre_Estación", "Modalidad", "Línea",
                       "Latitud", "Longitud", "Estacion_Proxima")
    assert rows[1][:4] == (1, "Central", "SITREN", "2")
    assert rows[1][6] == "(Juárez, 2, 5.0min)"
    assert rows[3][6] == "N/A"  # last station has no next


def test_geocoding_fallback_when_text_search_has_no_location(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":searchText"):
            # A recommendation without coordinates -> forces the fallback.
            return httpx.Response(
                200, json={"places": [{"id": "p1", "displayName": {"text": "Estación Central"}}]}
            )
        if "/geocode/" in request.url.path:
            assert request.url.params["address"] == "Estación Central"  # recommended name
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [{"geometry": {"location": {"lat": 20.5, "lng": -103.4}}}],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    report = run_locator(tmp_path, handler, stations="Central", use_distance_matrix=False)
    assert report.resolved_geocoding == 1
    assert report.unresolved == []


def test_estimated_times_without_distance_matrix(tmp_path: Path) -> None:
    coords = iter([(20.0, -103.0), (20.1, -103.0)])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":searchText")  # matrix must not be called
        lat, lng = next(coords)
        return httpx.Response(200, json=text_search_response(lat=lat, lng=lng))

    report = run_locator(
        tmp_path, handler, stations="A, B", use_distance_matrix=False, avg_speed_kmh=25.0
    )
    assert report.edges_real == 0
    assert report.edges_estimated == 1
    assert report.calls_made == 2


def test_budget_exhaustion_degrades_to_estimates(tmp_path: Path) -> None:
    coords = iter([(20.0, -103.0), (20.1, -103.0), (20.2, -103.0)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":searchText"):
            lat, lng = next(coords)
            return httpx.Response(200, json=text_search_response(lat=lat, lng=lng))
        raise AssertionError("budget must be exhausted before any matrix call")

    # Budget covers the 3 text searches only; both pairs fall back to estimates.
    report = run_locator(tmp_path, handler, stations="A, B, C", max_calls=3)
    assert report.resolved_text_search == 3
    assert report.edges_estimated == 2
    assert any("budget" in note for note in report.notes)


def test_unresolved_station_gets_generic_edge_time(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":searchText"):
            body = json.loads(request.content)
            if "Fantasma" in body["textQuery"]:
                return httpx.Response(200, json={"places": []})
            return httpx.Response(200, json=text_search_response())
        if "/geocode/" in request.url.path:
            return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        raise AssertionError(f"unexpected request: {request.url}")

    report = run_locator(
        tmp_path, handler, stations="Central, Fantasma",
        use_distance_matrix=False, max_route_minutes=60.0,
    )
    assert report.unresolved == ["Fantasma"]
    assert report.edges_generic == 1  # 60 / 2 stations = 30 min generic estimate


# --------------------------------------------------------------------------
# Gating and dry run: no network, clean statuses
# --------------------------------------------------------------------------

def test_noop_when_disabled(tmp_path: Path) -> None:
    report = run_station_locator(LocatorConfig(enabled=False, report_dir=tmp_path))
    assert report.status == "skipped_disabled"
    assert report.calls_made == 0
    assert (tmp_path / "station_locator_report.json").is_file()


def test_noop_when_key_missing(tmp_path: Path) -> None:
    report = run_station_locator(
        LocatorConfig(enabled=True, api_key=None, report_dir=tmp_path)
    )
    assert report.status == "skipped_no_key"
    assert report.calls_made == 0


def test_dry_run_reads_stations_but_makes_no_calls(tmp_path: Path) -> None:
    stations = tmp_path / "route.txt"
    stations.write_text("A, B, C", encoding="utf-8")
    report = run_station_locator(
        LocatorConfig(
            enabled=True, api_key=API_KEY, stations_file=stations,
            report_dir=tmp_path, dry_run=True,
        )
    )
    assert report.status == "dry_run"
    assert report.stations_total == 3
    assert report.calls_made == 0
    assert not (tmp_path / "stations_route.xlsx").exists()


def test_missing_stations_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stations file"):
        run_station_locator(
            LocatorConfig(
                enabled=True, api_key=API_KEY,
                stations_file=tmp_path / "missing.txt", report_dir=tmp_path,
            )
        )


def test_cli_noop_without_flag(tmp_path: Path, monkeypatch) -> None:
    from gtfs_ingest import cli

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    stations = tmp_path / "route.txt"
    stations.write_text("Central", encoding="utf-8")
    exit_code = cli.main(
        ["locate-stations", "--stations", str(stations), "--report", str(tmp_path)]
    )
    assert exit_code == 0
    assert (tmp_path / "station_locator_report.json").is_file()
