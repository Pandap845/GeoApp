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

> **[docs/guide/workflow.md](docs/guide/workflow.md)** traces the data
> end-to-end — every processing stage, normalization rule, and calculation
> formula in one narrative document. This README focuses on running and
> operating the pipeline.

### Repository layout

| Path | Contents |
| --- | --- |
| `gtfs_ingest/` | the Python package (one subpackage per phase: `analysis/`, `conflation/`, `qa/`, `export/`) |
| `tests/` | pytest suite |
| `data/` | inputs: `mexico.osm.pbf` and `feeds/` (the two GTFS zips and `mibici.csv`) |
| `outputs/` | analysis runs (gitignored): `out_mibici_v2` (before) and `out_mibici_v3` (current) |
| `docs/` | Sphinx site; `guide/` holds the narrative docs, `plans/` the design notes, `figures/` the charts |
| `presentations/` | LaTeX decks and their PDFs (compile from inside that folder) |
| `scripts/` | one-off helpers, e.g. `build_branch_gpkg.py` |
| `paper/` | the article draft |
| `init/`, `docker-compose.yaml` | PostGIS container setup |

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

```ini
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

# A stops-only CSV merged in alongside real feeds (see "Stops-only CSV sources")
python -m gtfs_ingest.cli ingest --feeds ./data/feeds ./data/extra_stops.csv --report ./out
```

(For backward compatibility, `python -m gtfs_ingest.cli --feeds ...` without
a subcommand is treated as `ingest`.)

`--feeds` accepts any mix of:

| Input | Treated as |
| --- | --- |
| `metro.zip` | one feed |
| `./metro_dir/` (contains `agency.txt`/`stops.txt`) | one feed |
| `./data/feeds/` (contains zips and/or feed folders) | every `*.zip` + every feed-like subfolder inside |
| `extra_stops.csv` | one **stops-only** feed — see [Stops-only CSV sources](#stops-only-csv-sources) |

A `.csv` is only picked up when you **name it explicitly**. Collection
directories are never scanned for CSVs, because this pipeline's own
`near_stations.csv` output would itself be a valid stops CSV — globbing would
let a previous run's output be silently re-ingested as a second copy of the
network.

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
CREATE INDEX ix_stops_geog    ON stops USING GIST ((geom::geography));  -- proximity query
CREATE INDEX ix_stops_feed_id ON stops (feed_id);
CREATE UNIQUE INDEX ux_stops_num_id ON stops (stop_num_id);
```

`stop_num_id` is a **deterministic numeric alias** of `stop_uid`: the first
48 bits of `sha256(stop_uid)` as an unsigned integer. It is stable across
runs and feed sets, unique in practice (collision odds at 100k stops are
~1e-5, and the unique index would surface one immediately), and fits both
`bigint` and JavaScript numbers. It is the database machine key (also on
the GeoPackage `stops`/`near_links` layers); note that `near_stations.csv`
uses its own sequential `ID` (1..N) for the node relations instead —
`stop_uid` remains the durable human-readable key joining everything.

**Reports** in the `--report` directory:

- `validation_report.json` — machine-readable; exact serialization of the
  `RunReport` pydantic model (`gtfs_ingest/models.py` *is* the schema).
  Per feed: source path, status, row counts per file, optional-file
  presence map, all findings (severity, stable `code`, message, file,
  count, sample values), normalized/loaded stop counts.
- `validation_report.md` — human-readable: run summary, a
  **feed × optional-file matrix**, and per-feed row counts + findings.
- `run_manifest.json` — written by every command, one entry per command:
  the sha256 of each input file, the git commit (and whether tracked files
  had local changes), a sha256 of the package's own code (so uncommitted
  edits show up too), the package versions and every parameter value.
  Secrets (database URL, API key) and output paths are left out, and there
  is no timestamp, so **two runs with equal manifests must give equal
  outputs**. Compare manifests before comparing results.

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
| `--service-date YYYY-MM-DD` | Service date to analyze (calendar + calendar_dates honored). Default: each feed's date from `--date-rule`. |
| `--date-rule typical-weekday\|busiest` | How to pick a feed's date when none is given. Default **typical-weekday**: the most common Monday–Friday service on days without `calendar_dates` exceptions, middle Tue–Thu date. `busiest` = most trips (a holiday in these feeds). |
| `--feed-service-date FEED_ID=YYYY-MM-DD` | Pin one feed's date (repeatable); other feeds keep `--service-date` or the rule. Not combinable with `--common-date` / `--service-date-range`. |
| `--service-date-range START..END` | Analyze every served day in the window (max 31 days). Headways get **one row per day** (the DB PKs include `service_date`; existing databases are migrated automatically and idempotently on the next run); connectivity/near-stations use the union of days. The report shows `service_date` as `START..END` with `days_analyzed`. |
| `--common-date` | Resolve ONE service date shared by ALL feeds (the latest date every feed has active service) — coherent cross-feed outputs by construction. Exits 1 when the calendars share no such date. |
| `--analysis-date YYYY-MM-DD` | Reference date for expiry/staleness notes (also `GTFS_ANALYSIS_DATE`; default today, resolved once per run). Makes reports reproducible — also accepted by `ingest` for the `feed_expired` check. |
| `--proximity-m METERS` | Stop proximity threshold for connectivity. Default `150`. |
| `--window NAME=HH:MM-HH:MM` | Replace the default time windows (repeatable). Hours may exceed 23, e.g. `night=23:00-27:00`. |

The three date-selection flags are mutually exclusive. Whenever analyzed
feeds resolve to **different** service dates, the report sets
`service_dates_coherent: false` and warns prominently — cross-feed
connectivity and near-stations would mix non-contemporaneous schedules.

`analyze-frequency` only (in addition to `--service-date`, `--proximity-m`,
`--window`): `--network-walk` computes near-station walking times along the
OSM street network in PostGIS instead of straight lines (pairs without a
street path within 800 m keep the straight-line value). The built walk
graph is cached on disk in `.gtfs_cache/` keyed by feeds + OSM state
(`--walk-cache-dir DIR` to relocate, `--no-walk-cache` to force a rebuild —
an OSM reimport or stop reload invalidates automatically);
`--prune-station-network` leaves out station-network arcs that change no
shortest time (see the station network output below); `--no-fix-directions`
keeps the feed's `direction_id` where it mixes both senses of a route (see
the headway definition); `--no-route-dedupe` keeps both copies of a line
published by two feeds in the routing inputs (see `route_equivalences.csv`
below); `--min-transfer-s` sets the timetable's change time (default 120);
`--transport-mode
MODE` and `--route ROUTE` (both repeatable or
comma-separated) restrict the analysis to certain transport modes and/or
routes — **default: everything**. Modes accept names (bus, tram, metro,
rail, ferry, cable_tram, aerial_lift, funicular, trolleybus, monorail) or
GTFS route_type codes (basic or extended, e.g. `3`, `700`); routes match
`route_id` or `route_short_name`; a route passes if it matches either
filter. The filter cascades to everything trip-derived (headways,
connectivity, near/same-line stations, route shapes/links); the stop
inventory itself stays complete. Feeds with no matching route are reported
as `filtered_out`. [Stops-only CSV](#stops-only-csv-sources) sources are
**exempt** from both flags — they have no route attributes to match, so
filtering them would delete their stops from every output for no gain.

`analyze-frequency` and `detect-interchanges` (**station deduplication**, on
by default):

| Flag | Description |
| --- | --- |
| `--dedupe` / `--no-dedupe` | Merge duplicate stations (the same physical stop under different stop_ids) before connectivity/near-stations/clustering. Default **on**; `--no-dedupe` restores the raw behavior. |
| `--dedupe-distance-m METERS` | **Cross-feed** alias candidate radius (stops of *different* feeds). Default `30`. |
| `--dedupe-name-threshold RATIO` | Minimum normalized-name similarity (0–1) to accept a *cross-feed* pair on names alone; pairs sharing a `route_short_name` match regardless. Default `0.75`. |
| `--dedupe-within-feed-distance-m METERS` | **Within-feed** alias candidate radius (stops of the *same* feed). Default `5` (also the `AnalysisConfig`/`InterchangeConfig` default); `0` disables. |
| `--drop-unserved-stops` / `--no-drop-unserved-stops` | Leave out stops (`location_type` 0) that appear in **no** `stop_times.txt` row of the whole feed: no trip can board them. Default **on**. They stay in the PostGIS `stops` table (and so in `conflate-osm`), but appear in no `analyze-frequency` / `detect-interchanges` output. The report counts them per feed (`unserved_stops`) either way. |

Two kinds of duplicate, with deliberately different evidence:

- **Cross-feed** — two feeds describe one station differently
  (`improved:SITEUR_x` vs `mdb:TL_x`). Accepted on name similarity **or** a
  shared route.
- **Within-feed** — one feed publishes a station twice under two id schemes,
  both scheduled (observed in the wild: a light-rail line appearing as both
  `SITEUR_001_025` and `ST_01_025`, which also splits the rendered route into
  two disjoint chains). Accepted on a **shared route and nothing else** — a
  shared name between two stops of one feed is not evidence. The radius is
  deliberately tight (**5 m**): duplicate records sit at 0 m, while the same
  route's stop for the *opposite direction* (across the street, or the other
  platform of a median station) is 20–40 m away and shares every route. The
  former 50 m default merged 2,697 such pairs (median 28.9 m), turning two
  platforms into one station and giving routing a free U-turn.

`stop_equivalences.csv` records which rule matched each pair in `match_kind`
(`cross_feed` / `within_feed`), so the two are reviewable separately.

[Stops-only CSV](#stops-only-csv-sources) stops (e.g. MiBici docks) take part
in **neither** rule. The within-feed rule could never fire for them (no
routes), but the cross-feed *name* rule could: a dock named after a corner,
within 30 m of a bus stop named after the same corner, would otherwise become
its alias. A dock is never the same station as a transit stop, so they are
excluded up front.

**Same-feed conflicts.** Pairs are then joined into stations closest first,
and a pair is **rejected** when it would put two stops of the *same* feed more
than the within-feed radius (5 m) apart into one station. Without this, the
cross-feed rule paired one feed's platform with the *other* feed's
opposite-direction platform (same name, same route, 20–30 m away), and
grouping chained both platforms into one station through their 0 m twins
(e.g. `improved:…C98_STP_103` = `mdb:…C98_STP_103` at 0 m, plus
`mdb:…C58V1_STP_01` 29 m across the street). Every rejected pair is listed
in `stop_equivalences_rejected.csv` with the two same-feed stops it would
have merged. With within-feed dedupe off (`0`), only coincident records of
one feed can share a station.

Aliases stop appearing in each other's `near_stations` lists (row set and
`ID` sequence of the CSV are unchanged), their links vanish from the
GeoPackage, their routes connect as `same_station_dedup`, and interchange
clusters report `physical_stop_count`. The audit trail is
`stop_equivalences.csv` + the `stop_equivalences` table.

### Counting stations, not stop records

**Deduplication records equivalences; it never deletes rows.** Every served
stop record stays in `near_stations.csv` and in the GeoPackage (and every
record in `stops`), so a raw `COUNT(*)` counts *records*, not physical
stations. To count stations,
resolve each record to its canonical station using `station_dedup` from
`near_stations_quality.csv`:

```sql
COUNT(DISTINCT COALESCE(NULLIF("station_dedup",''), "stop_uid"))
```

On the two Guadalajara feeds plus MiBici this is not a rounding difference:

| Level | Stations |
| --- | ---: |
| Raw stop records (incl. 484 MiBici docks) | 14,349 |
| Records some trip serves (1,581 unserved mdb stops dropped, default) | 12,768 |
| Dedupe (default: cross-feed 30 m, within-feed 5 m, same-feed conflicts rejected) | **10,203** |
| *for reference: keeping the unserved stops* | *11,054* |
| *for reference: before the same-feed conflict rule* | *10,533* |
| *for reference: within-feed at the former 50 m* | *8,877* |

The conflict rule rejects 1,077 cross-feed pairs (all 768 at 15–30 m, 304 at
5–15 m, 5 at 1–5 m; none at ≤ 1 m), so opposite platforms stay separate
stations: the widest station now spans 7.5 m (median 0 m). The 1,581
unserved stops are in `stops.txt` but in no trip; 111 of them were the 49
small pieces of the station network, which is now **one piece**. Any
density, coverage or accessibility figure computed on the raw record
count is inflated by ~1.41×.

`detect-interchanges` only: `--eps METERS` (DBSCAN radius, default 120),
`--min-samples N` (default 2), `--crs EPSG:XXXXX` (metric CRS override).

`conflate-osm` only (no `--feeds` — reads the `stops` table):
`--match-threshold-m` (default 50), `--street-threshold-m` (default 50),
`--osm-line-table`, `--osm-point-table`, `--osm-geom-column`,
`--crs EPSG:XXXXX`. A DB is required even with `--dry-run`.

`google-qa` only (no `--feeds`; **no-ops** unless `--enable-google-qa` is
passed AND `GOOGLE_MAPS_API_KEY` is set): `--max-calls` (default 200),
`--radius-m` (default 50), `--interchange-radius-m` (default 150),
`--rate-per-s` (default 5), `--dry-run` (eligibility only — no API calls,
no DB writes). See the [Phase 4 licensing constraints](#️-licensing--storage-constraints-enforced-in-code).

`locate-stations` only (no `--feeds`, no DB; **no-ops** unless
`--enable-google-api` is passed AND `GOOGLE_MAPS_API_KEY` is set):
`--stations FILE` (required), `--transport-type` (default SITREN),
`--line`, `--language` (default es), `--region` (default mx),
`--search-radius-m` (default 3000), `--no-distance-matrix`,
`--avg-speed-kmh` (default 25), `--max-route-minutes` (default 60),
`--max-calls` (default 200), `--rate-per-s` (default 5), `--excel-name`,
`--dry-run`. See [Station locator](#station-locator-locate-stations).

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
check) runs only when `GTFS_TEST_DATABASE_URL` is set. Point it at a
separate database: the live tests replace the `stops` and match tables.

```powershell
docker exec transit-db psql -U transit -d transit -c "CREATE DATABASE gtfs_test"
$env:GTFS_TEST_DATABASE_URL = "postgresql+psycopg://transit:cambia_esto@localhost:5432/gtfs_test"
pytest
```

`tests/test_golden.py` compares a fixed two-feed run with the CSVs in
`tests/golden/`. When a change to ordering, rounding or formatting is
intended, regenerate them with `GTFS_UPDATE_GOLDEN=1 pytest tests/test_golden.py`
and review the diff.

Lint and format (CI runs both and fails on any finding; rules live in
`pyproject.toml`):

```bash
ruff check . && ruff format --check .
```

## 10. Documentation site

A Sphinx site combines this README and the other narrative documents with an API
reference generated from the docstrings:

```bash
pip install -e ".[docs]"
python -m sphinx -b html docs docs/_build/html   # or: cd docs && make html
```

Open `docs/_build/html/index.html`. Because `autodoc` imports every module to
read its docstrings, the package and its runtime dependencies must be installed
— the docs extra alone is not enough.

CI builds the same site with `-W`, so malformed docstring markup fails the
pipeline. Two conventions keep that build green:

- **an attribute doc comment (`#:`) must not put a plain colon in its first
  line.** Napoleon reads everything before that colon as the attribute's *type*,
  so `#: Safety cap: max runs...` renders with the type "Safety cap". Use `-`
  instead.
- **an indented block needs `::` on the line above it**, and a bullet or
  numbered list needs a blank line before and after it — otherwise docutils
  reads the indentation as an error.

Set `DOCS_NO_INTERSPHINX=1` to skip the external inventory downloads (offline
builds; CI does this so third-party docs outages cannot turn the build red).

## 11. Troubleshooting

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
  stops_csv.py   stops-only CSV reader: header aliases, encoding, delimiter
  validators.py  all integrity checks -> structured Finding list
  normalizer.py  raw stops.txt -> common-schema GeoDataFrame (EPSG:4326)
  db.py          engine, DDL, idempotent per-feed load
  report.py      JSON + Markdown emission
  pipeline_phase1.py  orchestration + per-feed failure isolation
  cli.py         argparse entry point: ingest / analyze-frequency subcommands
  analysis/      Phase 2 subpackage
    service_filter.py   service-date + calendar_dates filtering (partridge)
    headways.py         headway math per route/direction/stop/window + rollup
    connectivity.py     shared-stop / parent_station / proximity edges
    graph_export.py     networkx graph, metrics, GraphML
    storage.py          DDL + idempotent loads for the analysis tables
    pipeline_phase2.py  Phase 2 orchestration + report
  conflation/    Phase 3 subpackage
    clustering.py       DBSCAN + parent_station merge -> interchanges
    transfers_check.py  inferred vs declared transfers
    osm_match.py        OSM schema validation + nearest transit node + confidence
    side_of_street.py   nearest centerline + left/right sign math
    storage.py          DDL + idempotent loads for the Phase 3 tables
    pipeline_phase3.py  Phase 3 orchestration + reports
  qa/            Phase 4 subpackage (Google QA - off by default)
    google_client.py    Places API (New) + legacy Geocoding/Distance Matrix client
    crosscheck.py       eligibility + verdict logic (in-memory comparisons)
    storage.py          allowlisted verdict tables (StorageViolation guard)
    pipeline_phase4.py  gating, budget enforcement, QA report
    station_locator.py  station list -> coords + travel times -> Excel route
tests/           pytest suite with synthetic fixture feeds
docs/            Sphinx site (see "Documentation site" above)
  conf.py        autodoc + napoleon + MyST configuration
  index.md       landing page and toctrees
  api/           API reference, one page per package, grouped by phase
  guide/         thin stubs that include the root Markdown docs verbatim
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
boundary (`pipeline_phase1.process_feed`): any failure becomes an ERROR finding on
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
| Feed's declared service (calendar + added calendar_dates) ended before today | `feed_expired` | WARNING |
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

## Stops-only CSV sources

Not every stop inventory arrives as GTFS. A field survey, an agency
spreadsheet or a manually digitized list can be ingested directly as a
four-column CSV and **merges into the same `stops` table and the same output
files** as a real feed:

```text
id,name,latitude,longitude
C1,Plaza Universidad,20.6736,-103.3440
C2,Mercado Corona,20.6772,-103.3495
```

Coordinates are WGS84 decimal degrees. `name` is optional; `id`, `latitude`
and `longitude` are required.

**Header matching is forgiving.** Column names are compared
case-insensitively, accent-insensitively and against an alias table, so files
exported from any locale usually just work:

| Canonical | Accepted spellings |
| --- | --- |
| `stop_id` | `id`, `stop_id`, `stopid`, `codigo`, `clave` |
| `stop_name` | `name`, `stop_name`, `stopname`, `nombre`, `estacion` |
| `stop_lat` | `latitude`, `lat`, `stop_lat`, `latitud` |
| `stop_lon` | `longitude`, `lon`, `lng`, `long`, `stop_lon`, `longitud` |

UTF-8 (with or without BOM), UTF-16 and CP1252 are decoded automatically, as
are `,`, `;`, tab and `|` delimiters — the Excel-on-Spanish-Windows default
(semicolon-delimited UTF-8 with BOM) is the expected case, not an exotic one.
Unrecognized columns are ignored. A header that cannot be resolved is a single
`unresolved_csv_header` **ERROR** naming the columns found, the fields still
missing and the spellings that would have worked; the feed is marked `failed`
rather than partially read.

**What the relaxation does and does not cover.** A stops-only source is
exempt from the *required-files* check only — it is not a GTFS feed and never
claimed to be, so `agency.txt`/`routes.txt`/`trips.txt`/`stop_times.txt`/
calendar are reported once as an INFO `stops_only_source` instead of five
ERRORs. Everything else runs unchanged: coordinates are range-checked, null
and duplicate `stop_id`s are dropped with the *same* finding codes as a real
feed, `location_type` defaults to `0`, and `stop_uid = "{feed_id}:{stop_id}"`
is built the same way — so a CSV `S1` and a GTFS `S1` never collide.

**What it contributes, and what it cannot.** Having no schedule, such a feed
takes no part in anything route-derived, and it is reported as `stops_only`:

| Contributes | Does not contribute |
| --- | --- |
| `stops` table rows, GeoPackage `stops`/`near_links` layers | headways (`route_stop_headways`, `route_headways`) |
| proximity pairs and `near_stations.csv` entries | route connectivity edges of any type |
| `station_network.graphml` nodes, flagged `stops_only` | `route_connectivity.graphml` nodes |
| | station deduplication of any kind — a dock is never an alias of a transit stop |
| | station-network `walk`/`same_station` arcs — an unlinked node can never be a waypoint between two transit stops; `n_weak_components` counts transit stops only and `n_stops_only` reports the rest |
| | `in_vehicle` edges, `same_line_stations` |
| OSM conflation and side-of-street (Phase 3B/C read the `stops` table) | interchange *modality* — it can join a cluster but never make it multimodal |

Because such a stop serves no line, it appears in other stops' `near_stations`
lists with `-` in the `linea` slot (see [Phase 2 outputs](#phase-2-outputs)),
and its own `system` and `stop_line_ids` columns stay empty.

Two things to watch:

- **Dedup may merge a CSV stop into a GTFS stop.** Within
  `--dedupe-distance-m` (30 m), a name similarity of ≥ 0.75 is enough on its
  own. That is usually what you want when the CSV annotates an existing
  network, and not what you want when it adds genuinely new stops — check
  `stop_equivalences.csv`, or use `--no-dedupe`.
- **Coordinates that are valid but wrong cannot be detected.** Swapped
  lat/lon and `(0, 0)` from a blank-as-zero export are both legal WGS84.
  Coordinates written with a decimal comma (`20,6736`) *are* caught: they are
  flagged `csv_decimal_comma` and dropped as invalid.

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
that ship only `calendar_dates.txt`. A date with no service in a feed
marks that feed `no_service` and the run continues.

Without `--service-date`, each feed's date comes from `--date-rule`:
**`typical-weekday`** (default) takes the most common Monday–Friday service
pattern on days that `calendar_dates.txt` doesn't touch (so holidays never
count) and picks the middle Tuesday–Thursday date with that pattern;
**`busiest`** takes the date with most trips. In the Guadalajara feeds
`busiest` is a holiday: holidays add Sunday service on top of the
Monday–Saturday service, so they have more trips than any real day (mdb:
807 against 511 on a weekday). `--feed-service-date FEED_ID=YYYY-MM-DD`
pins one feed, which is needed when the feeds' calendars don't overlap.
The report records the date, how it was picked and why.

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
- **Mixed directions are relabelled** (`--fix-directions`, on by default).
  Some feeds put both senses of a route under one `direction_id` (mdb
  `MT_L3` runs 101→118 and 118→101, both as `0`); grouping by direction
  would then count both ways together and understate the headway (`MT_L3`
  publishes 420 s per direction at peak; mixed, it read 230–330 s). On such
  routes each trip's direction comes from its stop order compared with the
  route's longest trip (sign of the rank correlation over shared stops);
  routes whose labels already agree with their senses keep the feed's
  labels. Every change is listed in `direction_fixes.csv`;
  `--no-fix-directions` keeps the feed's labels.
- **`frequencies.txt` is expanded**: each frequency row generates one
  synthetic run per `headway_secs` interval (start times
  `start_time + k·headway`, strictly before `end_time`), replaying the
  trip's per-stop time offsets. The template's own absolute times are
  replaced, never doubled (GTFS `exact_times=0`: they are a travel-time
  pattern). `trip_count` therefore counts real vehicle passages — on
  frequency-dominated feeds this changes headway numbers massively, by
  design. Same-line `travel_min` stays on the un-expanded pattern.

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
| 3 | `same_station_dedup` | serve stops merged into one canonical physical station by cross-feed deduplication (`--dedupe`, on by default) — inferred, so kept distinct from the GTFS-declared type above |
| 4 | `proximity` | serve stops within `--proximity-m` meters (default **150 m**) |

Each undirected pair appears once (`route_a < route_b`) with
`shared_stop_count` (identical stops, 0 if none) and `min_stop_distance_m`
(0.0 for same-stop pairs; smallest inter-stop distance for proximity pairs;
NULL for parent-only and dedup-only pairs farther apart than the threshold).

Distances are **geodesic meters**: computed in **PostGIS on `geography`**
(the authoritative path; the pipeline refreshes the Phase 1 `stops` table
for the analyzed feeds first, so queries match the analyzed data). In
`--dry-run` (no DB), a local fallback projects stops to an estimated UTM
zone — accurate to well under a meter at these scales within one region.

Connectivity is computed from the stops **actually served on the analyzed
service date**. Proximity and `same_station_dedup` are the only connection
types that can cross feeds (ids never match across feeds, geography does).

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
- `stop_equivalences` — PK `(stop_uid_a, stop_uid_b)`; `group_id`,
  `canonical_uid`, `distance_m`, `name_score`, `shared_routes`, `score`,
  `match_kind` — the dedupe audit (empty after a `--no-dedupe` run).
  `match_kind` is `cross_feed` or `within_feed`. Databases created before
  `match_kind` existed gain the column automatically on the next run.

Idempotency: the headway tables use **per-feed delete-and-reload** in one
transaction (same strategy and rationale as Phase 1 stops).
`route_connections` and `stop_equivalences` use **full truncate-and-reload
per run** because their rows pair feeds, so they aren't attributable to a
single feed — both are cheap derived data recomputed from the analyzed set.

Files in `--report DIR`:

- `route_connectivity.graphml` — the networkx graph (nodes: analyzed routes
  with feed/route metadata; edges: the connection attributes), loadable in
  Gephi/yEd/networkx.
- `route_connections.csv` — the route graph as a table (same rows as the
  `route_connections` DB table): `route_a`, `route_b` (`route_uid`s,
  `route_a < route_b`), `feed_id_a`, `feed_id_b`, `connection_type` (the
  strongest: `same_stop` > `same_parent_station` > `same_station_dedup` >
  `proximity`), `shared_stop_count`, `min_stop_distance_m`.
- `route_connection_stops.csv` — **where** two routes connect: one row per
  route pair and stop pair, `route_a`, `route_b`, `stop_uid_a` (served by
  `route_a`), `stop_uid_b` (served by `route_b`), `connection_type` and
  `distance_m` (0 for a shared stop). A stop pair keeps only its strongest
  evidence; the strongest type per route pair equals the route graph's.
- `routes.csv` — route metadata to decode the `route_uid`s: `route_uid`,
  `feed_id`, `route_id`, `route_short_name`, `route_long_name`, `route_type`,
  `modalidad` (agency name), `duplicate_of` (the kept twin when route dedupe
  left this route out of the routing inputs; empty otherwise). The route graph
  and its CSVs list only routes without `duplicate_of`.
- `route_equivalences.csv` — lines present in two feeds (route dedupe):
  `kept_route_uid`, `dropped_route_uid`, `stop_jaccard` (stop-set overlap after
  station dedupe; ≥ 0.8 to match), `same_short_name`, `rule`
  (`newer_calendar` / `later_service_date` / `feed_id`). The dropped twin is
  left out of the station network, the route graph and the timetable.
- `duplicate_trips.csv` — trips a feed lists twice over alias stops (same
  times, stops within the within-feed radius): `feed_id`, `route_uid`,
  `kept_trip_id`, `dropped_trip_id`. Left out of the same routing inputs.
- `connections.csv.gz` — the **timetable** for CSA/RAPTOR/time-dependent
  Dijkstra (gzip; `pd.read_csv` reads it as is): one row per vehicle hop,
  frequencies expanded, sorted by departure. `trip_id`
  (`{feed_id}:{trip_id}`, a frequency run adds `@HH:MM:SS`), `route_uid`,
  `direction_id`, `from_stop`, `to_stop`, `dep_s`, `arr_s` (seconds after
  midnight of the service day; may pass 86,400), `time_quality`. Single-day
  runs only.
- `footpaths.csv` — `from_stop`, `to_stop`, `walk_s`, `kind`: `self` (the
  minimum change time at a stop, `--min-transfer-s`, default 120 s),
  `same_station` (between dedupe aliases, also `--min-transfer-s`) and `walk`
  (both ways per proximity pair, the station network's walking minutes × 60).
  One hop, not transitively closed.
- `route_stops.csv` — `route_uid`, `stop_uid`: the stops each route serves
  on the analyzed day.
- `analysis_report.json` / `analysis_report.md` — per feed: status, the
  service date used and whether it was explicit or auto-picked, routes and
  trips analyzed; plus route-pair counts by connection type, graph metrics
  (components, sizes), and **isolated routes** (no connections at all).
- `near_stations.csv` — one row per stop with `ID`, `stop_uid`, `station`
  (name), `system`, `stop_id`, `stop_lat`, `stop_lon`, `stop_line_ids`,
  and two relation columns:

  ```
  near_stations      = [(ID, station, linea, minutes), ...]          # walking, any line
  same_line_stations = [(ID, station, linea, dirD, minutes), ...]    # route order, same line
  ```

  Conventions: `ID` is the file's own **sequential node id** (1..N in
  `stop_uid` order, starts at 1) — the key every relation column references.
  It is only meaningful inside one run's files: adding a stop shifts the IDs
  after it. Use `stop_uid` to join across runs or against the database. `system` is the serving
  system(s) (agency names, e.g. `Mi Macro, Sitren`). `stop_line_ids` is
  the row's own `"{ID}-{line}"` combos (e.g. `17-C98`). `linea` is
  `route_short_name` (falling back to `route_id`), restricted to lines
  active on the analyzed service date. A near stop that **no route serves**
  that day — a bare parent-station node, or a stop from a
  [stops-only CSV](#stops-only-csv-sources) — is still listed as a neighbour,
  with `-` in the `linea` slot: it is genuinely there and genuinely walkable.
  **Consumers parsing `linea` must tolerate `-`.** This affects only how a
  stop appears in *other* stops' lists; an unserved stop's own `system` and
  `stop_line_ids` stay empty, because it still serves no line.
  All times are bare numbers in **minutes**:
  `near_stations` uses walking time at ~5 km/h (`max(1, round(m/83))`,
  straight-line, or along the OSM street network with `--network-walk`);
  `same_line_stations` uses the **scheduled in-vehicle minutes** between
  consecutive stations (median over the day's trips; distance at ~25 km/h
  when a segment has no departure times). "Near" reuses `--proximity-m`
  (default 150 m); the same-line relation follows `stop_times` order with
  no distance cap. `dirD` is the GTFS `direction_id` (dir0/dir1).
  Cross-feed **aliases** of one physical station are excluded from each
  other's `near_stations` lists by default (`--no-dedupe` restores them);
  the row set and `ID` sequence are identical either way.
- `stop_equivalences.csv` — the dedupe audit (only written when dedupe is
  on): one row per accepted cross-feed alias pair with `group_id`,
  `canonical_uid` (the group's smallest `stop_uid`), both `stop_uid`s,
  `distance_m`, `name_score` (0–1 normalized-name similarity),
  `shared_routes` (shared `route_short_name` count) and `score`.
- `stop_equivalences_rejected.csv` — accepted pairs that were **not** used
  because they would put two stops of one feed more than the within-feed
  radius apart into one station (written when dedupe is on): the pair's
  columns plus `conflict_uid_a`, `conflict_uid_b` and `conflict_m`, the
  same-feed stops and their distance.
- `near_stations_quality.csv` — **provenance sidecar**, always written:
  one row per stop with the same `ID` key and row order as
  `near_stations.csv` (the main file's format never changes because of it).

  | column | values |
  | --- | --- |
  | `walk_source` | `network` (OSM street routing) / `straight_line` / `mixed` / `unsnapped` (stop snaps >200 m from any walkable way) / `none` (no rendered near tuples) |
  | `travel_source` | `observed` (scheduled stop_times medians) / `estimated` (25 km/h fallback) / `mixed` / `none` |
  | `travel_n_obs` | worst-case (minimum) number of trips behind the stop's `travel_min` medians; 0 when estimated/none |
  | `coord_confidence` | numeric match score (`"0.812"`), `needs_review`, `no_match` — from Phase 3 `stop_osm_matches`; `unavailable` in dry runs or when `conflate-osm` hasn't run |
  | `station_dedup` | the dedupe `canonical_uid` when the stop is a cross-feed alias; empty otherwise |

  `analysis_report.json` carries the per-run totals as `quality_summary`.
- `station_network.graphml` + `station_network_nodes.csv` +
  `station_network_edges.csv` + the `station_network` GeoPackage layer —
  the **routable stop-level network** (a `networkx.MultiDiGraph`): one node
  per stop (`stop_uid` key; attributes `near_id` = the CSV `ID`, `name`,
  `feed`, `canonical_uid`, `lat`, `lon`) and typed, time-weighted edges:

  | edge_type | direction | `time_min` |
  | --- | --- | --- |
  | `in_vehicle` | directed, per route/direction | scheduled `travel_min` (absent when the segment is untimed) |
  | `walk` | reciprocal arcs | walking minutes from the (dedupe-filtered, optionally network-walked) proximity pairs |
  | `same_station` | reciprocal arcs | `0.0` — cross-feed alias links from the dedupe stage, so routing crosses feeds |

  `nx.shortest_path(g, a, b, weight="time_min")` works out of the box.
  `in_vehicle` rows also carry `time_quality` — the scheduled time is kept,
  the label says whether to trust it: `ok`; `zero` (0 s); `too_fast` (the
  straight line between the stops is faster than 80 km/h, so the real path
  is even faster); `too_slow` (the route direction's shape length over its
  median trip duration is below 5 km/h — placeholder times); `untimed`.
  The report counts them in `station_graph.in_vehicle_time_quality`.
  When a feed lists two stops under one `stop_sequence` of a trip (mdb
  `MT_L1`/`MT_L2` alias platforms, 0 m apart), one stop is kept per
  sequence (the smallest `stop_id`), so the line no longer zig-zags
  between aliases; the per-feed notes count those rows.
  **Redundant arcs.** Many walk arcs never change a shortest time: another
  walking path is as fast (A→B→C in 1 + 1 minutes matches A→C in 2). The
  report always counts them in `station_graph` as `redundant_walk_pairs` and
  `redundant_same_station_pairs` (pairs that close a cycle of 0-minute alias
  links). On the paper run that is 42,557 of 72,286 walk pairs and 247 of
  2,812 same_station pairs. `--prune-station-network` (default off) writes the
  network without them: every shortest `time_min` path stays the same (pairs
  are removed longest first, only when the remaining walking network still
  links the two stops within the same minutes). Walking *metres* along a path
  can grow, and an edge list no longer says that every two stops within the
  proximity radius are directly linked. `near_stations.csv` is never affected.
  Gephi merges parallel edges unless "parallel edges" is enabled on import;
  yEd/igraph/networkx read the multigraph natively. The CSVs are the same
  graph flattened (reciprocal arcs deduped to one row per pair).
- `direction_fixes.csv` — one row per relabelled trip: `feed_id`,
  `route_id`, `trip_id`, `direction_id_feed`, `direction_id_used` (header
  only when nothing was relabelled or with `--no-fix-directions`).
- `route_stop_headways.csv` / `route_headways.csv` — the headway tables
  written to the report directory on **every** run (previously DB-only, so
  dry runs produced no headway output). Same columns as the DB tables
  including `service_date`; range runs produce one row set per served day.
- `transit_analysis.gpkg` — **GeoPackage** (open directly in QGIS/GDAL)
  with four EPSG:4326 layers: `stops` (points; the CSV attributes plus
  `stop_num_id`, the database join key), `near_links` (one LineString per
  near pair with both `stop_num_id`s, `distance_m`, `walk_min`),
  `route_shapes` (real route polylines from shapes.txt per
  route/direction/shape), and `route_stop_links` (**directed**
  consecutive-stop segments per route/direction with scheduled
  `travel_min`). GeoPackage keeps 64-bit integers intact, so the numeric
  ids are safe (a shapefile would truncate them). The file is rewritten
  per run.

## Phase 2 assumptions

- Headways are schedule-based (`stop_times.departure_time`); rows without a
  departure time (non-timepoints) are excluded.
- `frequencies.txt` service **is expanded** into synthetic runs (see the
  headway definition above); `route_stop_links.travel_min` deliberately
  reads the un-expanded template pattern.
- Expired calendars don't block analysis: the report carries
  `calendar_status`/`calendar_end` per feed plus notes for expired feeds
  and stale auto-picked dates (> 30 days old).
- The route rollup is unweighted across stops (each stop counts equally).
- A feed that fails anywhere in analysis is recorded `failed` with the
  exception message in its notes; the run continues (same isolation
  contract as Phase 1).
- A [stops-only CSV](#stops-only-csv-sources) source is recorded
  `stops_only` with a null service date, so it never enters the cross-feed
  service-date coherence check. It is exempt from `--transport-mode`/
  `--route` (filtering on route attributes it cannot have would delete its
  stops from every output) and is excluded from `--common-date` resolution
  (it has no calendar to share). Its stops still take part in proximity,
  near-stations and the GeoPackage, and appear as unlinked `stops_only` nodes
  in the station network; they are excluded from deduplication.

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
- A [stops-only CSV](#stops-only-csv-sources) source is recorded
  `stops_only` in Part A: its stops cluster normally (clustering is geometry
  plus `parent_station`), but they carry no route evidence, so they can join
  a multimodal cluster without ever making one. Parts B and C read the
  `stops` table directly and conflate such stops with no special handling.
- The live-DB path is covered by an integration test gated on
  `GTFS_TEST_DATABASE_URL` (synthetic OSM tables built on the fly); all
  geometry/clustering math is unit-tested without a database.

---

# Phase 4: Google Maps QA cross-check (`google-qa`)

```bash
python -m gtfs_ingest.cli google-qa --db-url $DATABASE_URL \
    --enable-google-qa --max-calls 200 --radius-m 50 --report ./out [--dry-run]
```

An **ephemeral, targeted** quality-assurance pass that cross-checks a small
subset of Phase 3 results against the Google Places API (New). It is *not*
a data source, *not* a bulk enrichment pass, and never mutates GTFS/OSM
data — it only writes verdict rows.

## ⚠️ Licensing & storage constraints (enforced in code)

The Google Maps Platform Terms restrict caching and prohibit building a
derived or substitute database from Google content. This phase therefore:

- **Persists only**: the opaque `place_id`, verdicts/flags **we** compute,
  and a timestamp **we** compute. Never Google names, transit line names,
  agency names, coordinates, ratings, or any other Places field — to DB,
  files, or logs.
- **Keeps everything else in memory** for the duration of one comparison,
  then discards it. Debug logging redacts responses to field *names* only.
- **Never modifies upstream data**: no GTFS or OSM record is corrected or
  overwritten from Google data.
- Enforcement is mechanical: `qa/storage.py` validates every record
  against a frozen column allowlist and raises `StorageViolation`
  otherwise, and `tests/test_storage_guard.py` fails if any non-allowlisted
  column ever appears in the DDL or INSERT statements.

> **This code is not legal advice.** Verify the current
> [Google Maps Platform Terms of Service](https://cloud.google.com/maps-platform/terms)
> yourself before enabling this phase.

## Conditions of use

- **OFF by default.** Runs only when `--enable-google-qa` is passed AND
  `GOOGLE_MAPS_API_KEY` is set in the environment (env only — there is
  deliberately no CLI flag for the key). Missing either → clean no-op with
  a clear log message and a `skipped_*` status in the report, exit 0.
- **Targeted, never exhaustive.** Only two record sets are eligible:
  (a) `interchanges` rows with `is_multimodal = true` — verify the mode
  combination; (b) stops with `stop_osm_matches.needs_review = true` —
  verify a transit place exists. The full stops table is never iterated.
- **Hard call budget**: `--max-calls` (default **200**) is a ceiling with a
  running counter; the run stops and logs when it is reached (retries of a
  failed call count once). Rate limit via `--rate-per-s` (default 5) with
  exponential backoff on 429/5xx.

## 💰 Cost note (Enterprise SKU)

The interchange check requests exactly
`places.id, places.types, places.transitStation` — **`transitStation`
triggers Google's Enterprise SKU, the highest price tier**. That is why
this check is limited to multimodal interchanges and capped by the budget.
The stop-existence check requests only `places.id, places.types` (cheaper
SKU). An explicit `X-Goog-FieldMask` is mandatory — the client refuses to
issue any request without one.

## Verdicts

Interchange (Google's observed mode set vs ours, compared in our GTFS
route_type vocabulary and discarded immediately after):

| Verdict | Meaning |
| --- | --- |
| `CONFIRMED` | Google's modes cover every mode we inferred (`mode_agreement = true`) |
| `PARTIAL` | some overlap, but not all our modes observed |
| `CONFLICT` | transit observed with zero mode overlap — listed for human review |
| `NO_DATA` | call failed, nothing returned, or no usable mode evidence |

Stop existence: `STATION_FOUND` (a transit-typed place within
`--radius-m`, default 50 m), `NOT_FOUND` (Google answered and nothing
transit was there), `NO_DATA` (the call failed).

## Phase 4 outputs

- `google_qa_interchange` (interchange_cluster_id PK, verdict,
  mode_agreement, google_place_id, checked_at) and `google_qa_stop`
  (stop_uid PK, verdict, google_place_id, checked_at). Idempotency:
  **per-row upsert** — QA runs are budgeted and partial by design, so a
  re-check must not wipe verdicts from earlier runs (deliberate deviation
  from Phase 3's full reload).
- `google_qa_report.json/.md` — verdict counts, the CONFLICT list (our
  cluster ids + our inferred modes only), calls made and budget remaining.

## Phase 4 assumptions

- Nearby Search (`places:searchNearby`) is used for both checks because its
  `locationRestriction` circle makes "nothing within the radius" a real
  answer (Text Search only *biases* toward a location); Text Search and
  Place Details are implemented in the client and available.
- The `transitStation` payload shape is treated as opaque: `vehicleType`
  values are extracted tolerantly wherever they appear; schema drift
  degrades to `NO_DATA`, never a crash. Google vehicle types map onto basic
  GTFS route_type codes (BUS→3, SUBWAY→1, HEAVY_RAIL→2, …); places without
  vehicle data fall back to their place types (`bus_station`→bus, …).
- All test-suite network traffic uses mocked transports; no live calls.

## Out of scope

Pedestrian-network routing (walking times here remain straight-line),
polygon/area conflation, turn-by-turn interchange path generation, and any
bulk use of Google data.

---

# Station locator (`locate-stations`)

```bash
python -m gtfs_ingest.cli locate-stations --stations route.txt \
    --enable-google-api --transport-type SITREN --line 2 \
    --report ./out [--dry-run]
```

A user-facing tool (ported from the Station Locator desktop app, see
`docs/guide/google-apis.md`) that reads a list of station names in route
order from a TXT/CSV file (comma- and/or newline-separated), resolves each
name to coordinates, computes the travel time between consecutive
stations, and exports the directed route to an Excel file plus the usual
JSON + Markdown report.

## How it resolves stations

1. **Text Search** (Places API New, `places:searchText`) with the query
   `"Estación {transport-type} {name}"` (+ `" línea {line}"` when
   `--line` is given). The first result is taken.
2. **Anchoring**: the coordinates of the previously resolved station are
   sent as a location-bias circle (`--search-radius-m`, default 3000 m) so
   Google ranks results near the route first — this is what disambiguates
   two stations sharing a name. The first station has no anchor.
3. **Geocoding API fallback** when Text Search yields no coordinates,
   using the recommended name if one was returned, else the raw query.

## Travel times (directed, consecutive pairs)

- **Distance Matrix API** (default; disable with `--no-distance-matrix`)
  with the mode mapped from `--transport-type` (SITREN → transit/train;
  MiMacro/Troncal/Alimentador/Expreso → transit/bus) and
  `departure_time=now` → labeled `real`.
- **Haversine estimate** at `--avg-speed-kmh` (default 25) when the matrix
  is disabled or returns nothing → labeled `estimado`.
- **Generic estimate** `--max-route-minutes / stations` (default 60) for
  pairs with missing coordinates → labeled `generico`.

## Conditions of use (same rails as Phase 4)

- **OFF by default**: needs `--enable-google-api` AND `GOOGLE_MAPS_API_KEY`
  in the environment (no CLI flag for the key); otherwise a clean no-op.
- Hard `--max-calls` budget (default 200) shared across all three APIs,
  `--rate-per-s` throttle, backoff on 429/5xx, `--dry-run` (reads the
  station list and reports the worst-case call count; zero API calls).
- Cost: 1 Text Search per station (+ up to 1 Geocoding fallback each) and
  1 Distance Matrix element per consecutive pair when real times are on.

## ⚠️ Licensing note (read before enabling)

Unlike the QA cross-check, this tool's Excel export
(`--excel-name`, default `stations_route.xlsx`: `ID, Nombre_Estación,
Modalidad, Línea, Latitud, Longitud, Estacion_Proxima`) deliberately
contains Google-resolved coordinates — that is its purpose as a user
export. It **never writes to the database**, and the export must not be
loaded into PostGIS or redistributed; the Phase 4 storage constraints
continue to apply to everything else. Verify the
[Google Maps Platform Terms](https://cloud.google.com/maps-platform/terms)
for your use case.
