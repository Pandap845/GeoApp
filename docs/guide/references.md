# References

Data sources used by this project (GTFS and OSM), in IEEE citation style.
Local acquisition date of the data files: **July 1, 2026** (file timestamps);
online resources last accessed: **July 5, 2026**.

## Data sets

[1] Busmaps.com, *GTFS feed: Mi Macro, Mi Tren, SiTren, Mi Transporte
Eléctrico (improved feed), version 20260619-2117*, Guadalajara, Jalisco,
Mexico. Feed validity: Jan. 1, 2020 – Dec. 31, 2022. File:
`improved-gtfs-mimacro-mitren-sitren.zip` (40 routes, 1,634 stops).
[Online]. Available: https://busmaps.com. [Accessed: Jul. 1, 2026].

[2] Secretaría de Transporte de Jalisco / SITEUR, *GTFS feed: Mi
Transporte, Mi Macro, Mi Tren, SiTren, Mi Transporte Eléctrico*, Mobility
Database catalog entry mdb-2366, snapshot of Feb. 24, 2025, 19:22 UTC.
File: `mdb-2366-202502241922.zip` (276 routes, 12,231 stops). [Online].
Available: https://mobilitydatabase.org/feeds/mdb-2366.
[Accessed: Jul. 1, 2026].

[3] OpenStreetMap contributors, *Mexico OSM extract* (`mexico.osm.pbf`,
~628 MB), OpenStreetMap Foundation. Data licensed under the Open Data
Commons Open Database License (ODbL) v1.0. [Online]. Available:
https://download.geofabrik.de/north-america/mexico.html.
[Accessed: Jul. 1, 2026].

## Operating agencies (primary sources of the transit data)

[4] Sistema de Tren Eléctrico Urbano (SITEUR), Gobierno del Estado de
Jalisco — operator of Mi Macro, Mi Tren and SiTren. [Online]. Available:
http://siteur.gob.mx/. [Accessed: Jul. 5, 2026].

[5] Secretaría de Transporte, Gobierno del Estado de Jalisco (Setrans) —
authority for Mi Transporte and Mi Transporte Eléctrico. [Online].
Available: https://setrans.jalisco.gob.mx/. [Accessed: Jul. 5, 2026].

## Specifications and platforms

[6] MobilityData, "GTFS Schedule Reference," General Transit Feed
Specification. [Online]. Available: https://gtfs.org/schedule/reference/.
[Accessed: Jul. 5, 2026].

[7] MobilityData, "The Mobility Database," catalog of public GTFS feeds.
[Online]. Available: https://mobilitydatabase.org/.
[Accessed: Jul. 5, 2026].

[8] OpenStreetMap Foundation, "Open Database License (ODbL) v1.0."
[Online]. Available: https://opendatacommons.org/licenses/odbl/1-0/.
[Accessed: Jul. 5, 2026].

[9] Google LLC, "Google Maps Platform — Places API (New), Geocoding API,
Distance Matrix API" (optional verification layer; off by default, only
opaque place identifiers persisted). [Online]. Available:
https://developers.google.com/maps. [Accessed: Jul. 5, 2026].

## Notes on provenance

- Reference [1] declares its validity window in `feed_info.txt`
  (2020-01-01 to 2022-12-31); analyses over this feed auto-select the
  busiest service date inside that window (2020-01-01).
- Reference [2] ships no `feed_info.txt`; its identity and snapshot date
  come from the Mobility Database catalog id embedded in the filename
  (`mdb-2366-202502241922` = catalog entry 2366, snapshot 2025-02-24
  19:22).
- The OSM extract [3] was imported into PostGIS with osm2pgsql (classic
  schema: `planet_osm_point`, `planet_osm_line`, SRID 3857) and is used
  exclusively as a local reference layer — no OSM API is queried at run
  time. Attribution "© OpenStreetMap contributors" applies to every
  derived artifact that includes OSM-derived fields (`stop_osm_matches`,
  `stop_side_of_street`, `osm_coverage_gaps.gpkg`, network walking times).
