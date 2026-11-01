# Running the project

## Environment

- Python: the root `.venv` → `.venv/Scripts/python.exe` (Windows). Shell is Git Bash or PowerShell.
- Database: `docker compose up -d --wait` starts `postgis/postgis:16-3.4` as `transit-db` on port 5432.
  `.env` holds `DATABASE_URL=postgresql+psycopg://transit:cambia_esto@localhost:5432/transit`; the CLI reads it.
- OSM must already be loaded in that database (`planet_osm_line`, `planet_osm_point`) for `conflate-osm` and `--network-walk`.
- Windows console: set `PYTHONIOENCODING=utf-8` when a script prints non-ASCII characters.

## Full run (the paper configuration)

Run from the repo root. About 10 minutes; the walk graph is cached in `.gtfs_cache/`.

```bash
FEEDS="data/feeds/improved-gtfs-mimacro-mitren-sitren.zip data/feeds/mdb-2366-202502241922.zip data/feeds/mibici.csv"
OUT=outputs/out_mibici_v3
PY=.venv/Scripts/python.exe

$PY -m gtfs_ingest.cli ingest              --feeds $FEEDS --report $OUT
$PY -m gtfs_ingest.cli analyze-frequency   --feeds $FEEDS --report $OUT --network-walk --proximity-m 300
$PY -m gtfs_ingest.cli detect-interchanges --feeds $FEEDS --report $OUT
$PY -m gtfs_ingest.cli conflate-osm                       --report $OUT
$PY -m gtfs_ingest.cli analyze-frequency   --feeds $FEEDS --report $OUT --network-walk --proximity-m 300
$PY -m gtfs_ingest.cli export-neo4j                       --report $OUT
```

`analyze-frequency` runs twice because the quality sidecar needs the OSM matches that only `conflate-osm` writes.
Each run replaces the database tables, so only the files in `outputs/` keep older results.
Use a new `outputs/<name>` folder per run and compare folders, never overwrite the reference runs.

Quick check without a database: add `--dry-run` (not possible for `conflate-osm`).

## Tests and docs

```bash
.venv/Scripts/python.exe -m pytest tests -q                 # ~300 pass, 4 DB tests skip
.venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m ruff format --check .
DOCS_NO_INTERSPHINX=1 .venv/Scripts/python.exe -m sphinx -b html -W --keep-going docs /tmp/docs_html
```

Live DB tests: point `GTFS_TEST_DATABASE_URL` at the separate `gtfs_test` database in the
same container, never at `transit` (the tests replace its tables).
Golden CSVs: `tests/golden/`; regenerate on purpose with `GTFS_UPDATE_GOLDEN=1`.

CI (`.github/workflows/tests.yml`): lint (ruff check + format, blocking), unit matrix, integration against a PostGIS service, Sphinx with `-W`.

## Useful flags

| Flag | Default | Note |
| --- | --- | --- |
| `--proximity-m` | 150 | paper runs use 300 |
| `--dedupe-distance-m` | 30 | cross-feed radius |
| `--dedupe-within-feed-distance-m` | 5 | `0` disables; must equal `WITHIN_FEED_DISTANCE_M` |
| `--service-date` / `--common-date` / `--service-date-range` | busiest per feed | see data-findings.md before relying on "busiest" |
| `--network-walk` | off | OSM street walking distances |
| `--no-dedupe` | dedupe on | |
