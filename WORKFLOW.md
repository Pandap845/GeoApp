# Data Workflow

This document follows the data through the whole pipeline — from raw GTFS
zips and an OSM extract to the PostGIS tables, CSV/GeoPackage/GraphML
exports, and reports. It explains **how each stage processes the data, how
it is normalized, and every calculation applied**, with the exact formulas.

For install/run instructions see [README.md](README.md); this file is about
what happens to the data.

```
                     ┌──────────────────────────── PHASE 1: ingest ────────────────────────────┐
 GTFS feeds (.zip/dir) → discover → load (partridge, raw) → validate → normalize stops → PostGIS `stops`
                     └──────────────────────────────────┬─────────────────────────────────────┘
                                                        │ validation_report.json/.md
                     ┌──────────────────── PHASE 2: analyze-frequency ────────────────────────┐
 same feeds ────────→ service-date filter → headway math ──────────────→ route_stop_headways / route_headways
                      route↔stop map      → connectivity (stop/parent/proximity) → route_connections
                      stops + distances   → near-stations relation → near_stations.csv / transit_analysis.gpkg
                      routes + edges      → networkx graph → route_connectivity.graphml
                     └──────────────────────────────────┬─────────────────────────────────────┘
                                                        │ analysis_report.json/.md
                     ┌──────────────────────── PHASE 3: conflation ──────────────────────────┐
 same stops ────────→ DBSCAN + parent merge → interchanges (multimodal detection, transfers check)
 OSM in PostGIS ────→ nearest transit node (KNN) → stop_osm_matches (confidence score)
                      nearest highway line (KNN) → stop_side_of_street (left/right sign math)
                     └──────────────────────────────────┬─────────────────────────────────────┘
                                                        │ interchanges_report / conflation_report
```

---

## 1. Ingestion (Phase 1 — `ingest`)

### 1.1 Feed discovery & identity

Inputs can be `.zip` files, unzipped feed folders, or a folder containing
many feeds. Every feed receives a deterministic **`feed_id`**:

1. Take the zip filename without extension (or the folder name).
2. Lowercase; replace each run of non-`[a-z0-9]` with `_`; trim edge `_`.
3. Empty result → `feed`; a collision appends `_<first 8 hex of
   sha256(absolute path)>`.

Example: `improved-gtfs-mimacro-mitren-sitren.zip` →
`improved_gtfs_mimacro_mitren_sitren`. The `feed_id` is the provenance key
carried into **every** downstream table and file.

### 1.2 Loading

Feeds are opened with `partridge.load_raw_feed` — deliberately **raw**: all
values stay strings, nothing is coerced, no service filtering. Validation
must see the file as published; all typing happens explicitly in the
normalizer. Encodings are sniffed by partridge; a file that still cannot be
parsed becomes a per-table error (a finding), never a crash. A feed whose
container is unreadable (corrupt zip) is recorded as failed and the run
continues with the other feeds — this isolation applies at every stage of
every phase.

### 1.3 Validation

All checks run for every feed (nothing stops at the first error). Findings
carry a severity — **ERROR** (spec violation / data loss), **WARNING**
(recoverable; rows skipped or condition non-blocking), **INFO** (inventory):

- required files present (`agency`, `stops`, `routes`, `trips`,
  `stop_times`; `calendar` OR `calendar_dates`), non-empty, parseable;
- required columns per file;
- `stops.txt`: non-null `stop_id`, duplicate `stop_id`s, coordinates
  parseable and inside WGS84 ranges (lat ∈ [−90, 90], lon ∈ [−180, 180]);
- referential integrity: `trips.route_id → routes`, `stop_times.trip_id →
  trips`, `stop_times.stop_id → stops`, `parent_station → stops` (WARNING);
- optional-file inventory (feeds × optional-file matrix in the report).

Validation only *reports*; the next stage *repairs by exclusion*, and every
excluded row is counted, so `source rows − dropped = loaded rows` is always
reconstructible from the report.

### 1.4 Normalization of stops (the common schema)

Input: raw `stops.txt` (strings). Output: one uniform GeoDataFrame per feed.
Transformations, in order:

| # | Transformation | Rule |
| --- | --- | --- |
| 1 | Uniform columns | missing optional columns (`stop_name`, `location_type`, `parent_station`) added as NULL |
| 2 | String cleanup | all strings whitespace-stripped; `""` → NULL |
| 3 | Coordinate typing | `stop_lat`/`stop_lon` → float; unparseable → NaN |
| 4 | `location_type` typing | → int; blank/unparseable → `0` (GTFS default) |
| 5 | Drop: no key | rows with null/blank `stop_id` dropped (counted) |
| 6 | Drop: no geometry | rows with missing/unparseable/out-of-range coordinates dropped (counted) — including coordinate-less `location_type` 3/4 rows: a geometry table cannot hold a row without a point |
| 7 | Deduplication | duplicate `stop_id`: **first occurrence wins** (file order, deterministic); rest dropped (counted) |
| 8 | Identity | `stop_uid = "{feed_id}:{stop_id}"` and `stop_num_id` (below) |
| 9 | Geometry | shapely `POINT(lon lat)`, CRS **EPSG:4326** (WGS84 — what GTFS mandates; never reprojected at rest) |

**Numeric ID calculation** — `stop_num_id` is a deterministic numeric alias
of `stop_uid`:

```
stop_num_id = int( first 6 bytes of sha256(stop_uid) )      # 48 bits, 0 … 2.8e14
```

Example: `improved_gtfs_mimacro_mitren_sitren:2771` → `226672952178765`.
Properties: stable across runs and feed sets, unique in practice
(collision odds ≈ n²/2²⁹ — about 1e-5 at 100k stops, and a unique DB index
would surface one), fits `bigint` and JavaScript numbers. `stop_uid` stays
the human-readable durable key; `stop_num_id` is the machine key used in
node relations.

### 1.5 Load into PostGIS

Geometry is built server-side: `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`.
**Idempotency = per-feed delete-and-reload in one transaction**: re-running
a feed atomically replaces exactly that feed's rows (removes stops that
disappeared from newer feed versions — an upsert would strand them), and
readers never see a half-loaded feed. GiST index on `geom`, unique index on
`stop_num_id`.

---

## 2. Service analysis (Phase 2 — `analyze-frequency`)

### 2.1 Service-date filtering

Headways depend on *which trips run that day*. partridge resolves the
active `service_id`s for the chosen date from `calendar.txt` (weekday
pattern + validity range) **and** `calendar_dates.txt` exceptions (type 1
added / type 2 removed). Without `--service-date`, each feed's **busiest
date** (most active trips) is used and recorded. The filter cascades: only
trips/stop_times of the selected services remain. Times are converted to
**seconds since service-day midnight**; GTFS times past 24:00 stay as-is
(`25:10:00` → 90600 s — the trip still belongs to that service day).

### 2.2 Headway calculation

**Definition:** a headway is the gap between two consecutive scheduled
**departures** at the same stop, for the same
`(feed_id, route_id, direction_id)`, inside one time window.

For each group and window `[start, end)` (defaults: am_peak 06:00–09:00,
midday 09:00–15:00, pm_peak 15:00–19:00, evening 19:00–23:00; overridable,
and a window may extend past 24:00 like `23:00–27:00`):

```
departures  = sorted departure_s within [start, end)
gaps        = departures[i+1] - departures[i]          # N departures → N−1 gaps
trip_count  = N
mean/median/min/max_headway_s = statistics over gaps    # seconds
```

- `N < 2` → headway **undefined**: the row is kept with its `trip_count`
  and NULL statistics (marked, never dropped silently).
- Zero departures in a window → no row.
- Blank `direction_id` → sentinel `-1` (unknown groups separately; opposite
  directions are never merged).
- Rows without a `departure_time` (non-timepoints) are excluded.
- `frequencies.txt`-defined service is **not** expanded — for
  frequency-based feeds the headway truth largely lives in
  `frequencies.headway_secs`, and the tables will be sparse (documented
  limitation).

**Route rollup** (`route_headways`), per `(route, direction, window)`:

```
mean_headway_s     = mean( stop-level mean_headway_s over stops with a defined headway )
stop_count         = distinct stops served in the window
stops_with_headway = stops with ≥ 2 departures
trip_count         = max( stop-level trip_count )   # proxy for runs through the corridor
```

### 2.3 Route connectivity calculation

Routes are `route_uid = "{feed_id}:{route_id}"`. Route↔stop pairs come from
the service-filtered `stop_times ⋈ trips`. Two routes connect when
(precedence order, strongest wins as `connection_type`):

1. **`same_stop`** — an identical `stop_uid` (same feed by construction);
   `shared_stop_count` = number of shared stops; `min_stop_distance_m = 0`.
2. **`same_parent_station`** — stops sharing a `parent_station` (a stop
   that *is* the parent belongs to its own station).
3. **`proximity`** — stops within `--proximity-m` (default **150 m**).
   Distance is geodesic: PostGIS `ST_DWithin/ST_Distance` on `geography`
   when a DB is available, otherwise a local UTM projection. Proximity is
   the only connection type that can cross feeds.

Edges are undirected, normalized to `route_a < route_b`, one row per pair.
The same edges feed a networkx graph (all analyzed routes are nodes, so
isolated routes are visible) exported as GraphML.

### 2.4 Near-stations relation

For every stop, the stops within the proximity threshold, rendered as

```
[(ID, station, linea, time to reach), …]     one tuple per (near stop, line)
```

Calculations and conventions:

```
ID            = stop_num_id of the near stop            (numeric; joins against the stops table)
linea         = route_short_name, falling back to route_id
                (only lines active on the analyzed service date; an unserved
                 station node appears in no one's list)
time to reach = max(1, round(distance_m / 83))          # minutes; 83 m/min ≈ 5 km/h walking
stop_line_ids = "{stop_num_id}-{line}" per line serving the row's own stop
```

Walking time is straight-line — no street network is consulted (that is
Phase 3 territory). The same data ships as **`transit_analysis.gpkg`**:
layer `stops` (points with all attributes) and layer `near_links` (one
LineString per near pair with both numeric ids, `distance_m`, `walk_min`).

### 2.5 Phase 2 persistence

`route_stop_headways` and `route_headways`: per-feed delete-and-reload
(same strategy as `stops`). `route_connections`: **full reload per run** —
proximity edges can span feeds, so rows aren't attributable to one feed;
always pass *all* feeds you care about in one run.

---

## 3. Interchanges & OSM conflation (Phase 3)

### 3.1 Interchange detection (`detect-interchanges`)

1. Stops (all feeds, full feed — an interchange is infrastructure, not a
   schedule property) are projected to a **metric CRS** (auto-detected UTM
   zone, or `--crs`) and clustered with **DBSCAN** (`eps` = 120 m,
   `min_samples` = 2; configurable).
2. Clusters are **merged with `parent_station` groups** via connected
   components — a declared station is never split, even when its platforms
   are farther apart than `eps`. Components with ≥ 2 stops become clusters.
3. Each cluster collects the `route_type`s of the routes serving its
   members. Extended codes normalize to the basic set first (700-series →
   bus 3, 100-series → rail 2, 400-series → metro 1, …).
4. **`is_multimodal = (distinct modes > 1)`** — the bus ↔ train case.
   Single-mode clusters are kept, labeled as such. Geometry: centroid +
   convex hull of the member stops (EPSG:4326).

**Transfers cross-check:** where a feed ships `transfers.txt`, each
declared `(from_stop, to_stop)` pair *agrees* with the inference iff both
stops landed in the same cluster. The report shows declared-and-inferred,
declared-but-NOT-inferred (examples listed), and inferred clusters backed
by no declaration.

### 3.2 Stop ↔ OSM matching (`conflate-osm`, Part B)

OSM lives in the same PostGIS database (osm2pgsql classic schema; table and
column names configurable, validated at startup with fail-fast messages).
For each stop, the nearest OSM node tagged `highway=bus_stop`,
`public_transport=platform|stop_position|station`, or
`railway=station|halt|tram_stop` is found with the index-assisted KNN
operator (`<->`), and the true geodesic distance is measured on
`geography`.

**Confidence formula** (threshold `--match-threshold-m`, default 50 m):

```
distance_score = max(0, 1 − distance_m / threshold_m)
type_score     = 1.0  station-stop ↔ station node, or platform-stop ↔ platform-ish node
                 0.8  platform-stop ↔ station node
                 0.6  station-stop ↔ non-station node
                 0.7  otherwise/unknown
confidence     = round(distance_score × type_score, 3)
needs_review   = no candidate  OR  distance > threshold  OR  confidence < 0.5
```

### 3.3 Side of street (Part C)

For each stop, the nearest OSM highway centerline (KNN, threshold
`--street-threshold-m`, default 50 m), then pure planar math in the metric
CRS:

```
segment = nearest segment of the way to the stop
d       = segment direction vector in TRAVEL direction
          (digitized coordinate order; reversed when oneway ∈ {-1, reverse})
v       = vector from the stop's perpendicular foot to the stop
cross   = d.x·v.y − d.y·v.x
side    = left  if cross > 0        ("driver's view")
          right if cross < 0
          on    if perpendicular distance ≤ 1 m
```

Worked example: way digitized `(0,0) → (100,0)` (+x), stop at `(50, 10)`:
`d = (1,0)`, `v = (0,10)`, `cross = +10` → **left**. With `oneway=-1`
travel is −x → the same stop is **right**.

`side_confidence = low` when the geometry is fragile: side is `on`, the
foot lies within 10 m of a way end (intersection proxy), or the
perpendicular distance is under 2 m. A stop with no highway inside the
threshold keeps a NULL way/side — recorded, never a crash.

### 3.4 Phase 3 persistence

`interchanges`, `stop_osm_matches`, `stop_side_of_street`: **full reload
per run** (derived artifacts; cluster ids are run-scoped and matches depend
on the current OSM import).

---

## 4. Every output at a glance

| Artifact | Producer | Content |
| --- | --- | --- |
| `stops` (PostGIS) | ingest | normalized stops, `stop_uid` + `stop_num_id`, POINT 4326, GiST |
| `validation_report.json/.md` | ingest | findings per feed, row counts, optional-file matrix |
| `route_stop_headways` / `route_headways` | analyze-frequency | headway statistics per stop/window and route/window |
| `route_connections` | analyze-frequency | undirected route pairs with connection_type / shared stops / min distance |
| `near_stations.csv` | analyze-frequency | per stop: numeric id, combined `id-line` ids, `[(ID, station, linea, time), …]` |
| `transit_analysis.gpkg` | analyze-frequency | GeoPackage layers `stops` (points) + `near_links` (lines) |
| `route_connectivity.graphml` | analyze-frequency | networkx route graph for Gephi/yEd/networkx |
| `analysis_report.json/.md` | analyze-frequency | service dates used, headway/connectivity summary |
| `interchanges` | detect-interchanges | clusters: modes, `is_multimodal`, members, centroid + hull |
| `interchanges_report.json/.md` | detect-interchanges | mode combinations, transfers agreement |
| `stop_osm_matches` / `stop_side_of_street` | conflate-osm | OSM match + confidence; way + side + side confidence |
| `conflation_report.json/.md` | conflate-osm | match rate, review counts, side distribution |

## 5. Cross-cutting rules

- **Provenance everywhere:** `feed_id` is a column in every table; feeds
  are never mixed in headway math.
- **Failure isolation:** a broken feed/file/row is logged, counted in the
  report, and skipped; only run-level problems (unreachable DB, missing
  PostGIS/OSM layers, unwritable report dir) abort a run.
- **Determinism:** feed ids, numeric stop ids, dedup order, cluster ids,
  and edge ordering are all deterministic for identical inputs.
- **Idempotency:** per-feed replace for feed-scoped tables; full reload for
  run-scoped derived tables — re-running any command is always safe.
- **CRS policy:** data at rest is always EPSG:4326 (GTFS's own datum);
  metric math (DBSCAN, side-of-street, local distances) happens in an
  auto-detected UTM zone or a configured CRS; authoritative distances use
  PostGIS `geography` (geodesic meters).
