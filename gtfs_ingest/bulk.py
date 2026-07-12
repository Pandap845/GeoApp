"""Bulk DB loading: psycopg3 COPY with a chunked-executemany fallback.

Every write path in the pipeline is a DELETE + INSERT of a pandas frame
inside one transaction. For plain scalar tables, psycopg3's ``COPY ... FROM
STDIN`` is an order of magnitude faster than chunked executemany. This
module provides that fast path behind one call - and because it is a
performance feature, ANY failure (adapter surprises, restricted proxies,
exotic types) degrades to the proven executemany path with a logged
warning; correctness is never at stake, since both run inside the caller's
transaction.

Tables with array columns or SQL-expression columns (``interchanges``)
deliberately stay on their own executemany writers.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sqlalchemy import Connection, text

log = logging.getLogger(__name__)

_CHUNK_SIZE = 1000


def clean_records(df: pd.DataFrame) -> list[dict]:
    """Frame rows as plain-Python dicts: NaN/NA -> None, numpy -> natives.

    Both COPY and executemany need real ``None`` for SQL NULL, and COPY's
    text protocol is safest with native Python scalars (no dependence on
    psycopg's optional numpy adapters).
    """
    cleaned = df.astype(object).where(pd.notna(df), None)
    records = cleaned.to_dict("records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, np.generic):
                record[key] = value.item()
    return records


def copy_rows(conn: Connection, table: str, df: pd.DataFrame) -> int:
    """COPY the frame into *table* (columns = frame columns). Raises on failure.

    Args:
        conn: SQLAlchemy connection inside an open transaction.
        table: target table (trusted internal name, interpolated).
        df: rows to write.

    Returns:
        Number of rows written.
    """
    if df.empty:
        return 0
    cols = list(df.columns)
    raw = conn.connection  # DBAPI fairy -> psycopg3 connection
    with raw.cursor() as cur:
        with cur.copy(
            f"COPY {table} ({', '.join(cols)}) FROM STDIN"  # noqa: S608 - internal names
        ) as copy:
            for record in clean_records(df):
                copy.write_row(tuple(record[c] for c in cols))
    return len(df)


def bulk_insert(conn: Connection, table: str, df: pd.DataFrame) -> int:
    """COPY the frame into *table*, falling back to chunked executemany.

    Args:
        conn: SQLAlchemy connection inside an open transaction.
        table: target table name.
        df: rows to write (empty frame is a no-op).

    Returns:
        Number of rows written.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: only if the fallback also fails
            (a genuine data/schema problem, not a COPY limitation).
    """
    if df.empty:
        return 0
    try:
        # SAVEPOINT: a failed COPY must not poison the caller's transaction
        # (PostgreSQL aborts it otherwise) nor leave partial rows behind.
        with conn.begin_nested():
            return copy_rows(conn, table, df)
    except Exception as exc:  # noqa: BLE001 - COPY is an optimization, never a gate
        log.warning(
            "%s: COPY failed (%s: %s); falling back to executemany",
            table, type(exc).__name__, exc,
        )
        cols = list(df.columns)
        sql = text(
            f"INSERT INTO {table} ({', '.join(cols)}) "  # noqa: S608
            f"VALUES ({', '.join(':' + c for c in cols)})"
        )
        records = clean_records(df)
        for start in range(0, len(records), _CHUNK_SIZE):
            conn.execute(sql, records[start : start + _CHUNK_SIZE])
        return len(df)
