"""Header resolution, encoding and delimiter handling for stops-only CSVs."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gtfs_ingest.models import Severity
from gtfs_ingest.stops_csv import ALIASES, REQUIRED, read_stops_csv, resolve_header

CANONICAL_CSV = "id,name,latitude,longitude\nS1,Centro,20.6736,-103.344\nS2,Norte,20.70,-103.30\n"


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def write(tmp_path: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


# --------------------------------------------------------------------------
# Header resolution
# --------------------------------------------------------------------------

def test_canonical_header_resolves(tmp_path: Path) -> None:
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", CANONICAL_CSV))

    assert frame is not None
    assert list(frame.columns) == ["stop_id", "stop_name", "stop_lat", "stop_lon"]
    assert frame["stop_id"].tolist() == ["S1", "S2"]
    assert not [f for f in findings if f.severity is not Severity.INFO]


@pytest.mark.parametrize(
    "header",
    [
        "id,name,latitude,longitude",
        "ID,NAME,LATITUDE,LONGITUDE",
        "stop_id,stop_name,stop_lat,stop_lon",
        "codigo,nombre,latitud,longitud",
        "clave,estacion,lat,lng",
        "Id , Nombre , LATITUD , Longitud ",   # stray whitespace
        "ID,ESTACIÓN,Latitud,Longitud",        # accents
        "id;name;latitude;longitude",          # semicolon-delimited
    ],
)
def test_aliases_resolve_case_accent_and_punctuation_insensitively(
    tmp_path: Path, header: str
) -> None:
    sep = ";" if ";" in header else ","
    body = sep.join(["S1", "Centro", "20.6736", "-103.344"])
    frame, _ = read_stops_csv(write(tmp_path, "stops.csv", f"{header}\n{body}\n"))

    assert frame is not None
    assert set(REQUIRED) <= set(frame.columns)
    assert frame["stop_lat"].tolist() == ["20.6736"]


def test_resolve_header_drops_unknown_columns() -> None:
    mapping, ambiguities = resolve_header(["id", "zone_id", "name", "lat", "lon", "notes"])

    assert mapping == {"id": "stop_id", "name": "stop_name", "lat": "stop_lat", "lon": "stop_lon"}
    assert ambiguities == []


def test_resolve_header_first_column_wins_on_duplicate_claim() -> None:
    mapping, ambiguities = resolve_header(["id", "lat", "latitude", "lon"])

    assert mapping["lat"] == "stop_lat"
    assert "latitude" not in mapping
    assert len(ambiguities) == 1


def test_ambiguous_header_warns_and_keeps_first(tmp_path: Path) -> None:
    text = "id,lat,latitude,lon\nS1,20.67,99.99,-103.34\n"
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", text))

    assert frame is not None
    assert frame["stop_lat"].tolist() == ["20.67"]     # the first column won
    (warning,) = [f for f in findings if f.code == "ambiguous_csv_header"]
    assert warning.severity is Severity.WARNING


def test_extra_columns_are_dropped(tmp_path: Path) -> None:
    text = "id,name,latitude,longitude,zone_id,wheelchair\nS1,Centro,20.67,-103.34,Z1,1\n"
    frame, _ = read_stops_csv(write(tmp_path, "stops.csv", text))

    assert frame is not None
    assert list(frame.columns) == ["stop_id", "stop_name", "stop_lat", "stop_lon"]


def test_stop_name_is_optional(tmp_path: Path) -> None:
    frame, findings = read_stops_csv(
        write(tmp_path, "stops.csv", "id,latitude,longitude\nS1,20.67,-103.34\n")
    )

    assert frame is not None
    assert "stop_name" not in frame.columns
    assert "unresolved_csv_header" not in codes(findings)


# --------------------------------------------------------------------------
# Failure reporting
# --------------------------------------------------------------------------

def test_unresolved_header_returns_finding_not_exception(tmp_path: Path) -> None:
    text = "ID_PARADA,DESC,X,Y\n1,Centro,20.67,-103.34\n"
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", text))

    assert frame is None
    (finding,) = [f for f in findings if f.code == "unresolved_csv_header"]
    assert finding.severity is Severity.ERROR
    # The message must be actionable: it names every missing field AND the
    # spellings that would have worked.
    for name in ("stop_lat", "stop_lon"):
        assert name in finding.message
        for alias in ALIASES[name]:
            assert alias in finding.message
    assert "ID_PARADA" in finding.message           # what was actually found
    assert finding.sample == ["ID_PARADA", "DESC", "X", "Y"]


def test_partially_resolved_header_reports_what_resolved(tmp_path: Path) -> None:
    text = "id,name,X,Y\nS1,Centro,20.67,-103.34\n"
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", text))

    assert frame is None
    (finding,) = [f for f in findings if f.code == "unresolved_csv_header"]
    assert "stop_id <- id" in finding.message       # tells the user what already worked
    assert finding.count == 2                        # stop_lat + stop_lon


def test_headerless_file_is_unresolved(tmp_path: Path) -> None:
    frame, findings = read_stops_csv(
        write(tmp_path, "stops.csv", "S1,Centro,20.67,-103.34\nS2,Norte,20.7,-103.3\n")
    )

    assert frame is None
    assert "unresolved_csv_header" in codes(findings)


# --------------------------------------------------------------------------
# Encoding and delimiters
# --------------------------------------------------------------------------

def test_utf8_bom_header_resolves(tmp_path: Path) -> None:
    """The Excel-on-Windows default, which partridge cannot read."""
    path = tmp_path / "stops.csv"
    path.write_bytes(b"\xef\xbb\xbf" + CANONICAL_CSV.encode("utf-8"))

    frame, findings = read_stops_csv(path)

    assert frame is not None
    assert list(frame.columns)[0] == "stop_id"      # not "﻿stop_id"
    assert "csv_encoding" not in codes(findings)    # utf-8-sig is the expected case


def test_utf16_is_decoded(tmp_path: Path) -> None:
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", CANONICAL_CSV, "utf-16"))

    assert frame is not None
    assert frame["stop_id"].tolist() == ["S1", "S2"]
    assert "csv_encoding" in codes(findings)


def test_cp1252_fallback_preserves_accents(tmp_path: Path) -> None:
    text = "id,name,latitude,longitude\nS1,Estación Niños,20.67,-103.34\n"
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", text, "cp1252"))

    assert frame is not None
    assert frame["stop_name"].tolist() == ["Estación Niños"]
    assert "csv_encoding" in codes(findings)


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_alternate_delimiters(tmp_path: Path, delimiter: str) -> None:
    header = delimiter.join(["id", "name", "latitude", "longitude"])
    row = delimiter.join(["S1", "Centro", "20.6736", "-103.344"])
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", f"{header}\n{row}\n"))

    assert frame is not None
    assert frame["stop_name"].tolist() == ["Centro"]
    assert "csv_delimiter" in codes(findings)


def test_decimal_comma_warns_with_actionable_message(tmp_path: Path) -> None:
    text = "id;name;latitud;longitud\nS1;Centro;20,6736;-103,344\n"
    frame, findings = read_stops_csv(write(tmp_path, "stops.csv", text))

    assert frame is not None                        # parsed, but flagged
    (warning,) = [f for f in findings if f.code == "csv_decimal_comma"]
    assert warning.severity is Severity.WARNING
    assert "decimal point" in warning.message


# --------------------------------------------------------------------------
# Contract with the normalizer
# --------------------------------------------------------------------------

def test_values_are_strings(tmp_path: Path) -> None:
    """All-string dtypes are what let normalize_stops run unchanged."""
    frame, _ = read_stops_csv(write(tmp_path, "stops.csv", CANONICAL_CSV))

    assert frame is not None
    for column in frame.columns:
        assert pd.api.types.is_string_dtype(frame[column])


def test_values_are_whitespace_stripped(tmp_path: Path) -> None:
    text = "id,name,latitude,longitude\n  S1  ,  Centro  , 20.67 , -103.34 \n"
    frame, _ = read_stops_csv(write(tmp_path, "stops.csv", text))

    assert frame is not None
    assert frame.iloc[0].tolist() == ["S1", "Centro", "20.67", "-103.34"]


def test_header_only_file_yields_empty_frame(tmp_path: Path) -> None:
    frame, findings = read_stops_csv(
        write(tmp_path, "stops.csv", "id,name,latitude,longitude\n")
    )

    assert frame is not None
    assert frame.empty
    assert "unresolved_csv_header" not in codes(findings)
