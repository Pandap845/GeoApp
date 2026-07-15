# Neo4j export module (`export-neo4j`)

## Context

The presentation's "siguiente paso" — a queryable graph model in Neo4j —
became nearly free once `station_network` shipped: the routable network
already exists as flat frames. This module reformats the report-dir outputs
into an **offline Neo4j load package** (CSVs + generated Cypher). No new
dependency, no live connection; loadable in Neo4j Desktop/Aura via
`cypher-shell -f load.cypher` or `neo4j-admin database import`.

**User decisions (fixed):** offline Cypher/CSV package; rich model
(`:Stop`, `:Route`, `:Station` + `IN_VEHICLE`/`WALK`/`SAME_STATION`/
`SERVES`/`PART_OF`); standalone subcommand reading from the report dir.

**Verified against the real `./out`:** 13,865 stops · 274 routes (GraphML
carries short/long name, route_type, modalidad) · 1,406 multi-member
canonical groups · 25,485 in_vehicle / 31,352 walk / 4,376 same_station
edges · SERVES derivation yields 25,399 rows covering all 274 routes ·
PART_OF = 5,221.

## Key design points

- **One CSV set serves both load paths**: data files carry the
  `neo4j-admin` inline header (`stop_uid:ID(Stop)`, `:START_ID(Stop)`,
  `time_min:double`…). `neo4j-admin database import full` consumes them
  directly; the generated `load.cypher` accesses the same columns with
  backticks (``row.`stop_uid:ID(Stop)` ``) — no duplicate files.
- **ID spaces are mandatory**: `canonical_uid` values ARE `stop_uid`
  values, so `:ID(Stop)` / `:ID(Station)` / `:ID(Route)` spaces prevent a
  silent collision in the admin path.
- **`:Station` only for multi-member canonical groups** (1,406) — a
  station per stop would add 12k no-information nodes. PART_OF from every
  member incl. the canonical stop itself.
- **SERVES** derived from in_vehicle edges: distinct
  (route_uid, endpoint stop) pairs. Limitation (documented): a route with
  no timed consecutive pair gets no SERVES — none exist in current data.
- **Idempotent Cypher**: `CREATE CONSTRAINT ... IF NOT EXISTS` + `MERGE`
  in `CALL { } IN TRANSACTIONS OF 1000 ROWS`; IN_VEHICLE merge-keyed on
  `{route_uid, direction_id}` (parallel rels are legitimate); WALK/
  SAME_STATION stored once, queried undirected (`-[:WALK]-`).
  `direction_id` normalized to string ("0"/"1") — it's a merge key.
- **Injection-safe by construction**: no data value is ever interpolated
  into `.cypher`; everything flows through pandas-quoted CSV (utf-8, no BOM).
- Missing `route_connectivity.graphml` degrades to uid-only `:Route`
  nodes with a warning; missing nodes/edges CSVs → ValueError → exit 1
  ("run analyze-frequency first"). `stop_equivalences.csv` optional
  (enriches SAME_STATION props).

## Package layout (`<report>/neo4j/`)

```
stops.csv            stop_uid:ID(Stop),name,feed,near_id:long,lat:double,lon:double,canonical_uid
routes.csv           route_uid:ID(Route),feed_id,route_id,route_short_name,route_long_name,route_type:long,modalidad
stations.csv         canonical_uid:ID(Station),name,lat:double,lon:double,n_stops:long
rel_in_vehicle.csv   :START_ID(Stop),:END_ID(Stop),time_min:double,linea,direction_id,route_uid
rel_walk.csv         :START_ID(Stop),:END_ID(Stop),time_min:double,distance_m:double
rel_same_station.csv :START_ID(Stop),:END_ID(Stop),distance_m:double,name_score:double,score:double
rel_serves.csv       :START_ID(Route),:END_ID(Stop)
rel_part_of.csv      :START_ID(Stop),:END_ID(Station)
load.cypher          constraints + point index + LOAD CSV per file (idempotent)
post_import.cypher   constraints + location point + indexes (admin path)
admin_import.txt     documented neo4j-admin command (both shells)
```

## Steps

1. **New subpackage** `gtfs_ingest/export/` (`__init__.py` + `neo4j.py`):
   constants (`REQUIRED_FILES`, `ADMIN_HEADERS` plain→admin column map as
   the single source of truth, `FRAME_FILES`), frozen dataclass
   `Neo4jInputs(nodes, edges, routes, equivalences)`,
   `load_inputs(report_dir)` (ValueError naming missing files).
2. **`build_package(inputs) -> dict[str, pd.DataFrame]`** (pure): stops;
   routes from `nx.read_graphml` attrs (fallback: uid-only from in_vehicle
   route_uids + warning); stations/PART_OF from multi-member canonical
   groups; the three Stop-Stop edge frames (SAME_STATION props merged from
   equivalences when present); SERVES union of endpoints per route.
3. **Renderers**: `render_load_cypher(frames)`, `render_post_import_cypher()`,
   `render_admin_command()` — driven by `ADMIN_HEADERS`/`FRAME_FILES` so
   filenames/columns can never drift from the writers.
4. **`write_package(frames, out_dir)`** (rename plain→admin headers, write
   CSVs + 3 text files, log counts) + `run_export(report_dir, out_dir=None)`
   orchestrator (default out = `<report>/neo4j`).
5. **CLI** ([cli.py](gtfs_ingest/cli.py)): `export-neo4j` subparser (pattern of
   `conflate-osm`'s independent arg set): `--report` (default ./out),
   `--out`, `--log-level`; handler `_run_export_neo4j` with the
   ValueError→exit-1 convention; `_SUBCOMMANDS` + `main()` dispatch.
6. **Tests** (`tests/test_neo4j_export.py`): build-package frames/counts;
   SERVES dedup across directions; Station only for multi-member groups
   (canonical stop gets PART_OF too); SAME_STATION with/without
   equivalences; untimed in_vehicle keeps empty time_min; rendered cypher
   contains constraints/`point(`/`IN TRANSACTIONS`/one LOAD CSV per file/
   the `(Stop)` ID-space suffix; missing-input ValueError; CLI e2e chained
   on an analyze-frequency dry run (package exists, row parity with
   station_network_nodes.csv, load.cypher references every CSV);
   missing-report CLI → exit 1.
7. **Docs**: README (subcommand + 2–3 example Cypher queries: weighted
   shortest path over IN_VEHICLE|WALK by `time_min`; routes per station),
   WORKFLOW.md (short §3.11 + §4.2 rows for `neo4j/`),
   diccionario_datos.md (Spanish §6: etiquetas, relaciones, cabeceras,
   cómo cargar). No gitignore change (`out/` covered).

## Verification

```
python -m pytest tests/test_neo4j_export.py -q     # + full suite green
python -m gtfs_ingest.cli export-neo4j --report out
```
Expected on the real `out/`: stops 13,865 · routes 274 · stations 1,406 ·
in_vehicle 25,485 · walk 31,352 · same_station 4,376 · serves 25,399 ·
part_of 5,221; `load.cypher` references all 8 CSVs; re-running the command
overwrites the package cleanly. Optional live check (user-driven): copy
CSVs to the DBMS import dir, `cypher-shell -f load.cypher`, then
`MATCH (n) RETURN labels(n), count(*)`.

## Risks (mitigated)
- Stop/Station id collision in admin import → ID spaces (tested).
- Backticked admin headers in Cypher — unusual but valid; confined to the
  generated script.
- Quotes/accents in names → CSV-only data flow, pandas quoting.
- MERGE slower than CREATE — fine at ~90k rels; noted in a comment.
