"""Phase 3: multimodal interchange detection + OSM conflation.

Builds on Phases 1-2 (stops normalization, PostGIS access, route/stop maps
are reused, not re-implemented). OSM data already loaded into the same
PostGIS database is the ONLY reference source - no external APIs.

- ``clustering``       - DBSCAN over metric-projected stops, merged with
  ``parent_station`` groupings, -> interchange clusters with mode sets.
- ``transfers_check``  - inferred interchanges vs declared transfers.txt.
- ``osm_match``        - nearest compatible OSM transit node per stop (KNN)
  with a documented confidence score; also validates the OSM schema.
- ``side_of_street``   - nearest highway centerline + left/right sign math.
- ``storage``          - DDL + idempotent loads for the Phase 3 tables.
- ``pipeline_phase3`` - orchestrates ``detect-interchanges`` and
  ``conflate-osm``.
"""
