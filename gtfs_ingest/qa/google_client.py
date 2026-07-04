"""Thin Places API (New) client with the Phase 4 safety rails built in.

Endpoints: Nearby Search (``POST /v1/places:searchNearby`` - used for both
checks because its ``locationRestriction`` circle makes "nothing within the
radius" a real answer), Text Search (``POST /v1/places:searchText``), and
Place Details (``GET /v1/places/{id}``).

The station-locator tool additionally uses two *legacy* Web Service APIs
through the same client (same budget, throttle, and retry machinery):
Geocoding (``GET /geocode/json``) and Distance Matrix
(``GET /distancematrix/json``). Legacy APIs take the key as a query
parameter and have no field-mask concept, so the mask guard applies only
to Places (New) calls; the redaction rule (field names only, never values)
applies to both.

Safety rails (all enforced here, not left to callers):

- **Field mask required**: every call must carry an explicit non-empty
  ``X-Goog-FieldMask``; an empty mask raises before any request is made.
- **Hard call budget**: a counter across the client's lifetime; when
  ``max_calls`` is reached, :class:`CallBudgetExhausted` is raised BEFORE
  the request. Retries of a failed call do not increment the counter again
  (429/5xx attempts are not billed by Google).
- **Rate limit**: a minimum interval between requests
  (``rate_per_s``; 0 disables, for tests).
- **Backoff**: exponential retry on 429/5xx/transport errors (tenacity),
  then the call resolves to ``None`` - callers treat that as NO_DATA.
- **Redaction**: response bodies are NEVER logged. Debug logging emits the
  response's field *names* only; error logging emits status codes only.

Responses parse into :class:`PlaceResult` - an in-memory model. Nothing
here writes to disk or DB.
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
    """Raised when the configured --max-calls ceiling has been reached."""


class _RetryableApiError(RuntimeError):
    """Internal: 429/5xx/transport failure - retried with backoff."""


class LocalizedText(BaseModel):
    """Places (New) ``displayName`` payload (``{text, languageCode}``)."""

    model_config = ConfigDict(extra="ignore")

    text: Optional[str] = None


class LatLng(BaseModel):
    """Places (New) ``location`` payload (``{latitude, longitude}``)."""

    model_config = ConfigDict(extra="ignore")

    latitude: Optional[float] = None
    longitude: Optional[float] = None


class PlaceResult(BaseModel):
    """One place, IN MEMORY ONLY.

    Only ``id`` may ever be persisted (as ``google_place_id``); every other
    field is read for one comparison and discarded. ``transit_station`` is
    kept as an opaque dict because its exact schema varies; the crosscheck
    extracts ``vehicleType`` values from it tolerantly. ``display_name``
    and ``location`` are only populated when the caller's field mask asks
    for them (the station locator does; the QA crosscheck does not).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[str] = None
    types: list[str] = Field(default_factory=list)
    transit_station: Optional[dict[str, Any]] = Field(default=None, alias="transitStation")
    display_name: Optional[LocalizedText] = Field(default=None, alias="displayName")
    location: Optional[LatLng] = None

    @property
    def name(self) -> Optional[str]:
        """The display name text, when the mask requested it."""
        return self.display_name.text if self.display_name else None

    @property
    def coords(self) -> Optional[tuple[float, float]]:
        """``(lat, lon)`` when the mask requested ``places.location``."""
        if (
            self.location is not None
            and self.location.latitude is not None
            and self.location.longitude is not None
        ):
            return (self.location.latitude, self.location.longitude)
        return None


def field_names_only(payload: Any, prefix: str = "", limit: int = 60) -> list[str]:
    """Collect nested field NAMES (never values) from a response payload.

    Used for debug logging so that Google content is never written to logs.
    """
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
    """Budgeted, rate-limited, mask-enforcing Places API (New) client.

    Args:
        api_key: Google Maps Platform API key (from the environment only).
        max_calls: hard ceiling on API calls for this client's lifetime.
        rate_per_s: max requests per second (0 disables throttling).
        timeout_s: per-request timeout.
        backoff_base: exponential backoff base seconds (0 for tests).
        base_url / transport: overrides for testing (httpx.MockTransport).
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

    # -- bookkeeping --------------------------------------------------------

    @property
    def calls_made(self) -> int:
        """API calls performed so far (retries of one call count once)."""
        return self._calls_made

    @property
    def budget_remaining(self) -> int:
        """Calls left before :class:`CallBudgetExhausted`."""
        return max(0, self._max_calls - self._calls_made)

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    # -- endpoints -----------------------------------------------------------

    def search_nearby(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        field_mask: Sequence[str],
        included_types: Optional[Sequence[str]] = None,
        max_results: int = 10,
    ) -> Optional[list[PlaceResult]]:
        """Places within *radius_m* of a point (restriction, not bias).

        Returns:
            Parsed places (possibly empty = Google answered "nothing
            there"), or ``None`` when the call failed after retries.

        Raises:
            ValueError: empty field mask.
            CallBudgetExhausted: budget ceiling reached.
        """
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
        """Text Search, optionally biased (not restricted) to a circle.

        The bias circle is the station locator's *anchoring* mechanism:
        passing the previously resolved station's coordinates makes Google
        rank nearby homonyms first. Omit lat/lon/radius for an unanchored
        search (e.g. the first station of a route).
        """
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
        """Details for one place id (mask fields are unprefixed here)."""
        data = self._call("GET", f"/places/{place_id}", field_mask, None)
        return None if data is None else PlaceResult.model_validate(data)

    # -- legacy Web Service endpoints (station locator) ----------------------

    def geocode(
        self, address: str, language: str = "es", region: str = "mx"
    ) -> Optional[tuple[float, float]]:
        """Legacy Geocoding API: free-text address -> ``(lat, lon)``.

        Used only as the fallback when Text Search yielded no coordinates.
        Non-``OK`` statuses are logged (status string only, never content)
        and resolve to ``None`` so the caller's fallback chain continues.

        Raises:
            CallBudgetExhausted: budget ceiling reached.
        """
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
        """Legacy Distance Matrix API: real travel time in minutes.

        Returns ``None`` on any non-``OK`` top-level or element status
        (e.g. ``ZERO_RESULTS`` when the system is not in Google Transit),
        so the caller falls back to the Haversine estimate.

        Raises:
            CallBudgetExhausted: budget ceiling reached.
        """
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

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _parse_places(data: dict[str, Any]) -> list[PlaceResult]:
        return [PlaceResult.model_validate(p) for p in data.get("places", [])]

    def _call(
        self, method: str, path: str, field_mask: Sequence[str], body: Optional[dict]
    ) -> Optional[dict[str, Any]]:
        """One budgeted call; ``None`` on failure after retries."""
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
            # Non-retryable 4xx. Log the status code only - never the body.
            log.warning("Places API %s %s -> HTTP %s", method, path, exc.response.status_code)
            return None

    def _call_legacy(self, path: str, params: dict[str, str]) -> Optional[dict[str, Any]]:
        """One budgeted legacy GET (key as query param); ``None`` on failure.

        Shares the budget counter, throttle, and retry policy with the
        Places (New) calls; the field-mask guard does not apply because
        legacy Web Service APIs have no mask concept.
        """
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
            # Redaction guard: field names only, never values.
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
            # Redaction guard: field names only, never values.
            log.debug("Places response fields: %s", field_names_only(data))
        return data

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._last_call + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
