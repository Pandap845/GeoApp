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

Inputs are GTFS feeds (`.zip` or unzipped directories), an OSM extract in
PostGIS, and optionally **stops-only CSVs** (`id, name, latitude, longitude`)
named explicitly on `--feeds`. A CSV is renamed to GTFS column names at load
time and then follows exactly the same normalization, validation and storage
path as a real feed, so it merges into the same tables and files; having no
schedule, it contributes geometry only. See
[Stops-only CSV sources](../../README.md#stops-only-csv-sources).

For install/run instructions see [README.md](../../README.md); this file is about
what happens to the data. Orchestrators: `pipeline_phase1.py` (ingest),
`analysis/pipeline_phase2.py` (analyze-frequency),
`conflation/pipeline_phase3.py` (detect-interchanges, conflate-osm),
`qa/pipeline_phase4.py` (google-qa; plus `qa/station_locator.py` for
locate-stations).

```
                  ┌──────────────────────── PHASE 1: ingest ─────────────────────────┐
 GTFS feeds ─────→ discover → load raw → VALIDATE → normalize stops → PostGIS `stops`
 (.zip / dir)                    ↑
 stops CSV ──────→ header aliases┘  (id, name, latitude, longitude; stops-only)
 (.csv, explicit) └────────────────────────────┬──────────────────────────────────────┘
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
numbers. It is the **database machine key** (`stops.stop_num_id`, also the
`num_id_a/b` attributes of the GeoPackage `near_links` layer). Note: the
node relations in `near_stations.csv` do NOT use it — they reference the
file's own sequential `ID` column (1..N, see §3.4); `stop_uid` is the key
to join the CSV against the database.

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
- Non-timepoints (no `departure_time`) excluded.
- **`frequencies.txt` IS expanded** (`headways.expand_frequencies`): each
  frequency row generates one synthetic run per `headway_secs` interval,
  starting at `start_time + k·headway` while strictly before `end_time`;
  each run replays the trip's per-stop offsets relative to its first
  departure. The template's own absolute times are **replaced** (GTFS
  `exact_times=0`: they are a travel-time pattern, not real departures), so
  `trip_count` counts real vehicle passages including synthetic runs — on
  these frequency-dominated feeds (improved: 181/184 trips; mdb: 970/989)
  this is where the headway numbers become meaningful at all. Guards:
  unusable rows skipped, ≤1000 runs per row. `route_stop_links.travel_min`
  deliberately stays on the UN-expanded template (the pattern already
  encodes the correct segment times).
- **Calendar honesty**: Phase 1 emits a `feed_expired` WARNING when the
  feed's declared service (calendar + added calendar_dates) ends before
  today; Phase 2 records `calendar_end`/`calendar_status` per feed and
  notes expired calendars, out-of-validity analysis dates, and auto-picked
  "busiest" dates more than 30 days in the past. Nothing blocks — the
  analysis is valid *for its date*; the reader just learns how old that
  date is. All these judgments compare against the run's **analysis
  clock** (`--analysis-date` / `GTFS_ANALYSIS_DATE`, default today,
  resolved once per run and recorded in the reports) — identical inputs +
  identical clock ⇒ identical findings.
- **Service-date coherence**: when the analyzed feeds resolve to different
  service days (e.g. per-feed busiest dates years apart), the report sets
  `service_dates_coherent: false` and warns — the cross-feed outputs mix
  non-contemporaneous schedules. `--common-date` resolves one day every
  feed serves (latest such date; exit 1 when the calendars don't overlap).
- **Date ranges** (`--service-date-range START..END`, ≤ 31 days): each
  served day in the window is analyzed separately (headway gaps never span
  days) and stored per day — the headway table PKs include `service_date`
  (existing databases are migrated idempotently). Connectivity,
  near-stations and geometry use the union of days; `travel_min` per
  segment is the median across days.

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
3. **`same_station_dedup`** — stops merged into one canonical physical
   station by cross-feed deduplication (§3.3b). Inferred evidence, kept
   distinguishable from the GTFS-declared `same_parent_station`;
   `min_stop_distance_m` stays NULL.
4. **`proximity`** — stops within `--proximity-m` (default 150 m).
   Geodesic distance: PostGIS `ST_DWithin/ST_Distance` on `geography`, or
   local UTM projection without a DB. Proximity and `same_station_dedup`
   are the only cross-feed connection types.

Edges are **undirected**, normalized to `route_a < route_b`, one row per
pair. The same edges feed a networkx graph (isolated routes included as
nodes) exported as GraphML. The specific stops that created an edge are
aggregated away (count + min distance survive); the physical *places* where
routes meet are the Phase 3 interchanges.

### 3.3b Cross-feed station deduplication (`analysis/dedupe.py`)

Different feeds describe the same physical station under different stop ids
(`improved:SITEUR_x` vs `mdb:TL_x` …). Without deduplication those aliases
inflate the outputs: a station is listed as "near" its own alias in
`near_stations.csv`, and one physical place looks like several. On by
default in `analyze-frequency` and `detect-interchanges`; disable with
`--no-dedupe`.

**Match rule** (per candidate pair):

- candidates: stops of **different feeds** within `--dedupe-distance-m`
  (default 30 m; computed locally in the estimated UTM CRS — no DB needed);
- accept when `name_score >= --dedupe-name-threshold` (default 0.75) **or**
  the two stops share at least one normalized `route_short_name`;
- `name_score` = `difflib.SequenceMatcher` ratio of the **normalized**
  names: NFKD accent-strip → casefold → non-alphanumerics to spaces →
  tokens sorted ("Estación Juárez" ≡ "JUAREZ, Estacion"). Missing/empty
  names score 0.0 — they can only match through shared routes.

**Groups**: connected components (networkx) over the accepted pairs, sorted
by each group's smallest `stop_uid` (deterministic `group_id`), which is
also the group's `canonical_uid`. Transitivity is deliberate: two same-feed
platforms can merge via a cross-feed bridge — they are one physical station
by transitivity.

**Effects** (all outputs, one canonical map):

- alias pairs are removed from the proximity stop pairs **before**
  `--network-walk`, near-stations, GeoPackage `near_links` and the
  connectivity edges — `near_stations.csv` keeps its exact row set, `ID`
  sequence and format either way; only the alias tuples disappear;
- routes serving aliased stops connect as `same_station_dedup` (§3.3);
- Phase 3A clustering treats each canonical group as unsplittable (like
  parent_station) and reports `physical_stop_count` per cluster —
  a cluster with `physical_stop_count = 1` is a *phantom* made of one
  station's aliases;
- the full audit trail lands in `stop_equivalences.csv` + the
  `stop_equivalences` table (§4).

Phase 3 **recomputes** equivalences rather than reading the Phase 2 table,
so the pipelines stay independently runnable (dry runs included). Distance
and name evidence are schedule-independent; only pairs rescued exclusively
by shared routes can differ (full feed in Phase 3 vs service-day filtered
in Phase 2). A pair holding both parent-station and dedupe evidence keeps
the `same_parent_station` label, so report counts undercount total alias
evidence — the CSV is the complete audit.

### 3.4 Near stations & same-line stations (node relations)

Per stop, **two complementary relations**:

- **`near_stations`** — geographic proximity, any line: the stops within
  the proximity threshold, one tuple per (near stop, line active that
  service date), sorted by walking time:

```
[(ID, station, linea, minutes), …]              # times are bare numbers, always minutes
ID            = the near stop's sequential node id (the CSV's first column:
                1..N in row order — the key all relation columns reference;
                stop_uid remains the durable cross-run key)
linea         = route_short_name, falling back to route_id
minutes       = max(1, round(distance_m / 83))   # walking; 83 m/min ≈ 5 km/h, straight-line
stop_line_ids = "{ID}-{line}" per line serving the row's own stop
system        = distinct agency names of the serving lines ("Mi Macro, Sitren")
```

- **`same_line_stations`** — route topology, same line only: the
  **adjacent stations in route order** (previous/next stop per line and
  direction, from `stop_times` sequence), regardless of distance:

```
[(ID, station, linea, dir{d}, {minutes}), …]
minutes = SCHEDULED in-vehicle time of the segment (median departure-time
          difference across the day's trips, 1 decimal); falls back to the
          straight-line distance at ~25 km/h when the segment has no
          departure times in stop_times
```

Inclusion criteria differ deliberately: `near_stations` = "what can I walk
to" (distance only, no route filter); `same_line_stations` = "what comes
before/after on my line" (sequence only, no distance threshold).

**Optional street-network walking distances** (`--network-walk`,
`analysis/walk_network.py`): instead of the straight line, each near pair's
`distance_m` becomes the distance **walked along OSM streets** — walkable
`planet_osm_line` ways (motorways excluded) inside the stops' buffered hull
are fetched in one indexed query, built into a vertex graph in the metric
CRS (vertices merged at 0.1 m), stops are snapped via KD-tree, and routed
with **scipy's C Dijkstra** (`csgraph.dijkstra`, cutoff 800 m, sources
batched 16 per call to bound the dense distance-matrix memory); both snap
offsets are added and a street path is never allowed to be shorter than the
straight line. Pairs without a path keep the straight-line value (counted
in the log). Applied AFTER the connectivity edges — `route_connections.
min_stop_distance_m` stays geodesic — and BEFORE near-stations/GeoPackage,
so `near_stations` minutes and `near_links.walk_min` reflect real walking.
Measured on these feeds: mean detour ≈ 2.2× the straight line.

**Walk-graph cache** (`analysis/graph_cache.py`, on by default): the built
graph arrays are persisted as a compressed `.npz` in `.gtfs_cache/`
(`--walk-cache-dir` to relocate, `--no-walk-cache` to bypass). The cache
key hashes every input that shapes the graph — sorted feed ids, hull
buffer, OSM table/geometry column, excluded highways, metric CRS — plus
two DB-state probes (`count(*)`/`max(osm_id)` of the line table, and the
feeds' `stops max(loaded_at)`), so an OSM reimport or stop reload
invalidates automatically. Corrupt cache files are treated as misses.

**Bulk loading** (`gtfs_ingest/bulk.py`): all plain-scalar tables (stops
via a temp-table stage that builds `geom` in SQL, both headway tables,
`route_connections`, `stop_equivalences`, `stop_osm_matches`,
`stop_side_of_street`) load via psycopg3 `COPY`, falling back to the
chunked executemany path (with a logged warning) if COPY ever fails —
inside the same transaction either way. `interchanges` (array + geometry
expression columns) deliberately stays on executemany.

**Estimate provenance is auditable per stop**: every run writes
`near_stations_quality.csv` beside the main CSV (same rows, same `ID` key)
flagging which walking minutes were network-routed vs straight-line, which
same-line minutes are scheduled observations vs the 25 km/h estimate (and
how many trips back each median), the Phase 3 OSM coordinate confidence,
and the dedupe canonical id — see §4.2. A measured 4 and a guessed 4 look
identical in the main file; the sidecar is where they differ.

### 3.5 Route geometry (Phase 2 exports)

- **`route_shapes`** — real polylines from `shapes.txt` (points ordered by
  `shape_pt_sequence`), one feature per distinct
  `(route, direction, shape_id)` of the analyzed service day.
- **`route_stop_links`** — **directed** consecutive-stop segments per
  `(route, direction)` derived from `stop_times` order and deduplicated
  across trips: each stop chains to the *next stop of its own route*.
  Each segment carries `travel_min` — the scheduled in-vehicle minutes
  (median departure-time difference across the day's trips; NULL when the
  segment has no timed rows). Distinct from `near_links` (the walking
  overlay).

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

### 3.11 Neo4j export (`export-neo4j`, offline)

Reshapes an existing report directory into a Neo4j load package
(`<report>/neo4j/`): data CSVs with `neo4j-admin` inline headers
(`stop_uid:ID(Stop)`, `:START_ID(Stop)`, typed props — the ID spaces
prevent the Stop/Station id collision, since a Station's id IS its
canonical stop's uid) plus a generated `load.cypher` (constraints
`IF NOT EXISTS`, `MERGE`, `IN TRANSACTIONS` — idempotent),
`post_import.cypher` and `admin_import.txt`. Model: `:Stop` / `:Route`
(metadata from the route GraphML) / `:Station` (multi-member dedupe groups
only) with `IN_VEHICLE` (directed, `time_min`), `WALK` + `SAME_STATION`
(stored once, queried undirected), `SERVES` (derived from in_vehicle
endpoints) and `PART_OF`. No new dependency, no live connection; no data
value is ever interpolated into the Cypher (injection-safe by
construction). Reads only files, so it works on any saved run.

---

## 4. Outputs & what is stored

### 4.1 Database tables (PostGIS)

| Table | Producer / idempotency | What each row stores |
| --- | --- | --- |
| `stops` | ingest / per-feed replace | one normalized stop: `stop_uid` PK, `stop_num_id` (unique), `feed_id`, `stop_id`, `stop_name`, lat/lon, `location_type`, `parent_station`, `geom` POINT 4326 (GiST), `loaded_at` |
| `route_stop_headways` | analyze-frequency / per-feed replace | headway stats per (feed, **service_date**, route, direction, stop, window) — PK includes the date, so range runs store one row per served day: `trip_count`, mean/median/min/max headway seconds |
| `route_headways` | analyze-frequency / per-feed replace | the rollup per (feed, **service_date**, route, direction, window): mean headway, `stop_count`, `stops_with_headway`, `trip_count` |
| `route_connections` | analyze-frequency / full reload | one undirected route pair: `route_a < route_b`, feed ids, `connection_type`, `shared_stop_count`, `min_stop_distance_m` |
| `stop_equivalences` | analyze-frequency / full reload | one accepted cross-feed alias pair (§3.3b): `group_id`, `canonical_uid`, `stop_uid_a < stop_uid_b` PK, `distance_m`, `name_score`, `shared_routes`, `score` (empty after a `--no-dedupe` run) |
| `interchanges` | detect-interchanges / full reload | one cluster: `cluster_id`, `is_multimodal`, `stop_count`, `physical_stop_count` (distinct stations after dedupe), `modes` int[], `mode_names` text[], `member_stop_uids` text[], `routes` text[], `feed_ids` text[], `centroid` + `hull` geometry |
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
| `near_stations.csv` | analyze-frequency | one row per stop: `ID` (sequential node id, 1..N — the key of every relation column), `stop_uid`, `station`, `system` (serving system names), `stop_id`, lat/lon, `stop_line_ids` (own `ID-line` combos), `near_stations` `[(ID, station, linea, walk minutes), …]`, `same_line_stations` `[(ID, station, linea, dir, scheduled minutes), …]` — all times bare numbers in minutes. Cross-feed aliases of one physical station are excluded from each other's lists by default (§3.3b; `--no-dedupe` restores them) |
| `stop_equivalences.csv` | analyze-frequency | the dedupe audit (§3.3b): one row per accepted alias pair — `group_id`, `canonical_uid`, both `stop_uid`s, `distance_m`, `name_score`, `shared_routes`, `score`. Only written when dedupe is on |
| `near_stations_quality.csv` | analyze-frequency | provenance sidecar, always written: same rows and `ID` key as `near_stations.csv`. Per stop: `walk_source` (network / straight_line / mixed / unsnapped >200 m snap / none), `travel_source` (observed / estimated / mixed / none), `travel_n_obs` (worst-case trips behind the scheduled medians), `coord_confidence` (numeric score / needs_review / no_match from Phase 3 `stop_osm_matches`; `unavailable` in dry runs or before conflate-osm), `station_dedup` (dedupe `canonical_uid`, empty when unaliased). The main CSV's format never changes because of it |
| `station_network.graphml` | analyze-frequency | the **routable** stop-level network (nx.MultiDiGraph): nodes = stops (`stop_uid` key, `near_id`/`name`/`feed`/`canonical_uid`/lat/lon), edges typed `in_vehicle` (directed, scheduled `travel_min`), `walk` (reciprocal, walking minutes) and `same_station` (reciprocal, 0 min, dedupe alias links) — `nx.shortest_path(..., weight="time_min")` works directly |
| `station_network_nodes.csv` / `station_network_edges.csv` | analyze-frequency | the same network flattened for pandas/Excel: nodes with the attributes above; edges with `edge_type, stop_uid_a, stop_uid_b, time_min, linea, direction_id, route_uid, distance_m` (reciprocal arcs deduped to one row per pair) |
| `route_stop_headways.csv` / `route_headways.csv` | analyze-frequency | the headway tables as files, written on every run incl. dry runs — same columns as the DB tables (with `service_date`; one row set per served day on range runs) |
| `neo4j/` (package) | export-neo4j | offline Neo4j load package (§3.11): 8 data CSVs with admin-import headers (`stops`, `routes`, `stations`, `rel_in_vehicle`, `rel_walk`, `rel_same_station`, `rel_serves`, `rel_part_of`) + `load.cypher` (idempotent LOAD CSV) + `post_import.cypher` + `admin_import.txt` |
| `transit_analysis.gpkg` | analyze-frequency | 5 EPSG:4326 layers: **`stops`** (points + the CSV attributes), **`near_links`** (walking lines: both num ids, `distance_m`, `walk_min`), **`route_shapes`** (real route polylines: route_uid, linea, direction, shape_id), **`route_stop_links`** (directed consecutive-stop segments: route_uid, linea, direction, both stop_uids, `travel_min` scheduled minutes), **`station_network`** (the routable network's edges as lines, typed + time-weighted) |
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
