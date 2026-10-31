"""Phase 4: opt-in Google Places QA and the station locator.

Rules enforced in code: off unless both the flag and GOOGLE_MAPS_API_KEY are
set, a hard call budget, and only verdicts plus the opaque place_id are ever
stored (qa.storage raises StorageViolation otherwise). The station locator
may write Google-resolved names to its Excel export, never to the database.
"""
