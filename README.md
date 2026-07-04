# GTFS Ingestion & Validation Pipeline (Phase 1)

Ingests one or more GTFS feeds (`.zip` files or unzipped directories),
validates their integrity **without ever crashing on a broken feed**,
normalizes stops into a common cross-feed schema with provenance, and loads
them into a PostgreSQL/PostGIS `stops` table. Every run also emits a
validation/coverage report in JSON (machine-readable) and Markdown
(human-readable).

Downstream phases (headway analysis, multimodal interchange detection) will
consume the `stops` table and the JSON report; their contracts — the table
schema, `feed_id` semantics, and finding codes — are documented here and are
considered stable.

---

## Install

Requires Python 3.11+ and a PostgreSQL server with the PostGIS extension
available (only for non-dry runs).

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; use `source .venv/bin/activate` on POSIX
pip install -e .[dev]
```

Copy `.env.example` to `.env` and set your connection string (never hardcode
credentials — the pipeline only reads them from the environment/CLI):

```bash
DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname
```

### Local database via Docker

`docker-compose.yaml` provides a ready PostGIS instance whose credentials
match `.env.example`:

```bash
docker compose up -d --wait    # postgis/postgis:16-3.4 on localhost:5432
DATABASE_URL=postgresql+psycopg://transit:cambia_esto@localhost:5432/transit
```

SQL/shell scripts dropped into `./init/` run once on first initialization of
the data volume (see `init/README.md`); none are needed for the pipeline
itself — PostGIS comes pre-enabled and the pipeline creates its own schema.

## Run

```bash
# Full run: validate + load stops into PostGIS + write reports
python -m gtfs_ingest.cli --feeds ./data/feeds --db-url $DATABASE_URL --report ./out

# Validation only, no database needed
python -m gtfs_ingest.cli --feeds feedA.zip ./feedB_dir --report ./out --dry-run

# Extra flags
#   --log-level DEBUG|INFO|WARNING|ERROR
#   --fail-on-error     exit 2 if any feed produced an ERROR finding (for CI)
```

`--feeds` accepts any mix of: a single `.zip`, a single unzipped feed
directory, or a directory *containing* feeds (all `*.zip` files plus
subdirectories that contain `agency.txt`/`stops.txt`).

Exit codes: `0` run completed · `1` fatal run-level problem (no feeds found,
DB unreachable/missing PostGIS, bad config) · `2` errors found and
`--fail-on-error` was set. **Per-feed problems never abort the run** — they
are logged, recorded in the report, and the pipeline continues.

```bash
pytest            # run the test suite (no database required)
```

---

## The process, end to end

```
--feeds paths
   │
   ▼
1. DISCOVER (loader.py)          zip files + feed directories → FeedSource
   │                             deterministic feed_id assigned here
   ▼
2. LOAD (loader.py)              partridge.load_raw_feed, no type coercion;
   │                             unopenable feed → load_error, run continues
   ▼
3. VALIDATE (validators.py)      all checks always run; findings collected,
   │                             classified ERROR / WARNING / INFO
   ▼
4. NORMALIZE (normalizer.py)     stops → common schema + WGS84 POINT geometry;
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

### 1. Discovery & `feed_id` derivation

Every feed gets a **deterministic** `feed_id` used as the provenance key
everywhere downstream:

1. Take the zip filename without extension (or the directory name).
2. Lowercase; replace each run of non-`[a-z0-9]` characters with `_`; strip
   leading/trailing `_` (e.g. `My Feed (2026-Q1).zip` → `my_feed_2026_q1`).
3. Empty result → `feed`.
4. Collision within a run → append `_<first 8 hex of sha256(absolute path)>`,
   stable across runs on the same layout.

Renaming a feed file changes its `feed_id` (and therefore replaces rather
than updates its rows) — keep source filenames stable.

### 2. Loading

Feeds are opened with `partridge.load_raw_feed`, deliberately **raw**
(all-string dtypes, no service-date filtering): validation must see the file
as published, and all type coercion is done explicitly in the normalizer so
it is documented and testable. partridge sniffs character encodings; a file
it still cannot parse is recorded as a per-table error and reported as an
`unreadable_file` ERROR instead of raising. (partridge's service-date
filtering stays available for later phases.)

### 3. Validation checks performed

All checks run for every feed; nothing stops at the first problem.

| Check | Finding code | Severity |
| --- | --- | --- |
| Feed container (zip/dir) unopenable | `feed_unreadable` | ERROR |
| Required file missing (`agency`, `stops`, `routes`, `trips`, `stop_times`) | `missing_required_file` | ERROR |
| Neither `calendar.txt` nor `calendar_dates.txt` present | `missing_calendar` | ERROR |
| Present file unparseable (encoding, malformed CSV, empty file) | `unreadable_file` | ERROR |
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
**INFO** = inventory facts. Notes:

- `stop_lat`/`stop_lon` are treated as required *columns* (geometry is built
  from them) even though the GTFS spec makes them conditionally required.
  Per-row blanks on `location_type` 3/4 (generic node / boarding area) rows
  are legal per spec and are not flagged by the validator — but see the
  normalization note below.
- Dangling `parent_station` is a WARNING, not an ERROR, because the stop is
  still a valid point without its station link.
- Optional files tracked in the coverage matrix: `calendar.txt`,
  `calendar_dates.txt`, `shapes.txt`, `transfers.txt`, `frequencies.txt`,
  `feed_info.txt`, `fare_attributes.txt`, `fare_rules.txt`, `pathways.txt`,
  `levels.txt`, `attributions.txt`.

### 4. Normalization — every transformation, in order

Input: the raw `stops.txt` of one feed. Output: one uniform GeoDataFrame per
feed. Validation *reports* defects; normalization *repairs by exclusion* —
every dropped row is counted in a WARNING finding, so
`source rows − dropped = stops_normalized` is always reconstructible from
the report.

| # | Transformation | Rule |
| --- | --- | --- |
| 1 | Uniform columns | Missing optional columns (`stop_name`, `location_type`, `parent_station`) are added as NULL so all feeds share one schema |
| 2 | String cleanup | All string columns whitespace-stripped; empty strings standardized to NULL |
| 3 | Coordinate typing | `stop_lat`/`stop_lon` coerced to `float`; unparseable → NaN (handled in step 5) |
| 4 | `location_type` typing | Coerced to `int`; blank/unparseable → `0` (the GTFS default, stop/platform) |
| 5 | Row filter: key | Rows with null/blank `stop_id` dropped (`dropped_null_stop_id`) — no usable key |
| 6 | Row filter: geometry | Rows with missing/unparseable/out-of-range coordinates dropped (`dropped_invalid_coordinates`) |
| 7 | Deduplication | Duplicate `stop_id`: **first occurrence wins** (file order — deterministic); rest dropped (`dropped_duplicate_stop_id`) |
| 8 | Provenance & key | `feed_id` column added; `stop_uid = "{feed_id}:{stop_id}"` |
| 9 | Geometry | shapely `POINT(stop_lon stop_lat)` built via geopandas, CRS `EPSG:4326` |

Documented assumptions:

- **Coordinate-less `location_type` 3/4 rows are dropped** even though the
  spec allows them: the `stops` table is a geometry table and a row without
  a point cannot live in it. They remain visible in the source row counts.
- **First-wins deduplication** is arbitrary but deterministic; the duplicate
  values are listed in the validation findings for manual follow-up.
- `stop_uid` is globally unique because `feed_id`s are unique per run and
  the slug alphabet (`[a-z0-9_]`) can never contain the `:` separator.
- Stops are loaded **even when the feed has other ERRORs** (e.g. broken
  `stop_times`): partial data with provenance beats no data, and the report
  documents every defect. Feeds whose `stops.txt` is unusable contribute
  zero rows.

### 5. PostGIS load

Output table (created automatically, `CREATE ... IF NOT EXISTS`):

```sql
CREATE TABLE stops (
    stop_uid       text PRIMARY KEY,          -- "{feed_id}:{stop_id}"
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
```

Design decisions:

- **SRID 4326 (WGS84)** because GTFS mandates WGS84 for `stop_lat`/
  `stop_lon` — coordinates are stored exactly as published, no reprojection
  at ingest time. Geometry is built server-side with
  `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`.
- **Idempotency = per-feed delete-and-reload in one transaction.** Re-running
  a feed deletes its rows and inserts the fresh set atomically. Chosen over
  row-level upsert because it (a) correctly removes stops that disappeared
  from a newer feed version — upsert would strand stale rows; (b) is trivially
  atomic — readers never see a half-loaded feed; (c) leaves other feeds
  untouched, so feeds re-ingest independently. The trade-off (losing DB-side
  row history) is acceptable in Phase 1 because feeds are the system of
  record.
- Inserts run in 1 000-row batches to bound memory on very large feeds.
- `psycopg` (v3) is always used: a bare `postgresql://` URL is upgraded to
  `postgresql+psycopg://` automatically.

### 6. Reports

Written to the `--report` directory:

- `validation_report.json` — machine-readable; the exact serialization of
  the `RunReport` pydantic model (`gtfs_ingest/models.py` is the schema
  definition). Per feed: `feed_id`, source path, status, row counts per
  file, the optional-file presence map, all findings (severity, stable
  `code`, message, affected file, count, sample values), and the
  normalized/loaded stop counts.
- `validation_report.md` — human-readable: run summary table, a
  **feed × optional-file matrix**, and a per-feed section with row counts
  and findings.

---

## Project layout

```
gtfs_ingest/
  config.py      pydantic-settings config (env/.env/CLI), logging setup
  models.py      pydantic models + GTFS schema constants (single source of truth)
  loader.py      feed discovery, feed_id derivation, partridge loading
  validators.py  all integrity checks → structured Finding list
  normalizer.py  raw stops.txt → common schema GeoDataFrame (EPSG:4326)
  db.py          engine, DDL, idempotent per-feed load
  report.py      JSON + Markdown emission
  pipeline.py    orchestration + per-feed failure isolation
  cli.py         argparse entry point (python -m gtfs_ingest.cli)
tests/           pytest suite with synthetic fixture feeds (no DB needed;
                 a live PostGIS test runs only when GTFS_TEST_DATABASE_URL is set)
```

## Error-handling contract

- A broken feed (corrupt zip, missing files, bad encoding, malformed rows,
  pathological content) is logged, fully recorded in the report, and
  **skipped where necessary — the run always continues.**
- Only run-level problems abort: unreachable database, missing PostGIS,
  unwritable report directory, no DB URL on a non-dry run. These would fail
  every feed identically, so failing fast is correct.
- A database failure while loading one feed rolls back that feed's
  transaction (previous data intact), is recorded as a `db_load_failed`
  ERROR finding, and the run continues.

## Out of scope (later phases)

Headway analysis and multimodal interchange detection. Phase 1 deliberately
loads only stops; routes/trips/stop_times are validated but not persisted.
