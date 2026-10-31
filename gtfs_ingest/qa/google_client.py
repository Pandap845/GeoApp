"""Small Google Maps client with the QA safety rules built in.

Places (New): Nearby Search, Text Search, Place Details. Legacy APIs for the
station locator: Geocoding and Distance Matrix (same budget and retries).

Rules enforced here: a field mask is required on Places calls; a hard call
budget (CallBudgetExhausted is raised before the request); a minimum time
between calls; retries with backoff on 429/5xx, then None; response bodies are
never logged, only field names. Nothing here writes to disk or the database.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

PLACES_BASE_URL = "https://places.googleapis.com/v1"
LEGACY_BASE_URL = "https://maps.googleapis.com/maps/api"


class CallBudgetExhausted(RuntimeError):
    """The --max-calls budget is used up."""


class _RetryableApiError(RuntimeError):
    """429, 5xx or network error: retried with backoff."""


class LocalizedText(BaseModel):
    """displayName payload."""

    model_config = ConfigDict(extra="ignore")

    text: Optional[str] = None


class LatLng(BaseModel):
    """location payload."""

    model_config = ConfigDict(extra="ignore")

    latitude: Optional[float] = None
    longitude: Optional[float] = None


class PlaceResult(BaseModel):
    """One place, kept in memory only; just its id may ever be stored.

    transit_station stays a raw dict because its shape varies. display_name
    and location are filled only when the field mask asks for them.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[str] = None
    types: list[str] = Field(default_factory=list)
    transit_station: Optional[dict[str, Any]] = Field(default=None, alias="transitStation")
    display_name: Optional[LocalizedText] = Field(default=None, alias="displayName")
    location: Optional[LatLng] = None

    @property
    def name(self) -> Optional[str]:
        """Display name, if requested."""
        return self.display_name.text if self.display_name else None

    @property
    def coords(self) -> Optional[tuple[float, float]]:
        """(lat, lon), if requested."""
        if (
            self.location is not None
            and self.location.latitude is not None
            and self.location.longitude is not None
        ):
            return (self.location.latitude, self.location.longitude)
        return None


def field_names_only(payload: Any, prefix: str = "", limit: int = 60) -> list[str]:
    """Nested field names of a response (never values), for safe debug logs."""
    names: list[str] = []

    def walk(node: Any, path: str) -> None:
        if len(names) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                dotted = f"{path}.{key}" if path else str(key)
                names.append(dotted)
                walk(value, dotted)
        elif isinstance(node, list) and node:
            walk(node[0], f"{path}[]")

    walk(payload, prefix)
    return names[:limit]


class GooglePlacesClient:
    """Google Maps client with a call budget, rate limit and required field masks.

    rate_per_s=0 and backoff_base=0 turn waiting off (for tests); base_url and
    transport can be overridden with httpx.MockTransport.
    """

    def __init__(
        self,
        api_key: str,
        max_calls: int = 200,
        rate_per_s: float = 5.0,
        timeout_s: float = 10.0,
        backoff_base: float = 0.5,
        base_url: str = PLACES_BASE_URL,
        legacy_base_url: str = LEGACY_BASE_URL,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not api_key:
            raise ValueError("GooglePlacesClient requires a non-empty API key")
        self._api_key = api_key
        self._legacy_base_url = legacy_base_url.rstrip("/")
        self._max_calls = int(max_calls)
        self._min_interval = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._last_call = 0.0
        self._calls_made = 0
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)
        self._retrying = Retrying(
            retry=retry_if_exception_type(_RetryableApiError),
            wait=wait_exponential(multiplier=backoff_base, max=8),
            stop=stop_after_attempt(4),
            reraise=True,
        )

    @property
    def calls_made(self) -> int:
        """Calls made so far; retries of one call count once."""
        return self._calls_made

    @property
    def budget_remaining(self) -> int:
        return max(0, self._max_calls - self._calls_made)

    def close(self) -> None:
        self._client.close()

    def search_nearby(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        field_mask: Sequence[str],
        included_types: Optional[Sequence[str]] = None,
        max_results: int = 10,
    ) -> Optional[list[PlaceResult]]:
        """Places strictly within *radius_m*; [] means none, None means the call failed."""
        body: dict[str, Any] = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": float(radius_m),
                }
            },
            "maxResultCount": max_results,
        }
        if included_types:
            body["includedTypes"] = list(included_types)
        data = self._call("POST", "/places:searchNearby", field_mask, body)
        return None if data is None else self._parse_places(data)

    def search_text(
        self,
        query: str,
        field_mask: Sequence[str],
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        radius_m: Optional[float] = None,
        max_results: int = 10,
        language: Optional[str] = None,
        region: Optional[str] = None,
    ) -> Optional[list[PlaceResult]]:
        """Text Search, optionally biased toward a circle (used to prefer nearby namesakes)."""
        body: dict[str, Any] = {"textQuery": query, "maxResultCount": max_results}
        if lat is not None and lon is not None and radius_m:
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": float(radius_m),
                }
            }
        if language:
            body["languageCode"] = language
        if region:
            body["regionCode"] = region
        data = self._call("POST", "/places:searchText", field_mask, body)
        return None if data is None else self._parse_places(data)

    def place_details(self, place_id: str, field_mask: Sequence[str]) -> Optional[PlaceResult]:
        """Details for one place (mask fields have no "places." prefix here)."""
        data = self._call("GET", f"/places/{place_id}", field_mask, None)
        return None if data is None else PlaceResult.model_validate(data)

    def geocode(
        self, address: str, language: str = "es", region: str = "mx"
    ) -> Optional[tuple[float, float]]:
        """Legacy Geocoding: address -> (lat, lon), or None. Fallback after Text Search."""
        params = {"address": address, "language": language, "region": region}
        data = self._call_legacy("/geocode/json", params)
        if data is None:
            return None
        status = data.get("status")
        results = data.get("results") or []
        if status != "OK" or not results:
            log.info("Geocoding returned status %s", status)
            return None
        location = results[0].get("geometry", {}).get("location", {})
        lat, lng = location.get("lat"), location.get("lng")
        if lat is None or lng is None:
            return None
        return (float(lat), float(lng))

    def travel_time_minutes(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: str = "transit",
        transit_mode: str = "bus",
        language: str = "es",
        region: str = "mx",
    ) -> Optional[float]:
        """Legacy Distance Matrix travel time in minutes, or None (caller then estimates)."""
        params = {
            "origins": f"{origin[0]},{origin[1]}",
            "destinations": f"{destination[0]},{destination[1]}",
            "mode": mode,
            "language": language,
            "region": region,
        }
        if mode == "transit":
            # Transit results require a departure time.
            params["transit_mode"] = transit_mode
            params["departure_time"] = "now"
        data = self._call_legacy("/distancematrix/json", params)
        if data is None:
            return None
        status = data.get("status")
        if status != "OK":
            log.info("Distance Matrix returned status %s", status)
            return None
        rows = data.get("rows") or []
        elements = (rows[0].get("elements") or []) if rows else []
        if not elements:
            return None
        element = elements[0]
        if element.get("status") != "OK":
            log.info("Distance Matrix element status %s", element.get("status"))
            return None
        seconds = element.get("duration", {}).get("value")
        return round(seconds / 60, 1) if seconds is not None else None

    @staticmethod
    def _parse_places(data: dict[str, Any]) -> list[PlaceResult]:
        return [PlaceResult.model_validate(p) for p in data.get("places", [])]

    def _call(
        self, method: str, path: str, field_mask: Sequence[str], body: Optional[dict]
    ) -> Optional[dict[str, Any]]:
        """One budgeted Places call; None if it still fails after retries."""
        mask = [f for f in field_mask if f and f.strip()]
        if not mask:
            raise ValueError(
                "Refusing to call the Places API without an explicit field mask "
                "(cost and data-minimization guard)"
            )
        if self._calls_made >= self._max_calls:
            raise CallBudgetExhausted(f"call budget of {self._max_calls} reached")
        self._throttle()
        self._calls_made += 1
        try:
            return self._retrying(self._request, method, path, mask, body)
        except _RetryableApiError as exc:
            log.warning("Places API %s %s failed after retries (%s)", method, path, exc)
            return None
        except httpx.HTTPStatusError as exc:
            # 4xx: log the status code only, never the body.
            log.warning("Places API %s %s -> HTTP %s", method, path, exc.response.status_code)
            return None

    def _call_legacy(self, path: str, params: dict[str, str]) -> Optional[dict[str, Any]]:
        """One budgeted legacy GET (no field masks in these APIs); None on failure."""
        if self._calls_made >= self._max_calls:
            raise CallBudgetExhausted(f"call budget of {self._max_calls} reached")
        self._throttle()
        self._calls_made += 1
        try:
            return self._retrying(self._request_legacy, path, params)
        except _RetryableApiError as exc:
            log.warning("Legacy Maps API %s failed after retries (%s)", path, exc)
            return None
        except httpx.HTTPStatusError as exc:
            log.warning("Legacy Maps API %s -> HTTP %s", path, exc.response.status_code)
            return None

    def _request_legacy(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self._legacy_base_url}{path}"
        try:
            response = self._client.get(url, params={**params, "key": self._api_key})
        except httpx.TransportError as exc:
            raise _RetryableApiError(f"transport: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableApiError(f"HTTP {response.status_code}")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if log.isEnabledFor(logging.DEBUG):
            # Log field names only, never values.
            log.debug("Legacy Maps response fields: %s", field_names_only(data))
        return data

    def _request(
        self, method: str, path: str, mask: list[str], body: Optional[dict]
    ) -> dict[str, Any]:
        headers = {"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": ",".join(mask)}
        try:
            response = self._client.request(method, path, headers=headers, json=body)
        except httpx.TransportError as exc:
            raise _RetryableApiError(f"transport: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableApiError(f"HTTP {response.status_code}")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if log.isEnabledFor(logging.DEBUG):
            # Log field names only, never values.
            log.debug("Places response fields: %s", field_names_only(data))
        return data

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._last_call + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
