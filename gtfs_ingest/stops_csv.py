"""Minimal stops-only CSV input (id, name, latitude, longitude).

Some stop inventories never come as GTFS: a field survey, an agency
spreadsheet, a manually digitized list. This module reads such a file into a
frame shaped exactly like a raw GTFS ``stops.txt`` so the rest of the pipeline
can treat it as an ordinary feed - :func:`gtfs_ingest.loader.load_feed` seeds it
into a :class:`~gtfs_ingest.models.LoadedFeed`, and normalization, validation,
the PostGIS load and every Phase 2/3 consumer run unchanged.

**Header resolution is deliberately forgiving.** Real files come from Excel in
whatever locale the author had, so :func:`resolve_header` matches column names
case-insensitively, accent-insensitively and across a small alias table
(:data:`ALIASES`) - ``Latitud``, ``LATITUDE`` and ``lat`` all mean the same
thing. What it will *not* do is guess: a header that cannot be resolved to
``stop_id``/``stop_lat``/``stop_lon`` produces one ERROR finding naming what was
found, what is missing and which spellings are accepted, rather than a partial
read that silently drops half the file.

**Values come back as strings**, exactly as ``partridge.load_raw_feed`` returns
them. That is what lets :mod:`gtfs_ingest.normalizer` apply the same coercion,
coordinate-range and duplicate-id rules it applies to real feeds - a bad
coordinate in a CSV is dropped with the same finding code as a bad coordinate
in a GTFS feed, and nothing here re-implements that logic.

The encoding/delimiter handling is not paranoia: Excel on a Spanish-locale
Windows exports semicolon-delimited UTF-8-with-BOM by default, and partridge
cannot read that (its own decoder reports plain ``utf-8`` for a BOM'd file and
its column cleanup only strips whitespace, so the first column would arrive
named ``"﻿id"``).
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .models import Finding, Severity

log = logging.getLogger(__name__)

#: GTFS column names this module emits, in canonical order.
CANONICAL: tuple[str, ...] = ("stop_id", "stop_name", "stop_lat", "stop_lon")

#: Canonical columns a file must supply. ``stop_name`` is optional - a stop
#: without a name is usable, a stop without a position is not.
REQUIRED: tuple[str, ...] = ("stop_id", "stop_lat", "stop_lon")

ALIASES: dict[str, tuple[str, ...]] = {
    "stop_id": ("id", "stop_id", "stopid", "codigo", "clave"),
    "stop_name": ("name", "stop_name", "stopname", "nombre", "estacion"),
    "stop_lat": ("latitude", "lat", "stop_lat", "latitud"),
    "stop_lon": ("longitude", "lon", "lng", "long", "stop_lon", "longitud"),
}

#: Delimiters tried, in order, when the sniffed one does not resolve a header.
DELIMITERS: tuple[str, ...] = (",", ";", "\t", "|")

#: Bytes of the file fed to :class:`csv.Sniffer`.
_SNIFF_CHARS = 8192

#: Max offending values carried in a finding's ``sample``.
_SAMPLE_SIZE = 5

#: Matches a coordinate written with a decimal comma, e.g. ``20,6736``.
_DECIMAL_COMMA = re.compile(r"^\s*-?\d+,\d+\s*$")


def _norm(header: str) -> str:
    """Normalize one header cell to its comparison key.

    Strips a stray BOM, surrounding whitespace and quotes, decomposes accents
    away (``Latitud`` -> ``latitud``, ``Estación`` -> ``estacion``), casefolds,
    and collapses every run of non-alphanumerics to a single underscore. Case-
    and accent-insensitivity both fall out of this one function, which is why
    :data:`ALIASES` lists only plain lowercase forms.
    """
    text = header.replace("﻿", "").strip().strip("\"'")
    decomposed = unicodedata.normalize("NFKD", text)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", unaccented.casefold()).strip("_")


#: Normalized alias -> canonical column, built once from :data:`ALIASES`.
_ALIAS_LOOKUP: dict[str, str] = {
    _norm(alias): canonical
    for canonical, aliases in ALIASES.items()
    for alias in aliases
}


def resolve_header(columns: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Map a file's columns onto canonical GTFS stop column names.

    Args:
        columns: the header cells as read, in file order.

    Returns:
        ``(mapping, ambiguities)`` where *mapping* is ``{original: canonical}``
        for every column that resolved, and *ambiguities* describes columns
        dropped because an earlier column already claimed that canonical name.

    Columns that resolve to nothing are omitted without comment - a stops file
    may legitimately carry extra columns (zone, wheelchair, notes) that this
    pipeline has no use for. When two columns claim the same canonical name
    (a file carrying both ``lat`` and ``latitude``), the **first in file order
    wins**, so the result never depends on dict iteration order.
    """
    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    ambiguities: list[str] = []
    for column in columns:
        canonical = _ALIAS_LOOKUP.get(_norm(str(column)))
        if canonical is None:
            continue
        if canonical in claimed:
            ambiguities.append(f"{column} (already taken by another column)")
            continue
        mapping[str(column)] = canonical
        claimed.add(canonical)
    return mapping, ambiguities


def _decode(raw: bytes) -> tuple[str, str]:
    """Decode file bytes, returning ``(text, encoding_used)``.

    ``utf-8-sig`` is tried first and is the whole point of decoding here rather
    than delegating to pandas: it removes the BOM that would otherwise become
    part of the first column's name.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        # Legacy Windows export; may still raise, which the loader turns into
        # a load_error rather than a crash.
        return raw.decode("cp1252"), "cp1252"


def _candidate_delimiters(text: str) -> list[str]:
    """Delimiters to try, sniffed guess first, then the rest of :data:`DELIMITERS`."""
    try:
        sniffed = csv.Sniffer().sniff(text[:_SNIFF_CHARS], delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        sniffed = ","
    return [sniffed] + [d for d in DELIMITERS if d != sniffed]


def _parse(text: str, delimiter: str) -> Optional[pd.DataFrame]:
    """Read *text* with one delimiter, or ``None`` if it cannot be parsed.

    ``dtype=str`` reproduces ``partridge.load_raw_feed``'s all-string contract,
    which is what lets :func:`gtfs_ingest.normalizer.normalize_stops` run on the
    result unchanged.
    """
    try:
        return pd.read_csv(
            io.StringIO(text),
            sep=delimiter,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
            skipinitialspace=True,
            engine="python",
        )
    except Exception:  # noqa: BLE001 - a delimiter that does not parse is just not the one
        return None


def _unresolved_finding(
    filename: str, columns: Sequence[str], mapping: dict[str, str]
) -> Finding:
    """Build the ERROR explaining exactly why a header could not be used."""
    resolved = {v: k for k, v in mapping.items()}
    missing = [name for name in REQUIRED if name not in resolved]
    wanted = "; ".join(f"{name} (accepted: {', '.join(ALIASES[name])})" for name in missing)
    found = ", ".join(str(c) for c in columns) or "<no columns>"
    got = (
        " Resolved so far: " + ", ".join(f"{v} <- {k}" for k, v in mapping.items()) + "."
        if mapping
        else ""
    )
    return Finding(
        severity=Severity.ERROR,
        code="unresolved_csv_header",
        message=(
            f"Stops CSV header could not be resolved. Found columns: [{found}]. "
            f"Missing required: {wanted}.{got} "
            "Rename the columns to one of the accepted spellings, or add a header row."
        ),
        file=filename,
        count=len(missing),
        sample=[str(c) for c in columns[:_SAMPLE_SIZE]],
    )


def read_stops_csv(path: Path) -> tuple[Optional[pd.DataFrame], list[Finding]]:
    """Read a stops-only CSV into a raw ``stops.txt``-shaped frame.

    Args:
        path: the ``.csv`` file to read.

    Returns:
        ``(frame, findings)``. The frame carries the resolved subset of
        :data:`CANONICAL` as all-string columns, ready to be handed to
        :func:`gtfs_ingest.normalizer.normalize_stops`; it is ``None`` when the
        header could not be resolved, in which case *findings* holds the
        explanatory ERROR.

    Raises:
        OSError / UnicodeDecodeError / pandas parse errors: the caller
        (``loader._load_stops_csv``) converts these into a feed ``load_error``.
    """
    findings: list[Finding] = []
    filename = path.name

    text, encoding = _decode(path.read_bytes())
    if encoding != "utf-8-sig":
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="csv_encoding",
                message=f"Stops CSV decoded as {encoding} (not UTF-8).",
                file=filename,
            )
        )

    # Delimiter detection is outcome-driven rather than heuristic: try the
    # sniffed guess first, but accept whichever candidate actually yields a
    # resolvable header. Keeps the best near-miss for the error message.
    best: Optional[tuple[list[str], dict[str, str]]] = None
    parsed: Optional[tuple[pd.DataFrame, dict[str, str], list[str], str]] = None
    for delimiter in _candidate_delimiters(text):
        frame = _parse(text, delimiter)
        if frame is None:
            continue
        mapping, ambiguities = resolve_header(frame.columns)
        if all(name in mapping.values() for name in REQUIRED):
            parsed = (frame, mapping, ambiguities, delimiter)
            break
        # Record the closest miss for the error message. `best is None` must
        # come first: with nothing resolved anywhere, no candidate would ever
        # beat the initial score and the message would name no columns at all.
        if best is None or len(mapping) > len(best[1]):
            best = ([str(c) for c in frame.columns], mapping)

    if parsed is None:
        columns, mapping = best if best is not None else ([], {})
        findings.append(_unresolved_finding(filename, columns, mapping))
        return None, findings

    frame, mapping, ambiguities, delimiter = parsed
    if delimiter != ",":
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="csv_delimiter",
                message=f"Stops CSV parsed with {delimiter!r} as the field delimiter.",
                file=filename,
            )
        )
    if ambiguities:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="ambiguous_csv_header",
                message=(
                    "Several columns map to the same field; the first in file order "
                    f"was used and these were ignored: {', '.join(ambiguities)}."
                ),
                file=filename,
                count=len(ambiguities),
                sample=ambiguities[:_SAMPLE_SIZE],
            )
        )

    frame = frame.rename(columns=mapping)[list(mapping.values())]
    for column in frame.columns:
        frame[column] = frame[column].astype("string").str.strip()

    findings.extend(_decimal_comma_findings(filename, frame))
    log.info("Stops CSV %s: %d row(s), columns %s", filename, len(frame), list(frame.columns))
    return frame, findings


def _decimal_comma_findings(filename: str, frame: pd.DataFrame) -> list[Finding]:
    """WARNING when coordinates use a decimal comma, which cannot be parsed.

    ``20,6736`` coerces to NaN in the normalizer and the row is dropped as
    ``dropped_invalid_coordinates`` - correct, but that message says
    "unparseable" and would not point at the comma. Nothing is rewritten here:
    silently reinterpreting a user's numbers is worse than refusing them.
    """
    offenders: list[str] = []
    for column in ("stop_lat", "stop_lon"):
        if column not in frame.columns:
            continue
        values = frame[column].dropna()
        hits = values[values.str.match(_DECIMAL_COMMA)]
        offenders.extend(f"{column}={v}" for v in hits.head(_SAMPLE_SIZE))
    if not offenders:
        return []
    return [
        Finding(
            severity=Severity.WARNING,
            code="csv_decimal_comma",
            message=(
                "Coordinates appear to use a decimal comma, which cannot be parsed as "
                "a number; these rows will be dropped as invalid coordinates. Re-export "
                "with a decimal point (in Excel, change the locale or use Save As > CSV UTF-8)."
            ),
            file=filename,
            count=len(offenders),
            sample=offenders[:_SAMPLE_SIZE],
        )
    ]
