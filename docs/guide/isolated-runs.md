# Isolated per-feed pipeline runs

Two fully isolated executions of the whole pipeline (no Google API):

- **Run 1** — only `improved-gtfs-mimacro-mitren-sitren.zip` → reports in `out1/`
- **Run 2** — only `mdb-2366-202502241922.zip` → reports in `out2/`

Because the feed-scoped tables (`stops`, `route_stop_headways`,
`route_headways`) use *per-feed* replace, ingesting one feed does not remove
the other's rows — so each run includes a **purge step** that deletes every
other feed from those tables. The run-scoped tables (`route_connections`,
`interchanges`, `stop_osm_matches`, `stop_side_of_street`) are fully
reloaded by their own commands, no purge needed.

Settings used: `--network-walk` (OSM street-network walking times),
`--match-threshold-m 75` (adopted after the 50 m vs 75 m comparison),
`--crs EPSG:32613` (UTM 13N, Guadalajara).

> After Run 2 the database contains **only the mdb feed's data**. To restore
> the combined state, re-run the pipeline passing both zips to `--feeds`.

```powershell
$DB = "postgresql+psycopg://transit:cambia_esto@localhost:5432/transit"
```

## Run 1 — improved feed only → out1/

```powershell
# 1. Ingest (validate + normalize + load stops)
python -m gtfs_ingest.cli ingest --feeds data/feeds/improved-gtfs-mimacro-mitren-sitren.zip `
    --db-url $DB --report ./out1 --fail-on-error

# 2. Purge the other feeds' rows from the feed-scoped tables (isolation).
#    Equivalent SQL, per table (stops, route_stop_headways, route_headways):
#      DELETE FROM <table> WHERE feed_id != 'improved_gtfs_mimacro_mitren_sitren';
python keep_only_feed.py improved_gtfs_mimacro_mitren_sitren

# 3. Headways + connectivity + near/same-line stations + GeoPackage + GraphML
python -m gtfs_ingest.cli analyze-frequency --feeds data/feeds/improved-gtfs-mimacro-mitren-sitren.zip `
    --db-url $DB --network-walk --report ./out1

# 4. Multimodal interchange detection
python -m gtfs_ingest.cli detect-interchanges --feeds data/feeds/improved-gtfs-mimacro-mitren-sitren.zip `
    --db-url $DB --crs EPSG:32613 --report ./out1

# 5. OSM conflation: matching + side of street + reverse coverage
python -m gtfs_ingest.cli conflate-osm --db-url $DB `
    --match-threshold-m 75 --street-threshold-m 50 --crs EPSG:32613 --report ./out1
```

## Run 2 — mdb feed only → out2/

```powershell
python -m gtfs_ingest.cli ingest --feeds data/feeds/mdb-2366-202502241922.zip `
    --db-url $DB --report ./out2 --fail-on-error

python keep_only_feed.py mdb_2366_202502241922

python -m gtfs_ingest.cli analyze-frequency --feeds data/feeds/mdb-2366-202502241922.zip `
    --db-url $DB --network-walk --report ./out2

python -m gtfs_ingest.cli detect-interchanges --feeds data/feeds/mdb-2366-202502241922.zip `
    --db-url $DB --crs EPSG:32613 --report ./out2

python -m gtfs_ingest.cli conflate-osm --db-url $DB `
    --match-threshold-m 75 --street-threshold-m 50 --crs EPSG:32613 --report ./out2
```

## Notes

- Order matters inside each run: `detect-interchanges` and `conflate-osm`
  read the `stops` table, so the purge must happen before them.
- `google-qa` was intentionally skipped (no Google API); running it without
  `--enable-google-qa` is a clean no-op if you want the report stub.
- `keep_only_feed.py` is a 15-line helper (SQLAlchemy `DELETE ... WHERE
  feed_id != :keep` over the three feed-scoped tables); the equivalent SQL
  is inlined in step 2 above.
