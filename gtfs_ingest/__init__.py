"""GTFS ingestion & validation pipeline (Phase 1).

Ingests one or more GTFS feeds (zip files or directories), validates their
integrity without aborting on broken feeds, normalizes stops into a common
schema with feed provenance, and loads them into a PostGIS ``stops`` table.

Downstream phases (headway analysis, interchange detection) consume the
``stops`` table and the JSON validation report produced here.
"""

__version__ = "0.1.0"
