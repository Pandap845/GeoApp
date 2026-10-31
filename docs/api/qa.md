# Phase 4 — Google Maps QA and station locator

```{eval-rst}
.. automodule:: gtfs_ingest.qa
```

:::{warning}
This phase calls a paid third-party API under licensing terms that forbid
persisting most of what comes back. The storage guards and budget rails are part
of the implementation, not advisory — see
{mod}`gtfs_ingest.qa.storage` and the licensing sections of the
{doc}`overview <../guide/readme>`.
:::

## Orchestration

```{eval-rst}
.. automodule:: gtfs_ingest.qa.pipeline_phase4
```

## API client

```{eval-rst}
.. automodule:: gtfs_ingest.qa.google_client
```

## Eligibility and verdicts

```{eval-rst}
.. automodule:: gtfs_ingest.qa.crosscheck
```

## Station locator

```{eval-rst}
.. automodule:: gtfs_ingest.qa.station_locator
```

## Guarded persistence

```{eval-rst}
.. automodule:: gtfs_ingest.qa.storage
```
