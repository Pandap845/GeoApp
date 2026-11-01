"""Storage guard: FAILS if the QA tables could ever hold Google content.

Two layers of defense are asserted:
1. the runtime allowlist check raises before any SQL executes;
2. the DDL and INSERT statements in ``qa.storage`` are parsed and every
   column must be on the allowlist - adding a column for a Google field
   (name, rating, coordinates, ...) makes this suite fail.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from gtfs_ingest.qa import storage
from gtfs_ingest.qa.storage import (
    ALLOWED_INTERCHANGE_COLUMNS,
    ALLOWED_STOP_COLUMNS,
    DDL_STATEMENTS,
    StorageViolation,
    _assert_persistable,
    upsert_interchange_verdicts,
    upsert_stop_verdicts,
)

#: Google content that must NEVER become a column.
FORBIDDEN_COLUMNS = {
    "name",
    "display_name",
    "google_name",
    "formatted_address",
    "address",
    "rating",
    "phone",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "location",
    "types",
    "transit_station",
    "vehicle_type",
    "lines",
    "agency",
}


def valid_interchange_record() -> dict:
    return {
        "interchange_cluster_id": 1,
        "verdict": "CONFIRMED",
        "mode_agreement": True,
        "google_place_id": "opaque-id",
        "checked_at": datetime.now(UTC),
    }


def test_allowlists_contain_only_permitted_concepts() -> None:
    # place_id (opaque), verdict/flags, timestamp, and OUR keys - nothing else.
    assert {
        "interchange_cluster_id",
        "verdict",
        "mode_agreement",
        "google_place_id",
        "checked_at",
    } == ALLOWED_INTERCHANGE_COLUMNS
    assert {"stop_uid", "verdict", "google_place_id", "checked_at"} == ALLOWED_STOP_COLUMNS
    assert not (ALLOWED_INTERCHANGE_COLUMNS | ALLOWED_STOP_COLUMNS) & FORBIDDEN_COLUMNS


def test_runtime_guard_rejects_extra_columns_before_any_sql() -> None:
    bad = valid_interchange_record() | {"google_name": "Central Station"}
    with pytest.raises(StorageViolation, match="google_name"):
        # engine=None proves the guard fires before the DB is ever touched.
        upsert_interchange_verdicts(None, [bad])  # type: ignore[arg-type]

    with pytest.raises(StorageViolation, match="rating"):
        upsert_stop_verdicts(  # type: ignore[arg-type]
            None,
            [
                {
                    "stop_uid": "f:S1",
                    "verdict": "STATION_FOUND",
                    "google_place_id": None,
                    "checked_at": datetime.now(UTC),
                    "rating": 4.5,
                }
            ],
        )


@pytest.mark.parametrize("forbidden", ["displayName", "formattedAddress", "location", "rating"])
def test_runtime_guard_rejects_raw_google_fields(forbidden: str) -> None:
    with pytest.raises(StorageViolation):
        _assert_persistable(
            valid_interchange_record() | {forbidden: "anything"},
            ALLOWED_INTERCHANGE_COLUMNS,
        )


def _columns_from_ddl(ddl: str) -> set[str]:
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    columns = set()
    for line in body.splitlines():
        token = line.strip().split(" ")[0].strip(",").lower()
        if token and not token.startswith(("primary", "constraint", "unique", ")")):
            columns.add(token)
    return columns


def test_ddl_columns_are_exactly_the_allowlists() -> None:
    interchange_ddl, stop_ddl = DDL_STATEMENTS
    assert _columns_from_ddl(interchange_ddl) == ALLOWED_INTERCHANGE_COLUMNS
    assert _columns_from_ddl(stop_ddl) == ALLOWED_STOP_COLUMNS


def test_insert_statements_use_only_allowlisted_columns() -> None:
    # Parse every INSERT in the storage module source; any new column that
    # isn't allowlisted fails here, even if the runtime guard were bypassed.
    import inspect

    source = inspect.getsource(storage)
    for match in re.finditer(r"INSERT INTO (\w+)\s*\(([^)]*)\)", source):
        table, cols = match.group(1), {c.strip().lower() for c in match.group(2).split(",")}
        allowed = (
            ALLOWED_INTERCHANGE_COLUMNS
            if table == "google_qa_interchange"
            else ALLOWED_STOP_COLUMNS
        )
        assert cols <= allowed, f"{table} INSERT writes non-allowlisted columns: {cols - allowed}"
        assert not cols & FORBIDDEN_COLUMNS
