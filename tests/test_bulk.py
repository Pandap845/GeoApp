"""Bulk COPY loading: record cleaning and the executemany fallback."""

from __future__ import annotations

import numpy as np
import pandas as pd

from gtfs_ingest.bulk import _CHUNK_SIZE, bulk_insert, clean_records


def test_clean_records_nulls_and_native_types() -> None:
    df = pd.DataFrame(
        {
            "a": [1.5, np.nan],
            "b": np.array([7, 9], dtype=np.int64),
            "c": ["x", None],
            "d": np.array([True, False], dtype=np.bool_),
        }
    )
    records = clean_records(df)

    assert records[1]["a"] is None
    assert records[1]["c"] is None
    # numpy scalars become plain Python types (COPY-safe).
    assert type(records[0]["a"]) is float and records[0]["a"] == 1.5
    assert type(records[0]["b"]) is int and records[0]["b"] == 7
    assert type(records[0]["d"]) is bool and records[0]["d"] is True


class _FailingCopyConn:
    """Fake connection whose raw-cursor access fails -> fallback path."""

    def __init__(self) -> None:
        self.executed: list = []

    class _Nested:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False  # propagate, like a rolled-back SAVEPOINT

    def begin_nested(self):
        return self._Nested()

    @property
    def connection(self):  # what copy_rows touches first
        raise RuntimeError("raw connection unavailable")

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))


def test_bulk_insert_falls_back_to_executemany() -> None:
    conn = _FailingCopyConn()
    df = pd.DataFrame({"x": range(2500), "y": ["v"] * 2500})

    written = bulk_insert(conn, "some_table", df)

    assert written == 2500
    # ceil(2500 / _CHUNK_SIZE) executemany batches on the fallback path.
    assert len(conn.executed) == -(-2500 // _CHUNK_SIZE)
    sql, params = conn.executed[0]
    assert "INSERT INTO some_table (x, y)" in sql
    assert len(params) == _CHUNK_SIZE


def test_bulk_insert_empty_frame_is_noop() -> None:
    conn = _FailingCopyConn()
    assert bulk_insert(conn, "t", pd.DataFrame(columns=["x"])) == 0
    assert conn.executed == []
