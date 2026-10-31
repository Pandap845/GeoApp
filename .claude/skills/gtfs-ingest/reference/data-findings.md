# Data facts and known problems

Measured on `outputs/out_mibici_v3` (full run, 300 m proximity, network walk).

## Inputs

| Source | feed_id | Stops | Routes | Calendar |
| --- | --- | ---: | ---: | --- |
| `improved-gtfs-mimacro-mitren-sitren.zip` | `improved_gtfs_mimacro_mitren_sitren` | 1,634 | 36 analyzed | ends 2022-12-31 |
| `mdb-2366-202502241922.zip` | `mdb_2366_202502241922` | 12,231 | 238 analyzed | ends 2024-12-31 |
| `mibici.csv` | `mibici` | 484 | — | — |

Both feeds describe many of the same stations; ~3,200 cross-feed pairs sit at exactly 0 m.
The mdb feed also lists the light-rail line twice (`SITEUR_*` and `ST_*` ids at 0 m).

## Current headline numbers

- 14,349 stop records → **10,533 physical stations** (cross-feed 4,376 pairs + within-feed 247 pairs, 1,407 groups).
- Route graph: 274 routes, 9,112 edges, 1 component (`same_stop` 3,669, `same_station_dedup` 1,587, `proximity` 3,856).
- Station network: 13,865 transit stops, 50 weak components (largest 13,754), 484 unlinked docks.
- OSM conflation: 10,485 / 14,349 stops matched (73.1 %).
- Interchanges: 2,708 clusters, 71 multimodal.

## Known problems (open)

1. **Service dates differ.** "Busiest" picks 2020-01-01 for `improved` and 2024-01-01 for `mdb`; the calendars do not overlap, so cross-feed outputs mix schedules four years apart.
2. **2024-01-01 is a holiday.** `calendar_dates` adds Sunday service `D` and removes weekday `LV`. Route T16B's outbound direction runs only on `LV`/`SD`, so that day T16B runs one way and **86 stops become one-way reachable**. On 2024-01-02 this drops to 9 (real route terminals: C114V1, C106, C111V1). Prefer an explicit normal weekday for mdb.
3. **111 orphan mdb stops.** Listed in `stops.txt` but used by no trip in the whole feed, each ≥ 307 m (median 1.2 km) from the rest. They are the small components of the station network.
4. **Cross-feed dedupe still merges opposite platforms.** Same name + ≤ 30 m across feeds (e.g. `improved:ST_02_018` ↔ `mdb:ST_02_027`, 28.6 m). 283 groups hold two stops of one feed > 15 m apart; 1,081 cross-feed pairs lie 5–30 m apart. Each becomes a 0-minute U-turn in routing.
5. **Route graph is fragile at T16B.** Two bridges (`T16B-C07-C`, `T16B-C08-M`); without `proximity` edges it splits into 6 components.
6. **Redundant walk arcs.** ~80 % of walk arcs can be removed without changing any shortest time (whole-minute weights); 807 redundant same_station pairs.

## History worth knowing

- The within-feed radius was 50 m until Oct 2026; it merged 2,697 mostly opposite-direction pairs (8,877 "stations"). Now 5 m.
- MiBici docks used to have walk arcs (5,756 pairs) and acted as stepping stones between bus stops; now unlinked.
- `scripts/build_branch_gpkg.py` used to read `out/transit_gdl_2.0.gpkg`, a renamed run GeoPackage; it now reads `outputs/out_mibici_v3/transit_analysis.gpkg`.
