"""Regenerate every figure used by ``docs/guide/pipeline-guide.md``.

Two kinds of figure:

* **Explanatory** (``e*_``): small synthetic examples that show how an
  algorithm works - headway gaps, frequency expansion, side-of-street geometry,
  the OSM confidence formula. They need no data.
* **Measured** (``r*_``): charts and maps drawn from a real run's outputs.

Usage (from the repo root; needs matplotlib, which is not a runtime
dependency)::

    python docs/figures/make_figures.py                     # reads ./outputs/out_mibici_v12
    python docs/figures/make_figures.py --run ./outputs/out_mibici_v12 --before ./outputs/out_mibici_v9

The measured figures expect a run made with ``--network-walk``, with the
``detect-interchanges`` and ``conflate-osm`` reports in the same directory.

Styling follows one fixed palette: light surface, recessive hairline grid,
thin marks, categorical hues in a fixed order (blue, orange, aqua, yellow),
an ordinal blue ramp for ordered magnitudes, and text always in ink colours.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import networkx as nx
import numpy as np
import pandas as pd
from matplotlib.patches import Circle, FancyArrowPatch, Rectangle
from scipy.spatial import cKDTree

from gtfs_ingest.analysis.dedupe import (
    DEDUPE_DISTANCE_M,
    NAME_SIM_THRESHOLD,
    WITHIN_FEED_DISTANCE_M,
)
from gtfs_ingest.analysis.route_dedupe import ROUTE_MATCH_MIN_JACCARD

# --------------------------------------------------------------------------
# Palette. Categorical slots 1-3 were run through the palette validator and
# pass every all-pairs colour-vision check; the aqua slot sits below 3:1
# contrast on the surface, so every aqua mark carries a visible text label.
# --------------------------------------------------------------------------

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
CONTEXT = "#d6d5ce"  # de-emphasised marks (background stops, templates)

BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
RAMP = {250: "#86b6ef", 400: "#3987e5", 550: "#1c5cab", 700: "#0d366b"}

LINE_W = 1.6
DPI = 160
LABEL_BOX = {"boxstyle": "square,pad=0.15", "fc": SURFACE, "ec": "none"}

plt.rcParams.update(
    {
        "font.family": ["Segoe UI", "Arial", "DejaVu Sans"],
        "font.size": 10,
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": BASELINE,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK_2,
        "axes.titlecolor": INK,
        "axes.titlesize": 12,
        "axes.titleweight": "semibold",
        "axes.titlelocation": "left",
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK_2,
        "axes.grid": False,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "grid.linestyle": "-",
        "legend.frameon": False,
        "legend.labelcolor": INK_2,
        "axes.spines.top": False,
        "axes.spines.right": False,
    }
)


def _save(fig: plt.Figure, out: Path, name: str) -> None:
    path = out / name
    fig.savefig(path, dpi=DPI, bbox_inches="tight", pad_inches=0.18)
    plt.close(fig)
    print(f"  wrote {path}")


def _title(ax: plt.Axes, title: str, subtitle: str | None = None) -> None:
    """Title plus an optional one-line subtitle, leaving room so they never overlap."""
    ax.set_title(title, pad=24 if subtitle else 10)
    if subtitle:
        ax.text(
            0,
            1.02,
            subtitle,
            transform=ax.transAxes,
            color=INK_2,
            fontsize=9,
            va="bottom",
            ha="left",
        )


def _clock(minutes: float) -> str:
    return f"{int(minutes // 60):02d}:{round(minutes % 60):02d}"


# ==========================================================================
# Explanatory figures
# ==========================================================================


def e1_headway_timeline(out: Path) -> None:
    """Departures at one stop, one window, gaps and statistics."""
    deps = [
        5 * 60 + 52,
        6 * 60 + 7,
        6 * 60 + 19,
        6 * 60 + 34,
        6 * 60 + 52,
        7 * 60 + 5,
        7 * 60 + 22,
        7 * 60 + 40,
        8 * 60 + 5,
        8 * 60 + 31,
        8 * 60 + 55,
        9 * 60 + 12,
    ]
    start, end = 6 * 60, 9 * 60
    inside = [d for d in deps if start <= d < end]
    gaps = np.diff(inside)

    fig, ax = plt.subplots(figsize=(10, 3.4))
    ax.add_patch(Rectangle((start, -0.2), end - start, 1.55, color=RAMP[250], alpha=0.16, lw=0))
    ax.text(start + 2, 1.23, "am_peak window  [06:00, 09:00)", color=INK_2, fontsize=9)
    ax.axhline(0, color=BASELINE, lw=0.8)

    for d in deps:
        on = start <= d < end
        ax.plot(
            [d, d],
            [0, 0.42],
            color=BLUE if on else CONTEXT,
            lw=LINE_W + 0.4,
            solid_capstyle="round",
        )
        ax.text(
            d,
            -0.12,
            _clock(d),
            ha="center",
            va="top",
            fontsize=7.5,
            color=INK_2 if on else MUTED,
            rotation=90,
        )
    for a, b, g in zip(inside, inside[1:], gaps):
        ax.annotate(
            "",
            xy=(b, 0.6),
            xytext=(a, 0.6),
            arrowprops={"arrowstyle": "<->", "color": INK_2, "lw": 0.8, "shrinkA": 0, "shrinkB": 0},
        )
        ax.text((a + b) / 2, 0.68, f"{g}", ha="center", fontsize=8, color=INK)

    ax.text(deps[0], 0.52, "outside\nwindow", fontsize=7.5, color=MUTED, ha="center")
    ax.text(deps[-1], 0.52, "belongs to\nmidday", fontsize=7.5, color=MUTED, ha="center")
    stats = (
        f"N = {len(inside)} departures\n→ {len(gaps)} gaps (minutes)\n\n"
        f"mean     {gaps.mean():.1f}\nmedian   {np.median(gaps):.0f}\n"
        f"min      {gaps.min()}\nmax      {gaps.max()}"
    )
    ax.text(end + 32, 0.62, stats, fontsize=9, color=INK, va="center", family="monospace")

    ax.set_xlim(deps[0] - 12, end + 100)
    ax.set_ylim(-0.75, 1.45)
    ax.axis("off")
    ax.set_title(
        "Headways at one stop: gaps between consecutive departures inside one window", pad=4
    )
    _save(fig, out, "e1_headway_timeline.png")


def e2_frequency_expansion(out: Path) -> None:
    """String-line diagram: template pattern vs synthetic runs."""
    stops = ["A", "B", "C", "D"]
    offsets = [0, 4, 9, 15]
    template_start = 7 * 60 + 30
    runs = [6 * 60 + k * 15 for k in range(4)]  # 06:00, 06:15, 06:30, 06:45

    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.grid(axis="x", zorder=0)
    y = np.arange(len(stops))
    ax.plot(
        [template_start + o for o in offsets],
        y,
        color=CONTEXT,
        lw=LINE_W,
        marker="o",
        ms=5,
        zorder=2,
    )
    ax.text(
        template_start + offsets[-1] + 3,
        y[-1] - 0.35,
        "template trip — its absolute\ntimes are only a pattern,\nso its rows are replaced",
        fontsize=8.5,
        color=INK_2,
        va="center",
    )
    for i, r in enumerate(runs):
        xs = [r + o for o in offsets]
        ax.plot(
            xs,
            y,
            color=BLUE,
            lw=LINE_W,
            marker="o",
            ms=5,
            zorder=3,
            markeredgecolor=SURFACE,
            markeredgewidth=1.2,
        )
        ax.text(r, -0.3, f"k={i}\n{_clock(r)}", ha="center", va="bottom", fontsize=8, color=INK_2)
    ax.axvline(6 * 60, color=MUTED, lw=1.0, zorder=1)
    ax.axvline(7 * 60, color=ORANGE, lw=1.4, zorder=1)
    ax.text(6 * 60 - 1.5, -1.05, "start_time 06:00", fontsize=8.5, color=INK_2, ha="right")
    ax.text(
        7 * 60 + 1.5,
        -1.05,
        "end_time 07:00 — exclusive: no run starts here",
        fontsize=8.5,
        color=INK_2,
        ha="left",
    )

    ax.set_yticks(y, [f"stop {s}  (+{o} min)" for s, o in zip(stops, offsets)])
    ax.set_ylim(3.4, -1.3)
    xt = list(range(6 * 60, 8 * 60 + 1, 15))
    ax.set_xticks(xt, [_clock(t) for t in xt])
    ax.set_xlim(5 * 60 + 35, 8 * 60 + 25)
    ax.tick_params(axis="y", length=0)
    _title(
        ax,
        "frequencies.txt expansion: one row becomes runs at start_time + k · headway_secs",
        "frequencies row: start 06:00, end 07:00, headway_secs 900 (15 min) — "
        "each run replays the template's per-stop offsets",
    )
    _save(fig, out, "e2_frequency_expansion.png")


def _side_panel(ax: plt.Axes, reversed_way: bool) -> None:
    ax.add_patch(Rectangle((0, -2.2), 100, 4.4, color=CONTEXT, alpha=0.35, lw=0))
    for x0 in (0, 90):
        ax.add_patch(Rectangle((x0, -26), 10, 52, color=RAMP[250], alpha=0.14, lw=0))
    ax.text(
        5, 22, "≤ 10 m from\na way end\n→ low", ha="center", va="center", fontsize=7.5, color=INK_2
    )
    ax.plot([0, 100], [0, 0], color=MUTED, lw=1.0)
    tail, head = ((100.0, 0.0), (0.0, 0.0)) if reversed_way else ((0.0, 0.0), (100.0, 0.0))
    ax.add_patch(
        FancyArrowPatch(tail, head, arrowstyle="-|>", mutation_scale=14, color=INK, lw=1.4)
    )
    label = (
        "travel direction d  (oneway = −1: reversed)"
        if reversed_way
        else "travel direction d  (digitized order)"
    )
    ax.text(28, -5.4, label, ha="center", va="top", fontsize=8.5, color=INK)
    ax.text(
        13,
        -14,
        "≤ 1 m from the\ncentreline → “on”",
        ha="left",
        va="center",
        fontsize=7.5,
        color=INK_2,
    )

    d = np.subtract(head, tail) / 100.0
    for (px, py), name in (((50, 14), "P"), ((68, -13), "Q")):
        v = np.array([0.0, py])
        cross = d[0] * v[1] - d[1] * v[0]
        side = "left" if cross > 0 else "right"
        colour = BLUE if side == "left" else ORANGE
        ax.plot([px, px], [0, py], color=colour, lw=1.2)
        ax.plot(
            px, py, "o", ms=8, color=colour, markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=4
        )
        ax.plot(px, 0, "o", ms=4, color=INK_2, zorder=4)
        ax.text(
            px + 2.5,
            py,
            f"{name}: v = (0, {py:+.0f})\ncross = {cross:+.0f} → {side}",
            fontsize=8.5,
            color=INK,
            va="center",
        )
    ax.set_xlim(-4, 108)
    ax.set_ylim(-27, 27)
    ax.set_aspect("equal")
    ax.axis("off")


def e3_side_of_street(out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 3.8))
    _side_panel(axes[0], reversed_way=False)
    _side_panel(axes[1], reversed_way=True)
    axes[0].set_title("Side of street: the sign of cross = d.x·v.y − d.y·v.x", pad=6)
    axes[1].set_title("Same stops with oneway = −1: the answer flips", pad=6)
    _save(fig, out, "e3_side_of_street.png")


def e4_osm_confidence(out: Path) -> None:
    threshold = 50.0
    d = np.linspace(0, 60, 400)
    fig, ax = plt.subplots(figsize=(8.8, 4.2))
    ax.grid(axis="y", zorder=0)
    ax.add_patch(Rectangle((0, 0), 60, 0.5, color=ORANGE, alpha=0.07, lw=0, zorder=0))
    ax.add_patch(Rectangle((threshold, 0.5), 10, 0.58, color=ORANGE, alpha=0.07, lw=0, zorder=0))
    ax.text(1, 0.03, "needs_review: confidence < 0.5", fontsize=8.5, color=INK_2, va="bottom")
    ax.text(
        threshold + 0.8, 1.02, "needs_review:\ndistance > 50 m", fontsize=8.5, color=INK_2, va="top"
    )
    cases = [
        (1.0, 700, "type_score 1.0 — types agree (station↔station, platform↔platform-ish)"),
        (0.8, 550, "0.8 — platform stop ↔ station node"),
        (0.7, 400, "0.7 — otherwise / unknown kind"),
        (0.6, 250, "0.6 — station stop ↔ non-station node"),
    ]
    for ts, step, text in cases:
        conf = np.where(d <= threshold, np.maximum(0, 1 - d / threshold) * ts, np.nan)
        ax.plot(d, conf, color=RAMP[step], lw=LINE_W, zorder=3)
        ax.text(
            0.8, ts + 0.012, text, fontsize=8, color=INK_2, va="bottom", bbox=LABEL_BOX, zorder=4
        )
    ax.set_xlim(0, 60)
    ax.set_ylim(0, 1.08)
    ax.set_xlabel("distance from the stop to the nearest transit-tagged OSM node (m)")
    ax.set_ylabel("confidence")
    _title(ax, "OSM match confidence = max(0, 1 − distance / 50) × type_score")
    _save(fig, out, "e4_osm_confidence.png")


# ==========================================================================
# Measured figures
# ==========================================================================


class Run:
    """Outputs of one run directory, loaded once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.report = json.loads((path / "analysis_report.json").read_text(encoding="utf-8"))
        self.stops = gpd.read_file(path / "transit_analysis.gpkg", layer="stops")
        self.quality = pd.read_csv(path / "near_stations_quality.csv", keep_default_na=False)
        self.equiv = pd.read_csv(path / "stop_equivalences.csv")
        self.canon = dict(
            zip(
                self.quality["stop_uid"],
                np.where(
                    self.quality["station_dedup"] != "",
                    self.quality["station_dedup"],
                    self.quality["stop_uid"],
                ),
            )
        )
        self.metric = self.stops.to_crs(self.stops.estimate_utm_crs())
        self.xy = np.column_stack([self.metric.geometry.x, self.metric.geometry.y])
        self._links: pd.DataFrame | None = None

    @property
    def links(self) -> pd.DataFrame:
        if self._links is None:
            self._links = gpd.read_file(
                self.path / "transit_analysis.gpkg", layer="near_links", ignore_geometry=True
            )
        return self._links


def _hbar_axes(ax: plt.Axes) -> None:
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)


def r1_records_to_stations(run: Run, out: Path) -> None:
    raw = len(run.stops)
    cross = run.equiv[run.equiv["match_kind"] == "cross_feed"]
    g = nx.Graph()
    g.add_nodes_from(run.stops["stop_uid"])
    g.add_edges_from(zip(cross["stop_uid_a"], cross["stop_uid_b"]))
    rows = [
        ("Stop records", raw, RAMP[250]),
        ("After cross-feed dedupe", nx.number_connected_components(g), RAMP[400]),
        ("After within-feed dedupe", len(set(run.canon.values())), RAMP[700]),
    ]

    fig, ax = plt.subplots(figsize=(8.8, 2.3))
    ax.grid(axis="x", zorder=0)
    for i, (_label, value, colour) in enumerate(rows[::-1]):
        ax.barh(i, value, height=0.34, color=colour, zorder=3)
        ax.text(value + raw * 0.012, i, f"{value:,}", va="center", fontsize=9.5, color=INK)
    ax.set_yticks(range(3), [r[0] for r in rows[::-1]])
    ax.set_xlim(0, raw * 1.13)
    ax.xaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    _hbar_axes(ax)
    final = rows[-1][1]
    _title(
        ax,
        "Records are not stations",
        f"{raw - final:,} of {raw:,} records ({(raw - final) / raw:.0%}) describe a station "
        "that is already counted",
    )
    _save(fig, out, "r1_records_to_stations.png")


def r2_dedupe_distances(run: Run, out: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(9, 5.4), sharex=True, gridspec_kw={"hspace": 0.62})
    panels = (
        (
            "cross_feed",
            DEDUPE_DISTANCE_M,
            BLUE,
            f"name similarity ≥ {NAME_SIM_THRESHOLD:g} OR a shared route",
        ),
        (
            "within_feed",
            WITHIN_FEED_DISTANCE_M,
            ORANGE,
            "a shared route ONLY — names are not evidence",
        ),
    )
    for ax, (kind, radius, colour, rule) in zip(axes, panels):
        sub = run.equiv.loc[run.equiv["match_kind"] == kind, "distance_m"]
        ax.grid(axis="y", zorder=0)
        ax.hist(
            sub, bins=np.arange(0, DEDUPE_DISTANCE_M + 2, 1), color=colour, rwidth=0.82, zorder=3
        )
        ax.axvline(radius, color=INK_2, lw=1.0)
        ax.text(
            radius + 0.4,
            ax.get_ylim()[1] * 0.95,
            f"radius {radius:g} m",
            fontsize=8.5,
            color=INK_2,
            va="top",
            ha="left",
            bbox=LABEL_BOX,
        )
        ax.set_ylabel("pairs")
        ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
        _title(
            ax,
            f"{kind.replace('_', '-')}  ·  {len(sub):,} pairs  ·  median {sub.median():.1f} m",
            f"accepted on {rule}",
        )
        ax.title.set_fontsize(10.5)
    axes[1].set_xlabel("distance between the two stop records (m)")
    _save(fig, out, "r2_dedupe_distances.png")


def r3_duplicate_cluster_map(run: Run, out: Path, name: str = "Capuchina") -> None:
    """Every record of one real station area and the pairs that merged them."""
    stops = run.metric.assign(
        x=run.xy[:, 0], y=run.xy[:, 1], canon=run.metric["stop_uid"].map(run.canon)
    )
    named = stops[stops["station"].str.contains(name, case=False, na=False)]
    if named.empty:
        print(f"  skipped r3: no stop named {name!r}")
        return
    # The name recurs across the city; anchor on its largest duplicate group.
    group = named["canon"].value_counts().index[0]
    centre = stops.loc[stops["canon"] == group, ["x", "y"]].mean().to_numpy()
    near = stops[np.hypot(stops["x"] - centre[0], stops["y"] - centre[1]) <= 90].copy()
    near["px"] = (near["x"] - centre[0]).round(0)
    near["py"] = (near["y"] - centre[1]).round(0)

    # Records at identical coordinates would hide each other (and draw their
    # merge pair as a zero-length line): fan each stack out on a small circle.
    fan = 3.0
    drawn: dict[str, tuple[float, float]] = {}
    stacks = list(near.groupby(["px", "py"]))
    for (x, y), stack in stacks:
        uids = sorted(stack["stop_uid"])
        for k, uid in enumerate(uids):
            if len(uids) == 1:
                drawn[uid] = (x, y)
            else:
                a = 2 * np.pi * k / len(uids) + np.pi / 2
                drawn[uid] = (x + fan * np.cos(a), y + fan * np.sin(a))
    uids = set(near["stop_uid"])
    pairs = run.equiv[run.equiv["stop_uid_a"].isin(uids) & run.equiv["stop_uid_b"].isin(uids)]

    fig, ax = plt.subplots(figsize=(10, 6.2))
    ax.grid(zorder=0)
    for kind, colour in (("cross_feed", BLUE), ("within_feed", ORANGE)):
        sub = pairs[pairs["match_kind"] == kind]
        for a, b in zip(sub["stop_uid_a"], sub["stop_uid_b"]):
            ax.plot(
                [drawn[a][0], drawn[b][0]],
                [drawn[a][1], drawn[b][1]],
                color=colour,
                lw=LINE_W,
                zorder=2,
                solid_capstyle="round",
            )
    marker = {
        "improved_gtfs_mimacro_mitren_sitren": "o",
        "mdb_2366_202502241922": "s",
        "mibici": "D",
    }
    for _, r in near.iterrows():
        ax.plot(
            *drawn[r["stop_uid"]],
            marker.get(r["feed_id"], "o"),
            ms=8,
            color=INK_2,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
            zorder=4,
        )

    # One label per neighbourhood of stacks, listing every record with its feed.
    tag = {"improved_gtfs_mimacro_mitren_sitren": "improved", "mdb_2366_202502241922": "mdb"}
    placed: list[np.ndarray] = []
    for (x, y), _stack in sorted(stacks, key=lambda kv: (kv[0][1], kv[0][0]), reverse=True):
        members = near[np.hypot(near["px"] - x, near["py"] - y) <= 8]
        centre_xy = np.array([members["px"].mean(), members["py"].mean()])
        if any(np.hypot(*(centre_xy - q)) < 8 for q in placed):
            continue
        placed.append(centre_xy)
        text = "\n".join(
            f"{tag.get(f, f)}: {sid.replace('2215-', '')}"
            for f, sid in sorted(zip(members["feed_id"], members["stop_id"]))
        )
        left = centre_xy[0] < near["px"].mean()
        ax.annotate(
            text,
            centre_xy,
            xytext=(-16 if left else 16, 0),
            textcoords="offset points",
            ha="right" if left else "left",
            va="center",
            fontsize=7.5,
            color=INK,
            bbox=LABEL_BOX,
            zorder=5,
            arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.7, "shrinkA": 0, "shrinkB": 6},
        )

    ax.plot([], [], "-", color=BLUE, lw=LINE_W, label="cross-feed pair (name or route)")
    ax.plot([], [], "-", color=ORANGE, lw=LINE_W, label="within-feed pair (shared route)")
    ax.plot([], [], "o", color=INK_2, ms=8, label="record from the improved feed")
    ax.plot([], [], "s", color=INK_2, ms=8, label="record from the mdb feed")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8.5)
    ax.set_aspect("equal")
    pad = 38
    ax.set_xlim(near["px"].min() - pad * 2.2, near["px"].max() + pad * 2.2)
    ax.set_ylim(near["py"].min() - pad * 0.6, near["py"].max() + pad * 0.6)
    ax.set_xlabel("metres east of the group centre")
    ax.set_ylabel("metres north")
    n_groups = len({run.canon[u] for u in uids})
    _title(
        ax,
        f"One place, many records: the “{name}” station area",
        f"{len(near)} stop records within 90 m · {len(pairs)} accepted pairs · {n_groups} physical "
        f"station groups · records at identical coordinates are fanned out {fan:.0f} m to stay visible",
    )
    _save(fig, out, "r3_duplicate_cluster_map.png")


def _angular_spread(points: np.ndarray) -> float:
    angles = np.sort(np.arctan2(points[:, 1], points[:, 0]))
    gaps = np.diff(np.concatenate([angles, angles[:1] + 2 * np.pi]))
    return float(gaps.min())


def r4_near_stations_example(run: Run, out: Path) -> None:
    """A real MiBici dock, its neighbourhood, and the rendered tuples."""
    links = run.links
    idx = {u: i for i, u in enumerate(run.stops["stop_uid"])}
    stops = run.stops.set_index("stop_uid")
    bikes = set(run.stops.loc[run.stops["feed_id"] == "mibici", "stop_uid"])
    deg = pd.concat([links["stop_uid_a"], links["stop_uid_b"]]).value_counts()
    candidates = [u for u in deg.index if u in bikes and 5 <= deg[u] <= 7]
    best, best_score = None, -1.0
    for uid in sorted(candidates):
        mine = links[(links["stop_uid_a"] == uid) | (links["stop_uid_b"] == uid)]
        other = np.where(mine["stop_uid_a"] == uid, mine["stop_uid_b"], mine["stop_uid_a"])
        pts = run.xy[[idx[o] for o in other]] - run.xy[idx[uid]]
        if np.any(np.hypot(pts[:, 0], pts[:, 1]) < 40):
            continue  # co-located neighbours make labels collide
        score = _angular_spread(pts)
        if score > best_score:
            best, best_score = uid, score
    if best is None:
        print("  skipped r4: no suitable MiBici dock")
        return
    mine = links[(links["stop_uid_a"] == best) | (links["stop_uid_b"] == best)].copy()
    mine["other"] = np.where(mine["stop_uid_a"] == best, mine["stop_uid_b"], mine["stop_uid_a"])
    prox = float(run.report["proximity_m"])
    origin = run.xy[idx[best]]

    fig, ax = plt.subplots(figsize=(8.6, 7.6))
    ax.add_patch(Circle((0, 0), prox, fill=False, color=BASELINE, lw=1.0))
    ax.text(
        0,
        -prox - 12,
        f"--proximity-m {prox:.0f}: straight-line candidate radius",
        ha="center",
        va="top",
        fontsize=8.5,
        color=INK_2,
    )
    for _, r in mine.iterrows():
        p = run.xy[idx[r["other"]]] - origin
        colour = AQUA if r["other"] in bikes else BLUE
        ax.plot([0, p[0]], [0, p[1]], color=colour, lw=LINE_W, zorder=2)
        ax.plot(*p, "o", ms=9, color=colour, markeredgecolor=SURFACE, markeredgewidth=1.6, zorder=4)
        unit = p / max(np.hypot(*p), 1e-9)
        ax.annotate(
            f"{stops.loc[r['other'], 'station'][:30]}\n"
            f"{r['walk_min']:.0f} min · {r['distance_m']:.0f} m walked",
            p,
            xytext=(unit[0] * 12, unit[1] * 12),
            textcoords="offset points",
            ha="left" if unit[0] >= 0 else "right",
            va="center",
            fontsize=7.5,
            color=INK,
            bbox=LABEL_BOX,
            zorder=5,
        )
    ax.plot(0, 0, "D", ms=11, color=AQUA, markeredgecolor=INK, markeredgewidth=1.0, zorder=5)
    ax.annotate(
        stops.loc[best, "station"][:44],
        (0, 0),
        xytext=(0, -18),
        textcoords="offset points",
        ha="center",
        fontsize=9,
        fontweight="semibold",
        color=INK,
        bbox=LABEL_BOX,
        zorder=6,
    )
    ax.plot([], [], "o", color=BLUE, ms=8, label="transit stop neighbour")
    ax.plot([], [], "o", color=AQUA, ms=8, label="MiBici dock neighbour")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8.5)
    ax.set_xlim(-prox * 1.6, prox * 1.6)
    ax.set_ylim(-prox * 1.3, prox * 1.3)
    ax.set_aspect("equal")
    ax.grid(zorder=0)
    ax.set_xlabel("metres east")
    ax.set_ylabel("metres north")
    _title(
        ax,
        "near_stations for one real MiBici dock",
        "walk minutes = max(1, round(walked metres / 83)) · a routed walk can exceed the "
        "straight-line radius",
    )
    _save(fig, out, "r4_near_stations_example.png")


def r5_detour_distribution(run: Run, out: Path) -> None:
    links = run.links
    idx = {u: i for i, u in enumerate(run.stops["stop_uid"])}
    ia = links["stop_uid_a"].map(idx).to_numpy()
    ib = links["stop_uid_b"].map(idx).to_numpy()
    straight = np.hypot(run.xy[ia, 0] - run.xy[ib, 0], run.xy[ia, 1] - run.xy[ib, 1])
    ok = straight >= 5  # near-co-located pairs have no meaningful ratio
    ratio = np.clip(links["distance_m"].to_numpy()[ok] / straight[ok], 1.0, None)

    fig, ax = plt.subplots(figsize=(9, 4.1))
    ax.grid(axis="y", zorder=0)
    ax.hist(
        np.clip(ratio, 1.0, 4.0), bins=np.arange(1.0, 4.05, 0.05), color=BLUE, rwidth=0.85, zorder=3
    )
    mean, median = ratio.mean(), np.median(ratio)
    top = ax.get_ylim()[1] * 1.14
    ax.set_ylim(0, top)
    for value, label, dx, ha in (
        (median, f"median {median:.2f}×", -0.03, "right"),
        (mean, f"mean {mean:.2f}×", 0.03, "left"),
    ):
        ax.axvline(value, color=INK, lw=1.0, zorder=4)
        ax.text(value + dx, top * 0.955, label, fontsize=9, color=INK, ha=ha, va="top")
    over = int((ratio > 4).sum())
    ax.annotate(
        f"{over:,} pairs above 4×\n(drawn in the last bar)",
        (3.98, 1900),
        xytext=(3.55, top * 0.45),
        fontsize=8.5,
        color=INK_2,
        ha="center",
        arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.8},
    )
    ax.set_xlabel("walking distance on OSM streets ÷ straight-line distance")
    ax.set_ylabel("stop pairs")
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    _title(
        ax,
        "How much longer is the real walk?",
        f"{ok.sum():,} near pairs at least 5 m apart · pairs with no street path count as 1.0× · "
        "the long right tail pulls the mean above the median",
    )
    _save(fig, out, "r5_detour_distribution.png")


def _nearest_other_station(run: Run) -> np.ndarray:
    """Straight-line distance from each record to the nearest DIFFERENT physical station."""
    canon = np.array([run.canon[u] for u in run.stops["stop_uid"]])
    tree = cKDTree(run.xy)
    n = len(run.xy)
    result = np.full(n, np.inf)
    todo = np.arange(n)
    k = 16
    while len(todo) and k <= 4096:
        dist, nbr = tree.query(run.xy[todo], k=min(k, n))
        for row, i in enumerate(todo):
            for dd, j in zip(dist[row], nbr[row]):
                if j != i and canon[j] != canon[i]:
                    result[i] = dd
                    break
        todo = todo[~np.isfinite(result[todo])]
        k *= 4
    return result


def r6_r7_proximity(run: Run, out: Path) -> None:
    d = _nearest_other_station(run)
    is_bike = (run.stops["feed_id"] == "mibici").to_numpy()
    for t in (150, 300):
        print(f"  check: records with no other station within {t} m: {(d > t).sum():,}")

    # r6 - share of records with a neighbour, as a function of the threshold
    t = np.arange(50, 651, 5)
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.grid(zorder=0)
    series = (("transit stop records", ~is_bike, BLUE), ("MiBici docks", is_bike, AQUA))
    for label, mask, colour in series:
        ax.plot(
            t, [(d[mask] <= x).mean() for x in t], color=colour, lw=LINE_W, zorder=3, label=label
        )
    for x in (150, 300):
        ax.axvline(x, color=BASELINE, lw=1.0, zorder=1)
        ax.text(x, 1.035, f"{x} m", ha="center", fontsize=8.5, color=INK_2)
        for (_label, mask, colour), (dx, ha, _dy) in zip(
            series, ((-9, "right", 0.0), (9, "left", -0.05))
        ):
            s = (d[mask] <= x).mean()
            ax.plot(
                x,
                s,
                "o",
                ms=7,
                color=colour,
                markeredgecolor=SURFACE,
                markeredgewidth=1.5,
                zorder=4,
            )
            ax.annotate(
                f"{s:.0%}",
                (x, s),
                xytext=(dx, 0),
                textcoords="offset points",
                ha=ha,
                va="center",
                fontsize=8.5,
                color=INK,
                bbox=LABEL_BOX,
                zorder=5,
            )
    ax.legend(loc="lower right", fontsize=9)
    ax.set_xlim(50, 650)
    ax.set_ylim(0, 1.07)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set_xlabel("--proximity-m (straight-line metres)")
    ax.set_ylabel("records with at least one neighbour")
    _title(
        ax,
        "Choosing the proximity threshold",
        "a neighbour is a stop of a DIFFERENT physical station — a record's own duplicates don't count",
    )
    _save(fig, out, "r6_proximity_threshold.png")

    # r7 - where the isolated records are
    x0, y0 = run.xy[:, 0].min(), run.xy[:, 1].min()
    X, Y = (run.xy[:, 0] - x0) / 1000, (run.xy[:, 1] - y0) / 1000
    fixed = (d > 150) & (d <= 300)
    still = d > 300
    fig, ax = plt.subplots(figsize=(8.4, 8.4))
    ax.scatter(X, Y, s=1.2, color=CONTEXT, lw=0, zorder=1, label="has a neighbour at 150 m")
    ax.scatter(
        X[fixed],
        Y[fixed],
        s=9,
        color=BLUE,
        lw=0,
        zorder=2,
        label=f"isolated at 150 m, connected at 300 m  ({fixed.sum():,})",
    )
    ax.scatter(
        X[still],
        Y[still],
        s=14,
        color=ORANGE,
        lw=0,
        zorder=3,
        label=f"still isolated at 300 m  ({still.sum():,})",
    )
    ax.set_aspect("equal")
    ax.set_xlabel("km")
    ax.set_ylabel("km")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=1, fontsize=9, markerscale=2.2)
    _title(
        ax,
        "Where the isolated stop records are",
        "what remains at 300 m lies mostly along outer radial corridors, where stops are far apart",
    )
    _save(fig, out, "r7_isolation_map.png")


def r8_headways_by_window(run: Run, out: Path) -> None:
    rh = pd.read_csv(run.path / "route_headways.csv").dropna(subset=["mean_headway_s"])
    rh["minutes"] = rh["mean_headway_s"] / 60
    order = ["am_peak", "midday", "pm_peak", "evening"]
    colours = [BLUE, ORANGE, AQUA, YELLOW]
    medians = {w: rh.loc[rh["time_window"] == w, "minutes"].median() for w in order}

    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.grid(zorder=0)
    for name, colour in zip(order, colours):
        v = np.sort(rh.loc[rh["time_window"] == name, "minutes"].to_numpy())
        y = np.arange(1, len(v) + 1) / len(v)
        ax.step(
            np.minimum(v, 60),
            y,
            where="post",
            color=colour,
            lw=LINE_W,
            zorder=3,
            label=f"{name}  —  median {medians[name]:.1f} min, {len(v):,} route-directions",
        )
    ax.axvline(15, color=BASELINE, lw=1.0, zorder=1)
    ax.text(
        15.6,
        0.34,
        "a step at 15 min:\nmany routes run\nexactly every 15 min",
        fontsize=8.5,
        color=INK_2,
        bbox=LABEL_BOX,
    )
    ax.legend(loc="lower right", fontsize=8.5)
    ax.set_xlim(0, 60)
    ax.set_ylim(0, 1.02)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set_xlabel("route mean headway (minutes; values above 60 are drawn at 60)")
    ax.set_ylabel("share of route-directions")
    low, high = min(medians.values()), max(medians.values())
    spread = (
        f"every median is {low:.1f} min"
        if low == high
        else f"medians span {low:.1f}–{high:.1f} min"
    )
    _title(
        ax,
        "Service frequency barely changes through the day",
        f"cumulative distribution per window — the four curves overlap; {spread}",
    )
    _save(fig, out, "r8_headways_by_window.png")


def r9_interchange_modes(run: Run, out: Path) -> None:
    report = json.loads((run.path / "interchanges_report.json").read_text(encoding="utf-8"))
    combos = sorted(report["mode_combinations"].items(), key=lambda kv: kv[1])
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.grid(axis="x", zorder=0)
    for i, (key, value) in enumerate(combos):
        ax.barh(i, value, height=0.42, color=BLUE if "+" in key else RAMP[250], zorder=3)
        ax.text(value * 1.18, i, f"{value:,}", va="center", fontsize=9, color=INK)
    ax.set_yticks(
        range(len(combos)),
        ["no route modes (unserved / MiBici-only)" if k == "(unknown)" else k for k, _ in combos],
    )
    ax.set_xscale("log")
    ax.set_xlim(0.8, max(v for _, v in combos) * 5)
    ax.xaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    _hbar_axes(ax)
    _title(
        ax,
        f"Interchange clusters by mode combination  ({report['clusters_total']:,} clusters)",
        f"darker bars are multimodal ({report['multimodal_count']} clusters) · logarithmic scale",
    )
    _save(fig, out, "r9_interchange_modes.png")


def r10_osm_match_outcomes(run: Run, out: Path) -> None:
    q = run.quality.copy()
    q["feed"] = q["stop_uid"].str.split(":", n=1).str[0]
    score = pd.to_numeric(q["coord_confidence"], errors="coerce")
    q["bucket"] = np.select(
        [
            score >= 0.8,
            score >= 0.5,
            q["coord_confidence"] == "needs_review",
            q["coord_confidence"] == "no_match",
        ],
        ["matched, confidence ≥ 0.8", "matched, 0.5–0.8", "needs review", "no match"],
        default="unavailable",
    )
    buckets = [
        b
        for b in ["matched, confidence ≥ 0.8", "matched, 0.5–0.8", "needs review", "no match"]
        if (q["bucket"] == b).any()
    ]
    palette = {
        "matched, confidence ≥ 0.8": RAMP[700],
        "matched, 0.5–0.8": RAMP[400],
        "needs review": RAMP[250],
        "no match": CONTEXT,
    }
    feeds = [
        ("improved_gtfs_mimacro_mitren_sitren", "improved feed"),
        ("mdb_2366_202502241922", "mdb feed"),
        ("mibici", "MiBici (bike docks)"),
    ]

    fig, ax = plt.subplots(figsize=(9.4, 2.6))
    for row, (fid, _) in enumerate(feeds[::-1]):
        sub = q[q["feed"] == fid]
        left = 0.0
        for b in buckets:
            share = (sub["bucket"] == b).mean()
            if share <= 0:
                continue
            ax.barh(row, share - 0.004, left=left + 0.002, height=0.36, color=palette[b], zorder=3)
            if share > 0.07:
                ink = SURFACE if palette[b] in (RAMP[700], RAMP[400]) else INK
                ax.text(
                    left + share / 2,
                    row,
                    f"{share:.0%}",
                    ha="center",
                    va="center",
                    fontsize=8.5,
                    color=ink,
                    zorder=4,
                )
            left += share
        ax.text(1.012, row, f"{len(sub):,} stops", va="center", fontsize=8.5, color=INK_2)
    ax.set_yticks(range(len(feeds)), [f[1] for f in feeds[::-1]])
    ax.set_xlim(0, 1.12)
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    _hbar_axes(ax)
    handles = [Rectangle((0, 0), 1, 1, color=palette[b]) for b in buckets]
    ax.legend(
        handles,
        buckets,
        ncol=len(buckets),
        loc="upper left",
        bbox_to_anchor=(0, -0.2),
        fontsize=8.5,
    )
    _title(
        ax,
        "OSM coordinate confidence by source",
        "MiBici is mostly “needs review” because bike docks are tagged amenity=bicycle_rental, "
        "which the transit matcher never searches",
    )
    _save(fig, out, "r10_osm_match_outcomes.png")


def _feed_label(route_uid: str) -> str:
    return route_uid.split(":", 1)[1]


def r11_route_jaccard(run: Run, out: Path) -> None:
    """Stop overlap of every cross-feed route pair that shares a station."""
    route_stops = pd.read_csv(run.path / "route_stops.csv", dtype=str)
    route_stops["feed"] = route_stops["route_uid"].str.split(":", n=1).str[0]
    route_stops["station"] = route_stops["stop_uid"].map(run.canon).fillna(route_stops["stop_uid"])
    stations = route_stops.drop_duplicates(["route_uid", "station"])
    sizes = stations.groupby("route_uid").size()
    joined = stations.merge(stations, on="station", suffixes=("_a", "_b"))
    joined = joined[(joined["feed_a"] < joined["feed_b"])]
    pairs = joined.groupby(["route_uid_a", "route_uid_b"]).size().rename("shared").reset_index()
    union = pairs["route_uid_a"].map(sizes) + pairs["route_uid_b"].map(sizes) - pairs["shared"]
    pairs["jaccard"] = pairs["shared"] / union
    matched = pd.read_csv(run.path / "route_equivalences.csv")
    is_match = pairs.set_index(["route_uid_a", "route_uid_b"]).index.isin(
        list(zip(matched["dropped_route_uid"], matched["kept_route_uid"]))
    ) | pairs.set_index(["route_uid_a", "route_uid_b"]).index.isin(
        list(zip(matched["kept_route_uid"], matched["dropped_route_uid"]))
    )

    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.grid(axis="y", zorder=0)
    bins = np.arange(0, 1.0001, 0.025)
    ax.hist(pairs.loc[~is_match, "jaccard"], bins=bins, color=CONTEXT, rwidth=0.85, zorder=3)
    ax.hist(pairs.loc[is_match, "jaccard"], bins=bins, color=BLUE, rwidth=0.85, zorder=4)
    ax.set_yscale("log")
    ax.set_ylim(bottom=0.7)  # keep single pairs visible
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    ax.axvline(ROUTE_MATCH_MIN_JACCARD, color=INK_2, lw=1.0)
    ax.text(
        ROUTE_MATCH_MIN_JACCARD - 0.01,
        ax.get_ylim()[1] * 0.6,
        f"match ≥ {ROUTE_MATCH_MIN_JACCARD:g}",
        fontsize=8.5,
        color=INK_2,
        ha="right",
        va="top",
        bbox=LABEL_BOX,
    )
    best_other = pairs.loc[~is_match, "jaccard"].max()
    ax.set_xlabel("Jaccard index of the two routes' stations (shared ÷ union)")
    ax.set_ylabel("route pairs")
    _title(
        ax,
        f"The same line in two feeds: {is_match.sum()} matches, a clean gap",
        f"{len(pairs):,} cross-feed route pairs share a station · matches (blue) score ≥ "
        f"{pairs.loc[is_match, 'jaccard'].min():.2f}, every other pair ≤ {best_other:.2f}",
    )
    _save(fig, out, "r11_route_jaccard.png")


def r12_direction_fix(run: Run, before: Path, out: Path) -> None:
    """AM-peak mean headway of the relabelled routes, before and after the direction fix."""
    fixes = pd.read_csv(run.path / "direction_fixes.csv", dtype={"route_id": str})
    fixes["route_uid"] = fixes["feed_id"] + ":" + fixes["route_id"]
    meta = pd.read_csv(run.path / "routes.csv", dtype=str)
    # Twins left out by route dedupe would repeat the same line.
    routing = set(meta.loc[meta["duplicate_of"].isna(), "route_uid"])
    routes = sorted(uid for uid in fixes["route_uid"].unique() if uid in routing)

    def peak(path: Path) -> pd.DataFrame:
        rollup = pd.read_csv(path / "route_headways.csv", dtype={"route_id": str})
        rollup["route_uid"] = rollup["feed_id"] + ":" + rollup["route_id"]
        return rollup[(rollup["time_window"] == "am_peak") & rollup["route_uid"].isin(routes)]

    old, new = peak(before), peak(run.path)
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(routes) + 1.4))
    ax.grid(axis="x", zorder=0)
    for i, route in enumerate(routes):
        mixed = old.loc[old["route_uid"] == route, "mean_headway_s"] / 60
        fixed = new[new["route_uid"] == route].sort_values("direction_id")
        ax.scatter(mixed, [i] * len(mixed), s=36, color=MUTED, zorder=3)
        for (_, row), colour in zip(fixed.iterrows(), (BLUE, ORANGE)):
            ax.scatter(row["mean_headway_s"] / 60, i, s=36, color=colour, zorder=4)
        if len(mixed):
            ax.plot(
                [mixed.iloc[0], fixed["mean_headway_s"].max() / 60],
                [i, i],
                color=CONTEXT,
                lw=1.2,
                zorder=2,
            )
    ax.set_yticks(range(len(routes)), [_feed_label(uid) for uid in routes])
    ax.invert_yaxis()
    ax.set_xlim(left=0)
    ax.set_xlabel("am_peak mean headway (minutes)")
    ax.scatter([], [], color=MUTED, label="before: both senses under one direction_id")
    ax.scatter([], [], color=BLUE, label="after: direction 0")
    ax.scatter([], [], color=ORANGE, label="after: direction 1")
    ax.legend(loc="lower right", fontsize=8.5)
    _title(
        ax,
        "Mixed directions understate the headway",
        f"{len(routes)} routes relabelled from stop order (route-dedupe twins left out) · "
        "each direction now shows its own headway",
    )
    _save(fig, out, "r12_direction_fix.png")


def r13_timetable_by_hour(run: Run, out: Path) -> None:
    """Vehicle runs starting per hour, from connections.csv.gz."""
    conn = pd.read_csv(run.path / "connections.csv.gz", usecols=["trip_id", "dep_s"])
    starts = conn.groupby("trip_id")["dep_s"].min() // 3600
    per_hour = starts.value_counts().sort_index()
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.grid(axis="y", zorder=0)
    ax.bar(per_hour.index, per_hour.to_numpy(), width=0.8, color=BLUE, zorder=3)
    ax.set_xticks(range(0, int(per_hour.index.max()) + 1, 2))
    ax.set_xlabel("hour of the service day (24 and later = after midnight)")
    ax.set_ylabel("runs starting")
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    _title(
        ax,
        f"The timetable: {len(conn):,} connections in {len(starts):,} runs",
        "frequencies.txt expanded into one run per headway · every run is a sequence of "
        "stop-to-stop connections",
    )
    _save(fig, out, "r13_timetable_by_hour.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, default=Path("outputs/out_mibici_v12"))
    parser.add_argument(
        "--before",
        type=Path,
        default=None,
        help="a run made before the direction fix (for r12; skipped when absent)",
    )
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    print("explanatory figures")
    e1_headway_timeline(args.out)
    e2_frequency_expansion(args.out)
    e3_side_of_street(args.out)
    e4_osm_confidence(args.out)

    print(f"measured figures from {args.run}")
    run = Run(args.run)
    r1_records_to_stations(run, args.out)
    r2_dedupe_distances(run, args.out)
    r3_duplicate_cluster_map(run, args.out)
    r4_near_stations_example(run, args.out)
    r5_detour_distribution(run, args.out)
    r6_r7_proximity(run, args.out)
    r8_headways_by_window(run, args.out)
    r9_interchange_modes(run, args.out)
    r10_osm_match_outcomes(run, args.out)
    r11_route_jaccard(run, args.out)
    if args.before is not None:
        r12_direction_fix(run, args.before, args.out)
    r13_timetable_by_hour(run, args.out)


if __name__ == "__main__":
    main()
