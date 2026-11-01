# Data facts and known problems

Measured on `outputs/out_mibici_v12` (full run, 300 m proximity, network walk, typical weekday: 2021-05-04 improved, 2024-06-27 mdb).

## Inputs

| Source | feed_id | Stops | Routes | Calendar |
| --- | --- | ---: | ---: | --- |
| `improved-gtfs-mimacro-mitren-sitren.zip` | `improved_gtfs_mimacro_mitren_sitren` | 1,634 | 36 analyzed | ends 2022-12-31 |
| `mdb-2366-202502241922.zip` | `mdb_2366_202502241922` | 12,231 | 238 analyzed | ends 2024-12-31 |
| `mibici.csv` | `mibici` | 484 | — | — |

Both feeds describe many of the same stations; ~3,200 cross-feed pairs sit at exactly 0 m.
The mdb feed also lists the light-rail line twice (`SITEUR_*` and `ST_*` ids at 0 m), and **every Sitren trip twice** (`SITEUR_T_xx` and `ST_xx`, same times; 8 on a weekday). Route dedupe keeps one (#18).
**improved is almost a subset of mdb:** 38 of its 40 routes are mdb routes with a `2215-` id prefix and identical departures. Only `MP-A05-2` and `ST_L1B` are its own. Short names can't match lines: `L1`–`L3` name both Mi Tren (`MT_L*`) and Sitren (`ST_L*`).
mdb `MT_L1`/`MT_L2` list each station twice (`MT-L1_xx`, `TL_0001_00xx`, 0 m) under **one `stop_sequence`**; one stop is kept per sequence (#21), the `TL_*` aliases are reached via `same_station`.
**`direction_id` mixes both senses** on `MT_L1`–`L3` and several `MP-*` routes (19 trips relabelled, #21). Without the fix, headways count both ways: `MT_L3` publishes 420 s, mixed it read 230–330 s.
Every T16B branch is scheduled at 70 min whatever its length; `T16B-C07-C`/`T16B-C08-M` (3.1–3.5 km) are flagged `too_slow`. 18 arcs are `too_fast` (> 80 km/h straight line, e.g. `MP-A06` 1.9 km in 0.7 min). `T09-O` (110 min) is a real 26.6 km loop.

## Current headline numbers

- 14,349 stop records; 1,581 mdb stops are used by no trip and are dropped (default) → 12,768 → **10,203 physical stations** (1,604 groups; 772 pairs rejected as same-feed conflicts; widest station 7.5 m).
- Route graph (route dedupe on): 249 routes, 7,902 edges, 1 component (`same_stop` 3,810, `same_station_dedup` 830, `proximity` 3,262). `routes.csv` still lists 287 (38 with `duplicate_of`).
- Timetable: 3,290,920 connections, 66,568 runs, 10,499 served stops; footpaths 101,018 walk + 10,499 self + 226 same_station.
- Station network: 12,284 transit stops, **1 weak component**, 484 unlinked docks; 24,036 in_vehicle arcs.
- `travel_source = none`: 514 = the 484 MiBici docks + the 30 `TL_0001_*` aliases.
- OSM conflation: 10,485 / 14,349 stops matched (73.1 %).
- Interchanges: 2,565 clusters, 73 multimodal.

## Known problems (open)

1. **Service dates differ, by necessity.** The calendars don't overlap (improved 2020–2022, mdb 2024), so each feed uses its own typical weekday (2021-05-04, 2024-06-27); cross-feed outputs combine schedules from different years. The report says so in `service_dates_note`.

## History worth knowing

- The within-feed radius was 50 m until Oct 2026; it merged 2,697 mostly opposite-direction pairs (8,877 "stations"). Now 5 m.
- Until Nov 2026 the default date was "busiest", which is a **holiday** in both feeds: holidays add Sunday service `D` on top of Monday–Saturday `LS` (mdb 807 trips vs 511 on a weekday). T16B then ran one way (55 one-way stops). The typical-weekday rule (#17) fixed it: 9 one-way stops, all route terminals.
- The two T16B route-graph bridges (#9) just cut off two 3-stop feeders; not fragility. Without `proximity` edges the graph splits mostly where BRT/light-rail lines are reached on foot.
- Until Oct 2026 cross-feed dedupe merged opposite platforms: a far pair (same name or route, 15–30 m) chained two 0 m twin pairs into one station (283 groups, 10,533 stations). The same-feed conflict rule (#7) now rejects those 1,077 pairs.
- 1,581 mdb stops are in `stops.txt` but in no `stop_times` row (738 duplicate a served stop of the other feed). Until Nov 2026 they were kept; 111 of them formed the 49 small pieces of the station network. Now dropped by default (#8, `--no-drop-unserved-stops` keeps them); still in the PostGIS `stops` table.
- Redundant station-network arcs (#10): 42,557 of 72,286 walk pairs and 247 of 2,812 same_station pairs change no shortest time. Always counted in the report; `--prune-station-network` (off by default) drops them.
- MiBici docks used to have walk arcs (5,756 pairs) and acted as stepping stones between bus stops; now unlinked.
- `scripts/build_branch_gpkg.py` used to read `out/transit_gdl_2.0.gpkg`, a renamed run GeoPackage; it now reads `outputs/out_mibici_v3/transit_analysis.gpkg`.
