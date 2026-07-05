# Data Workflow

This document follows the data through the whole pipeline — from raw GTFS
zips and an OSM extract to the PostGIS tables, the CSV/GeoPackage/GraphML
exports, and the reports — organized in four stages:

1. **[Validation & verification](#1-validation--verification)** — every
   check the data must pass, and every cross-check that verifies results.
2. **[Merge, integration & normalization](#2-merge-integration--normalization)** —
   how heterogeneous feeds (and OSM) become one coherent, keyed dataset.
3. **[Transformation & operations](#3-transformation--operations)** — every
   calculation applied, with the exact formulas.
4. **[Outputs](#4-outputs--what-is-stored)** — every table and file
   produced, and exactly what information each one stores.

For install/run instructions see [README.md](README.md); this file is about
what happens to the data. Orchestrators: `pipeline_phase1.py` (ingest),
`analysis/pipeline_phase2.py` (analyze-frequency),
`conflation/pipeline_phase3.py` (detect-interchanges, conflate-osm),
`qa/pipeline_phase4.py` (google-qa; plus `qa/station_locator.py` for
locate-stations).

```
                  ┌──────────────────────── PHASE 1: ingest ─────────────────────────┐
 GTFS feeds ─────→ discover → load raw → VALIDATE → normalize stops → PostGIS `stops`
 (.zip / dir)     └────────────────────────────┬──────────────────────────────────────┘
                                               │ validation_report.json/.md
                  ┌──────────────────── PHASE 2: analyze-frequency ──────────────────┐
 same feeds ─────→ [--transport-mode/--route filter] → service-date filter
                   → headways → route_stop_headways / route_headways
                   → connectivity (same stop / parent / proximity) → route_connections
                   → near + same-line stations → near_stations.csv
                   → route shapes + stop links → transit_analysis.gpkg (4 layers)
                   → route graph → route_connectivity.graphml
                  └────────────────────────────┬──────────────────────────────────────┘
                                               │ analysis_report.json/.md
                  ┌──────────────────────── PHASE 3: conflation ─────────────────────┐
 same stops ─────→ DBSCAN + parent merge → interchanges  (+ transfers.txt check)
 OSM in PostGIS ─→ nearest transit node (KNN) → stop_osm_matches (confidence)
                   nearest highway line (KNN) → stop_side_of_street (left/right)
                   reverse coverage → osm_coverage_gaps.gpkg (OSM-only stops)
                  └────────────────────────────┬──────────────────────────────────────┘
                                               │ interchanges_report / conflation_report
                  ┌──────────────── PHASE 4: google-qa (OFF by default) ─────────────┐
 phase 3 results → eligibility (multimodal / needs_review) → budgeted Places calls
                   → verdicts only → google_qa_interchange / google_qa_stop
                  └────────────────────────────┬──────────────────────────────────────┘
                                               │ google_qa_report.json/.md
```

---

## 1. Validation & verification

The governing principle: **validation reports, it never repairs or blocks**
(repair happens by exclusion in normalization, section 2), and every later
phase adds its own *verification* cross-checks against independent sources
(transfers.txt, OSM, Google).

### 1.1 Feed validation (Phase 1, `validators.py`)

All checks run for every feed — nothing stops at the first error, and a
crash inside one check becomes an ERROR finding (`validator_crash`) rather
than aborting. Findings carry `severity, code, message, file, count` and up
to 5 sample offending values.

| Check | Finding code(s) | Severity |
| --- | --- | --- |
| Required files present (`agency`, `stops`, `routes`, `trips`, `stop_times`) | `missing_required_file` | ERROR |
| `calendar.txt` OR `calendar_dates.txt` present | `missing_calendar` | ERROR |
| Every present file parseable; required files non-empty | `unreadable_file`, `empty_required_file` | ERROR |
| Required columns per file | `missing_required_columns` | ERROR |
| `stops.txt`: non-null `stop_id` | `null_stop_id` | ERROR |
| `stops.txt`: duplicate `stop_id` | `duplicate_stop_ids` | ERROR |
| Coordinates parseable + in WGS84 range (lat ∈ [−90, 90], lon ∈ [−180, 180]); location_type 3/4 may omit them | `invalid_coordinates` | ERROR |
| `parent_station` → known `stop_id` | `dangling_parent_station` | WARNING |
| `trips.route_id → routes`, `stop_times.trip_id → trips`, `stop_times.stop_id → stops` | `dangling_reference` | ERROR |
| Optional-file inventory | `optional_files` | INFO |

Severity policy: **ERROR** = spec violation / data loss; **WARNING** =
recoverable, loading proceeds; **INFO** = inventory. A feed whose container
cannot even be opened (corrupt zip) is recorded as `failed` and the run
continues — this failure isolation applies at every stage of every phase.

### 1.2 Reference-data verification (Phase 3 preconditions)

- **OSM schema validation** (`osm_match.validate_osm_schema`): before any
  per-stop work, the configured OSM layers are verified — tables exist,
  geometry column exists, required tag columns present (`highway`/`oneway`
  on lines; `highway`/`railway`/`public_transport` on points), SRIDs
  readable from the data. Table/column names must pass a strict SQL
  identifier check (they are interpolated into queries). Failure is
  **fail-fast** with one message listing every problem.
- **Database preconditions**: PostGIS must answer `PostGIS_Version()`
  (Phase 1); a DB is required for conflate-osm even with `--dry-run`
  because OSM in PostGIS is the reference source.

### 1.3 Result verification (cross-checks)

- **Transfers cross-check** (Phase 3A): every declared `transfers.txt` pair
  is compared with the inferred clusters — declared-and-inferred (both
  stops co-clustered), declared-but-NOT-inferred (with examples), and
  inferred clusters backed by no declaration.
- **Reverse OSM coverage** (Phase 3, Part D): OSM transit nodes inside the
  service area (stops hull + `--coverage-buffer-m`, default 500 m) with
  **no GTFS stop within `--coverage-radius-m`** (default 100 m) are
  reported as reviewable gaps — either feed coverage holes or OSM noise.
- **Google QA cross-check** (Phase 4, opt-in): a small, targeted subset of
  Phase 3 results is verified against the Google Places API — multimodal
  interchanges (is the mode combination observed?) and needs-review stops
  (does a transit place exist there?). Only verdicts are stored (section 4).
- **Edge-direction guarantee** (Phase 2): the directed
  `route_stop_links` follow `stop_times.stop_sequence`, and departure
  times are non-decreasing along that order — verified against the feeds
  (time oracle / independent rebuild / shape projection).

---

## 2. Merge, integration & normalization

### 2.1 Feed identity & provenance

Every feed gets a deterministic **`feed_id`**: filename/directory name →
lowercase → non-`[a-z0-9]` runs → `_` → trim (collision appends 8 hex chars
of `sha256(path)`). Example: `improved-gtfs-mimacro-mitren-sitren.zip` →
`improved_gtfs_mimacro_mitren_sitren`. The `feed_id` is the provenance key
carried into **every** downstream table and file; feeds are never mixed in
headway math, and identifiers are feed-qualified:

```
stop_uid  = "{feed_id}:{stop_id}"        route_uid = "{feed_id}:{route_id}"
stop_num_id = int(first 6 bytes of sha256(stop_uid))     # 48-bit numeric alias
```

`stop_num_id` is deterministic, unique in practice (collision odds ≈
n²/2⁴⁹; a unique DB index would surface one), fits `bigint` and JavaScript
numbers, and is the machine key used in node relations.

### 2.2 Normalization of stops (the common schema)

Feeds are loaded **raw** (partridge, all strings — validation must see the
file as published); typing happens here, explicitly. Transformations in
order:

| # | Transformation | Rule |
| --- | --- | --- |
| 1 | Uniform columns | missing optional columns (`stop_name`, `location_type`, `parent_station`) added as NULL |
| 2 | String cleanup | whitespace-stripped; `""` → NULL |
| 3 | Coordinate typing | `stop_lat`/`stop_lon` → float; unparseable → NaN |
| 4 | `location_type` typing | → int; blank/unparseable → `0` (GTFS default) |
| 5 | Drop: no key | null/blank `stop_id` dropped (counted) |
| 6 | Drop: no geometry | missing/unparseable/out-of-range coordinates dropped (counted) — including coordinate-less location_type 3/4 rows: a geometry table cannot hold a row without a point |
| 7 | Deduplication | duplicate `stop_id`: **first occurrence wins** (file order, deterministic); rest dropped (counted) |
| 8 | Identity | `stop_uid`, `stop_num_id`, `feed_id` added |
| 9 | Geometry | shapely `POINT(lon lat)`, **EPSG:4326** (GTFS's own datum; never reprojected at rest) |

Dropped-row counts mirror the validator's ERROR codes, so
`source rows − dropped = loaded rows` is always reconstructible from the
report.

### 2.3 Integration in PostGIS

- Geometry is built server-side: `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`.
- **Idempotency by data nature** (the merge contract):
  - *feed-scoped* tables (`stops`, `route_stop_headways`, `route_headways`):
    **per-feed delete-and-reload** in one transaction — re-running a feed
    atomically replaces exactly that feed's rows;
  - *run-scoped derived* tables (`route_connections`, `interchanges`,
    `stop_osm_matches`, `stop_side_of_street`): **full reload per run** —
    always pass all feeds you care about in one run;
  - *budgeted partial* tables (Phase 4 QA verdicts): **per-row upsert** —
    a re-check of 20 records must not wipe the other 180.
- **Cross-feed merging** happens in memory: per-feed frames are
  concatenated; ids never collide across feeds (feed-qualified), and
  **spatial proximity is the only relation that can cross feeds**.
- **OSM integration**: the OSM extract lives in the same PostGIS database
  (osm2pgsql classic schema; layer/column names configurable). Its SRID is
  read from the data and geometries are transformed on the fly — GTFS data
  itself is never reprojected at rest.

### 2.4 Mode normalization & the analysis filter

- **Extended `route_type` codes normalize to the basic 0–12 set** before
  any mode logic (700-series → bus 3, 100-series → rail 2, 400-series →
  metro 1, 900-series → tram 0, …). Mode names: tram 0, metro 1, rail 2,
  bus 3, ferry 4, cable_tram 5, aerial_lift 6, funicular 7, trolleybus 11,
  monorail 12.
- **Opt-in analysis filter** (Phase 2; default = everything):
  `--transport-mode` (names or route_type codes) and `--route` (route_id or
  route_short_name) select routes per feed (**union**: a route passes if it
  matches either). The selected `route_id` set is pushed into the partridge
  view, so everything trip-derived cascades (headways, connectivity,
  near/same-line stations, shapes, links). The **stop inventory stays
  complete** — stops of excluded routes appear unserved. Feeds with no
  matching route are reported `filtered_out`.

---

## 3. Transformation & operations

### 3.1 Service-date filtering (Phase 2)

Headways depend on *which trips run that day*. partridge resolves the
active `service_id`s from `calendar.txt` (weekday pattern + validity range)
**and** `calendar_dates.txt` exceptions (type 1 added / type 2 removed).
Without `--service-date`, each feed's **busiest date** (most active trips)
is used and recorded. The filter cascades through the feed's dependency
graph. Times become **seconds since service-day midnight**; GTFS times past
24:00 stay as-is (`25:10:00` → 90600 s).

### 3.2 Headways

A **headway** is the gap between two consecutive scheduled *departures* at
the same stop, same `(feed, route, direction)`, inside one half-open time
window `[start, end)` (defaults: am_peak 06:00–09:00, midday 09:00–15:00,
pm_peak 15:00–19:00, evening 19:00–23:00; windows may extend past 24:00,
e.g. `23:00-27:00`):

```
departures  = sorted departure_s within [start, end)
gaps        = departures[i+1] − departures[i]            # N departures → N−1 gaps
trip_count  = N
mean/median/min/max_headway_s = statistics over gaps      # seconds
```

- `N < 2` → headway **undefined**: row kept with `trip_count` and NULL
  statistics; zero departures → no row.
- Blank `direction_id` → sentinel `-1`; opposite directions never merged.
- Non-timepoints (no `departure_time`) excluded; `frequencies.txt` service
  is **not** expanded (documented limitation).

**Route rollup** per `(route, direction, window)`:

```
mean_headway_s     = mean( stop-level mean_headway_s over stops with a defined headway )
stop_count         = distinct stops served in the window
stops_with_headway = stops with ≥ 2 departures
trip_count         = max( stop-level trip_count )    # proxy for corridor runs
```

### 3.3 Route connectivity

Two routes connect when (precedence order, strongest wins as
`connection_type`):

1. **`same_stop`** — an identical `stop_uid`; `shared_stop_count` counted,
   `min_stop_distance_m = 0`.
2. **`same_parent_station`** — stops sharing a `parent_station` (a stop
   that *is* the parent belongs to its own station).
3. **`proximity`** — stops within `--proximity-m` (default 150 m).
   Geodesic distance: PostGIS `ST_DWithin/ST_Distance` on `geography`, or
   local UTM projection without a DB. The only cross-feed connection type.

Edges are **undirected**, normalized to `route_a < route_b`, one row per
pair. The same edges feed a networkx graph (isolated routes included as
nodes) exported as GraphML. The specific stops that created an edge are
aggregated away (count + min distance survive); the physical *places* where
routes meet are the Phase 3 interchanges.

### 3.4 Near stations & same-line stations (node relations)

Per stop, **two complementary relations**:

- **`near_stations`** — geographic proximity, any line: the stops within
  the proximity threshold, one tuple per (near stop, line active that
  service date), sorted by walking time:

```
[(ID, station, linea, time to reach), …]
ID            = the near stop's sequential node id (the CSV's first column:
                1..N in row order — the key all relation columns reference;
                stop_uid remains the durable cross-run key)
linea         = route_short_name, falling back to route_id
time to reach = max(1, round(distance_m / 83))    # minutes; 83 m/min ≈ 5 km/h, straight-line
stop_line_ids = "{ID}-{line}" per line serving the row's own stop
Modalidad     = distinct agency names of the serving lines ("Mi Macro, Sitren")
```

- **`same_line_stations`** — route topology, same line only: the
  **adjacent stations in route order** (previous/next stop per line and
  direction, from `stop_times` sequence), regardless of distance:

```
[(ID, station, linea, dir{d}, {dist}m), …]      dist = haversine between the consecutive stops
```

Inclusion criteria differ deliberately: `near_stations` = "what can I walk
to" (distance only, no route filter); `same_line_stations` = "what comes
before/after on my line" (sequence only, no distance threshold).

### 3.5 Route geometry (Phase 2 exports)

- **`route_shapes`** — real polylines from `shapes.txt` (points ordered by
  `shape_pt_sequence`), one feature per distinct
  `(route, direction, shape_id)` of the analyzed service day.
- **`route_stop_links`** — **directed** consecutive-stop segments per
  `(route, direction)` derived from `stop_times` order and deduplicated
  across trips: each stop chains to the *next stop of its own route*.
  Distinct from `near_links` (the walking overlay).

### 3.6 Interchange detection (Phase 3A)

1. Stops (all feeds, full feed — an interchange is infrastructure, not a
   schedule property) are projected to a **metric CRS** (auto-detected UTM
   or `--crs`) and clustered with **DBSCAN** (`eps` 120 m, `min_samples` 2).
2. DBSCAN clusters are **merged with `parent_station` groups** via
   connected components — a declared station is never split.
3. Components with ≥ 2 stops become clusters; ids are deterministic
   (ordered by smallest member `stop_uid`).
4. Each cluster collects the (normalized) `route_type`s of routes serving
   its members; **`is_multimodal = (distinct modes > 1)`**. Geometry:
   centroid + convex hull (EPSG:4326).

### 3.7 Stop ↔ OSM matching (Phase 3B)

Nearest OSM node tagged `highway=bus_stop`,
`public_transport ∈ {platform, stop_position, station}`, or
`railway ∈ {station, halt, tram_stop}`, found with the index-assisted KNN
operator (`<->`); true distance measured geodesically on `geography`.

```
distance_score = max(0, 1 − distance_m / threshold_m)          # threshold 50 m default
type_score     = 1.0  station-stop ↔ station node, or platform-stop ↔ platform-ish node
                 0.8  platform-stop ↔ station node
                 0.6  station-stop ↔ non-station node
                 0.7  otherwise/unknown
confidence     = round(distance_score × type_score, 3)
needs_review   = no candidate  OR  distance > threshold  OR  confidence < 0.5
```

### 3.8 Side of street (Phase 3C)

Nearest OSM highway centerline (KNN, `--street-threshold-m` 50 m), then
pure planar math in the metric CRS:

```
d     = direction vector of the nearest segment IN TRAVEL DIRECTION
        (digitized order; reversed when oneway ∈ {-1, reverse})
v     = vector from the stop's perpendicular foot to the stop
cross = d.x·v.y − d.y·v.x
side  = left  if cross > 0   ("driver's view")
        right if cross < 0
        on    if perpendicular distance ≤ 1 m
```

Worked example: way digitized `(0,0) → (100,0)`, stop at `(50, 10)`:
`cross = +10` → **left**; with `oneway=-1` the same stop is **right**.
`side_confidence = low` when fragile: side `on`, foot within 10 m of a way
end (intersection proxy), or perpendicular distance < 2 m.

### 3.9 Reverse OSM coverage (Phase 3D)

```
service area = ST_Buffer(ST_ConvexHull(all stops)::geography, coverage_buffer_m)
gap          = OSM transit node inside the area
               with no GTFS stop within coverage_radius_m (geodesic)
```

Each gap keeps its OSM id/kind/name, position, and the distance to the
nearest GTFS stop (worst-first ordering).

### 3.10 Google QA cross-check (Phase 4, opt-in)

Gating: runs only with `--enable-google-qa` AND `GOOGLE_MAPS_API_KEY` in
the environment (no CLI flag for the key); otherwise a clean no-op. Client
rails enforced in code: mandatory field mask per request, **hard
`--max-calls` budget** (default 200; retries of one call count once), rate
limit + exponential backoff, response bodies never logged (field names
only).

Eligibility (targeted, never the full stops table): multimodal
interchanges (one Nearby Search around the centroid,
`--interchange-radius-m` 150 m, mask includes `places.transitStation` —
Google's Enterprise SKU, which is why this check is budgeted) and
needs-review stops (radius `--radius-m` 50 m, cheap mask).

Verdicts (Google `vehicleType`s map onto our basic route_type codes; the
comparison happens in OUR vocabulary and the Google data is discarded):

| Interchange | Meaning |
| --- | --- |
| `CONFIRMED` | Google's observed modes cover every mode we inferred |
| `PARTIAL` | some overlap, not all |
| `CONFLICT` | transit observed, zero mode overlap — listed for review |
| `NO_DATA` | call failed / nothing returned / no usable mode evidence |

Stops: `STATION_FOUND` / `NOT_FOUND` / `NO_DATA`.

The **station locator** (`locate-stations`, same rails, same opt-in
pattern with `--enable-google-api`) is a user-facing tool, not a QA pass:
station names → Text Search anchored to the previously resolved station
(location-bias circle, `--search-radius-m` 3000 m) → Geocoding fallback →
consecutive travel times via Distance Matrix (`real`) or Haversine at
`--avg-speed-kmh` (`estimado`) or a generic `--max-route-minutes / n`
(`generico`) → Excel export. It never writes to the database.

---

## 4. Outputs & what is stored

### 4.1 Database tables (PostGIS)

| Table | Producer / idempotency | What each row stores |
| --- | --- | --- |
| `stops` | ingest / per-feed replace | one normalized stop: `stop_uid` PK, `stop_num_id` (unique), `feed_id`, `stop_id`, `stop_name`, lat/lon, `location_type`, `parent_station`, `geom` POINT 4326 (GiST), `loaded_at` |
| `route_stop_headways` | analyze-frequency / per-feed replace | headway stats per (feed, route, direction, stop, window): `trip_count`, mean/median/min/max headway seconds, `service_date` |
| `route_headways` | analyze-frequency / per-feed replace | the rollup per (feed, route, direction, window): mean headway, `stop_count`, `stops_with_headway`, `trip_count` |
| `route_connections` | analyze-frequency / full reload | one undirected route pair: `route_a < route_b`, feed ids, `connection_type`, `shared_stop_count`, `min_stop_distance_m` |
| `interchanges` | detect-interchanges / full reload | one cluster: `cluster_id`, `is_multimodal`, `stop_count`, `modes` int[], `mode_names` text[], `member_stop_uids` text[], `routes` text[], `feed_ids` text[], `centroid` + `hull` geometry |
| `stop_osm_matches` | conflate-osm / full reload | per stop: matched `osm_id`/`osm_kind`/`osm_name`, `distance_m`, `match_confidence`, `needs_review` |
| `stop_side_of_street` | conflate-osm / full reload | per stop: `osm_way_id`, `highway`, `oneway`, `perpendicular_distance_m`, `side` (left/right/on), `side_confidence`, `near_endpoint` |
| `google_qa_interchange` | google-qa / per-row upsert | `interchange_cluster_id` PK, `verdict`, `mode_agreement`, `google_place_id`, `checked_at` |
| `google_qa_stop` | google-qa / per-row upsert | `stop_uid` PK, `verdict`, `google_place_id`, `checked_at` |

**What is deliberately NOT stored** (Google Maps ToS, enforced by a frozen
column allowlist — `storage.StorageViolation` — and a DDL-parsing test):
any Google content other than the opaque `place_id` — no names, transit
lines, coordinates, ratings; not in the DB, files, or logs. The station
locator's Excel export is the single, documented exception (user export,
file only, never the database).

### 4.2 Files (report directory, default `./out`)

| File | Producer | What it stores |
| --- | --- | --- |
| `validation_report.json/.md` | ingest | per feed: status, row counts per file, findings (severity/code/count/samples), optional-file matrix, stops normalized vs loaded |
| `analysis_report.json/.md` | analyze-frequency | service dates used (+ how picked), transport-mode/route filter applied, per-feed route/trip/headway counts, connectivity summary, isolated routes |
| `near_stations.csv` | analyze-frequency | one row per stop: `ID` (sequential node id, 1..N — the key of every relation column), `stop_uid`, `station`, `Modalidad` (serving system names), feed/stop ids, lat/lon, `stop_line_ids` (own `ID-line` combos), `near_stations` `[(ID, station, linea, min), …]`, `same_line_stations` `[(ID, station, linea, dir, m), …]` |
| `transit_analysis.gpkg` | analyze-frequency | 4 EPSG:4326 layers: **`stops`** (points + the CSV attributes), **`near_links`** (walking lines: both num ids, `distance_m`, `walk_min`), **`route_shapes`** (real route polylines: route_uid, linea, direction, shape_id), **`route_stop_links`** (directed consecutive-stop segments: route_uid, linea, direction, both stop_uids) |
| `route_connectivity.graphml` | analyze-frequency | the undirected route graph: nodes = route_uid (+ short/long name, route_type, feed), edges = connections (+ type, shared stops, min distance) — for Gephi/yEd/networkx |
| `interchanges_report.json/.md` | detect-interchanges | cluster totals, mode combinations, per-feed notes, transfers.txt agreement (declared vs inferred, with examples) |
| `conflation_report.json/.md` | conflate-osm | match rate, needs-review count, side distribution, low-confidence count, reverse-coverage summary (gap count by kind) |
| `osm_coverage_gaps.gpkg` | conflate-osm | layer `osm_only_stops`: OSM transit nodes with no GTFS stop nearby — `osm_id`, `kind`, `osm_name`, `nearest_stop_m`, position (worst-first) |
| `google_qa_report.json/.md` | google-qa | run status (ran / dry_run / skipped_*), calls made vs budget, eligibility vs checked counts, verdict counts, CONFLICT list (our cluster ids + our modes only) |
| `station_locator_report.json/.md` | locate-stations | resolution counts (Text Search vs Geocoding vs unresolved), edge time sources (real/estimado/generico), call accounting |
| `stations_route.xlsx` | locate-stations | the route: `ID, Nombre_Estación, Modalidad, Línea, Latitud, Longitud, Estacion_Proxima` — contains Google-resolved coordinates (user export only; see README licensing note) |

JSON reports are the machine-readable source of truth (direct pydantic
serialization); the Markdown files render the same data for humans.

---

## 5. Cross-cutting rules

- **Provenance everywhere:** `feed_id` is a column in every table; feeds
  are never mixed in headway math.
- **Failure isolation:** a broken feed/file/row is logged, counted in the
  report, and skipped; only run-level problems (unreachable DB, missing
  PostGIS/OSM layers, unwritable report dir) abort a run.
- **Determinism:** feed ids, numeric stop ids, dedup order, cluster ids,
  and edge ordering are all deterministic for identical inputs.
- **Idempotency:** per-feed replace for feed-scoped tables; full reload for
  run-scoped derived tables; per-row upsert for budgeted QA verdicts —
  re-running any command is always safe.
- **CRS policy:** data at rest is always EPSG:4326 (GTFS's own datum);
  metric math (DBSCAN, side-of-street, local distances) happens in an
  auto-detected UTM zone or a configured CRS; authoritative distances use
  PostGIS `geography` (geodesic meters).
- **External APIs:** OFF by default, explicit opt-in flags + env-only
  keys, hard call budgets, and nothing beyond opaque ids ever persisted.
