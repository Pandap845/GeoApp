# Prompt: How the Google Maps APIs are used in `station_location_distance_time.py` (Station Locator App)

You are an AI assistant working on a Python/Tkinter desktop application called **Station Locator**. This document explains exactly how the application consumes the Google Maps Platform APIs so that you can maintain, extend, or replicate this integration. Follow this specification precisely.

## Context

The app reads a list of public-transport station names from a TXT/CSV file (comma-separated), resolves each station name to geographic coordinates using Google APIs, computes the travel time between consecutive stations of a route, and exports everything to an Excel file (`openpyxl`). The user provides a **Google API Key** through the GUI (`self.api_key`, a `tk.StringVar`). All requests are plain HTTPS GET calls made with the `requests` library — no official Google client library is used.

Three Google APIs are involved:

1. **Places API — Text Search** (primary geocoding / disambiguation)
2. **Geocoding API** (fallback)
3. **Distance Matrix API** (real travel times, optional)

The API key must therefore have these three services enabled in Google Cloud Console.

---

## 1. Places API — Text Search (`get_place_recommendation`)

**Endpoint:** `https://maps.googleapis.com/maps/api/place/textsearch/json`

**Purpose:** Get the *first recommendation* (name + coordinates) for a free-text query describing a station. Text Search is used instead of Autocomplete because it behaves like a normal Google search: it understands full descriptive phrases such as `"Estación SITREN Central línea 2"` even when they do not literally match the official place name registered in Maps.

**Query construction** (done in `get_coordinates`):

```
"Estación {transport_type} {station_name}"          # e.g. "Estación SITREN Central"
+ " línea {line}"  (only if the user typed a line)  # e.g. "... línea 2"
```

**Request parameters:**

| Param | Value | Notes |
|---|---|---|
| `query` | the constructed Spanish query | |
| `language` | `es` | Spanish results |
| `region` | `mx` | bias to Mexico |
| `key` | user's API key | |
| `location` | `"{lat},{lng}"` | **only** when an anchor exists (see below) |
| `radius` | `self.search_radius` (default 3000 m) | only together with `location` |

**Geographic anchoring (duplicate-name disambiguation):** the app keeps `self.last_coords`, the coordinates of the *previous successfully resolved station* in the route. When present, it is passed as `location` + `radius` so Google prioritizes results near the previous station. This is the key mechanism to distinguish two stations that share the same name but are in different places: only the one geographically close to the route should rank first. The first station of a run has no anchor (`self.last_coords` is reset to `None` at the start of each processing run and by the Clear button).

**Response handling:** if `status != "OK"`, log the status and `error_message`. If `results` is non-empty, take **`results[0]`** and extract `name`, `geometry.location.lat`, `geometry.location.lng`. Return `(name, lat, lng)`; on no results or exception return `(None, None, None)` so the caller falls back to Geocoding.

---

## 2. Geocoding API — fallback (`get_coordinates`)

**Endpoint:** `https://maps.googleapis.com/maps/api/geocode/json`

**Purpose:** Backup when Text Search returned no coordinates (e.g. no results, or first station edge cases).

**Request parameters:** `address` (the recommended name from Text Search if one was returned, otherwise the original constructed query), `language=es`, `region=mx`, `key`.

**Response handling:** take `results[0].geometry.location` → `lat`, `lng`. Log non-`OK` statuses.

**Post-processing (both paths):** whenever coordinates are obtained (from either API), update the anchor `self.last_coords = (lat, lng)` for the *next* station lookup, and the caller stores them in `self.station_coords[station_name]`.

---

## 3. Distance Matrix API — real travel times (`get_travel_time_distance_matrix`)

**Endpoint:** `https://maps.googleapis.com/maps/api/distancematrix/json`

**Purpose:** Obtain the *real* transit travel time between two consecutive stations. Only called when the GUI checkbox `use_distance_matrix` is enabled (default: true).

**Transport-mode mapping** (`self.TRANSPORT_MODE_MAP`): the GUI transport type is mapped to Google's `mode` + `transit_mode`:

| GUI value | mode | transit_mode |
|---|---|---|
| SITREN | transit | train |
| MiMacro / Troncal / Alimentador / Expreso | transit | bus |
| (unknown) | transit | bus (default) |

**Request parameters:** `origins="{lat},{lng}"`, `destinations="{lat},{lng}"`, `mode`, `language=es`, `region=mx`, `key`; and when `mode == "transit"`, also `transit_mode` and `departure_time=now` (transit results require a departure time).

**Response handling:**
- Top-level `status != "OK"` → log and return `None`.
- Missing `rows` / `elements` → return `None`.
- Element `status != "OK"` (e.g. `NOT_FOUND`, `ZERO_RESULTS` — the system is not integrated into Google Transit) → return `None`.
- Otherwise return `round(element.duration.value / 60, 1)` (minutes as float).

**Fallback chain** (`get_travel_time_minutes`): if Distance Matrix is disabled or returns `None`, estimate the time with the **Haversine straight-line distance** between the two coordinates divided by the user-configured average speed (`avg_speed`, default 25 km/h), returning minutes. The function returns a tuple `(minutes, source)` where `source` is `"real"` or `"estimado"` — this label is logged to the status console.

---

## Overall processing flow (order matters)

1. Reset `self.last_coords = None` and `self.station_coords = {}`.
2. Read the station list from the file.
3. **Geocode every station first** (Text Search → Geocoding fallback), sequentially, updating the anchor after each success. Store results in `self.station_coords`.
4. **Then** build a *directed* graph: for each consecutive pair `(station[i], station[i+1])` with known coordinates, call `get_travel_time_minutes` and add a single edge `station[i] → station[i+1]` with `{neighbor, line, time}`. No reverse edge — each station reports only the *next* station in the route. Pairs with missing coordinates fall back to a generic estimate (`max_time / len(stations)`).
5. Export to Excel with columns: `ID, Nombre_Estación, Modalidad, Línea, Latitud, Longitud, Estacion_Proxima`, where `Estacion_Proxima` is formatted as `(neighbor, line, timemin)` or `"N/A"` for the last station.

## Constraints and conventions you must preserve

- All Google requests use `requests.get(url, params=...)` and parse JSON manually; keep this style.
- Always send `language=es` and `region=mx`.
- Never crash on API errors: log via `self.log_status(...)` and return `None`-sentinels so the fallback chain continues.
- The API key comes only from the GUI field; never hardcode it.
- Processing runs in a background `threading.Thread` to keep the Tkinter GUI responsive; API calls must stay off the main thread.
- Take the **first** result from Text Search / Geocoding — the anchoring mechanism (`location` + `radius`) is what makes the first result trustworthy, so keep the anchor logic intact if you modify the lookup.
- Cost note: each station costs 1 Text Search call (plus possibly 1 Geocoding call), and each consecutive pair costs 1 Distance Matrix element when real times are enabled.
