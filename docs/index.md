# gtfs-ingest

GTFS ingestion, validation and service analysis for the Guadalajara metropolitan
transit network — feeds and OpenStreetMap data in, a PostGIS database plus
reproducible analysis outputs out.

The pipeline runs in four phases, each a CLI subcommand that consumes the
previous phase's tables:

| Phase | Subcommand | What it does |
| --- | --- | --- |
| 1 | `ingest` | Validate feeds without aborting on broken ones, normalize stops into a cross-feed schema, load into PostGIS |
| 2 | `analyze-frequency` | Service-date-aware headways per (route, direction, stop, window) and route-to-route connectivity |
| 3 | `detect-interchanges`, `conflate-osm` | Multimodal interchange clustering, OSM location validation, side-of-street disambiguation |
| 4 | `google-qa`, `locate-stations` | Gated, budgeted Google Maps cross-check of stop positions; station route resolution |

Exports to GeoPackage, GraphML and Neo4j sit alongside phase 2.

:::{note}
This documentation is generated from the source tree: the narrative chapters are
the project's own Markdown documents, and the {doc}`API reference <api/index>` is
extracted directly from the docstrings, so both stay in step with the code.
:::

## Getting started

```{toctree}
:maxdepth: 2
:caption: Guide

Overview & CLI reference <guide/readme>
Data workflow <guide/workflow>
Pipeline & algorithms guide <guide/pipeline-guide>
Data dictionary <guide/data-dictionary>
Isolated per-feed runs <guide/isolated-runs>
```

## API reference

```{toctree}
:maxdepth: 2
:caption: API reference

api/index
```

## Background

```{toctree}
:maxdepth: 2
:caption: Background

Google Maps API usage <guide/google-apis>
Roadmap & gap analysis <guide/roadmap>
Data sources & references <guide/references>
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
