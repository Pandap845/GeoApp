# API reference

Generated from the docstrings in [`gtfs_ingest/`](https://github.com/) — every
page below mirrors one package in the source tree, ordered by the pipeline phase
it implements. Within a page, members appear in source order rather than
alphabetically, so each module reads top-down from its public entry point.

```{toctree}
:maxdepth: 2

phase1
analysis
conflation
qa
```

## Package overview

```{eval-rst}
.. automodule:: gtfs_ingest
   :no-index:
```

| Package | Phase | Responsibility |
| --- | --- | --- |
| {doc}`gtfs_ingest <phase1>` | 1 | Feed discovery, validation, normalization, PostGIS loading |
| {doc}`gtfs_ingest.analysis <analysis>` | 2 | Headways, connectivity, walk/station networks, spatial exports |
| {doc}`gtfs_ingest.conflation <conflation>` | 3 | Interchange clustering, OSM matching, side-of-street |
| {doc}`gtfs_ingest.qa <qa>` | 4 | Google Maps cross-check and station locator, behind licensing rails |
