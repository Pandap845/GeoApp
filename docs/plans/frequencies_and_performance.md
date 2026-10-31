# Roadmap 6 + 8: frequencies expansion, calendar expiry, walk-cache, COPY, scipy Dijkstra

## Context

Two roadmap items from `future_project.md`:

**Point 6 (correctness).** Both real feeds are **frequency-dominated** —
improved: 181/184 trips defined in frequencies.txt (1,291 rows); mdb: 970/989
(9,166 rows) — yet nothing reads frequencies.txt, so each such trip
contributes exactly ONE departure to headway math. Headways for ~95% of
trips are wrong today. Separately, both feeds' calendars are **expired**
(improved ended 2022-12-31 — its auto "busiest date" is 2020-01-01!; mdb
ended 2024-12-31) and nothing warns.

**Point 8 (performance).** `--network-walk` rebuilds the OSM walk graph from
PostGIS every run; all DB writes are chunked executemany; the bounded
Dijkstra is pure Python.

**User decisions (fixed):**
1. Expiry: Phase 2 per-feed note + machine fields AND a Phase 1 `feed_expired`
   WARNING finding. Nothing blocks.
2. Cache: auto key (params hash + DB probes: `planet_osm_line` count/max osm_id,
   feeds' `stops max(loaded_at)`), stored as `.npz` in `.gtfs_cache/`
   (gitignored); `--walk-cache-dir` override, `--no-walk-cache` bypass.
3. COPY: the 6 plain-scalar tables + `stops` via temp-table staging;
   `interchanges` (arrays + geometry exprs, ~2.7k rows) stays executemany.

**Key verified facts:** partridge already loads AND service-filters
`feed.frequencies` (times pre-parsed to seconds) — expansion just consumes it.
`route_stop_links`/`travel_min` must stay on UN-expanded stop_times (the
template rows already encode the correct travel pattern). The existing
`(indptr, indices, weights)` arrays are a valid symmetric CSR that maps
directly to scipy. scipy is installed (transitive via scikit-learn) but must
become an explicit dependency.

---

## Stream A — frequencies + calendar expiry (changes outputs; do first)

### A-1. `expand_frequencies` in [headways.py](gtfs_ingest/analysis/headways.py)
Pure function `expand_frequencies(stop_times, frequencies, max_runs=MAX_FREQUENCY_RUNS=1000)
-> DataFrame[trip_id, stop_id, departure_time]`, vectorized:
- coerce start/end/headway numeric; drop NaN / `headway_secs <= 0` /
  `end <= start` (warn with count);
- `n_runs = ceil((end-start)/headway)` clipped at `max_runs` (warn if clipped);
  runs at `start + k*headway`, last run strictly `< end_time`;
- per-stop offsets = `departure_time - groupby(trip_id).min()` from the
  template rows (dropna); merge, `departure_time = run_start + offset`;
- `drop_duplicates([trip_id, stop_id, departure_time])` (overlapping ranges);
  stable sort → deterministic. exact_times 0 and 1 expanded identically
  (documented).

### A-2. Wire into `stop_departures` ([headways.py:114-148](gtfs_ingest/analysis/headways.py#L114-L148))
Signature/contract unchanged. When `feed.frequencies` is non-empty: **drop
the template departures of frequency trips** (GTFS: with exact_times=0 the
template's absolute times are a pattern, not real departures), concat the
synthetic rows, `log.info` the expansion counts. `compute_stop_headways`
unchanged. Module docstring: `trip_count` now counts departures incl.
synthetic runs — the metric-semantics change is the point.

### A-3. Report visibility ([pipeline_phase2.py](gtfs_ingest/analysis/pipeline_phase2.py))
`FeedAnalysis` gains `frequency_trips_expanded: int = 0`,
`calendar_end: Optional[str]`, `calendar_status: str = "unknown"`. In
`_analyze_feed`: set the count from `feed.frequencies` + append a note
(notes render in the Feeds table automatically).

### A-4. Phase 1 `feed_expired` WARNING ([validators.py](gtfs_ingest/validators.py))
Shared pure helper `calendar_range(calendar, calendar_dates) ->
(Optional[date], Optional[date])`: min start / max end across
calendar start_date/end_date + calendar_dates ADDED dates (exception_type 1;
missing column → all dates), `errors="coerce"` on YYYYMMDD, (None, None)
when nothing parses. New check `_check_calendar_expiry` appended to the
checks list: `end < date.today()` → WARNING `feed_expired` with end date +
days stale. Update the module docstring check list.

### A-5. Phase 2 expiry note (`_analyze_feed`)
Using the raw `LoadedFeed.table()` frames (view-independent):
`calendar_end`/`calendar_status` set; notes appended when expired, when the
analyzed date falls outside validity (defensive), and when
`selection.source == "busiest"` and the busiest date is
> `STALE_BUSIEST_DAYS = 30` days in the past. Import `calendar_range` from
`..validators` (no cycle).

### A-6. Tests
`test_headways.py`: expand basics (3 runs × 2 stops at exact seconds),
end-exclusive boundary, bad-row guards, `max_runs` cap, determinism; new
`FREQUENCY_FEED` fixture proving template replacement end-to-end through
`load_service_feed`; window `trip_count` rises. `test_validators.py`:
`feed_expired` emitted/not-emitted (use `date.today()`-relative dates for
"current" cases — no time bombs), calendar_dates-only feeds, type-1
extension, junk tolerated. Pipeline dry-run: `calendar_status == "expired"`,
`frequency_trips_expanded` in JSON, notes in MD.

### A-7. Docs
WORKFLOW.md §3.2 (currently says frequencies are NOT expanded — rewrite),
README (headway semantics + `feed_expired` in check list),
diccionario_datos.md (`trip_count` meaning), future_project.md row 6 done.

---

## Stream B — cache + COPY + scipy (pure performance)

### B-1. New [gtfs_ingest/analysis/graph_cache.py](gtfs_ingest/analysis/graph_cache.py)
`CACHE_VERSION = 1`; `probe_state(engine, feed_ids, line_table)` → two cheap
queries (`count(*), max(osm_id)` from line table; `max(loaded_at)` from
stops for the feeds); `cache_key(...)` = sha256 of the canonical
`v1|feeds|buffer|table|geom|excluded|crs|probes` string;
`load(cache_dir, key)` → arrays or None (try/except, corrupt = miss);
`save(...)` → `np.savez_compressed` as `walkgraph-{key[:12]}.npz`.

### B-2. Cache in `network_distances` ([walk_network.py:266-291](gtfs_ingest/analysis/walk_network.py#L266-L291))
New param `cache_dir: Optional[Path] = None` (None = today's behavior).
Resolve the metric CRS first (part of the key). Hit → load arrays; miss →
time fetch+build with `perf_counter`, save, log both paths (hit/miss +
seconds + vertex count). Any cache error → warn + rebuild.

### B-3. Config + CLI
`AnalysisConfig`: `walk_cache: bool = True`, `walk_cache_dir: Path =
Path(".gtfs_cache")`. CLI (`analyze-frequency`): `--walk-cache`
(BooleanOptionalAction, default True), `--walk-cache-dir DIR`. Call site
passes `cache_dir` only when `network_walk and walk_cache`.
`.gitignore`: add `.gtfs_cache/`.

### B-4. New [gtfs_ingest/bulk.py](gtfs_ingest/bulk.py) — COPY helper
`clean_records(df)` (NaN→None + numpy scalars→`.item()` natives);
`copy_rows(conn, table, df)` via SQLAlchemy raw psycopg3 cursor:
`conn.connection.cursor().copy(f"COPY {table} ({cols}) FROM STDIN")` +
`write_row` per record — inside the caller's transaction;
`bulk_insert(conn, table, df)` = copy_rows with fallback to chunked
executemany on ANY failure (logged warning) — a performance feature never
breaks a run.

### B-5. Swap sites
- [analysis/storage.py](gtfs_ingest/analysis/storage.py) `_insert` body → `bulk_insert` (covers both
  headway tables, route_connections, stop_equivalences); local
  `_to_records`/`_CHUNK_SIZE` removed.
- [conflation/storage.py](gtfs_ingest/conflation/storage.py) `_reload_plain` → `bulk_insert`
  (stop_osm_matches, stop_side_of_street). `load_interchanges` untouched.
- [db.py](gtfs_ingest/db.py) `load_feed_stops`: `CREATE TEMP TABLE tmp_stops (... scalars ...)
  ON COMMIT DROP` → `copy_rows` → `INSERT INTO stops SELECT ...,
  ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326) FROM tmp_stops`, all in
  the existing transaction; try/except falls back to the current
  executemany. Add elapsed seconds to the load log.

### B-6. scipy Dijkstra ([walk_network.py](gtfs_ingest/analysis/walk_network.py))
Replace `_bounded_dijkstra` (delete + drop heapq) inside
`pair_network_distances` — output contract unchanged (NaN no-path, snap
offsets added, snap_m columns, NO line clip; locked by existing tests):
`csr_matrix((weights, indices, indptr))`, dedupe snapped source vertices,
chunks of `_DIJKSTRA_CHUNK = 16` sources per
`dijkstra(graph, directed=True, indices=chunk, limit=cutoff_m)` call —
dense (chunk, N) allocation bounds memory at ~256 MB even for a 2M-node
graph (chunk 256 would be 4+ GB). `inf` → NaN. Add `"scipy>=1.11"` to
pyproject dependencies (already a real import via cKDTree).

### B-7. Tests
New `test_graph_cache.py`: key determinism + sensitivity to every component,
save/load roundtrip (dtypes), missing/corrupt → None, integration with
monkeypatched probes counting `fetch_walk_lines` calls (hit = zero fetches).
`bulk`: clean_records units; fallback path with a fake failing cursor; one
live gated test (NaN→NULL roundtrip via replace_feed_rows) — the existing
gated stops test exercises the temp-table path in CI. `test_walk_network.py`:
all 7 existing tests pass unchanged post-swap; add same-vertex-snap pair and
at-cutoff pair. CLI flag test extension.

### B-8. Docs
WORKFLOW.md (cache key/invalidation + bulk-loading note), README (flags +
`.gtfs_cache/`), future_project.md row 8 done.

---

## Verification

```
python -m pytest -q                          # all pass after each stream

# Stream A, real feeds:
python -m gtfs_ingest.cli ingest --feeds improved-*.zip mdb-*.zip --report <scratch> --dry-run
#   -> feed_expired WARNING for both feeds
python -m gtfs_ingest.cli analyze-frequency --feeds improved-*.zip mdb-*.zip --report <scratch> --dry-run
#   -> "Frequencies: 181/970 trip(s) expanded"; stop_headway_rows + window
#      trip_counts jump massively vs the pre-change baseline;
#      calendar_status "expired" + notes; near_stations.csv/quality/gpkg
#      schemas unchanged (travel_min untouched)

# Stream B (live DB + OSM):
analyze-frequency ... --network-walk        # run 1: "Walk graph built in Xs; cached"
analyze-frequency ... --network-walk        # run 2: "cache hit ... loaded in <1s",
                                            #   byte-identical near_stations.csv
analyze-frequency ... --no-walk-cache       # forced rebuild logged
# COPY: stops/headways load logs show elapsed drop
```

## Risks (accepted/mitigated)
- Headway values/trip_count jump — intended and documented; DB rows not
  comparable across versions.
- Fixture calendars end 2026-12-31 → will emit `feed_expired` from 2027
  (warnings only; new tests use today-relative dates).
- Cache misses in-place OSM edits that keep count+max(osm_id) — escape:
  `--no-walk-cache` / delete `.gtfs_cache/`; `CACHE_VERSION` bump on format
  changes.
- COPY failures degrade to executemany with a logged warning, same
  transaction — correctness never at stake.
- scipy dense allocation bounded by chunk=16; one-line tune if profiling
  demands.
