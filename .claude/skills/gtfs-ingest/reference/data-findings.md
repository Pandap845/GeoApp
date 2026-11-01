# Data facts and known problems

Measured on `outputs/out_mibici_v6` (full run, 300 m proximity, network walk).

## Inputs

| Source | feed_id | Stops | Routes | Calendar |
| --- | --- | ---: | ---: | --- |
| `improved-gtfs-mimacro-mitren-sitren.zip` | `improved_gtfs_mimacro_mitren_sitren` | 1,634 | 36 analyzed | ends 2022-12-31 |
| `mdb-2366-202502241922.zip` | `mdb_2366_202502241922` | 12,231 | 238 analyzed | ends 2024-12-31 |
| `mibici.csv` | `mibici` | 484 | — | — |

Both feeds describe many of the same stations; ~3,200 cross-feed pairs sit at exactly 0 m.
The mdb feed also lists the light-rail line twice (`SITEUR_*` and `ST_*` ids at 0 m).

## Current headline numbers

- 14,349 stop records; 1,581 mdb stops are used by no trip and are dropped (default) → 12,768 → **10,203 physical stations** (1,604 groups; 772 pairs rejected as same-feed conflicts; widest station 7.5 m).
- Route graph: 274 routes, 9,112 edges, 1 component (`same_stop` 3,669, `same_station_dedup` 1,428, `proximity` 4,015).
- Station network: 12,284 transit stops, **1 weak component**, 484 unlinked docks.
- OSM conflation: 10,485 / 14,349 stops matched (73.1 %).
- Interchanges: 2,565 clusters, 73 multimodal.

## Known problems (open)

1. **Service dates differ.** "Busiest" picks 2020-01-01 for `improved` and 2024-01-01 for `mdb`; the calendars do not overlap, so cross-feed outputs mix schedules four years apart.
2. **2024-01-01 is a holiday.** `calendar_dates` adds Sunday service `D` and removes weekday `LV`. Route T16B's outbound direction runs only on `LV`/`SD`, so that day T16B runs one way and **86 stops become one-way reachable**. On 2024-01-02 this drops to 9 (real route terminals: C114V1, C106, C111V1). Prefer an explicit normal weekday for mdb.
3. **Route graph is fragile at T16B.** Two bridges (`T16B-C07-C`, `T16B-C08-M`); without `proximity` edges it splits into 6 components.

## History worth knowing

- The within-feed radius was 50 m until Oct 2026; it merged 2,697 mostly opposite-direction pairs (8,877 "stations"). Now 5 m.
- Until Oct 2026 cross-feed dedupe merged opposite platforms: a far pair (same name or route, 15–30 m) chained two 0 m twin pairs into one station (283 groups, 10,533 stations). The same-feed conflict rule (#7) now rejects those 1,077 pairs.
- 1,581 mdb stops are in `stops.txt` but in no `stop_times` row (738 duplicate a served stop of the other feed). Until Nov 2026 they were kept; 111 of them formed the 49 small pieces of the station network. Now dropped by default (#8, `--no-drop-unserved-stops` keeps them); still in the PostGIS `stops` table.
- Redundant station-network arcs (#10): 42,557 of 72,286 walk pairs and 247 of 2,812 same_station pairs change no shortest time. Always counted in the report; `--prune-station-network` (off by default) drops them.
- MiBici docks used to have walk arcs (5,756 pairs) and acted as stepping stones between bus stops; now unlinked.
- `scripts/build_branch_gpkg.py` used to read `out/transit_gdl_2.0.gpkg`, a renamed run GeoPackage; it now reads `outputs/out_mibici_v3/transit_analysis.gpkg`.
