"""Runtime configuration and logging setup.

Settings resolve in the usual precedence order: explicit CLI flags beat
environment variables, which beat values from a local ``.env`` file, which
beat defaults. Credentials are never hardcoded - the database URL comes
exclusively from ``--db-url`` or the ``DATABASE_URL`` env var.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pipeline configuration.

    Attributes:
        database_url: SQLAlchemy URL for PostgreSQL/PostGIS. Read from the
            ``DATABASE_URL`` (or ``GTFS_DATABASE_URL``) environment variable
            when not passed explicitly. May be ``None`` for dry runs.
        feed_paths: paths to individual feeds (.zip or directory) or to
            directories containing multiple feeds.
        report_dir: directory the JSON + Markdown reports are written to.
        log_level: root log level name (DEBUG/INFO/WARNING/ERROR).
        dry_run: when True, validate and report only - no DB writes.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Each AliasChoices includes the field name itself so explicit keyword
    # construction (Settings(database_url=...)) keeps working alongside the
    # environment-variable aliases.
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
    dry_run: bool = False

    # --- OSM layer names (Phase 3 conflation; osm2pgsql classic defaults) ---
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


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging for the pipeline.

    Args:
        level: log level name; unknown names fall back to INFO rather
            than raising, so a typo in an env var can't break a run.
    """
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
