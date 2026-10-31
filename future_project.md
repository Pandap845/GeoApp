# future_project.md — Gap analysis and roadmap for gtfs-ingest

Comparison baseline: [transitland-lib](https://github.com/interline-io/transitland-lib)
(local clone in `transitland-lib/`), plus the wider ecosystem
(MobilityData `gtfs-validator`, `gtfstidy`, OpenTripPlanner/r5py, UrbanAccess).

The two tools do not compete head-on: transitland-lib is **feed
infrastructure** (fetch, validate, version, store, serve), while gtfs-ingest is
**spatial analysis on top of feeds** (interchanges, near-stations relations,
OSM conflation, side-of-street, walk network). Nothing in transitland-lib
produces `near_stations.csv`. The gaps worth closing are the infrastructure
habits transitland-lib has that this project lacks — mostly around feed
lifecycle, validation depth, and reproducibility.

---

## 0. Feature matrix

| Capability | transitland-lib | gtfs-ingest today | Gap severity |
|---|---|---|---|
| Feed fetching / registry (DMFR) | `fetch`, `sync`, DMFR files | Manual zips in repo root | Medium |
| Feed versioning (sha256, import/unimport) | `feed_versions` table, activation | Delete-reload per feed, no history | **High** |
| Spec validation | `validator/` + `rules/` (spec-referenced), notices with entity/line context | `validators.py`: required files/columns, coords, referential integrity | **High** |
| Feed merge with dedupe | `merge` (ID collision handling) | Concatenation with `feed_id:` namespacing, no cross-feed dedupe | **High** (source of phantom multimodal clusters) |
| Feed diff | `diff` command | None | Low |
| Extract/subset feed | `extract` | `--transport-mode/--route` filter (analysis-time only, no filtered GTFS artifact) | Low |
| calendar + calendar_dates service model | `service/` package | `service_filter.py` (date window); `frequencies.txt` NOT expanded | Medium |
| GTFS-Realtime | `rt/` full support | None (out of scope) | N/A |
| Schema migrations | golang-migrate, versioned `.pgsql` files | Ad-hoc `CREATE TABLE IF NOT EXISTS` | Medium |
| CI | GitHub Actions: tests + PostGIS service + coverage + releases | None | **High** |
| API layer | GraphQL/REST server | None (research tool; not needed) | N/A |
| Spatial analysis (interchanges, conflation, walk network, near-stations) | None | **This is the project's value** | — |

---

## 1. What can be improved, and how

### 1.1 Feed lifecycle and provenance (borrow the transitland-lib model)

Today the two GTFS zips sit in the repo root and every run overwrites tables.
There is no record of *which bytes* produced *which output*.

**How:**
- Add a `feed_versions` table: `(feed_id, sha256, source_url, fetched_at,
  earliest_service_date, latest_service_date, imported_at)`. Compute the sha256
  of each zip at Phase 1 and refuse to silently re-import identical bytes
  (skip) or changed bytes under the same name (warn).
- Store raw zips content-addressed under `data/feeds/<sha256>.zip` instead of
  the repo root; keep a small `feeds.json` registry (a lightweight DMFR: URL,
  license, fetch date) — this replaces and mechanizes `references.md`.
- Stamp every output (`near_stations.csv` sidecar, GeoPackage metadata table,
  `phase2_report.json`) with the input feed sha256s + tool git commit.

### 1.2 Validation depth

`validators.py` covers structure and referential integrity well, but the
canonical validators check far more. Missing checks that *directly affect this
project's outputs*:

| Missing check | Why it matters here |
|---|---|
| `stop_times` sequence monotonicity (arrival ≤ departure, times increase along trip) | `travel_min` medians in `route_stop_links` silently absorb negative/absurd segments |
| Travel-speed plausibility (segment speed vs mode) | A 300 km/h bus segment poisons the median for that pair |
| Feed expiration (`calendar` range vs analysis date) | The improved feed's calendar is 2020–2022 — analysis in 2026 runs on formally expired service and nothing warns |
| Duplicate/near-duplicate stops within a feed | Inflates near-station lists with self-aliases |
| `frequencies.txt` presence without expansion | Headway analysis undercounts frequency-defined trips |
| `transfers.txt` consistency | Free ground truth to score detected interchanges against |

**How:** two options, not exclusive:
1. **Integrate an external validator** as an optional Phase 1 pre-step: run
   MobilityData `gtfs-validator` (JAR) or `transitland validate` on each zip,
   parse its JSON report, and fold notices into the existing
   `Finding`/`Severity` report. Cheapest path to ~80 spec checks.
2. **Add the analysis-critical checks natively** (speed outliers, calendar
   expiry, intra-feed duplicate stops) so they gate/annotate the pipeline even
   without Java/Go installed.

### 1.3 Cross-feed station deduplication (highest analytical payoff)

The isolated runs proved the problem: 74 "multimodal" clusters combined vs
14 + 36 isolated — ~24 phantoms created purely by the same physical station
appearing under different IDs (`SITEUR_x`, `ST_x`, `TL_x`) across/within feeds.

**How:** add a Phase 2.5 equivalence stage before interchange detection and
before `near_stations`:
- Candidate pairs: stops from *different* feeds (or different agencies) within
  ~30 m.
- Match score: distance + normalized name similarity (casefold, strip accents,
  token sort ratio) + shared route short-names.
- Emit `stop_equivalences (stop_uid_a, stop_uid_b, score, method)` and a
  derived `canonical_station_id` (connected components, like the
  parent_station merge already does).
- Interchange detection and `near_stations` then operate on canonical
  stations: a stop is never "near" its own alias, and multimodality requires
  distinct modes on distinct *physical* infrastructure.
- Keep it opt-out (`--no-dedupe`) and log every merged group for audit — same
  philosophy as the existing conflation confidence scores.

### 1.4 Service model completeness

- **Expand `frequencies.txt`** into synthetic departures in
  `service_filter.py` before headway/`travel_min` computation (already flagged
  as pending). Without it, frequency-based routes get wrong headways and may
  lose `travel_min` observations entirely.
- **Use `transfers.txt` and `pathways.txt`** where present as declared-truth
  layers: report agreement/disagreement between detected interchanges and
  agency-declared transfers (a validation metric for free).

### 1.5 Route connections need the "where" (already identified)

Materialize `route_connection_stops (route_a, route_b, stop_uid, cluster_id)`
so each route-level connection is traceable to the physical stops/cluster that
created it. This also makes the GraphML edges auditable.

### 1.6 Clustering robustness

Fixed `eps=120 m` over-merges downtown (the 52-stop Juárez chain) and
under-merges suburban terminals. Options in order of effort:
- Two-pass DBSCAN: eps 60–80 m inside a high-density mask, 120 m elsewhere.
- HDBSCAN (density-adaptive, no global eps) — one dependency, deterministic
  with fixed parameters.
- Post-split: if a cluster's convex-hull diameter exceeds ~250 m, re-cluster
  its members with smaller eps.

---

## 2. Optimizations

Ordered by expected wall-clock payoff:

1. **Cache the walk graph.** `fetch_walk_lines` + `build_walk_graph` rebuild
   the CSR graph from PostGIS every run. Key a cached artifact
   (`.npz` of the CSR arrays + node coords) on
   `(osm_import_hash, hull_wkb_hash, EXCLUDED_HIGHWAYS)` and reload when
   unchanged. Biggest single win for repeated `--network-walk` runs.
2. **COPY-based loading.** If Phase 1 inserts stops/headways via
   `to_sql`/executemany, switch to psycopg3 `COPY` (`cursor.copy()`). Bulk
   loads of 10⁴–10⁵ rows go from seconds-to-minutes to sub-second.
3. **scipy for Dijkstra.** Replace the pure-Python `heapq` bounded Dijkstra
   with `scipy.sparse.csgraph.dijkstra(indices=sources, limit=cutoff)` — same
   semantics (bounded, multi-source), C speed. Keep the current implementation
   as fallback; the CSR arrays are already the right input format.
4. **Batch and deduplicate snap queries.** Snap all stops to the network once
   per run (vectorized cKDTree query, already close) and reuse across both the
   near-stations pairs and any future isochrone work.
5. **Incremental conflation.** `stop_osm_matches` is full-reload; re-match
   only stops whose `(lat, lon)` changed since the stored match (compare
   against a stored coord hash). Matters as OSM re-imports become routine.
6. **Categorical dtypes** for `stop_uid`/`route_uid`/`feed_id` in the big
   Phase 2 frames — string interning cuts memory roughly 5–10× on those
   columns and speeds groupbys.
7. **Persist a normalized snapshot** (parquet per feed version: stops, trips,
   stop_times post-normalization). Phases 2–4 then read parquet instead of
   re-parsing the zip through partridge each run.

---

## 3. Determinism and output precision (`near_stations.csv`)

This is the contract file — other people build relations on `ID`. Two separate
problems: (a) *the same inputs must produce byte-identical output*, and
(b) *the values must be as close to reality as incomplete GTFS allows, with
honest flags where they are estimates*.

### 3.1 Determinism: current gaps and fixes

**Gap 1 — row order defines `ID`, and row order is not pinned.**
`build_near_stations` assigns `ID = 1..N` in whatever order the `stops` frame
arrives (`near_stations.py`, the `base.insert(0, "ID", ...)` step). That order
is an artifact of feed-file order, per-feed concatenation order, and any
upstream operation — none of it guaranteed. If stops ever come from a DB
`SELECT` without `ORDER BY`, PostgreSQL is free to return any order.

**Fix (one line, do it first):** sort by the stable natural key before
assigning the ID:

```python
base = base.sort_values("stop_uid", kind="mergesort").reset_index(drop=True)
base.insert(0, "ID", range(1, len(base) + 1))
```

`stop_uid` (`feed_id:stop_id`) is unique and input-derived, so identical
inputs ⇒ identical order ⇒ identical IDs. Use `kind="mergesort"` (stable)
everywhere a sort feeds output.

**Gap 2 — tuple ordering has tie-break holes.** `near_stations` entries sort
by `(stop_uid, minutes, near_station, line)`; two distinct stations with the
same rounded minutes, same name (common — empty names are filled with `""`),
and same line render in unpinned order. **Fix:** append the unique key as the
final sort column: `(stop_uid, minutes, near_station, line, near_num_id)`.
The `same_line_stations` sort already ends in `near_num_id` — mirror that.

**Gap 3 — every DB read that feeds an output needs `ORDER BY` on a unique
key.** Audit all `SELECT`s in Phase 2/3 (proximity pairs, route_stops,
interchange members). Unordered reads are the classic "works until the table
gets vacuumed" nondeterminism.

**Gap 4 — cross-run ID stability.** Even after Gap 1, IDs are only stable for
the *identical* stop set: adding one stop shifts every ID after it. Decide the
contract explicitly and document it in WORKFLOW.md:
- *Option A (current, cheapest):* `ID` is per-file only; consumers must treat
  each CSV as self-contained. Say so in the header docs.
- *Option B (recommended if the CSV feeds a persistent system):* an
  append-only `id_registry (stop_uid, assigned_id)` table. On each run, reuse
  registered IDs, assign fresh IDs (max+1…) only to unseen `stop_uid`s, never
  recycle. Deleted stops leave holes — that's correct behavior for a key.

**Gap 5 — prove it, don't assume it.** Add two cheap guards:
- A **golden test**: a small fixture feed committed to `tests/`, and a test
  asserting the produced `near_stations.csv` is byte-identical to a committed
  golden copy. Any change to ordering, rounding, or formatting fails loudly
  and forces a conscious golden update.
- A **run manifest** written next to every output:
  `run_manifest.json = {feed sha256s, osm pbf sha256, git commit, parameter
  values (radius, cutoff, eps, walk speed, fallback speed), package versions}`.
  Two manifests equal ⇒ outputs must be equal; if not, it's a bug.

Also verify float behavior once: `walk_minutes` uses numpy `.round()`
(round-half-to-even) — deterministic, fine; keep all rounding *at render time*
and compare/sort on rounded values plus the unique tiebreaker, never on raw
floats alone.

### 3.2 Precision when the GTFS is incomplete

The pipeline already has a fallback ladder; the problem is the ladder is
**invisible in the output** — a measured 4 and a guessed 4 look identical.
Precision you can't audit isn't precision.

**Make estimate provenance explicit.** Don't change the agreed column format;
add an optional sidecar `near_stations_quality.csv` keyed by `ID` with:

| column | meaning |
|---|---|
| `walk_source` | `network` (OSM Dijkstra) / `straight_line` (fallback ×1.0) / `unsnapped` (stop >200 m from any walkable way) |
| `travel_source` | `observed` (median of ≥N real stop_times gaps) / `estimated` (25 km/h fallback) |
| `travel_n_obs` | number of trips behind the median (1 observation ≠ 50) |
| `coord_confidence` | from Phase 3 conflation: matched ≤75 m / needs_review / no OSM match |
| `station_dedup` | canonical station id once §1.3 lands |

**Then raise each rung of the ladder:**

1. **Walking times:** default `--network-walk` on (mean detour was 2.24×, so
   straight-line is systematically optimistic). For unsnapped stops, report
   straight-line but flag it; never emit a network value from a snap >75 m.
2. **`travel_min`:** require a minimum observation count (e.g. ≥3 trips) to
   call it observed; below that, fall back and flag. Trim outliers (drop
   segments implying >90 km/h surface speed) *before* the median.
3. **Missing/expired calendars:** if the service window doesn't cover the
   analysis date, run in "all-services" mode and stamp
   `calendar_status=expired` in the manifest — the current silent behavior is
   the single biggest honesty gap given the 2020–2022 improved feed.
4. **Missing `stop_times` but present `frequencies.txt`:** expand (§1.4) so
   those routes get observed values instead of the 25 km/h guess.
5. **Cross-validate on a sample, every run.** Three independent oracles exist
   in the codebase already: schedule medians, OSM network distances, and the
   budgeted Google Distance Matrix client. Add a `--qa-sample N` mode that
   compares K random pairs across oracles and writes a disagreement summary
   (median abs error, worst cases) into the run report. Precision becomes a
   *measured number per run*, not a hope — and drift (a bad feed update, an
   OSM import problem) shows up immediately.
6. **Dedupe before relating** (§1.3): the most misleading rows today are
   stations listed as "near" their own alias from the other feed. No amount
   of numeric precision fixes a wrong entity model.

---

## 4. Maintenance

1. **CI (highest priority).** Mirror `transitland-lib/.github/workflows/test.yml`:
   GitHub Actions running `pytest` (140 tests exist and pass — they're worth
   protecting) with a `postgis/postgis` service container for the DB-touching
   tests, plus the golden-CSV test from §3.1. Add `ruff check` + `ruff format
   --check` for style.
2. **Commit discipline.** ~15 modified/untracked files are sitting
   uncommitted (this file included). Also `.gitignore` covers `out/` but not
   `out1/`/`out2/` from the isolated runs, and the feed zips + `data/mexico.osm.pbf`
   live in the working tree — replace big binaries with the `feeds.json`
   registry (URL + sha256) from §1.1 and ignore `data/`.
3. **Versioned schema migrations.** Move DDL from `CREATE TABLE IF NOT
   EXISTS` to Alembic. The moment a column changes (and `route_connection_stops`
   is already planned), in-place databases need a migration path, not a
   drop-and-recreate.
4. **Dependency pinning.** Ship a lockfile (`uv lock` or `pip-compile`) beside
   `pyproject.toml`. A silent pandas/shapely major bump is exactly the kind of
   thing that changes sort stability or geometry rounding and breaks the
   determinism contract without any code change.
5. **Generated docs where drift happened before.** WORKFLOW.md went stale once
   (the `num_id` description). The column dictionaries in `diccionario_datos.md`
   and WORKFLOW.md duplicate `COLUMNS` lists that exist in code — generate
   those tables from the code constants (a tiny `python -m gtfs_ingest.docs`
   emitter) or add a test asserting doc tables match `COLUMNS`.
6. **Tool versioning.** Adopt semver + a CHANGELOG; write the version into the
   run manifest and the GeoPackage metadata. "Which version produced this
   file?" must always be answerable — it's the maintenance twin of §3's
   determinism.
7. **Reproducible environment.** A `docker-compose.yml` with the PostGIS
   service (and documented `POSTGRES_*` vars) so a new machine goes from clone
   to first pipeline run in minutes; today the DB setup is tribal knowledge.
8. **License.** The repo has no LICENSE file. Pick one before sharing the tool
   alongside the paper — and note the data licenses (ODbL for OSM, feed terms
   for Busmaps/mdb) in `feeds.json`, as DMFR does.

---

## Suggested order of attack

| Step | Item | Effort | Payoff |
|---|---|---|---|
| 1 | Pin CSV row order + tie-breaks (§3.1 gaps 1–3) | ~1 h | Deterministic contract file |


| 4 | ✅ **DONE** — Cross-feed dedupe (§1.3): `analysis/dedupe.py`, `stop_equivalences.csv`/table, `same_station_dedup` edges, `physical_stop_count` | ~2–3 days | Kills phantom interchanges and alias "near" rows |
| 5 | ✅ **DONE** (flags) — Quality sidecar (§3.2): `near_stations_quality.csv` with walk/travel sources, `travel_n_obs`, `coord_confidence`, `station_dedup`. Behavior changes still open: default `--network-walk`, snap>75 m suppression, min-obs gating, outlier trimming | ~1 day | Honest precision, auditable estimates |
| 6 | ✅ **DONE** — frequencies.txt expansion (`headways.expand_frequencies`; both feeds are frequency-dominated: 181/184 + 970/989 trips) + calendar expiry (`feed_expired` WARNING, `calendar_status` in the analysis report, stale-busiest-date notes) | ~1 day | Correct headways, no silent stale-service runs |

| 8 | ✅ **DONE** — Walk-graph cache (`analysis/graph_cache.py`, `.gtfs_cache/`, auto-invalidating key), psycopg3 COPY loading (`gtfs_ingest/bulk.py`, all scalar tables + stops via temp-table staging), scipy `csgraph.dijkstra` (chunked sources) | ~1–2 days | Fast iteration for everything else |

**Shipped follow-ons (post-roadmap batch):** injectable analysis clock
(`--analysis-date`/`GTFS_ANALYSIS_DATE` — expiry/staleness judgments are
reproducible and recorded in both reports); cross-feed service-date
coherence warning (`service_dates_coherent`) + `--common-date`;
`--service-date-range START..END` (per-day headway rows; the headway-table
PKs now include `service_date`, migrated idempotently); the **routable
station network** (`station_network.graphml` + nodes/edges CSVs + gpkg
layer: in_vehicle/walk/same_station edges weighted by `time_min`);
headway tables also exported as CSVs on every run.

**Deferred (noted during the range work):** a range run parses the feed zip
once per served day (≤31, capped) — a later optimization is one
`ptg.load_feed` + per-day trip masks. Also open: directed/asymmetric walk
times, and using the station network for OTP2/r5py handoff.
