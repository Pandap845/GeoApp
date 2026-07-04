"""Phase 2: service-aware headway analysis and route connectivity.

Builds on the Phase 1 ``gtfs_ingest`` package (feed discovery, stop
normalization, PostGIS access are reused, not re-implemented):

- ``service_filter``  - trips active on a service date (calendar.txt +
  calendar_dates.txt exceptions, via partridge).
- ``headways``        - departure-to-departure headway statistics per
  (feed, route, direction, stop) and time window, plus route-level rollups.
- ``connectivity``    - route-to-route connection edges via shared stops,
  shared parent_station, or spatial proximity.
- ``graph_export``    - networkx graph construction, metrics, GraphML.
- ``storage``         - DDL + idempotent loads for the analysis tables.
- ``pipeline_day2``   - orchestration of the full Phase 2 flow.

OSM data and interchange clustering are explicitly out of scope (Phase 3).
"""
