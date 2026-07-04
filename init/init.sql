CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS hstore;
-- opcional pero útil para búsquedas por nombre de parada:
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS gtfs;

-- verificación (queda en el log del contenedor):
SELECT PostGIS_Full_Version();