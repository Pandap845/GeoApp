"""Fast bulk inserts: PostgreSQL COPY, falling back to chunked executemany.

COPY is only a speed-up, so any failure falls back to plain INSERTs inside the
same transaction. Tables with array or SQL-expression columns use their own writers.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sqlalchemy import Connection, text

log = logging.getLogger(__name__)

_CHUNK_SIZE = 1000


def clean_records(df: pd.DataFrame) -> list[dict]:
    """Rows as plain Python dicts: NaN/NA become None, numpy scalars become natives."""
    cleaned = df.astype(object).where(pd.notna(df), None)
    records = cleaned.to_dict("records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, np.generic):
                record[key] = value.item()
    return records


def copy_rows(conn: Connection, table: str, df: pd.DataFrame) -> int:
    """COPY the frame into *table* and return the row count. Raises on failure."""
    if df.empty:
        return 0
    cols = list(df.columns)
    raw = conn.connection  # underlying psycopg3 connection
    with (
        raw.cursor() as cur,
        cur.copy(
            f"COPY {table} ({', '.join(cols)}) FROM STDIN"  # internal names
        ) as copy,
    ):
        for record in clean_records(df):
            copy.write_row(tuple(record[c] for c in cols))
    return len(df)


def bulk_insert(conn: Connection, table: str, df: pd.DataFrame) -> int:
    """Insert the frame into *table* (COPY first, executemany fallback); return the row count."""
    if df.empty:
        return 0
    try:
        # Savepoint, so a failed COPY doesn't abort the caller's transaction.
        with conn.begin_nested():
            return copy_rows(conn, table, df)
    except Exception as exc:  # noqa: BLE001 - COPY is an optimization, never a gate
        log.warning(
            "%s: COPY failed (%s: %s); falling back to executemany",
            table,
            type(exc).__name__,
            exc,
        )
        cols = list(df.columns)
        sql = text(
            f"INSERT INTO {table} ({', '.join(cols)}) "  # table and column names are internal, never user input
            f"VALUES ({', '.join(':' + c for c in cols)})"
        )
        records = clean_records(df)
        for start in range(0, len(records), _CHUNK_SIZE):
            conn.execute(sql, records[start : start + _CHUNK_SIZE])
        return len(df)
