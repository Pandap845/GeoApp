"""Runtime settings and logging setup.

Precedence: CLI flags > environment variables > .env file > defaults.
The database URL only ever comes from --db-url or DATABASE_URL.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pipeline configuration (database, inputs, report folder, logging, OSM tables)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Each alias list includes the field name so Settings(database_url=...) still works.
    database_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("database_url", "DATABASE_URL", "GTFS_DATABASE_URL"),
    )
    feed_paths: list[Path] = Field(
        default_factory=list,
        validation_alias=AliasChoices("feed_paths", "GTFS_FEED_PATHS"),
    )
    report_dir: Path = Field(
        default=Path("out"),
        validation_alias=AliasChoices("report_dir", "GTFS_REPORT_DIR"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("log_level", "GTFS_LOG_LEVEL"),
    )
    analysis_date: Optional[date] = Field(
        default=None,
        validation_alias=AliasChoices("analysis_date", "GTFS_ANALYSIS_DATE"),
        description="Reference date for expiry/staleness judgments (the "
        "'analysis clock'); default: today, resolved once per run.",
    )
    dry_run: bool = False

    # OSM tables (osm2pgsql classic defaults)
    osm_line_table: str = Field(
        default="planet_osm_line",
        validation_alias=AliasChoices("osm_line_table", "GTFS_OSM_LINE_TABLE"),
    )
    osm_point_table: str = Field(
        default="planet_osm_point",
        validation_alias=AliasChoices("osm_point_table", "GTFS_OSM_POINT_TABLE"),
    )
    osm_geom_column: str = Field(
        default="way",
        validation_alias=AliasChoices("osm_geom_column", "GTFS_OSM_GEOM_COLUMN"),
    )
    metric_crs: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("metric_crs", "GTFS_METRIC_CRS"),
        description="Metric CRS like 'EPSG:32618'; default auto-detects a UTM zone.",
    )

    # Google QA key: environment only, never a CLI flag.
    google_maps_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_MAPS_API_KEY", "GTFS_GOOGLE_MAPS_API_KEY"),
        repr=False,  # keep the key out of reprs/logs
    )


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging; unknown level names fall back to INFO."""
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
