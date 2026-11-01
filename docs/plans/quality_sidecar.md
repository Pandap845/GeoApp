# Quality sidecar + observed/estimated flags (future_project.md §3.2, step 5)

## Context

`near_stations.csv` renders measured and guessed values identically: a
`same_line_stations` minute from real stop_times medians looks exactly like
the 25 km/h haversine fallback, and a network-walked minute looks like a
straight-line one. Precision you can't audit isn't precision. This adds a
sidecar `near_stations_quality.csv` — one row per stop, same `ID` key —
making the fallback ladder visible without touching the contract file.

**User decisions (fixed):**
1. Per-stop summary rows; `mixed` when a stop's tuples disagree;
   `travel_n_obs` = MINIMUM observation count (worst case).
2. `coord_confidence` always present: numeric score / `needs_review` /
   `no_match` / `unavailable` (dry-run or Phase 3 never ran).
3. Always written, right beside `near_stations.csv` — no new flag.

**Hard constraint:** `near_stations.csv` (columns, rows, IDs, tuple strings)
and the GeoPackage layer schemas MUST NOT change. Verified safe: the layers
slice explicit column lists ([geopackage.py:134](gtfs_ingest/analysis/geopackage.py#L134), [:163-164](gtfs_ingest/analysis/geopackage.py#L163-L164)) and
`build_near_stations` slices `COLUMNS`, so provenance columns on
intermediate frames can't leak. Behavior changes (default `--network-walk`,
snap>75 m suppression, min-obs gating) are OUT of scope — flags only.

## Sidecar columns (`QUALITY_COLUMNS`)

| column | vocabulary |
|---|---|
| `ID`, `stop_uid` | same rows/order as near_stations.csv (join keys) |
| `walk_source` | `network` / `straight_line` / `mixed` / `unsnapped` (snap > 200 m) / `none` (no rendered near tuples) |
| `travel_source` | `observed` / `estimated` / `mixed` / `none` (no same-line segments) |
| `travel_n_obs` | min trips behind the stop's `travel_min` medians; 0 when estimated/none |
| `coord_confidence` | `"0.812"`-style string / `needs_review` / `no_match` / `unavailable` |
| `station_dedup` | `canonical_uid` from cross-feed dedupe; `""` when unaliased |

## Steps

### 1. `walk_network.py` — expose walk_source + snap distances
- New constant `MAX_SNAP_M = 200.0` (classification-only; routing unchanged).
- `pair_network_distances` ([:164](gtfs_ingest/analysis/walk_network.py#L164)): return frame gains `snap_m_a`/`snap_m_b`
  (map `snap_of` from [:193](gtfs_ingest/analysis/walk_network.py#L193) onto both endpoints; NaN in the empty-graph
  guard [:187-188](gtfs_ingest/analysis/walk_network.py#L187-L188)). Signature unchanged — existing tests keep passing.
- `network_distances`: extract lines [:241-255](gtfs_ingest/analysis/walk_network.py#L241-L255) into a pure helper
  `_finalize_pairs(pairs)` that adds `line_m`, clips, sets
  `pairs["walk_source"] = np.where(routed, "network", "straight_line")`,
  logs, drops `network_m`. Update docstrings.
- Missing-column contract: default run / dry run / empty-pairs guard
  ([:230-231](gtfs_ingest/analysis/walk_network.py#L230-L231)) / pipeline exception fallback leave `stop_pairs` without
  these columns — consumers default to `straight_line`, never `unsnapped`.

### 2. `route_geometry.py` — n_obs
- Aggregation [:168-171](gtfs_ingest/analysis/route_geometry.py#L168-L171): `.agg(seg_s=("seg_s","median"), n_obs=("seg_s","count"))`
  (non-NaN count = trips with a timed gap; 0 exactly when `travel_min` NULL).
- Append `"n_obs"` to `LINK_COLUMNS` ([:34-35](gtfs_ingest/analysis/route_geometry.py#L34-L35)); cast int. Document.

### 3. `near_stations.py` — provenance through the entry builders + sidecar
- Constants `QUALITY_NAME = "near_stations_quality.csv"`, `QUALITY_COLUMNS`.
- `_near_entries` ([:233-265](gtfs_ingest/analysis/near_stations.py#L233-L265)): append `walk_source` to its output columns;
  default `both["walk_source"] = "straight_line"` when absent. Rendering
  untouched. This keeps the sidecar aligned with the *rendered* tuples
  (pairs to unserved stops are dropped by the `lines_at` merge for both).
- `_same_line_entries` ([:280-319](gtfs_ingest/analysis/near_stations.py#L280-L319)): carry `travel_min` + `n_obs` through
  (`keep_t` + output columns; default `n_obs = 0` beside the existing
  travel_min default [:295-296](gtfs_ingest/analysis/near_stations.py#L295-L296)). Fallback branch [:318](gtfs_ingest/analysis/near_stations.py#L318) untouched.
- `_stop_snap_m(stop_pairs) -> pd.Series`: stop_uid → snap meters from the
  per-pair snap columns; empty Series when columns absent.
- **`build_quality(near, route_stops, routes, stop_pairs, stop_links=None,
  osm_matches=None, canonical=None)`**: base = `near[["ID","stop_uid"]]`
  (row/ID parity by construction). Reuses `_near_entries` /
  `_same_line_entries` verbatim so classification matches rendering exactly:
  - walk: groupby stop_uid on entries' `walk_source` — unique value or
    `mixed`; override `unsnapped` when `_stop_snap_m > MAX_SNAP_M`;
    `none` when no entries.
  - travel: per-row observed iff `pd.to_numeric(travel_min).notna()` (the
    identical mask as the fallback); unique/`mixed`/`none`;
    `travel_n_obs = groupby.min()` (estimated rows contribute 0), int.
  - coord_confidence: `osm_matches is None` → all `unavailable`; else
    left-join by stop_uid: absent row → `unavailable`; `needs_review=False`
    → `f"{match_confidence:.3f}"`; needs_review + osm_id notna →
    `needs_review`; osm_id isna → `no_match`.
  - station_dedup: left-join `canonical`, `fillna("")`.
  - Sort by `ID` (stable), return `QUALITY_COLUMNS`; log source counts.
- `write_quality(quality, out_dir)` mirroring `write_near_stations`
  ([:347-364](gtfs_ingest/analysis/near_stations.py#L347-L364)); always called (header-only CSV when empty).

### 4. `analysis/storage.py` — OSM matches reader
`fetch_osm_matches(engine, feed_ids) -> Optional[pd.DataFrame]` with
`OSM_MATCH_COLUMNS = [stop_uid, osm_id, distance_m, match_confidence,
needs_review]`. Mirrors `connectivity.proximity_pairs_postgis` (text SQL +
mappings). `try/except SQLAlchemyError` → `log.info(...unavailable...)`,
return `None` — the table only exists after `conflate-osm`; never abort.

### 5. `pipeline_phase2.py` — wiring + report
- After `write_near_stations` ([:339](gtfs_ingest/analysis/pipeline_phase2.py#L339)):
  ```python
  osm_matches = None
  if engine is not None and analyzed_feed_ids:
      osm_matches = storage.fetch_osm_matches(engine, analyzed_feed_ids)
  quality = near_stations.build_quality(
      near,
      route_stops,
      routes,
      stop_pairs,
      stop_links=all_links,
      osm_matches=osm_matches,
      canonical=canonical,
  )
  near_stations.write_quality(quality, config.report_dir)
  ```
  (`canonical` already in scope from the dedupe stage; empty under
  `--no-dedupe` → `station_dedup = ""`.)
- `AnalysisReport`: one field `quality_summary: dict[str, dict[str, int]]`
  = value_counts of walk_source and travel_source.
- `_render_markdown`: small "Estimate provenance" block iterating the fixed
  vocabularies (deterministic order) + a footer line naming the sidecar.

### 6. Tests
`tests/test_near_stations.py` (existing `make_stops`/`rs`/`routes_meta`
helpers): row/ID parity + column set; walk defaults (`straight_line`,
`none` incl. unserved-neighbour case); `mixed` + `unsnapped` override;
travel observed/estimated/mixed with min-n_obs; coord_confidence all four
states + `None`; station_dedup from canonical; write roundtrip; and a
regression asserting `build_near_stations` output is byte-identical with
vs. without the provenance columns present.
`tests/test_walk_network.py`: extend snap-offset and empty-graph tests for
`snap_m_a/b`; new `_finalize_pairs` labeling test.
`tests/test_route_geometry.py`: `n_obs` values (timed vs untimed trips).
`tests/test_geopackage.py`: layer column sets unchanged when the frames
carry the new columns.
E2E: dry-run over a fixture feed dir asserting the sidecar exists, row/ID
parity, all `coord_confidence == "unavailable"`, `quality_summary` in
`analysis_report.json`.

### 7. Docs
- WORKFLOW.md §4.2: `near_stations_quality.csv` row after `near_stations.csv`.
- README.md Phase 2 outputs: new bullet with the column table + the
  dry-run/`unavailable` note.
- diccionario_datos.md: new section `## 2c. near_stations_quality.csv`
  (Spanish) with the vocabularies and join guidance.
- future_project.md roadmap row 5: mark shipped (flags); note the behavior
  changes remain open.

## Verification

```
python -m pytest tests -q     # all green incl. new tests

python -m gtfs_ingest.cli analyze-frequency --feeds improved-gtfs-mimacro-mitren-sitren.zip mdb-2366-202502241922.zip --report <scratch>/out_q --dry-run
```
Expected (dry run): sidecar exists with the same row count/ID column as
near_stations.csv; `walk_source` only `straight_line`/`none`;
`coord_confidence` all `unavailable`; `travel_source` mixes
observed/estimated with sensible `travel_n_obs`; `station_dedup` non-empty
exactly for the stop_uids in `stop_equivalences.csv`; provenance tables in
`analysis_report.md`. Diff `near_stations.csv` against a pre-change run —
byte-identical. Optional DB run with `--network-walk` (+ prior
`conflate-osm`): `walk_source` gains `network`/`mixed`/rare `unsnapped`;
`coord_confidence` shows numerics/needs_review/no_match; run twice → the
sidecar is byte-identical (determinism).

## Risks (mitigated)
- GPKG/CSV schema drift: impossible via explicit column slicing; locked by
  regression tests.
- Missing-column paths (no network walk / dry run / exception fallback):
  consumers default to `straight_line` / empty snap / `n_obs=0`.
- Entries computed twice (render + sidecar): negligible cost, chosen over
  changing `build_near_stations`'s contract.
- `coord_confidence` mixed types: rendered as strings for CSV determinism.
