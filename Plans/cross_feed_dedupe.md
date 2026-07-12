# Cross-feed station deduplication (future_project.md §1.3)

## Context

The pipeline ingests two GTFS feeds whose stops overlap: the same physical
station appears as `improved:SITEUR_x` in one feed and `mdb:TL_x`/`ST_x` in the
other. Verified consequences: ~24 phantom "multimodal" interchange clusters
(74 combined vs 14+36 isolated) and stations listed as "near" their own alias
in `near_stations.csv`. This plan adds a deduplication stage: candidate pairs
(different feeds, ≤ ~30 m), a match score (distance + normalized-name
similarity + shared route short names), equivalence records, and a
`canonical_station_id` per stop via connected components.

**User decisions (fixed):**
1. `near_stations.csv` format/rows/IDs unchanged — aliases only stop appearing
   in each other's `near_stations` lists; a new audit file
   `stop_equivalences.csv` records merged groups.
2. On by default; `--no-dedupe` disables (argparse `BooleanOptionalAction` —
   fine, `requires-python >= 3.11`).
3. Scope = all outputs: near_stations, GeoPackage, route_connections/GraphML
   (alias proximity edges upgrade to a same-station type), and Phase 3
   interchange clustering.

## Design

- **New module** [gtfs_ingest/analysis/dedupe.py](gtfs_ingest/analysis/dedupe.py) — Phase 2 runs first and
  conflation already imports from analysis ([clustering.py:34](gtfs_ingest/conflation/clustering.py#L34)), so both
  phases use it without circular imports. Stdlib matching only
  (`unicodedata` NFKD accent-strip + `casefold` + `difflib.SequenceMatcher`
  token-sort ratio) — no new dependency.
- **Candidates** always via `connectivity.proximity_pairs_local(stops, dedupe_distance_m)`
  ([connectivity.py:145-178](gtfs_ingest/analysis/connectivity.py#L145-L178)), never PostGIS: decoupled from `--proximity-m`,
  reusable by Phase 3 (which has no stop_pairs), deterministic, negligible
  UTM-vs-geodesic error at 30 m. Cross-feed filter by merging `feed_id` onto
  both sides.
- **Accept rule:** `distance_m <= 30` (config) AND (`name_score >= 0.75`
  (config) OR ≥ 1 shared normalized `route_short_name`), feeds differ. Empty
  or missing names score 0.0 (never match on name alone). Audit
  `score = 0.5*(1 - dist/max) + 0.5*name_score`.
- **Groups:** `nx.connected_components` over accepted pairs; components sorted
  by min `stop_uid`; `group_id` = index, `canonical_uid` = min `stop_uid` —
  the exact determinism idiom of [clustering.py:148-152](gtfs_ingest/conflation/clustering.py#L148-L152).
- **Connection type:** new distinct `same_station_dedup` (not reuse of
  `same_parent_station` — inferred merges must stay distinguishable from
  GTFS-declared stations). Precedence: `same_stop > same_parent_station >
  same_station_dedup > proximity`; existing pairs never change type.
- **`stop_equivalences` DB table** lives in `analysis/storage.py` (Phase 2
  writes it, full delete-reload like `route_connections`). Phase 3
  **recomputes** equivalences rather than reading the DB (pipelines stay
  independently runnable / dry-run capable; distance+name evidence is
  schedule-independent — drift on route evidence documented).

## Steps

### 0. Store this plan in the repo
Create `Plans/` at the repo root and save this document as
`Plans/cross_feed_dedupe.md`. Note: the current `.gitignore` ignores `*.md`
except README/WORKFLOW — add `!Plans/*.md` to the gitignore so plan documents
stay tracked.

### 1. New `gtfs_ingest/analysis/dedupe.py`
Constants `DEDUPE_DISTANCE_M = 30.0`, `NAME_SIM_THRESHOLD = 0.75`,
`EQUIVALENCES_NAME = "stop_equivalences.csv"`,
`EQUIVALENCE_COLUMNS = [group_id, canonical_uid, stop_uid_a, stop_uid_b, distance_m, name_score, shared_routes, score]`,
`CANONICAL_COLUMNS = [stop_uid, canonical_uid, group_id]`.
Functions: `normalize_name`, `name_similarity`,
`find_equivalences(stops, route_stops, routes, distance_m, name_threshold) -> DataFrame` (accepted pairs, sorted),
`canonical_map(equivalences, stops) -> DataFrame`,
`attach_groups(equivalences, canonical) -> DataFrame` (CSV/DB payload),
`filter_stop_pairs(stop_pairs, canonical)` (drop pairs whose two endpoints share a canonical_uid),
`write_equivalences(equivalences, out_dir)` (mirrors `write_near_stations`).
Shared-route evidence: normalized `route_short_name` sets per stop via
`route_stops` + `routes` (absent column → 0). All outputs stably sorted.

### 2. `connectivity.py` — fourth evidence source
`build_edges(route_stops, stations, stop_pairs=None, canonical=None)`
(backward compatible). New `_dedup_station_pairs(route_stops, canonical)` —
clone of `_parent_station_pairs` ([connectivity.py:202-209](gtfs_ingest/analysis/connectivity.py#L202-L209)) joining on
`canonical_uid`. Assignment order in `build_edges` ([:229-282](gtfs_ingest/analysis/connectivity.py#L229-L282)) puts
`same_station_dedup` between proximity and parent-station. Fix the module
docstring claim that proximity is the only cross-feed type.

### 3. `analysis/storage.py` — DDL
Append `stop_equivalences` CREATE TABLE (PK `(stop_uid_a, stop_uid_b)`,
`ix_stop_equiv_group` index) to `_DDL_STATEMENTS`; add
`"stop_equivalences": False` to `_TABLES` whitelist ([analysis/storage.py:76-80](gtfs_ingest/analysis/storage.py#L76-L80));
reuse `replace_all_rows`.

### 4. Phase 2 wiring — `pipeline_phase2.py`
- `AnalysisConfig` (after `network_walk`, [:73](gtfs_ingest/analysis/pipeline_phase2.py#L73)): `dedupe: bool = True`,
  `dedupe_distance_m: float = 30.0 (gt=0)`, `dedupe_name_threshold: float = 0.75 (0..1)`.
- Insert between stop_pairs ([:279](gtfs_ingest/analysis/pipeline_phase2.py#L279)) and `build_edges` ([:280](gtfs_ingest/analysis/pipeline_phase2.py#L280)) — the single
  choke point, before `--network-walk` refinement so alias pairs get no walk
  computation: find → canonical → attach → `filter_stop_pairs`; pass
  `canonical` to `build_edges`. **near_stations.py needs zero changes** (row
  count and ID sequence provably identical dedupe on/off); GeoPackage
  `near_links` filtered for free.
- Write `stop_equivalences.csv` next to [:308](gtfs_ingest/analysis/pipeline_phase2.py#L308) (only when dedupe on);
  `storage.replace_all_rows(engine, "stop_equivalences", equivalences)` next
  to [:317](gtfs_ingest/analysis/pipeline_phase2.py#L317) (reload-to-empty when off, non-dry).
- `AnalysisReport`: add `dedupe`, `dedupe_distance_m`, `station_groups`,
  `stops_deduplicated`. `_render_markdown`: dedup line block +
  `same_station_dedup` in the connection-type loop + CSV in exports footer.

### 5. CLI — `cli.py`
`analyze-frequency` (after `--network-walk`, [:146-152](gtfs_ingest/cli.py#L146-L152)):
`--dedupe` (`BooleanOptionalAction`, default True), `--dedupe-distance-m`,
`--dedupe-name-threshold`; wire in `_run_analyze` ([:362-375](gtfs_ingest/cli.py#L362-L375)). Same three
flags on `detect-interchanges` ([:163-187](gtfs_ingest/cli.py#L163-L187)) wired in `_run_detect_interchanges`.

### 6. Phase 3 wiring — conflation
- `clustering.cluster_stops(..., canonical=None)`: third edge source after the
  parent_station block ([clustering.py:140-145](gtfs_ingest/conflation/clustering.py#L140-L145)) chaining each canonical
  group's members; determinism preserved by existing `components.sort`.
- `clustering.build_interchanges(..., canonical=None)`: new
  `physical_stop_count` per cluster (`len({canonical.get(u, u) for u in uids})`);
  append to `CLUSTER_COLUMNS`. `is_multimodal` unchanged (deliberate:
  physical counts expose phantoms, they don't reclassify modes).
- `pipeline_phase3.py`: `InterchangeConfig` gains the three dedupe fields;
  `_collect_feed` ([:146-152](gtfs_ingest/conflation/pipeline_phase3.py#L146-L152)) keeps `route_short_name` when present (needed for
  shared-route evidence); recompute equivalences between `_concat` ([:194](gtfs_ingest/conflation/pipeline_phase3.py#L194)) and
  `cluster_stops` ([:196](gtfs_ingest/conflation/pipeline_phase3.py#L196)). `InterchangeReport` gains `dedupe`,
  `equivalence_groups`, `stops_deduplicated`, `single_physical_station_clusters`
  (clusters with `physical_stop_count == 1` — the phantom count).
- `conflation/storage.py`: `ALTER TABLE interchanges ADD COLUMN IF NOT EXISTS
  physical_stop_count integer NOT NULL DEFAULT 0` in `_DDL_STATEMENTS`
  (required — CREATE IF NOT EXISTS won't add it to existing DBs); add the
  column to the `load_interchanges` INSERT.

### 7. Tests
New `tests/test_dedupe.py` (reuse `rs`/`stops_geo` fixtures from
test_connectivity.py, house style: inline frames + exact asserts):
normalize_name (accents/case/punct/token-sort/None), name_similarity edges,
accept case, same-feed exclusion, distance gate, name gate + shared-route
rescue, empty-name behavior, transitive merge + input-order determinism,
`filter_stop_pairs`, empty-input frames.
Additions: test_connectivity.py — `same_station_dedup` type + precedence;
test_near_stations.py — alias tuple vanishes, row count/IDs identical on/off;
test_clustering.py — canonical group merges beyond eps (mirror of the
parent-station test), `physical_stop_count`; e2e two-feed CLI dry-run fixture —
default vs `--no-dedupe` (CSV presence, connection types, near_links count,
identical near_stations IDs); `detect-interchanges` dry-run accepts `--no-dedupe`.
Existing tests survive: all current fixtures are single-feed → cross-feed-only
dedupe finds nothing (verified against the exact-string asserts).

### 8. Docs
- WORKFLOW.md: new "§3.3b Cross-feed station deduplication" (rule, determinism,
  Phase 3 recompute tradeoff); `stop_equivalences` table in §4.1, CSV in §4.2.
- README.md: three new flags on both subcommands; `same_station_dedup` row in
  the connection-type table.
- diccionario_datos.md: `stop_equivalences.csv` column dictionary (Spanish);
  note that alias pairs are excluded from `near_stations` by default; new
  `connection_type` value.

## Verification

```
python -m pytest tests -q            # all green, incl. new test_dedupe.py

python -m gtfs_ingest.cli analyze-frequency --feeds improved-gtfs-mimacro-mitren-sitren.zip mdb-2366-202502241922.zip --report out_dedupe --dry-run
python -m gtfs_ingest.cli analyze-frequency --feeds ... --report out_nodedupe --no-dedupe --dry-run
python -m gtfs_ingest.cli detect-interchanges --feeds ... --report out_dedupe --dry-run
python -m gtfs_ingest.cli detect-interchanges --feeds ... --report out_nodedupe --no-dedupe --dry-run
```

Expected observables:
- `out_dedupe/stop_equivalences.csv` exists (all pairs cross-feed, ≤ 30 m);
  absent in `out_nodedupe`.
- `near_stations.csv`: identical row count and ID column in both runs; diff
  shows only removed alias tuples inside `near_stations` strings.
- `analysis_report.md`: dedup block, nonzero `same_station_dedup`,
  `proximity` count drops vs no-dedupe; graph `n_components` never increases.
- GeoPackage `near_links` count lower by exactly the filtered-pair count.
- `interchanges_report.json`: `single_physical_station_clusters` ≈ the ~24
  verified phantoms; every cluster has `physical_stop_count <= stop_count`.

## Risks (accepted / documented)
- Pair with both parent-station and dedup evidence keeps `same_parent_station`
  (stability) — report undercounts alias evidence; the CSV is the full audit.
- Two same-feed platforms can merge transitively via a cross-feed bridge —
  correct by transitivity, documented.
- Phase 2 (service-filtered) vs Phase 3 (full-feed) route evidence may differ
  for pairs rescued only by shared routes — documented.
- New `connection_type` value flows into GraphML edge attributes — README
  table updated for downstream consumers.
