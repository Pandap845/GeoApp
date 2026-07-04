# GTFS Ingestion, Validation & Service Analysis Pipeline

**Phase 1 (`ingest`):** `gtfs_ingest` ingests one or more GTFS feeds
(`.zip` files or unzipped directories), validates their integrity
**without ever crashing on a broken feed**, normalizes stops into a common
cross-feed schema with provenance, and loads them into a
PostgreSQL/PostGIS `stops` table. Every run also emits a
validation/coverage report in JSON (machine-readable) and Markdown
(human-readable).

**Phase 2 (`analyze-frequency`):** computes service-date-aware **headways**
per (route, direction, stop) and time window, route-level frequency
rollups, and a **route-to-route connectivity graph** (shared stops, shared
parent stations, spatial proximity) exported to PostGIS tables and GraphML.
See [Phase 2 documentation](#phase-2-headway-analysis--route-connectivity).

**Phase 3 (`detect-interchanges`, `conflate-osm`):** clusters stops into
(multimodal) **interchanges** with a transfers.txt cross-check, matches
every stop to the nearest **OSM transit node** with a confidence score, and
determines the **side of the street** each stop is on relative to the
nearest OSM highway centerline. OSM-in-PostGIS is the only reference
source — no external APIs. See
[Phase 3 documentation](#phase-3-interchange-detection--osm-conflation).

Downstream phases (headway analysis, multimodal interchange detection) will
consume the `stops` table and the JSON report; their contracts — the table
schema, `feed_id` semantics, and finding codes — are documented here and are
considered stable.

> **[WORKFLOW.md](WORKFLOW.md)** traces the data end-to-end — every
> processing stage, normalization rule, and calculation formula in one
> narrative document. This README focuses on running and operating the
> pipeline.

---

## 1. Requirements

- **Python 3.11+**
- **Docker** (optional but easiest) *or* any reachable PostgreSQL server
  with the **PostGIS** extension — only needed for real loads; `--dry-run`
  needs no database at all.
- GTFS feeds to ingest (`.zip` and/or unzipped folders).

## 2. Setup

From the repository root:

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

```bash
# Linux / macOS
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Verify the install:

```bash
python -m gtfs_ingest.cli --help
```

## 3. Start the database (skip for dry runs)

The repo ships a ready `docker-compose.yaml` with PostGIS:

```bash
docker compose up -d --wait
```

This starts `postgis/postgis:16-3.4` as container `transit-db` on
`localhost:5432` (user `transit`, password `cambia_esto`, database
`transit`), with a persistent named volume and a healthcheck. SQL/shell
scripts dropped into `./init/` run once on first initialization of the data
volume (see `init/README.md`) — none are needed for the pipeline itself:
PostGIS comes pre-enabled and the pipeline creates its own schema.

> Port already in use? If you run a local PostgreSQL, change the mapping in
> `docker-compose.yaml` to `"5433:5432"` and use port 5433 in the URL below.

Using your own PostgreSQL instead: any server works as long as the PostGIS
extension is installed or your role may `CREATE EXTENSION postgis`.

## 4. Configure the connection

Copy the template and adjust if needed (the defaults already match the
Docker service):

```bash
cp .env.example .env     # PowerShell: Copy-Item .env.example .env
```

```dotenv
DATABASE_URL=postgresql+psycopg://transit:cambia_esto@localhost:5432/transit
```

Credentials are **never hardcoded** — the pipeline reads the URL from (in
precedence order) the `--db-url` flag, the `DATABASE_URL` environment
variable, or a local `.env` file. A bare `postgresql://` scheme is upgraded
to `postgresql+psycopg://` automatically.

## 5. Run

```bash
# Phase 1 - validate + load stops into PostGIS + write reports to ./out
python -m gtfs_ingest.cli ingest --feeds ./data/feeds --report ./out

# Same, passing the URL explicitly instead of via .env
python -m gtfs_ingest.cli ingest --feeds ./data/feeds --db-url $DATABASE_URL --report ./out
#   PowerShell: ... --db-url $env:DATABASE_URL ...

# Validation only - no database needed at all
python -m gtfs_ingest.cli ingest --feeds feedA.zip ./feedB_dir --report ./out --dry-run

# Phase 2 - headways + route connectivity (see Phase 2 docs below)
python -m gtfs_ingest.cli analyze-frequency --feeds ./data/feeds \
    --db-url $DATABASE_URL --service-date 2025-06-10 --report ./out
```

(For backward compatibility, `python -m gtfs_ingest.cli --feeds ...` without
a subcommand is treated as `ingest`.)

`--feeds` accepts any mix of:

| Input | Treated as |
| --- | --- |
| `metro.zip` | one feed |
| `./metro_dir/` (contains `agency.txt`/`stops.txt`) | one feed |
| `./data/feeds/` (contains zips and/or feed folders) | every `*.zip` + every feed-like subfolder inside |

Each feed gets a deterministic `feed_id` derived from its filename
(`My Feed (2026).zip` → `my_feed_2026`; see [design notes](#design-notes)).
Re-running is **safe and idempotent**: a feed's rows are atomically replaced,
other feeds are untouched.

### Verify the load

```bash
docker exec -it transit-db psql -U transit -d transit \
  -c "SELECT feed_id, count(*) FROM stops GROUP BY feed_id;"
```

## 6. What you get

**PostGIS table `stops`** (created automatically):

```sql
CREATE TABLE stops (
    stop_uid       text PRIMARY KEY,          -- "{feed_id}:{stop_id}"
    stop_num_id    bigint NOT NULL,           -- numeric alias (see below)
    feed_id        text NOT NULL,             -- provenance
    stop_id        text NOT NULL,             -- original GTFS id
    stop_name      text,
    stop_lat       double precision NOT NULL,
    stop_lon       double precision NOT NULL,
    location_type  integer NOT NULL DEFAULT 0,
    parent_station text,
    geom           geometry(Point, 4326) NOT NULL,
    loaded_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_stops_geom    ON stops USING GIST (geom);
CREATE INDEX ix_stops_feed_id ON stops (feed_id);
CREATE UNIQUE INDEX ux_stops_num_id ON stops (stop_num_id);
```

`stop_num_id` is a **deterministic numeric alias** of `stop_uid`: the first
48 bits of `sha256(stop_uid)` as an unsigned integer. It is stable across
runs and feed sets, unique in practice (collision odds at 100k stops are
~1e-5, and the unique index would surface one immediately), and fits both
`bigint` and JavaScript numbers. Consumers that need integer node ids use
it; `stop_uid` remains the durable human-readable key.

**Reports** in the `--report` directory:

- `validation_report.json` — machine-readable; exact serialization of the
  `RunReport` pydantic model (`gtfs_ingest/models.py` *is* the schema).
  Per feed: source path, status, row counts per file, optional-file
  presence map, all findings (severity, stable `code`, message, file,
  count, sample values), normalized/loaded stop counts.
- `validation_report.md` — human-readable: run summary, a
  **feed × optional-file matrix**, and per-feed row counts + findings.

## 7. CLI reference

```
python -m gtfs_ingest.cli ingest              --feeds PATH [PATH ...] [options]
python -m gtfs_ingest.cli analyze-frequency   --feeds PATH [PATH ...] [options]
python -m gtfs_ingest.cli detect-interchanges --feeds PATH [PATH ...] [options]
python -m gtfs_ingest.cli conflate-osm        [options]        # reads stops + OSM from the DB
```

Flags shared by both subcommands:

| Flag | Required | Description |
| --- | --- | --- |
| `--feeds PATH...` | yes | Feed zips, feed dirs, or collection dirs (mixable). |
| `--db-url URL` | no | PostgreSQL/PostGIS SQLAlchemy URL. Default: `DATABASE_URL` env var / `.env`. |
| `--report DIR` | no | Output directory for reports/exports. Default: `./out`. |
| `--dry-run` | no | Validate/analyze + report only; no database writes. |
| `--log-level LEVEL` | no | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default `INFO`. |

`ingest` only:

| Flag | Description |
| --- | --- |
| `--fail-on-error` | Exit code 2 if any feed produced an ERROR finding (for CI gates). |

`analyze-frequency` only:

| Flag | Description |
| --- | --- |
| `--service-date YYYY-MM-DD` | Service date to analyze (calendar + calendar_dates honored). Default: each feed's **busiest** date. |
| `--proximity-m METERS` | Stop proximity threshold for connectivity. Default `150`. |
| `--window NAME=HH:MM-HH:MM` | Replace the default time windows (repeatable). Hours may exceed 23, e.g. `night=23:00-27:00`. |

`detect-interchanges` only: `--eps METERS` (DBSCAN radius, default 120),
`--min-samples N` (default 2), `--crs EPSG:XXXXX` (metric CRS override).

`conflate-osm` only (no `--feeds` — reads the `stops` table):
`--match-threshold-m` (default 50), `--street-threshold-m` (default 50),
`--osm-line-table`, `--osm-point-table`, `--osm-geom-column`,
`--crs EPSG:XXXXX`. A DB is required even with `--dry-run`.

Exit codes: **0** run completed (feeds may still have findings — read the
report) · **1** fatal run-level problem (no feeds found, DB unreachable or
missing PostGIS, no DB URL on a non-dry run, unwritable report dir, invalid
`--service-date`/`--window`) · **2** errors found *and* `--fail-on-error`
was set (ingest only).

**Per-feed problems never abort the run** — a corrupt zip, missing files,
bad encodings, malformed rows, or a failed DB load for one feed are logged,
recorded in the report, and the pipeline continues with the next feed.

## 8. Configuration reference

Precedence: CLI flag → environment variable → `.env` file → default.

| Environment variable | CLI equivalent | Default |
| --- | --- | --- |
| `DATABASE_URL` (or `GTFS_DATABASE_URL`) | `--db-url` | — (required unless `--dry-run`) |
| `GTFS_REPORT_DIR` | `--report` | `out` |
| `GTFS_LOG_LEVEL` | `--log-level` | `INFO` |

## 9. Tests

```bash
pytest                # full suite, no database required
```

A live PostGIS integration test (schema DDL + idempotent reload + SRID
check) runs only when `GTFS_TEST_DATABASE_URL` is set, e.g.:

```powershell
$env:GTFS_TEST_DATABASE_URL = "postgresql+psycopg://transit:cambia_esto@localhost:5432/transit"
pytest tests/test_db.py
```

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Pipeline aborted: No database URL configured` | Set `DATABASE_URL` (or `--db-url`), or add `--dry-run`. |
| `PostGIS is not available in the target database` | Use the `postgis/postgis` image (plain `postgres` lacks it), or have a superuser run `CREATE EXTENSION postgis;`. |
| `connection refused` on localhost:5432 | Database not up (`docker compose up -d --wait`) or a port conflict — remap to `5433:5432` and adjust the URL. |
| Feed shows `status: failed` in the report | The container itself was unreadable (corrupt zip, permissions). Details are in that feed's `feed_unreadable` finding; the rest of the run is unaffected. |
| `stops_normalized` < stops.txt row count | Rows without a usable key/geometry were dropped; the exact counts and reasons are in the feed's `dropped_*` WARNING findings. |
| IDE marks `sqlalchemy` imports as unresolved | The IDE is not using `.venv`. VS Code: **Python: Select Interpreter** → `.venv` (preconfigured in `.vscode/settings.json`); PyCharm: set the project interpreter to `.venv`. |
| Init scripts in `./init/` "don't run" | They only execute on **first** initialization of an empty volume; `docker compose down -v` then `up` re-runs them. |

## Project layout

```
gtfs_ingest/
  config.py      pydantic-settings config (env/.env/CLI), logging setup
  models.py      pydantic models + GTFS schema constants (single source of truth)
  loader.py      feed discovery, feed_id derivation, partridge loading
  validators.py  all integrity checks -> structured Finding list
  normalizer.py  raw stops.txt -> common-schema GeoDataFrame (EPSG:4326)
  db.py          engine, DDL, idempotent per-feed load
  report.py      JSON + Markdown emission
  pipeline.py    orchestration + per-feed failure isolation
  cli.py         argparse entry point: ingest / analyze-frequency subcommands
  analysis/      Phase 2 subpackage
    service_filter.py   service-date + calendar_dates filtering (partridge)
    headways.py         headway math per route/direction/stop/window + rollup
    connectivity.py     shared-stop / parent_station / proximity edges
    graph_export.py     networkx graph, metrics, GraphML
    storage.py          DDL + idempotent loads for the analysis tables
    pipeline_day2.py    Phase 2 orchestration + report
  conflation/    Phase 3 subpackage
    clustering.py       DBSCAN + parent_station merge -> interchanges
    transfers_check.py  inferred vs declared transfers
    osm_match.py        OSM schema validation + nearest transit node + confidence
    side_of_street.py   nearest centerline + left/right sign math
    storage.py          DDL + idempotent loads for the Phase 3 tables
    pipeline_day3.py    Phase 3 orchestration + reports
tests/           pytest suite with synthetic fixture feeds
docker-compose.yaml  local PostGIS (postgis/postgis:16-3.4)
init/            optional first-boot SQL/shell scripts for the DB container
```

---

# How it works (design documentation)

```
--feeds paths
   │
   ▼
1. DISCOVER (loader.py)          zip files + feed directories -> FeedSource
   │                             deterministic feed_id assigned here
   ▼
2. LOAD (loader.py)              partridge.load_raw_feed, no type coercion;
   │                             unopenable feed -> load_error, run continues
   ▼
3. VALIDATE (validators.py)      all checks always run; findings collected,
   │                             classified ERROR / WARNING / INFO
   ▼
4. NORMALIZE (normalizer.py)     stops -> common schema + WGS84 POINT geometry;
   │                             unusable rows dropped and counted
   ▼
5. LOAD DB (db.py)               per-feed delete-and-reload into PostGIS
   │                             (skipped with --dry-run)
   ▼
6. REPORT (report.py)            out/validation_report.json + .md
```

Each feed goes through steps 2–5 independently inside a per-feed exception
boundary (`pipeline.process_feed`): any failure becomes an ERROR finding on
that feed and the next feed proceeds.

## Validation checks performed

All checks run for every feed; nothing stops at the first problem.

| Check | Finding code | Severity |
| --- | --- | --- |
| Feed container (zip/dir) unopenable | `feed_unreadable` | ERROR |
| Required file missing (`agency`, `stops`, `routes`, `trips`, `stop_times`) | `missing_required_file` | ERROR |
| Neither `calendar.txt` nor `calendar_dates.txt` present | `missing_calendar` | ERROR |
| Present file unparseable (encoding, malformed CSV) | `unreadable_file` | ERROR |
| Required file present but zero data rows | `empty_required_file` | ERROR |
| Required column absent from a file | `missing_required_columns` | ERROR |
| `stops.txt` rows with null/blank `stop_id` | `null_stop_id` | ERROR |
| Duplicate `stop_id` values | `duplicate_stop_ids` | ERROR |
| `stop_lat`/`stop_lon` missing, unparseable, or outside [-90,90]/[-180,180] | `invalid_coordinates` | ERROR |
| `parent_station` referencing a non-existent `stop_id` | `dangling_parent_station` | WARNING |
| `trips.route_id` → `routes.route_id` broken | `dangling_reference` | ERROR |
| `stop_times.trip_id` → `trips.trip_id` broken | `dangling_reference` | ERROR |
| `stop_times.stop_id` → `stops.stop_id` broken | `dangling_reference` | ERROR |
| Inventory of optional files present/absent | `optional_files` | INFO |
| A validator itself crashed on pathological input | `validator_crash` | ERROR |

Severity policy: **ERROR** = spec violation or data loss; **WARNING** =
recoverable oddity (affected rows are kept or skipped, loading proceeds);
**INFO** = inventory facts. Optional files tracked in the coverage matrix:
`calendar.txt`, `calendar_dates.txt`, `shapes.txt`, `transfers.txt`,
`frequencies.txt`, `feed_info.txt`, `fare_attributes.txt`, `fare_rules.txt`,
`pathways.txt`, `levels.txt`, `attributions.txt`.

Notes:

- `stop_lat`/`stop_lon` are treated as required *columns* (geometry is built
  from them) even though the GTFS spec makes them conditionally required.
  Per-row blanks on `location_type` 3/4 (generic node / boarding area) rows
  are legal per spec and are not flagged by the validator.
- Dangling `parent_station` is a WARNING, not an ERROR, because the stop is
  still a valid point without its station link.

## Normalization — every transformation, in order

Input: the raw `stops.txt` of one feed (loaded string-typed, exactly as
published). Output: one uniform GeoDataFrame per feed. Validation *reports*
defects; normalization *repairs by exclusion* — every dropped row is counted
in a WARNING finding, so `source rows − dropped = stops_normalized` is
always reconstructible from the report.

| # | Transformation | Rule |
| --- | --- | --- |
| 1 | Uniform columns | Missing optional columns (`stop_name`, `location_type`, `parent_station`) added as NULL so all feeds share one schema |
| 2 | String cleanup | All string columns whitespace-stripped; empty strings standardized to NULL |
| 3 | Coordinate typing | `stop_lat`/`stop_lon` coerced to `float`; unparseable → NaN (handled in step 6) |
| 4 | `location_type` typing | Coerced to `int`; blank/unparseable → `0` (the GTFS default, stop/platform) |
| 5 | Row filter: key | Rows with null/blank `stop_id` dropped (`dropped_null_stop_id`) |
| 6 | Row filter: geometry | Rows with missing/unparseable/out-of-range coordinates dropped (`dropped_invalid_coordinates`) |
| 7 | Deduplication | Duplicate `stop_id`: **first occurrence wins** (file order — deterministic); rest dropped (`dropped_duplicate_stop_id`) |
| 8 | Provenance & key | `feed_id` column added; `stop_uid = "{feed_id}:{stop_id}"` |
| 9 | Geometry | shapely `POINT(stop_lon stop_lat)` built via geopandas, CRS `EPSG:4326` |

## Design notes

- **`feed_id` derivation (deterministic):** zip filename without extension
  (or directory name) → lowercase → each run of non-`[a-z0-9]` becomes `_`
  → strip edge `_` (empty result → `feed`). Collision within a run appends
  `_<first 8 hex of sha256(absolute path)>`. Renaming a feed file changes
  its `feed_id` (and therefore replaces rather than updates its rows) —
  keep source filenames stable.
- **Raw loading:** feeds open with `partridge.load_raw_feed` (all-string
  dtypes, no service-date filtering) so validation sees files as published
  and all type coercion is explicit and testable in the normalizer.
  partridge sniffs character encodings; still-unparseable files become
  `unreadable_file` findings. partridge's service-date filtering remains
  available for later phases.
- **SRID 4326 (WGS84)** because GTFS mandates WGS84 for
  `stop_lat`/`stop_lon` — coordinates are stored exactly as published, no
  reprojection at ingest. Geometry is built server-side with
  `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`.
- **Idempotency = per-feed delete-and-reload in one transaction.** Chosen
  over row-level upsert because it (a) removes stops that disappeared from
  newer feed versions — upsert would strand stale rows; (b) is trivially
  atomic — readers never see a half-loaded feed; (c) leaves other feeds
  untouched. Trade-off (no DB-side row history) is acceptable in Phase 1
  because feeds are the system of record. Inserts run in 1 000-row batches.
- **Coordinate-less `location_type` 3/4 rows are dropped** even though the
  spec allows them: `stops` is a geometry table and a row without a point
  cannot live in it. They remain visible in the source row counts.
- **Stops load even when the feed has other ERRORs** (e.g. broken
  `stop_times`): partial data with provenance beats no data, and the report
  documents every defect. Feeds whose `stops.txt` is unusable contribute
  zero rows.
- **`stop_uid` is globally unique** because `feed_id`s are unique per run
  and the slug alphabet (`[a-z0-9_]`) can never contain the `:` separator.

---

# Phase 2: Headway analysis & route connectivity

```bash
python -m gtfs_ingest.cli analyze-frequency \
    --feeds ./data/feeds --db-url $DATABASE_URL \
    --service-date 2025-06-10 --report ./out \
    [--proximity-m 150] [--window am_peak=06:00-09:00 ...] [--dry-run]
```

Phase 2 **reuses** Phase 1's discovery, normalization, and database modules;
it adds the `gtfs_ingest/analysis/` subpackage. Feeds are **never mixed** in
headway math — every table row carries `feed_id`. With `--dry-run`, no
database is needed: reports and the GraphML are still produced, and
proximity falls back to local computation (see below).

## Service-date selection

Trips are filtered to those active on the chosen date using partridge,
honoring **both** `calendar.txt` (weekday pattern + validity range) and
`calendar_dates.txt` exceptions (added/removed service), including feeds
that ship only `calendar_dates.txt`. Without `--service-date`, each feed's
**busiest date** (most active trips) is auto-selected and recorded in the
report. A date with no service in a feed marks that feed `no_service` and
the run continues.

## Headway definition (exact)

A **headway** is the gap between two consecutive scheduled **departures**
at the same stop, for the same `(feed_id, route_id, direction_id)`, within
one time window. Per group and window we report `mean / median / min / max`
headway **in seconds** plus `trip_count` (departures in the window):

- N departures in a window → N−1 gaps;
- **< 2 departures → headway undefined**: the row is kept with its
  `trip_count` and NULL statistics (marked, never crashed on); windows with
  zero departures are omitted;
- windows are half-open `[start, end)`;
- **times past 24:00 are not wrapped** (GTFS semantics: `25:10` belongs to
  the service day). They only match windows extended past 24:00, e.g.
  `--window night=23:00-27:00` — so owl service stays attributable to the
  analyzed date, and a gap across 24:00 (e.g. 23:50 → 00:10) is 20 min,
  not negative.
- `direction_id` is optional in GTFS; blank values become `-1` ("unknown")
  so they group separately rather than merging opposite directions.

Default windows: `am_peak 06:00–09:00`, `midday 09:00–15:00`,
`pm_peak 15:00–19:00`, `evening 19:00–23:00` (all overridable with
`--window`).

**Route rollup** (`route_headways`): per `(route, direction, window)`, the
mean of the *stop-level mean headways* across stops with a defined headway
(`stops_with_headway` of `stop_count`); `trip_count` is the max stop-level
departure count — a proxy for runs through the corridor without double
counting.

## Connectivity definition (exact)

Routes are identified as `route_uid = "{feed_id}:{route_id}"`. Two routes
are connected when (precedence order — strongest wins as `connection_type`):

| Precedence | connection_type | Condition |
| --- | --- | --- |
| 1 | `same_stop` | serve at least one identical stop (same `stop_uid`, so always same feed) |
| 2 | `same_parent_station` | serve stops sharing a `parent_station`; a stop that *is* the parent counts as part of its own station |
| 3 | `proximity` | serve stops within `--proximity-m` meters (default **150 m**) |

Each undirected pair appears once (`route_a < route_b`) with
`shared_stop_count` (identical stops, 0 if none) and `min_stop_distance_m`
(0.0 for same-stop pairs; smallest inter-stop distance for proximity pairs;
NULL for parent-only pairs farther apart than the threshold).

Distances are **geodesic meters**: computed in **PostGIS on `geography`**
(the authoritative path; the pipeline refreshes the Phase 1 `stops` table
for the analyzed feeds first, so queries match the analyzed data). In
`--dry-run` (no DB), a local fallback projects stops to an estimated UTM
zone — accurate to well under a meter at these scales within one region.

Connectivity is computed from the stops **actually served on the analyzed
service date**. Proximity is the only connection type that can cross feeds
(ids never match across feeds, geography does).

## Phase 2 outputs

Tables (created automatically, idempotent):

- `route_stop_headways` — PK `(feed_id, route_id, direction_id, stop_id,
  time_window)`; plus `service_date`, `trip_count`,
  `mean/median/min/max_headway_s`.
- `route_headways` — PK `(feed_id, route_id, direction_id, time_window)`;
  plus `service_date`, `mean_headway_s`, `stop_count`,
  `stops_with_headway`, `trip_count`.
- `route_connections` — PK `(route_a, route_b)`; `feed_id_a`, `feed_id_b`,
  `connection_type`, `shared_stop_count`, `min_stop_distance_m`.

Idempotency: the headway tables use **per-feed delete-and-reload** in one
transaction (same strategy and rationale as Phase 1 stops).
`route_connections` uses **full truncate-and-reload per run** because
proximity edges can span feeds, so rows aren't attributable to a single
feed — the table is cheap derived data recomputed from the analyzed set.

Files in `--report DIR`:

- `route_connectivity.graphml` — the networkx graph (nodes: analyzed routes
  with feed/route metadata; edges: the connection attributes), loadable in
  Gephi/yEd/networkx.
- `analysis_report.json` / `analysis_report.md` — per feed: status, the
  service date used and whether it was explicit or auto-picked, routes and
  trips analyzed; plus route-pair counts by connection type, graph metrics
  (components, sizes), and **isolated routes** (no connections at all).
- `near_stations.csv` — one row per stop with `stop_num_id`, `stop_uid`,
  `feed_id`, `stop_id`, `station` (name), `stop_lat`, `stop_lon`,
  `stop_line_ids`, and `near_stations`: the stations within the proximity
  threshold, formatted exactly as

  ```
  [(ID, station, linea, time to reach), (ID2, station, linea, time to reach)]
  ```

  one tuple per (near station, line) combination, sorted by walking time.
  Conventions: `ID` in the tuples is the near stop's **numeric id**
  (`stop_num_id`, matching the `stops` table column — node relations join
  directly against the database); `stop_line_ids` is the row's own set of
  **combined stop+line ids** `"{stop_num_id}-{line}"` (e.g.
  `128733941436-L10`), for consumers modelling one node per (station, line)
  pair; `linea` is the route's `route_short_name` (falling back to
  `route_id`), restricted to lines active on the analyzed service date — an
  unserved station node is omitted from neighbour lists; `time to reach` is
  straight-line distance at a **5 km/h walking speed** (~83 m/min), rounded
  to whole minutes with a 1-minute floor (no street network is consulted —
  Phase 3). "Near" reuses `--proximity-m` (default 150 m) and the same
  distance engine as connectivity (PostGIS geography, or local UTM math on
  dry runs).
- `transit_analysis.gpkg` — **GeoPackage** (open directly in QGIS/GDAL)
  with two EPSG:4326 layers: `stops` (points; numeric id, uid, feed/stop
  ids, name, location_type, parent_station, `stop_line_ids`, and the
  `near_stations` relation string) and `near_links` (one LineString per
  near pair with both numeric ids, geodesic `distance_m`, and `walk_min`).
  GeoPackage keeps 64-bit integers intact, so the numeric ids are safe
  (a shapefile would truncate them). The file is rewritten per run.

## Phase 2 assumptions

- Headways are schedule-based (`stop_times.departure_time`); rows without a
  departure time (non-timepoints) are excluded.
- `frequencies.txt`-based (headway-defined) service is **not** expanded in
  this phase; such trips contribute only their template `stop_times` rows.
- The route rollup is unweighted across stops (each stop counts equally).
- A feed that fails anywhere in analysis is recorded `failed` with the
  exception message in its notes; the run continues (same isolation
  contract as Phase 1).

---

# Phase 3: Interchange detection & OSM conflation

```bash
# Part A - multimodal interchange detection (+ transfers.txt cross-check)
python -m gtfs_ingest.cli detect-interchanges --feeds ./data/feeds \
    --db-url $DATABASE_URL --eps 120 --report ./out [--min-samples 2] [--crs EPSG:32618] [--dry-run]

# Parts B + C - OSM conflation + side-of-street
python -m gtfs_ingest.cli conflate-osm --db-url $DATABASE_URL \
    --match-threshold-m 50 --report ./out [--street-threshold-m 50] [--dry-run]
```

Phase 3 reuses Phase 1/2 modules (loader, normalizer, `db.py`, route/stop
maps). `conflate-osm` requires a database **even with `--dry-run`** — the
OSM extract inside PostGIS is the reference source (no Google Maps, no
external APIs); dry-run only skips the table writes.

## OSM schema assumptions (validated at startup)

Assumed layout (osm2pgsql "classic" import defaults; **all names
configurable**, never hardcoded at query sites):

| Config (CLI / env) | Default | Requirement |
| --- | --- | --- |
| `--osm-line-table` / `GTFS_OSM_LINE_TABLE` | `planet_osm_line` | LINESTRING geometries; columns `highway`, `oneway` |
| `--osm-point-table` / `GTFS_OSM_POINT_TABLE` | `planet_osm_point` | POINT geometries; columns `highway`, `railway`, `public_transport` |
| `--osm-geom-column` / `GTFS_OSM_GEOM_COLUMN` | `way` | any SRID (read from the data, transformed on the fly) |

A `name` column is used when present and skipped when not. On startup,
`conflate-osm` **fails fast** with one message listing every missing
table/column (and empty tables), before any per-stop work. Transit nodes
are OSM elements tagged `highway=bus_stop`,
`public_transport=platform|stop_position|station`, or
`railway=station|halt|tram_stop`.

## Part A — interchange detection

1. Stops (all feeds) are projected to a **metric CRS** — a UTM zone
   estimated from the data extent, overridable with `--crs`/`GTFS_METRIC_CRS`
   — and clustered with **DBSCAN** (`eps` = 120 m, `min_samples` = 2,
   both configurable).
2. DBSCAN clusters are **merged with `parent_station` groups** (connected
   components), so a declared station is never split, even when its
   platforms lie farther apart than `eps`.
3. Each cluster collects the GTFS `route_type`s of the routes serving its
   members (extended codes like 700-series bus are normalized to the basic
   0–12 set first). **> 1 distinct mode → `is_multimodal = true`** (the
   bus ↔ train case); single-mode clusters are kept, labeled as such.
4. Clustering uses the *full* feed, not a service date: an interchange is
   infrastructure, not a schedule property.

**transfers.txt cross-check** (feeds without the file are recorded and
skipped): a declared `(from_stop, to_stop)` pair *agrees* with the
inference when both stops land in the same cluster. The report shows both
directions — declared-and-inferred, declared-but-not-inferred (with
examples), and inferred clusters not backed by any declared transfer.

## Part B — OSM conflation (location validation)

For each stop, the nearest compatible OSM transit node is found with the
PostGIS **KNN operator (`<->`)** in a LATERAL join (index-assisted), and
the true distance is measured on `geography` (geodesic meters).

Confidence formula:

```
distance_score = max(0, 1 - distance_m / threshold_m)        # threshold: --match-threshold-m, default 50
type_score     = compatibility(stop.location_type, osm kind) # table below
confidence     = round(distance_score * type_score, 3)
```

| GTFS `location_type` | OSM kind | type_score |
| --- | --- | --- |
| 1 (station) | `station` (public_transport or railway) | 1.0 |
| 1 (station) | anything else | 0.6 |
| 0/other | `bus_stop`, `platform`, `stop_position`, `tram_stop`, `halt` | 1.0 |
| 0/other | `station` | 0.8 |
| 0/other | unknown | 0.7 |

`needs_review = true` when there is no candidate at all, the distance
exceeds the threshold, or `confidence < 0.5`.

## Part C — side-of-street (sign convention)

For each stop, the nearest OSM highway centerline (KNN again, threshold
`--street-threshold-m`, default 50 m). The side decision is planar math in
the metric CRS:

> Take the **nearest segment** of the way. Let `d` be the segment's
> direction vector *in travel direction* and `v` the vector from the stop's
> perpendicular foot to the stop. The 2D cross product
> `cross = d.x*v.y − d.y*v.x` is **positive → LEFT** of travel,
> **negative → RIGHT** ("driver's view").
>
> Travel direction = the way's **digitized direction** (coordinate order),
> **reversed when `oneway` is `-1`/`reverse`** (OSM's "one way against
> digitization"). Two-way / untagged ways use the digitized direction
> as-is — the side is then stable but relative to digitization.
>
> **Worked example:** way digitized `(0,0) → (100,0)` (pointing +x), stop
> at `(50, 10)`: `d = (1,0)`, `v = (0,10)`, `cross = 1·10 − 0·0 = +10` →
> **left**. With `oneway=-1`, travel is −x → the same stop is **right**.

`side = "on"` within 1 m of the centerline. `side_confidence = "low"` when
the answer is geometrically fragile: side is "on", the perpendicular foot
is within 10 m of a way end (intersection proxy — true junction detection
would need node degrees), or the perpendicular distance is under 2 m
(digitization noise dominates). A stop with no highway within the
threshold keeps NULL way/side (recorded, never a crash).

## Phase 3 outputs

Tables (created automatically; idempotency = **full delete-and-reload per
run** in one transaction — clusters get run-scoped ids and matches depend
on the current OSM import, so rows aren't attributable to a single feed):

- `interchanges` — cluster_id, `is_multimodal`, stop_count, `modes`
  (int[]) + `mode_names` (text[]), `member_stop_uids`, `routes`,
  `feed_ids`, `centroid` (Point 4326, GiST-indexed) and `hull` geometry.
- `stop_osm_matches` — stop_uid (PK), matched `osm_id`/`osm_kind`/
  `osm_name`, `distance_m`, `match_confidence`, `needs_review`.
- `stop_side_of_street` — stop_uid (PK), `osm_way_id`, `highway`,
  `oneway`, `perpendicular_distance_m`, `side` (left|right|on|NULL),
  `side_confidence`, `near_endpoint`.

Reports: `interchanges_report.json/.md` (clusters by mode combination,
multimodal count, transfers agreement stats) and
`conflation_report.json/.md` (match rate, review count, side distribution,
low-confidence count).

## Phase 3 assumptions

- OSM is imported into the **same** database as the stops (osm2pgsql or
  pyrosm; only the table/column names above matter).
- Nearest-node matching considers OSM *points* only (platform/station
  *areas* mapped as polygons land in the polygon table of an osm2pgsql
  import and are not matched in this phase).
- The intersection proxy for side confidence is endpoint proximity, not
  topological node degree.
- Modes come from `routes.txt route_type`; feeds lacking the column yield
  clusters with unknown modes (`is_multimodal = false`, noted per feed).
- The live-DB path is covered by an integration test gated on
  `GTFS_TEST_DATABASE_URL` (synthetic OSM tables built on the fly); all
  geometry/clustering math is unit-tested without a database.

## Out of scope

Pedestrian-network routing (walking times here remain straight-line),
polygon/area conflation, and turn-by-turn interchange path generation.
