# Phase 1 — ingest, validate, load

The `gtfs_ingest` top-level package: it discovers feeds, validates them without
aborting on broken ones, normalizes stops into the common cross-feed schema, and
loads the result into PostGIS. Every later phase reads the `stops` table and the
JSON report produced here.

## Command-line interface

```{eval-rst}
.. automodule:: gtfs_ingest.cli
```

## Orchestration

```{eval-rst}
.. automodule:: gtfs_ingest.pipeline_phase1
```

## Configuration

```{eval-rst}
.. automodule:: gtfs_ingest.config
```

## Models and GTFS schema constants

```{eval-rst}
.. automodule:: gtfs_ingest.models
```

## Feed discovery and loading

```{eval-rst}
.. automodule:: gtfs_ingest.loader
```

## Stops-only CSV input

```{eval-rst}
.. automodule:: gtfs_ingest.stops_csv
```

## Validation

```{eval-rst}
.. automodule:: gtfs_ingest.validators
```

## Normalization

```{eval-rst}
.. automodule:: gtfs_ingest.normalizer
```

## Database schema and loading

```{eval-rst}
.. automodule:: gtfs_ingest.db
```

## Bulk loading

```{eval-rst}
.. automodule:: gtfs_ingest.bulk
```

## Reporting

```{eval-rst}
.. automodule:: gtfs_ingest.report
```

## Run manifest

```{eval-rst}
.. automodule:: gtfs_ingest.manifest
```
