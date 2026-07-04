# Database init scripts

Files in this directory are mounted into the container at
`/docker-entrypoint-initdb.d`. Any `*.sql`, `*.sql.gz`, or `*.sh` here runs
**once**, in alphabetical order, when the data volume is first initialized
(i.e. on the very first `docker compose up`, or after `docker compose down -v`).

Nothing is required here for the pipeline: the `postgis/postgis` image
enables the PostGIS extension automatically, and `gtfs_ingest` creates its
own `stops` table and indexes on startup. Use this for extras only
(additional roles, schemas, seed data).
