# Project map

## Repository layout

| Path | Contents |
| --- | --- |
| `gtfs_ingest/` | the package |
| `tests/` | pytest suite (~280 tests; DB tests skip without `GTFS_TEST_DATABASE_URL`) |
| `data/mexico.osm.pbf` | OSM extract (loaded into PostGIS with osm2pgsql) |
| `data/feeds/` | `improved-gtfs-mimacro-mitren-sitren.zip`, `mdb-2366-202502241922.zip`, `mibici.csv` |
| `outputs/` | gitignored runs: `out_mibici_v2` (50 m within-feed dedupe, docks linked) and `out_mibici_v3` (current) |
| `docs/` | Sphinx site: `guide/` narrative docs, `api/` autodoc pages, `plans/` design notes (not built), `figures/` charts + `make_figures.py` |
| `presentations/` | LaTeX decks (`.tex` ignored by git, `.pdf` tracked); compile from inside the folder |
| `scripts/build_branch_gpkg.py` | branch-coloured GeoPackage for QGIS, built from `outputs/out_mibici_v3` |
| `paper/` | article draft |
| `init/`, `docker-compose.yaml` | PostGIS container |

Ignore `gtfs_ingest/.venv` (a stray second virtualenv); the project env is the root `.venv`.

## Phases and modules

### Phase 1 — `ingest` (package root)

| Module | Role |
| --- | --- |
| `loader.py` | discover feeds (`.zip`, folders, explicitly named `.csv`) and open them; never scans folders for CSVs |
| `stops_csv.py` | read a stops-only CSV: forgiving header aliases, BOM, `;` delimiters, decimal-comma warning |
| `validators.py` | integrity checks → `Finding`s (ERROR / WARNING / INFO) |
| `normalizer.py` | stops into the common schema; drops rows without id or valid coordinates |
| `db.py`, `bulk.py` | `stops` table DDL and fast COPY loading |
| `report.py` | JSON + Markdown validation report |
| `pipeline_phase1.py` | orchestration |
| `models.py` | shared pydantic models and GTFS constants; `FeedSource.is_stops_only` |
| `config.py`, `cli.py` | settings (`.env`) and the command line |

### Phase 2 — `analyze-frequency` (`analysis/`)

| Module | Role |
| --- | --- |
| `service_filter.py` | pick the service date (explicit, busiest, common, or a range) and filter the feed to it |
| `headways.py` | headways per (route, direction, stop, window); expands `frequencies.txt` |
| `connectivity.py` | route–stop map, proximity pairs, route-to-route edges |
| `dedupe.py` | station deduplication (cross-feed + within-feed) |
| `walk_network.py`, `graph_cache.py` | walking distances on OSM streets (`--network-walk`), cached on disk |
| `near_stations.py` | `near_stations.csv` and the quality sidecar |
| `station_network.py` | routable stop-level graph (in_vehicle, walk, same_station arcs) |
| `route_geometry.py`, `geopackage.py`, `graph_export.py` | GeoPackage and GraphML exports |
| `route_filter.py` | `--transport-mode` / `--route` |
| `storage.py` | Phase 2 tables |
| `pipeline_phase2.py` | orchestration (`AnalysisConfig`, `AnalysisReport`) |

### Phase 3 — `detect-interchanges`, `conflate-osm` (`conflation/`)

`clustering.py` (DBSCAN + parent_station + dedupe groups), `transfers_check.py`,
`osm_match.py` (nearest OSM transit node + confidence), `side_of_street.py`,
`coverage.py` (OSM stops missing from GTFS), `storage.py`, `pipeline_phase3.py`.

### Phase 4 — `google-qa`, `locate-stations` (`qa/`)

Opt-in Google Places cross-checks with a budget and a storage guard:
`google_client.py`, `crosscheck.py`, `station_locator.py`, `storage.py`, `pipeline_phase4.py`.

### Export — `export-neo4j` (`export/neo4j.py`)

Offline Neo4j package (CSV + `load.cypher`) built from the station-network CSVs.

## Key concepts

- **Finding:** a validation result with severity, code, message, count, sample. Never an exception.
- **Stops-only source:** `FeedSource.kind == "csv"`. Status `stops_only` in Phase 2, no service date.
- **Dedupe:** candidate pairs within a radius → accept rule → connected components → `canonical_uid` = smallest member. Records stay; `filter_stop_pairs` removes proximity pairs inside a group. `match_kind` says which rule matched.
- **Proximity pairs:** stops within `--proximity-m` (300 m in the paper runs); PostGIS in full runs, local UTM in dry runs. The spatial queries use every feed that contributed stops (`geographic_feed_ids`).
- **Route edges:** `same_stop` > `same_parent_station` > `same_station_dedup` > `proximity` (strongest wins).
- **Station network:** `in_vehicle` (directed, scheduled minutes), `walk` (both ways, `max(1, round(m / 83))` minutes), `same_station` (0 min). `n_weak_components` counts transit stops only; docks are `n_stops_only`.
- **near_stations `ID`:** sequential 1..N in `all_stops` order, which follows the `--feeds` order.

## Main outputs of `analyze-frequency`

`analysis_report.json/.md`, `near_stations.csv`, `near_stations_quality.csv`,
`stop_equivalences.csv`, `route_connectivity.graphml`, `station_network.graphml`
plus `station_network_nodes/edges.csv`, `route_headways.csv`,
`route_stop_headways.csv`, `transit_analysis.gpkg`. Phase 3 adds
`interchanges_report.*`, `conflation_report.*`, `osm_coverage_gaps.gpkg`.

## Docs map

- `README.md` — install, run, full CLI reference, output schemas.
- `docs/guide/workflow.md` — every check, rule and formula, end to end.
- `docs/guide/pipeline-guide.md` — algorithms with diagrams and measured numbers.
- `docs/guide/data-dictionary.md`, `data-documentation.txt` — column dictionary (Spanish).
- `docs/guide/roadmap.md` — gap analysis vs transitland-lib; `docs/plans/*.md` cite its sections.
