---
name: gtfs-ingest
description: Project knowledge and coding rules for the geoApp / gtfs-ingest repository (GTFS + OpenStreetMap + MiBici to PostGIS transit analysis for Guadalajara, backing an academic paper). Use it for any code change, refactor, analysis run, debugging, or question about the pipeline, its outputs, or its data.
---

# gtfs-ingest

A Python pipeline that turns two Guadalajara GTFS feeds, the MiBici bike-share
station list, and an OpenStreetMap extract into a PostGIS database and a set of
analysis files (headways, deduplicated stations, connectivity graphs, a routable
station network). The results back an academic paper, so **numbers must be
reproducible and every change must keep the tests green**.

## Read first

| When you need… | Read |
| --- | --- |
| what each phase and module does, key concepts, outputs | [reference/project.md](reference/project.md) |
| how to set up, run the full pipeline, test, build docs | [reference/running.md](reference/running.md) |
| facts and known problems in the real data | [reference/data-findings.md](reference/data-findings.md) |
| how code in this repo must be written | [reference/code-style.md](reference/code-style.md) |

## The ten things to remember

1. **Four phases, one CLI:** `ingest` → `analyze-frequency` → `detect-interchanges` / `conflate-osm` → `google-qa` / `locate-stations`, plus `export-neo4j`. Entry point: `python -m gtfs_ingest.cli`.
2. **A broken feed never stops a run.** Every per-feed step has a failure boundary; problems become findings in the report.
3. **`stop_uid = "{feed_id}:{stop_id}"`** is the global key. `feed_id` comes from the file name (`mibici.csv` → `mibici`).
4. **Dedupe groups records, it never deletes them.** Count stations with `station_dedup` from `near_stations_quality.csv`, not with row counts.
5. **Two dedupe rules plus one guard:** cross-feed (≤ 30 m, similar name *or* shared route) and within-feed (≤ 5 m, shared route only); then a station never holds two stops of one feed more than 5 m apart (rejected pairs → `stop_equivalences_rejected.csv`). CLI and config defaults must stay equal.
6. **Stops-only sources (MiBici) carry geometry only:** no routes, never deduplicated, unlinked nodes in the station network, but listed as neighbours with line `-`.
7. **DB writes are idempotent:** delete-and-reload per feed, or truncate-and-reload for cross-feed tables.
8. **Google APIs are hard-gated** (flag + key) and only verdicts are ever stored. Never loosen the storage guard.
9. **Docs are hand-synced:** a behaviour change needs the matching edit in `README.md` and `docs/guide/`.
10. **Write simple code:** short comments that say *why*, small functions, clear names. See the code-style reference before editing.

## Before you finish a change

- `.venv/Scripts/python.exe -m pytest tests -q` passes (DB tests self-skip without a database).
- `ruff check .` and `ruff format --check .` pass (CI blocks on both).
- `DOCS_NO_INTERSPHINX=1 .venv/Scripts/python.exe -m sphinx -b html -W --keep-going docs <tmp>` passes. CI runs it with `-W`, so broken docstring markup fails the build.
- README / `docs/guide/` updated if behaviour or defaults changed.
- The plan and a change log are saved as `plans/YYYY-MM-DD-<topic>.md` and `plans/YYYY-MM-DD-<topic>-changes.md`. The `plans/` folder is git-ignored; never commit it.
