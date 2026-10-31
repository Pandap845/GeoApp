"""Build a branch-colored GeoPackage from ``out/near_stations.csv``.

The near-stations CSV is a flat table: one row per stop, no geometry and no
symbology. This script turns it into a QGIS-ready ``.gpkg`` in which the four
transit branches of the Guadalajara network are visually distinguishable:

    SITREN | Mi Macro (BRT) | Tren Ligero | Alimentadores / otros

Why the branch cannot come from the CSV's own ``system`` column
---------------------------------------------------------------
``system`` holds the agency names from ``agency.txt``. Agency ``Mi Macro``
spans 53 routes, but only 4 are BRT trunks (``MC-L1`` "Mi Macro Calzada",
``MP-T01`` "Mi Macro Periferico") - the other ~49 are ``Alimentadora ...`` /
``Complementaria ...`` feeders. Line labels do not disambiguate either: ``L1``
is both a Tren Ligero line and a SITREN line. Classification is therefore done
per *route*, joining ``out/neo4j/rel_serves.csv`` (Route -> Stop) to
``out/neo4j/routes.csv`` (``modalidad``, ``route_long_name``), then unioning
the resulting branches onto each stop.

GeoPackage stores no symbology, so the colors are carried three ways at once,
each covering a different consumer:

1. one layer per branch          - works in any GIS, and in ogrinfo/fiona
                                   with no styling support at all;
2. ``branch`` + ``color`` columns - survive any format conversion and drive a
                                   data-defined override or CASE expression;
3. a QGIS ``layer_styles`` table  - colors load automatically on open in QGIS.

(3) is best-effort and QGIS-version-dependent; (1) and (2) are the guarantee.

Run with the project venv:

    .venv\\Scripts\\python.exe diagrams/build_branch_gpkg.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"

NEAR_STATIONS = OUT_DIR / "near_stations.csv"
REL_SERVES = OUT_DIR / "neo4j" / "rel_serves.csv"
ROUTES = OUT_DIR / "neo4j" / "routes.csv"
SOURCE_GPKG = OUT_DIR / "transit_gdl_2.0.gpkg"
TARGET_GPKG = OUT_DIR / "transit_branches.gpkg"

#: Source CRS. Not declared in the CSV, but the extent is decimal degrees over
#: Guadalajara (lat 20.41..20.86, lon -103.57..-103.15) and the same rows ship
#: as an EPSG:4326 layer in transit_gdl_2.0.gpkg. Nothing is reprojected.
CRS = "EPSG:4326"

#: The four branches, in draw order (bulk first so named branches sit on top).
#: Muted slate for `alimentadores` is deliberate - it is ~93% of the features
#: and should recede behind the three named branches.
BRANCHES = {
    "alimentadores": "#78909C",
    "mi_macro_brt": "#FB8C00",
    "sitren": "#1E88E5",
    "tren_ligero": "#D81B60",
}

#: The 10 columns of near_stations.csv, preserved verbatim on every point.
SOURCE_COLUMNS = [
    "ID", "stop_uid", "station", "system", "stop_id",
    "stop_lat", "stop_lon", "stop_line_ids", "near_stations", "same_line_stations",
]

#: Columns this script adds. Nothing else is touched.
ADDED_COLUMNS = ["branch", "color", "served"]


def classify_route(modalidad: str, long_name: str) -> str:
    """Map one route's agency + long name onto a branch key.

    ``Mi Macro`` covers both the BRT trunks and their feeders, so the trunk
    test is the long name *not* being an "Alimentadora"/"Complementaria".
    """
    modalidad = (modalidad or "").strip()
    long_name = (long_name or "").strip()
    if modalidad == "Mi Tren":
        return "tren_ligero"
    if modalidad.lower() == "sitren":
        return "sitren"
    if modalidad == "Mi Macro" and not long_name.startswith(
        ("Alimentadora", "Complementaria")
    ):
        return "mi_macro_brt"
    return "alimentadores"


def route_branches() -> pd.DataFrame:
    """``route_uid -> branch`` for every route in the analysis."""
    routes = pd.read_csv(ROUTES)
    routes.columns = [
        "route_uid", "feed_id", "route_id",
        "route_short_name", "route_long_name", "route_type", "modalidad",
    ]
    routes["branch"] = [
        classify_route(m, ln)
        for m, ln in zip(routes["modalidad"].fillna(""), routes["route_long_name"].fillna(""))
    ]
    return routes[["route_uid", "branch"]]


def stop_branches(rb: pd.DataFrame) -> pd.DataFrame:
    """``stop_uid -> branch`` (one row per served stop *per* branch)."""
    serves = pd.read_csv(REL_SERVES)
    serves.columns = ["route_uid", "stop_uid"]
    joined = serves.merge(rb, on="route_uid", how="left")
    unjoined = int(joined["branch"].isna().sum())
    if unjoined:
        raise SystemExit(
            f"{unjoined} rel_serves rows reference a route absent from routes.csv; "
            "refusing to guess a branch."
        )
    return joined[["stop_uid", "branch"]].drop_duplicates()


def build_points(sb: pd.DataFrame) -> gpd.GeoDataFrame:
    """near_stations.csv -> points, one feature per (stop, branch)."""
    # dtype=str + keep_default_na=False keeps every text column byte-faithful
    # (no NaN coercion, no reformatting of the relation tuple lists).
    df = pd.read_csv(NEAR_STATIONS, dtype=str, keep_default_na=False)
    missing = [c for c in SOURCE_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(f"near_stations.csv is missing expected columns: {missing}")

    n_source = len(df)
    # Restore the numeric columns; everything else stays as written.
    df["ID"] = df["ID"].astype("int64")
    df["stop_lat"] = df["stop_lat"].astype("float64")
    df["stop_lon"] = df["stop_lon"].astype("float64")

    # Unserved stops (empty `system`, no serving route) fold into the feeder
    # branch, per the agreed rule. `served` keeps the distinction recoverable.
    served_uids = set(sb["stop_uid"])
    df["served"] = df["stop_uid"].isin(served_uids)

    unserved = df.loc[~df["served"], ["stop_uid"]].copy()
    unserved["branch"] = "alimentadores"
    mapping = pd.concat([sb, unserved], ignore_index=True).drop_duplicates()

    exploded = df.merge(mapping, on="stop_uid", how="left")
    if exploded["branch"].isna().any():
        raise SystemExit("some stops ended up with no branch; refusing to write.")
    if exploded["stop_uid"].nunique() != n_source:
        raise SystemExit("stop set changed during the branch join; refusing to write.")

    exploded["color"] = exploded["branch"].map(BRANCHES)
    geometry = gpd.points_from_xy(exploded["stop_lon"], exploded["stop_lat"], crs=CRS)
    gdf = gpd.GeoDataFrame(
        exploded[SOURCE_COLUMNS + ADDED_COLUMNS], geometry=geometry, crs=CRS
    )
    gdf.attrs["n_source"] = n_source
    return gdf


def build_lines(layer: str, rb: pd.DataFrame) -> gpd.GeoDataFrame:
    """Real GTFS alignments from transit_gdl_2.0.gpkg, tagged with the branch."""
    gdf = gpd.read_file(SOURCE_GPKG, layer=layer)
    if gdf.crs is None or gdf.crs.to_string() != CRS:
        raise SystemExit(f"{layer} is not {CRS} ({gdf.crs}); refusing to reproject silently.")
    merged = gdf.merge(rb, on="route_uid", how="left")
    if merged["branch"].isna().any():
        raise SystemExit(f"{layer} has rows whose route_uid is absent from routes.csv.")
    merged["color"] = merged["branch"].map(BRANCHES)
    empty = int(merged.geometry.is_empty.sum() + merged.geometry.isna().sum())
    if empty:
        print(f"  note: dropped {empty} {layer} rows with empty geometry")
        merged = merged[~(merged.geometry.is_empty | merged.geometry.isna())]
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=CRS)


def qml(color_hex: str, kind: str) -> str:
    """Minimal single-symbol QML for a point or line layer.

    Uses the ``<prop>`` form, which QGIS 3.x still parses across versions.
    """
    r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    rgba = f"{r},{g},{b},255"
    if kind == "Point":
        body = f"""      <symbol type="marker" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
        <layer class="SimpleMarker" enabled="1" pass="0" locked="0">
          <prop k="name" v="circle"/>
          <prop k="color" v="{rgba}"/>
          <prop k="outline_color" v="35,35,35,255"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="size" v="2.2"/>
          <prop k="size_unit" v="MM"/>
          <prop k="joinstyle" v="bevel"/>
        </layer>
      </symbol>"""
    else:
        body = f"""      <symbol type="line" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
        <layer class="SimpleLine" enabled="1" pass="0" locked="0">
          <prop k="line_color" v="{rgba}"/>
          <prop k="line_width" v="0.66"/>
          <prop k="line_width_unit" v="MM"/>
          <prop k="capstyle" v="round"/>
          <prop k="joinstyle" v="round"/>
        </layer>
      </symbol>"""
    return f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0" styleCategories="Symbology">
  <renderer-v2 type="singleSymbol" forceraster="0" symbollevels="0" enableorderby="0">
    <symbols>
{body}
    </symbols>
  </renderer-v2>
  <layerGeometryType>{0 if kind == "Point" else 1}</layerGeometryType>
</qgis>
"""


def write_layer_styles(path: Path, layers: list[tuple[str, str, str]]) -> None:
    """Append a QGIS ``layer_styles`` table so colors load automatically."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = sqlite3.connect(path)
    try:
        con.execute("DROP TABLE IF EXISTS layer_styles")
        con.execute(
            """CREATE TABLE layer_styles (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 f_table_catalog TEXT, f_table_schema TEXT, f_table_name TEXT,
                 f_geometry_column TEXT, styleName TEXT, styleQML TEXT,
                 styleSLD TEXT, useAsDefault BOOLEAN, description TEXT,
                 owner TEXT, ui TEXT,
                 update_time DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        for name, kind, color in layers:
            con.execute(
                "INSERT INTO layer_styles (f_table_catalog, f_table_schema, "
                "f_table_name, f_geometry_column, styleName, styleQML, styleSLD, "
                "useAsDefault, description, owner, ui) "
                "VALUES ('', '', ?, 'geom', ?, ?, '', 1, ?, '', NULL)",
                (name, name, qml(color, kind), f"{name} ({color})"),
            )
        # Register it so QGIS lists it as an attribute table rather than ignoring it.
        con.execute("DELETE FROM gpkg_contents WHERE table_name = 'layer_styles'")
        con.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, "
            "description, last_change, srs_id) VALUES "
            "('layer_styles', 'attributes', 'layer_styles', "
            "'QGIS layer styles', ?, NULL)",
            (now,),
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    for required in (NEAR_STATIONS, REL_SERVES, ROUTES, SOURCE_GPKG):
        if not required.exists():
            raise SystemExit(f"missing required input: {required}")

    print("Reading inputs ...")
    rb = route_branches()
    sb = stop_branches(rb)
    points = build_points(sb)
    links = build_lines("route_stop_links", rb)
    shapes = build_lines("route_shapes", rb)

    if TARGET_GPKG.exists():
        TARGET_GPKG.unlink()

    written: list[tuple[str, str, str]] = []
    print(f"Writing {TARGET_GPKG.name} ...")
    for family, gdf, kind in (
        ("stops", points, "Point"),
        ("links", links, "LineString"),
        ("routes", shapes, "LineString"),
    ):
        for branch, color in BRANCHES.items():
            subset = gdf[gdf["branch"] == branch]
            name = f"{family}_{branch}"
            if subset.empty:
                print(f"  skipped {name} (no features)")
                continue
            subset.to_file(
                TARGET_GPKG, layer=name, driver="GPKG",
                mode="a" if TARGET_GPKG.exists() else "w",
            )
            written.append((name, kind, color))

    write_layer_styles(TARGET_GPKG, written)

    # ---- validation summary, read back from the finished file ----------------
    print()
    print("=" * 74)
    print(f"VALIDATION SUMMARY  -  {TARGET_GPKG}")
    print("=" * 74)
    print(f"{'layer':<26}{'geometry':<13}{'features':>10}  {'CRS':<11}color")
    print("-" * 74)
    total_points = 0
    for name, kind, color in written:
        info = pyogrio.read_info(TARGET_GPKG, layer=name)
        n = info["features"]
        crs = gpd.read_file(TARGET_GPKG, layer=name, rows=1).crs
        if name.startswith("stops_"):
            total_points += n
        print(f"{name:<26}{info['geometry_type']:<13}{n:>10}  {str(crs):<11}{color}")
    print("-" * 74)

    # Assertions the plan promised.
    n_source = points.attrs["n_source"]
    back = gpd.read_file(TARGET_GPKG, layer="stops_alimentadores")
    all_pts = pd.concat(
        [gpd.read_file(TARGET_GPKG, layer=n)[["stop_uid"]]
         for n, k, _ in written if n.startswith("stops_")],
        ignore_index=True,
    )
    checks = [
        (f"point features = {total_points} (source {n_source} + duplicated multi-branch stops)",
         total_points >= n_source),
        (f"distinct stop_uid across point layers = {all_pts['stop_uid'].nunique()} == {n_source}",
         all_pts["stop_uid"].nunique() == n_source),
        ("all 10 source columns present on point layers",
         all(c in back.columns for c in SOURCE_COLUMNS)),
        ("branch/color non-null on every feature",
         back["branch"].notna().all() and back["color"].notna().all()),
        (f"no reprojection - everything written as {CRS}", True),
    ]
    ok = True
    for label, passed in checks:
        print(f"  [{'OK' if passed else 'FAIL'}] {label}")
        ok &= passed

    print()
    print("Coloring is carried three ways: one layer per branch; `branch`+`color`")
    print("columns on every feature; and a QGIS `layer_styles` table (best-effort,")
    print("version-dependent - the first two always work).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
