# Next batch: analysis clock, date coherence, station network, date range

## Context

Four follow-ons exposed by the calendar/frequency/dedupe work:

1. **Cross-feed date coherence** — the combined run analyzes improved on
   2020-01-01 and mdb on 2024-01-01, yet merges both into one picture.
   Now visible (calendar notes), not yet flagged at run level.
2. **Analysis clock** — `date.today()` in `_check_calendar_expiry` and
   `_analyze_feed` makes outputs depend on the wall clock (breaks the
   determinism contract; fixture time bombs in 2027).
3. **Routable station network** — the data contains a shortest-path network
   (stops + travel_min + walk_min + alias links) but only exposes
   route-level adjacency.
4. **Date-range analysis** — a single per-feed busiest date is fragile;
   OTP2-style window analysis is coherent by construction.

**User decisions (fixed):** warning + opt-in `--common-date` (exit 1 when no
common date — true for the real feeds); nodes = stops with 0-min
`same_station` alias edges + `canonical_uid` attribute; GraphML **and** a
GeoPackage layer; per-day headway rows with **PKs widened** to include
`service_date` (idempotent ALTER migration).

**Implementation order: 2 → 1 → 4 → 3** (clock feeds the warnings; the
station network consumes the union frames the range produces).

---

## Feature 2 — Injectable analysis clock

- `Settings.analysis_date: Optional[date]` (AliasChoices +
  `GTFS_ANALYSIS_DATE` env, [config.py:52-55](gtfs_ingest/config.py#L52-L55) pattern); `None` → resolved
  once to `date.today()` at run start.
- Phase 1 threading: `run_pipeline` resolves → `process_feed(source, engine,
  analysis_date=None)` → `validate_feed(loaded, analysis_date=None)` → bind
  into the checks list via
  `lambda l, f: _check_calendar_expiry(l, f, today)`; the check gains a
  `today` param replacing [validators.py:369](gtfs_ingest/validators.py#L369). Defaults keep every
  existing caller/test working. `RunReport.analysis_date: Optional[str]`.
- Phase 2: `AnalysisConfig.analysis_date: date =
  Field(default_factory=date.today)`; `_analyze_feed` line
  [:228](gtfs_ingest/analysis/pipeline_phase2.py#L228) uses `config.analysis_date` (the `:246` staleness check
  reuses the local). `AnalysisReport.analysis_date: str` + a markdown line.
- `generated_at` timestamps stay wall-clock (metadata, not analysis input —
  document the distinction).
- CLI `--analysis-date YYYY-MM-DD` on `ingest` + `analyze-frequency`;
  `date.fromisoformat` in the existing try/ValueError→exit-1 pattern.
- Tests: injected past/future dates → deterministic `feed_expired` day
  counts; env var via monkeypatch; report fields; bad date → exit 1.

## Feature 1 — Coherence warning + `--common-date`

- `service_filter.common_service_date(paths) -> Optional[date]`: intersect
  `{d for d, ids in ptg.read_service_ids_by_date(p).items() if ids}` across
  feeds; `max` or `None`. One calendar read per feed, no feed load.
- `ServiceSelection.source` Literal gains `"common"`; `resolve_service` /
  `load_service_feed` gain a `label` pass-through so
  `service_date_source == "common"` in the report.
- `run_analysis`: when `config.common_date`, resolve after `discover_feeds`;
  `None` → `raise ValueError("--common-date: the discovered feeds share no
  calendar date with active service...")` → existing except → exit 1.
  Use `config.model_copy(update={"service_date": common})` (no mutation).
- **Always-on detection** post-loop: distinct `service_date` among
  `status=="analyzed"` feeds → `AnalysisReport.service_dates_coherent: bool`
  + `service_dates: list[str]`; prominent markdown warning block +
  `log.warning`.
- CLI `--common-date` (store_true); manual mutual exclusion with
  `--service-date`/`--service-date-range` (ValueError → exit 1, preserving
  the exit-code contract).
- Tests: helper (overlap → max; disjoint → None; calendar_dates-only feed);
  disjoint feeds + `--common-date` → exit 1; coherent/incoherent report
  fields; success path with two overlapping fixtures → both `"common"`.

## Feature 4 — `--service-date-range START..END`

- **Storage migration** ([analysis/storage.py](gtfs_ingest/analysis/storage.py)): fresh-DB PKs gain
  `service_date` (route_stop_headways [:51](gtfs_ingest/analysis/storage.py#L51), route_headways [:66](gtfs_ingest/analysis/storage.py#L66));
  plus two idempotent `DO $$ ... $$` blocks in `_DDL_STATEMENTS` that drop
  and re-add `*_pkey` **only when** the existing constraint lacks
  `service_date` (checked via pg_constraint/pg_attribute). Widening a PK
  can't violate uniqueness; `replace_feed_rows` deletes whole-feed so
  multi-date frames stay idempotent.
- CLI `_parse_date_range("START..END")` with `MAX_RANGE_DAYS = 31` cap,
  START≤END, ISO validation → ValueError → exit 1; mutual exclusion as above.
- `AnalysisConfig.service_date_range: Optional[tuple[date, date]]`.
- `_analyze_feed` refactor: extract the per-day core (frequencies note →
  stop_departures → headways → rollup → route_stop_map → metadata → shapes →
  links) into `_analyze_service_day(feed, source, config) -> _FeedData`;
  single-date path calls it once (byte-identical behavior).
- Range path: `read_service_ids_by_date` **once** per feed; served days in
  window (none → `no_service`); per served day build the partridge view
  directly (`{"trips.txt": {"service_id": ids_by_date[d]}}` + route filter)
  and run the day core; tag each day's headway frames with its own
  `service_date` before concat (gaps never span days). Union frames for
  everything downstream: route_stops/routes/shapes dedup on natural keys;
  stop_links grouped on the link key with `travel_min` median + `n_obs` max
  (preserving `LINK_COLUMNS` order).
- Report: `service_date = "START..END"`, `service_date_source = "range"`
  (plain strings), new `FeedAnalysis.days_analyzed: Optional[int]`;
  calendar notes count out-of-validity days.
- Tests: parser edge cases; VALID_FEED week range → `days_analyzed == 5`
  (weekend skipped), 5× headway rows; link-union median/max; DDL regex test
  for the DO-blocks + a DB-gated idempotency test (two `ensure` runs, two
  dates inserted); mutual exclusion; empty range → `no_service`.

## Feature 3 — Station network export

- **New module** [gtfs_ingest/analysis/station_network.py](gtfs_ingest/analysis/station_network.py) (graph_export.py
  stays route-level/undirected — zero regression risk; reuse its
  `_clean_attrs`).
- **`nx.MultiDiGraph`** — Multi because a pair can carry walk AND in_vehicle
  edges (and multiple lineas per segment); Di because travel_min is
  directional; walk/same_station become reciprocal arcs so
  `nx.shortest_path(..., weight="time_min")` just works. Deterministic edge
  keys: `in_vehicle:{route_uid}:{direction_id}` / `walk` / `same_station`.
  Cast numpy scalars to natives; `_clean_attrs` drops NaN travel_min
  (untimed links carry no `time_min` — documented).
- API: `build_station_graph(stops, near, stop_links, stop_pairs,
  equivalences, canonical)`; `station_graph_stats` (weakly-connected
  components — NOT graph_metrics); `write_station_graphml`;
  `station_edges_layer(graph, stops)`.
- Nodes: every `all_stops` row, key `stop_uid`; attrs `near_id` (the CSV
  `ID`), `name`, `feed`, `canonical_uid` (fallback own uid), `lat`, `lon`.
- Edges: `in_vehicle` from `all_links` (time_min=travel_min, linea,
  direction_id, route_uid); `walk` from the dedupe-filtered stop_pairs
  (time_min=`walk_minutes(distance_m)`, distance_m, walk_source when
  present); `same_station` 0.0-min reciprocal arcs from the **equivalences
  pair frame** (the accepted evidence; 0-cost chains route fine).
- Outputs: `station_network.graphml` next to the existing export
  ([pipeline_phase2.py:413-415](gtfs_ingest/analysis/pipeline_phase2.py#L413-L415)) + a `station_network` GeoPackage layer
  (LineStrings per undirected pair per type, reciprocals deduped
  `a < b`; new optional `station_edges` param on `write_geopackage`,
  `mode="a"` append like [:92-99](gtfs_ingest/analysis/geopackage.py#L92-L99)). `AnalysisReport.station_graph:
  dict` from the stats + export-list markdown lines.
- Tests (`tests/test_station_network.py`): node attrs + canonical fallback;
  parallel edges coexist (walk + in_vehicle = 3 arcs); reciprocity;
  cross-feed routing through a 0-min alias edge (`shortest_path_length`
  equals in-vehicle + walk sum); GraphML round-trip (catches numpy/NaN);
  empty inputs; determinism; geopackage layer columns + existing 4 layers
  unchanged.

## Feature 5 — More CSV exports (user addition)

Everything below already exists as in-memory frames; these are pure writers
mirroring `write_near_stations` (utf-8, `index=False`):

- **`station_network_nodes.csv`** — one row per node of the station graph:
  `stop_uid, near_id, name, feed, canonical_uid, lat, lon`.
- **`station_network_edges.csv`** — one row per edge:
  `edge_type, stop_uid_a, stop_uid_b, time_min, linea, direction_id,
  route_uid, distance_m` (same `EDGE_COLUMNS` as the gpkg layer; reciprocal
  walk/same_station arcs deduped `a < b`, in_vehicle kept per direction).
  Together with the nodes file this makes the routable network consumable
  from Excel/pandas without touching GraphML.
- **`route_stop_headways.csv`** + **`route_headways.csv`** — the headway
  tables written to the report dir on EVERY run (today they are DB-only, so
  `--dry-run` produces no headway output at all). Same columns as the DB
  tables incl. `service_date` — range runs produce per-day rows naturally.

Wire all four next to the near-stations write in `run_analysis`; list them
in the report's exports footer; add roundtrip tests + a dry-run e2e assert
that the files exist with expected row counts.

## Docs
README (three flags, PK auto-migration note, station network outputs + the
four new CSVs), diccionario_datos.md gains the CSV schemas,
WORKFLOW.md (invocations + station network section), diccionario_datos.md
(station_network node/edge schema, Gephi parallel-edges note),
future_project.md (defer: single-load per-day trip masks optimization).

## Verification
1. `python -m pytest -q` — 194 existing + new all green.
2. Baseline real-feed dry run → near_stations.csv/sidecar/
   route_connectivity.graphml unchanged; NEW `station_network.graphml` +
   gpkg layer; report has `analysis_date`, `service_dates_coherent: false`
   + markdown warning (2020 vs 2024 dates).
3. `--common-date` on the real feeds → **exit 1** with the no-common-date
   message (expected: their windows don't overlap).
4. `--analysis-date 2020-11-01` → expiry notes recomputed (improved no
   longer "expired" relative to that date).
5. `--service-date-range 2025-02-24..2025-03-02` → mdb analyzed with
   `days_analyzed ≤ 7`, `source=range`; improved `no_service`. With DB:
   per-day rows grouped by service_date; widened PK; idempotent re-run.

## Risks (mitigated)
- GraphML numpy/NaN rejection → native casts + `_clean_attrs` + round-trip
  test. Gephi merges parallel edges unless enabled — documented.
- PK migration needs ownership + exclusive lock; failure aborts at
  `ensure_analysis_schema` (run-level, consistent contract); widening can't
  violate uniqueness.
- Range cost: one zip parse per served day (≤31, capped) — honest,
  optimization deferred.
- `service_date` becomes `"a..b"` for ranges in the report only (DB rows
  carry real per-day dates) — README note.
