"""Read a stops-only CSV (id, name, latitude, longitude) as a raw stops.txt table.

Headers are matched ignoring case and accents, through a small alias table
(Latitud, LATITUDE and lat all work). An unresolvable header gives one ERROR
naming what is missing, never a partial read. Values stay strings so the
normalizer applies the same rules as for GTFS feeds. The BOM and ";" handling
covers what Spanish-locale Excel exports by default.
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from .models import Finding, Severity

log = logging.getLogger(__name__)

#: Output column names, in order.
CANONICAL: tuple[str, ...] = ("stop_id", "stop_name", "stop_lat", "stop_lon")

#: stop_name is optional; a stop without a position is not usable.
REQUIRED: tuple[str, ...] = ("stop_id", "stop_lat", "stop_lon")

ALIASES: dict[str, tuple[str, ...]] = {
    "stop_id": ("id", "stop_id", "stopid", "codigo", "clave"),
    "stop_name": ("name", "stop_name", "stopname", "nombre", "estacion"),
    "stop_lat": ("latitude", "lat", "stop_lat", "latitud"),
    "stop_lon": ("longitude", "lon", "lng", "long", "stop_lon", "longitud"),
}

#: Delimiters to try after the sniffed one.
DELIMITERS: tuple[str, ...] = (",", ";", "\t", "|")

_SNIFF_CHARS = 8192

#: Max example values in a finding.
_SAMPLE_SIZE = 5

#: A coordinate with a decimal comma, e.g. 20,6736.
_DECIMAL_COMMA = re.compile(r"^\s*-?\d+,\d+\s*$")


def _norm(header: str) -> str:
    """Header comparison key: no BOM, quotes or accents, lowercase, "_" between words."""
    text = header.replace("﻿", "").strip().strip("\"'")
    decomposed = unicodedata.normalize("NFKD", text)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", unaccented.casefold()).strip("_")


#: Normalized alias -> canonical column.
_ALIAS_LOOKUP: dict[str, str] = {
    _norm(alias): canonical for canonical, aliases in ALIASES.items() for alias in aliases
}


def resolve_header(columns: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Map header cells to canonical names; return (mapping, ignored duplicates).

    Unknown columns are ignored. If two columns mean the same thing, the first wins.
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
    """Decode bytes; return (text, encoding). utf-8-sig first so a BOM is removed."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"  # old Windows exports


def _candidate_delimiters(text: str) -> list[str]:
    """Delimiters to try: the sniffed one first, then the rest."""
    try:
        sniffed = csv.Sniffer().sniff(text[:_SNIFF_CHARS], delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        sniffed = ","
    return [sniffed] + [d for d in DELIMITERS if d != sniffed]


def _parse(text: str, delimiter: str) -> pd.DataFrame | None:
    """Parse *text* with one delimiter (all values as strings), or None if it fails."""
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


def _unresolved_finding(filename: str, columns: Sequence[str], mapping: dict[str, str]) -> Finding:
    """ERROR finding: what was found, what is missing, which spellings work."""
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


def read_stops_csv(path: Path) -> tuple[pd.DataFrame | None, list[Finding]]:
    """Return (frame, findings); frame is None when the header can't be resolved.

    Raises:
        OSError / UnicodeDecodeError: the loader turns these into a load_error.
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

    # Use the first delimiter that yields a usable header; keep the closest miss for the error.
    best: tuple[list[str], dict[str, str]] | None = None
    parsed: tuple[pd.DataFrame, dict[str, str], list[str], str] | None = None
    for delimiter in _candidate_delimiters(text):
        frame = _parse(text, delimiter)
        if frame is None:
            continue
        mapping, ambiguities = resolve_header(frame.columns)
        if all(name in mapping.values() for name in REQUIRED):
            parsed = (frame, mapping, ambiguities, delimiter)
            break
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
    """WARNING for coordinates like 20,6736, which will be dropped. Values are never rewritten."""
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
