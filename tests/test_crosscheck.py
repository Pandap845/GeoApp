"""Phase 4 verdict logic + client behavior. ALL responses are mocked -
this suite performs no live API calls."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gtfs_ingest.qa.crosscheck import (
    INTERCHANGE_FIELD_MASK,
    STOP_FIELD_MASK,
    extract_vehicle_types,
    google_mode_set,
    interchange_verdict,
    stop_verdict,
)
from gtfs_ingest.qa.google_client import (
    CallBudgetExhausted,
    GooglePlacesClient,
    PlaceResult,
    field_names_only,
)
from gtfs_ingest.qa.pipeline_phase4 import QaConfig, run_google_qa


def place(pid="p1", types=(), transit=None) -> PlaceResult:
    return PlaceResult.model_validate({"id": pid, "types": list(types), "transitStation": transit})


# Mode extraction
def test_vehicle_types_extracted_from_nested_payload() -> None:
    payload = {"lines": [{"vehicleType": "BUS"}, {"vehicle": {"vehicleType": "heavy_rail"}}]}
    assert extract_vehicle_types(payload) == {"BUS", "HEAVY_RAIL"}
    assert extract_vehicle_types(None) == set()
    assert extract_vehicle_types({"unexpected": ["shape"]}) == set()


def test_mode_set_maps_vehicle_types_with_place_type_fallback() -> None:
    places = [
        place("a", types=["transit_station"], transit={"lines": [{"vehicleType": "SUBWAY"}]}),
        place("b", types=["bus_station"]),  # no transitStation -> fallback
    ]
    assert google_mode_set(places) == {1, 3}  # metro + bus


# Verdicts
def test_interchange_verdicts() -> None:
    bus_rail = [
        place(
            "g1",
            types=["transit_station"],
            transit={"lines": [{"vehicleType": "BUS"}, {"vehicleType": "RAIL"}]},
        )
    ]
    bus_only = [place("g2", types=["bus_station"])]
    no_evidence = [place("g3", types=["restaurant"])]

    # CONFIRMED: Google's modes cover ours.
    verdict, agreement, place_id = interchange_verdict({3, 2}, bus_rail)
    assert (verdict, agreement, place_id) == ("CONFIRMED", True, "g1")
    # PARTIAL: overlap but not all.
    assert interchange_verdict({3, 2}, bus_only)[0] == "PARTIAL"
    # CONFLICT: transit modes observed, zero overlap.
    assert interchange_verdict({0, 4}, bus_only)[0] == "CONFLICT"
    # NO_DATA: failed call, empty result, or no mode evidence.
    assert interchange_verdict({3, 2}, None)[0] == "NO_DATA"
    assert interchange_verdict({3, 2}, [])[0] == "NO_DATA"
    assert interchange_verdict({3, 2}, no_evidence)[0] == "NO_DATA"


def test_stop_verdicts() -> None:
    assert stop_verdict([place("g9", types=["bus_stop"])]) == ("STATION_FOUND", "g9")
    assert stop_verdict([place("x", types=["restaurant"])]) == ("NOT_FOUND", None)
    assert stop_verdict([]) == ("NOT_FOUND", None)  # Google answered: nothing there
    assert stop_verdict(None) == ("NO_DATA", None)  # call failed: no answer


# Client (httpx.MockTransport - no network)
def make_client(handler, max_calls=10) -> GooglePlacesClient:
    return GooglePlacesClient(
        api_key="test-key",
        max_calls=max_calls,
        rate_per_s=0,  # no throttling in tests
        backoff_base=0,  # no backoff sleeps in tests
        transport=httpx.MockTransport(handler),
    )


def test_client_sends_field_mask_and_key_headers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["mask"] = request.headers["X-Goog-FieldMask"]
        seen["key"] = request.headers["X-Goog-Api-Key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"places": [{"id": "p1", "types": ["bus_station"]}]})

    client = make_client(handler)
    places = client.search_nearby(20.0, -103.0, 50.0, field_mask=STOP_FIELD_MASK)

    assert seen["mask"] == "places.id,places.types"
    assert seen["key"] == "test-key"
    assert seen["body"]["locationRestriction"]["circle"]["radius"] == 50.0
    assert places is not None and places[0].id == "p1"
    assert client.calls_made == 1


def test_client_refuses_empty_field_mask() -> None:
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("no request may be sent without a field mask")

    client = make_client(handler)
    with pytest.raises(ValueError, match="field mask"):
        client.search_nearby(0, 0, 50, field_mask=[])
    with pytest.raises(ValueError, match="field mask"):
        client.search_nearby(0, 0, 50, field_mask=["", "  "])
    assert client.calls_made == 0


def test_client_enforces_call_budget() -> None:
    def handler(request):
        return httpx.Response(200, json={"places": []})

    client = make_client(handler, max_calls=2)
    client.search_nearby(0, 0, 50, field_mask=STOP_FIELD_MASK)
    client.search_nearby(0, 0, 50, field_mask=STOP_FIELD_MASK)
    with pytest.raises(CallBudgetExhausted):
        client.search_nearby(0, 0, 50, field_mask=STOP_FIELD_MASK)
    assert client.calls_made == 2
    assert client.budget_remaining == 0


def test_client_retries_429_then_succeeds_counting_one_call() -> None:
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"places": [{"id": "ok"}]})

    client = make_client(handler)
    places = client.search_nearby(0, 0, 50, field_mask=INTERCHANGE_FIELD_MASK)
    assert places is not None and places[0].id == "ok"
    assert attempts["n"] == 3
    assert client.calls_made == 1  # retries are one logical (billed) call


def test_client_returns_none_after_persistent_5xx() -> None:
    def handler(request):
        return httpx.Response(503, json={})

    client = make_client(handler)
    assert client.search_nearby(0, 0, 50, field_mask=STOP_FIELD_MASK) is None
    assert client.calls_made == 1


def test_field_names_only_redacts_values() -> None:
    names = field_names_only({"places": [{"displayName": {"text": "SECRET"}, "id": "x"}]})
    assert "places[].displayName.text" in names
    assert not any("SECRET" in n or n == "x" for n in names)


# Gating: OFF by default, no key -> clean no-op (no DB, no network)
def test_pipeline_noop_when_disabled(tmp_path: Path) -> None:
    report = run_google_qa(QaConfig(enabled=False, report_dir=tmp_path))
    assert report.status == "skipped_disabled"
    assert report.calls_made == 0
    assert (tmp_path / "google_qa_report.json").is_file()


def test_pipeline_noop_when_key_missing(tmp_path: Path) -> None:
    report = run_google_qa(QaConfig(enabled=True, api_key=None, report_dir=tmp_path))
    assert report.status == "skipped_no_key"
    assert report.calls_made == 0


def test_cli_noop_without_flag(tmp_path: Path, monkeypatch) -> None:
    from gtfs_ingest import cli

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    exit_code = cli.main(["google-qa", "--report", str(tmp_path)])
    assert exit_code == 0
    assert (tmp_path / "google_qa_report.json").is_file()
