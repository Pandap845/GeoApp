"""Phase 4: Google Maps QA cross-check (ephemeral, off by default).

Uses the Google Places API (New) ONLY as an external quality-assurance
cross-check for a small, targeted subset of Phase 3 results. It is not a
data source, not an enrichment pass, and never mutates GTFS/OSM data.

Hard constraints enforced in code (see README for the legal context):

- The only Google-derived value ever persisted is the opaque ``place_id``,
  next to verdicts/flags and a timestamp WE compute (``qa.storage`` raises
  ``StorageViolation`` on anything else).
- All other Places response fields live in memory for one comparison and
  are discarded; debug logging redacts responses to field NAMES only.
- Off by default: runs only with ``--enable-google-qa`` AND a
  ``GOOGLE_MAPS_API_KEY`` in the environment; otherwise a clean no-op.
- Targeted: only multimodal interchanges and needs-review stops are
  eligible; a hard ``--max-calls`` budget caps API usage.

The subpackage also hosts the **station locator** (``station_locator``,
CLI ``locate-stations``): a user-facing tool that geocodes a station-name
list and exports a route with travel times to Excel. It shares the client
and its rails (gating, budget, rate limit) but, unlike the QA cross-check,
its export file deliberately contains Google-resolved names/coordinates -
file export only, never the database.
"""
