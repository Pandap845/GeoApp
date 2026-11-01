# Diccionario de datos

Significado de cada campo de los datos GTFS que el pipeline implementa y de
los tres productos principales de salida: `near_stations.csv`,
`transit_analysis.gpkg` y `route_connectivity.graphml`.

---

## 1. Datos GTFS de entrada (campos implementados)

### agency.txt — operadores / sistemas
| Campo | Significado |
| --- | --- |
| `agency_id` | Identificador del operador dentro del feed |
| `agency_name` | Nombre del sistema (Mi Macro, Mi Tren, Sitren, Mi Transporte, Mi Transporte Eléctrico). **Es el origen de la columna `system`** |
| `agency_url`, `agency_timezone` | Portal y zona horaria del operador |

### stops.txt — paradas y estaciones
| Campo | Significado |
| --- | --- |
| `stop_id` | Identificador de la parada dentro del feed (clave del feed) |
| `stop_name` | Nombre de la parada/estación |
| `stop_lat`, `stop_lon` | Coordenadas WGS84 (validadas: lat ∈ [−90, 90], lon ∈ [−180, 180]) |
| `location_type` | 0 = parada/andén (default), 1 = estación, 2 = entrada, 3 = nodo genérico, 4 = área de abordaje. Los tipos 3/4 pueden omitir coordenadas (pero entonces se excluyen de la tabla de geometría) |
| `parent_station` | `stop_id` de la estación madre; agrupa andenes de una misma estación |

### routes.txt — líneas
| Campo | Significado |
| --- | --- |
| `route_id` | Identificador de la línea (clave del feed) |
| `agency_id` | Operador de la línea (enlace a agency.txt) |
| `route_short_name` | Nombre corto (C98, L1…). **Es la `linea` de los exportes**; si falta se usa `route_id` |
| `route_long_name` | Nombre descriptivo ("Troncal C01 - 39A Poniente") |
| `route_type` | Modo: 0 tranvía/tren ligero, 1 metro, 2 tren, 3 autobús, 4 ferry, 11 trolebús, 12 monorriel. Los códigos extendidos (100–1499) se normalizan al básico (p. ej. 700→3) |

### trips.txt — viajes (una corrida de una línea)
| Campo | Significado |
| --- | --- |
| `trip_id` | Identificador del viaje |
| `route_id`, `service_id` | Línea a la que pertenece y calendario que lo activa |
| `direction_id` | Sentido del viaje: 0 = ida, 1 = vuelta (la asignación la define el operador); vacío → centinela −1. **Es el `dir0`/`dir1` de `same_line_stations`** |
| `shape_id` | Trazado geométrico del viaje (enlace a shapes.txt) |

### stop_times.txt — horario de cada viaje
| Campo | Significado |
| --- | --- |
| `trip_id`, `stop_id` | Qué viaje pasa por qué parada |
| `stop_sequence` | **Orden de recorrido** dentro del viaje; define la dirección de los segmentos `route_stop_links` y la adyacencia de `same_line_stations` |
| `departure_time` | Hora de salida programada; puede exceder 24:00 (25:10 = servicio búho del mismo día de servicio). Origen de los headways y de `travel_min` |

### calendar.txt / calendar_dates.txt — vigencia del servicio
| Campo | Significado |
| --- | --- |
| `service_id` | Calendario de servicio |
| `monday`…`sunday`, `start_date`, `end_date` | Patrón semanal y rango de vigencia |
| `date`, `exception_type` | Excepciones puntuales: 1 = servicio agregado ese día, 2 = quitado |

### shapes.txt — trazados
| Campo | Significado |
| --- | --- |
| `shape_id` | Identificador del trazado |
| `shape_pt_lat/lon`, `shape_pt_sequence` | Vértices del recorrido real por las calles, en orden. Origen de la capa `route_shapes` |

### Otros implementados
| Archivo | Uso |
| --- | --- |
| `transfers.txt` | Transbordos declarados (`from_stop_id`→`to_stop_id`); se contrastan con los intercambiadores inferidos |
| `frequencies.txt` | Servicio por intervalos; SE EXPANDE en el análisis de headways: cada fila genera una corrida sintética por `headway_secs` dentro de `[start_time, end_time)`, reproduciendo los desfases por parada del viaje plantilla (las horas absolutas de la plantilla se reemplazan — son un patrón, no salidas reales). `trip_count` cuenta pasadas reales incluidas las sintéticas. `travel_min` (misma línea) usa el patrón SIN expandir |
| `feed_info.txt` | Editor, versión y vigencia del feed (referencias) |

### Entrada CSV mínima (fuentes de solo paradas)

Además de feeds GTFS, el pipeline acepta un CSV de cuatro columnas que se
fusiona en la misma tabla `stops` y en los mismos archivos de salida. Se
indica **nombrando el archivo explícitamente** en `--feeds`; nunca se detecta
al escanear un directorio (el propio `near_stations.csv` del pipeline sería un
CSV de paradas válido).

| Columna canónica | Grafías aceptadas | Obligatoria |
| --- | --- | --- |
| `stop_id` | `id`, `stop_id`, `stopid`, `codigo`, `clave` | Sí |
| `stop_name` | `name`, `stop_name`, `stopname`, `nombre`, `estacion` | No |
| `stop_lat` | `latitude`, `lat`, `stop_lat`, `latitud` | Sí |
| `stop_lon` | `longitude`, `lon`, `lng`, `long`, `stop_lon`, `longitud` | Sí |

Coordenadas en WGS84 (grados decimales). La comparación de encabezados ignora
mayúsculas, acentos y puntuación. Se detectan automáticamente la codificación
(UTF-8 con o sin BOM, UTF-16, CP1252) y el delimitador (`,`, `;`, tabulador,
`|`) — el caso esperado es el predeterminado de Excel en Windows en español
(`;` con BOM). Las columnas no reconocidas se ignoran. Un encabezado
irresoluble produce un ERROR `unresolved_csv_header` que nombra las columnas
encontradas, las faltantes y las grafías aceptadas.

Las coordenadas con **coma decimal** (`20,6736`) se avisan como
`csv_decimal_comma` y sus filas se descartan como coordenadas inválidas: no se
reinterpretan silenciosamente.

Una fuente de solo paradas aporta geometría (paradas, cercanías,
deduplicación, GeoPackage, red de estaciones) pero **no** headways, aristas de
conectividad ni topología de misma línea; se reporta con estado `stops_only`.

### Identificadores creados por el pipeline
| Campo | Significado |
| --- | --- |
| `feed_id` | Slug determinístico del archivo del feed (o del CSV, sin extensión); clave de procedencia en todas las tablas |
| `stop_uid` / `route_uid` | `"{feed_id}:{id}"` — claves globales que nunca chocan entre feeds. Por eso un `S1` de un CSV y un `S1` de un GTFS coexisten sin conflicto |
| `stop_num_id` | Alias numérico estable de `stop_uid` (48 bits de sha256); clave máquina en la base de datos |

---

## 2. near_stations.csv — una fila por parada

| Columna | Significado |
| --- | --- |
| `ID` | Id secuencial de nodo **de este archivo** (1..N en orden de `stop_uid`). Es la clave que usan todas las columnas de relación. No es estable entre corridas con feeds distintos; para cruces durables usar `stop_uid` |
| `stop_uid` | Clave global durable (`feed:stop`); une con la tabla `stops` de PostGIS |
| `station` | Nombre de la parada (`stop_name`) |
| `system` | Sistema(s) que la sirven — `agency_name` de las líneas activas; varios se unen por coma ("Mi Macro, Sitren"); vacío = sin servicio ese día |
| `stop_id` | Id original dentro del feed |
| `stop_lat`, `stop_lon` | Coordenadas WGS84 |
| `stop_line_ids` | Combinaciones `"{ID}-{linea}"` de la propia parada (p. ej. `17-C98`), para modelar un nodo por (estación, línea) |
| `near_stations` | **Relación por cercanía a pie** (cualquier línea): `[(ID, estación, línea, minutos), …]` — paradas a ≤ 150 m; una tupla por (parada cercana, línea activa); minutos CAMINANDO = `max(1, round(distancia/83))` (~5 km/h). Con `--network-walk` la distancia se camina por la red de calles de OSM. Ordenada por minutos |
| `same_line_stations` | **Relación por topología de ruta** (misma línea): `[(ID, estación, línea, dirD, minutos), …]` — estación anterior/siguiente en orden de recorrido, por línea y sentido (`dir0`/`dir1`); minutos EN VEHÍCULO según horario (mediana de `stop_times`, 1 decimal; sin horario → estimación a ~25 km/h). Sin tope de distancia |

Todos los tiempos son números sin sufijo y siempre en minutos. `[]` = sin
vecinos; `""` en `stop_line_ids`/`system` = parada sin servicio en la fecha
analizada.

**`linea = "-"`**: una parada cercana a la que NINGUNA línea sirve ese día
—un nodo de estación padre, o una parada de un CSV de solo paradas— sí
aparece en las listas de vecinos, con `-` en el lugar de la línea. Quien
procese el campo `linea` debe tolerar ese valor. Esto solo afecta cómo se ve
esa parada en las listas de *otras* paradas: sus propias columnas `system` y
`stop_line_ids` siguen vacías, porque efectivamente no sirve ninguna línea.

Nota (deduplicación entre feeds, activa por defecto): cuando dos paradas de
feeds distintos son la MISMA estación física (alias, p. ej.
`improved:SITEUR_x` y `mdb:TL_x`), se excluyen mutuamente de sus listas
`near_stations` — una estación no es "cercana" a su propio alias. El
conjunto de filas y la secuencia `ID` no cambian. `--no-dedupe` restaura el
comportamiento anterior. El detalle de cada par fusionado queda en
`stop_equivalences.csv` (sección 2b).

---

## 2b. stop_equivalences.csv — auditoría de deduplicación

Una fila por par de alias aceptado (solo se escribe con dedupe activo). Hay
**dos criterios distintos**, según si el par cruza feeds o no:

- **`cross_feed`** — paradas de feeds **distintos** a ≤ 30 m
  (`--dedupe-distance-m`) que comparten nombre normalizado (similitud ≥ 0.75,
  sin acentos ni mayúsculas, tokens ordenados) **o** al menos un
  `route_short_name` en común.
- **`within_feed`** — paradas del **mismo** feed a ≤ 5 m
  (`--dedupe-within-feed-distance-m`) que comparten **al menos una línea, y
  nada más**. El nombre NO es evidencia aquí: dos paradas distintas de un
  mismo feed pueden llamarse igual (un corredor, una plaza), mientras que dos
  paradas de la **misma línea** a pocas decenas de metros sí delatan un
  registro duplicado — las paradas consecutivas de una línea están a cientos
  de metros. Es el caso de un feed que publica una línea dos veces con dos
  esquemas de id (p. ej. `SITEUR_001_025` y `ST_01_025`).

| Columna | Significado |
| --- | --- |
| `group_id` | Id del grupo de alias (componentes conexas; determinista, ordenado por el `stop_uid` mínimo del grupo) |
| `canonical_uid` | Representante canónico del grupo: su `stop_uid` mínimo |
| `stop_uid_a`, `stop_uid_b` | El par aceptado (`a < b`) |
| `distance_m` | Separación geodésica del par en metros |
| `name_score` | Similitud de nombres normalizados, 0–1 (0.0 = algún nombre vacío) |
| `shared_routes` | Número de `route_short_name` normalizados en común entre las líneas que sirven cada parada |
| `score` | Índice de auditoría: `0.5·(1 − distancia/radio) + 0.5·name_score` — no es el criterio de aceptación |
| `match_kind` | `cross_feed` o `within_feed` — qué regla aceptó el par |

Una fuente de solo paradas (CSV) no tiene líneas, así que la regla
`within_feed` nunca puede dispararse para ella: sus paradas jamás se fusionan.

**Conflictos del mismo feed.** Los pares aceptados se unen en estaciones del
más cercano al más lejano. Un par se **rechaza** si la estación resultante
tendría dos paradas del mismo feed a más de 5 m (el radio `within_feed`).
Así los andenes opuestos de una calle no se fusionan a través de sus gemelos
del otro feed. Los pares rechazados están en
`stop_equivalences_rejected.csv`: las mismas columnas del par más
`conflict_uid_a`, `conflict_uid_b` (las dos paradas del mismo feed que se
habrían juntado) y `conflict_m` (su distancia en metros).

La misma información se carga en la tabla `stop_equivalences` de PostGIS
(recarga completa por corrida; queda vacía tras una corrida `--no-dedupe`).

### Contar estaciones, no registros

**La deduplicación registra equivalencias; nunca borra filas.** Todos los
registros siguen en `stops`, en `near_stations.csv` y en el GeoPackage, así
que un `COUNT(*)` cuenta *registros*, no estaciones físicas. Para contar
estaciones hay que resolver cada registro a su estación canónica con
`station_dedup` (sección 2c):

```sql
COUNT(DISTINCT COALESCE(NULLIF("station_dedup",''), "stop_uid"))
```

Con los dos feeds de Guadalajara más MiBici la diferencia no es menor:
14,349 registros → **11,054** estaciones con la deduplicación actual (10,533
antes de la regla de conflictos del mismo feed; 8,877 con el antiguo radio
de 50 m dentro de cada feed).

---

## 2c. near_stations_quality.csv — procedencia de las estimaciones

Archivo companion escrito SIEMPRE junto a `near_stations.csv`: una fila por
parada, con el MISMO `ID` y el mismo orden de filas (el formato del archivo
principal nunca cambia por este sidecar). Hace auditable la escalera de
respaldos: en el archivo principal un 4 medido y un 4 estimado se ven
idénticos; aquí se distinguen.

| Columna | Significado |
| --- | --- |
| `ID`, `stop_uid` | Las mismas claves del archivo principal (`ID` para cruzar los archivos de esta corrida; `stop_uid` como clave durable) |
| `walk_source` | Origen de los minutos a pie de `near_stations`: `network` (ruteo por calles de OSM con `--network-walk`) / `straight_line` (línea recta) / `mixed` (tuplas con ambos orígenes) / `unsnapped` (la parada ancla a >200 m de cualquier calle caminable — el valor de red no es confiable) / `none` (sin vecinos renderizados) |
| `travel_source` | Origen de los minutos de `same_line_stations`: `observed` (mediana de horarios reales de `stop_times`) / `estimated` (respaldo a ~25 km/h) / `mixed` / `none` (sin segmentos de línea) |
| `travel_n_obs` | Número de viajes detrás de la mediana, en el PEOR caso (mínimo entre los segmentos de la parada); 0 cuando todo es estimado o no hay segmentos — 1 observación no vale lo mismo que 50 |
| `coord_confidence` | Veredicto de la conflación OSM (Fase 3, tabla `stop_osm_matches`): puntaje numérico `"0.812"` (emparejada con confianza) / `needs_review` (emparejada pero dudosa) / `no_match` (sin nodo OSM compatible) / `unavailable` (la conflación no ha corrido o la corrida fue `--dry-run`) |
| `station_dedup` | `canonical_uid` del grupo de alias cuando la parada es duplicado — entre feeds o dentro del mismo feed (sección 2b); vacío si no tiene alias. **Es la columna con la que se cuentan estaciones** en vez de registros |

El resumen por corrida (conteos por etiqueta) queda en
`analysis_report.json` como `quality_summary`.

Una parada proveniente de un CSV de solo paradas aparece con
`walk_source = straight_line` (o `network`/`mixed` con `--network-walk`),
`travel_source = none` y `travel_n_obs = 0`: tiene vecinos a pie reales pero
ningún segmento de línea. Su `coord_confidence` proviene de `conflate-osm`
igual que cualquier otra parada.

---

## 3. transit_analysis.gpkg — GeoPackage (EPSG:4326, 4 capas)

### Capa `stops` (puntos)
Todos los atributos del CSV (`ID`, `stop_uid`, `station`, `system`,
`stop_line_ids`, `near_stations`, `same_line_stations`) más:
| Atributo | Significado |
| --- | --- |
| `stop_num_id` | Clave numérica de la base de datos (join con PostGIS) |
| `feed_id`, `stop_id` | Procedencia |
| `location_type`, `parent_station` | Tipología GTFS de la parada |

### Capa `near_links` (líneas) — vecindad a pie
Una línea recta por PAR de paradas a ≤ 150 m (visualización de la relación
`near_stations`; es simétrica, un registro por par).
| Atributo | Significado |
| --- | --- |
| `num_id_a`, `num_id_b` | `stop_num_id` de ambos extremos (clave de BD) |
| `stop_uid_a`, `stop_uid_b` | Claves durables de ambos extremos |
| `distance_m` | Distancia en metros (de red de calles si se usó `--network-walk`, si no geodésica en línea recta) |
| `walk_min` | Minutos a pie derivados de `distance_m` |

### Capa `route_shapes` (líneas) — trazados reales
El recorrido de cada línea por las calles (de `shapes.txt`).
| Atributo | Significado |
| --- | --- |
| `route_uid`, `route_id`, `feed_id` | Identidad de la línea |
| `linea` | Etiqueta corta de la línea |
| `direction_id` | Sentido del trazado (0/1; −1 desconocido) |
| `shape_id` | Trazado GTFS de origen |

### Capa `route_stop_links` (líneas) — topología DIRIGIDA
Un segmento por par de paradas CONSECUTIVAS de una línea/sentido
(`stop_sequence`): "la siguiente estación de MI línea". A diferencia de
`near_links` (a pie, cualquier línea), esta capa es la ruta.
| Atributo | Significado |
| --- | --- |
| `route_uid`, `feed_id`, `linea`, `direction_id` | Línea y sentido del segmento |
| `stop_uid_a` → `stop_uid_b` | Origen → destino EN SENTIDO DE VIAJE |
| `travel_min` | Minutos EN VEHÍCULO programados del segmento (mediana del día; NULL si el tramo no tiene horarios) |

*(Archivo hermano: `osm_coverage_gaps.gpkg`, capa `osm_only_stops` — nodos
de transporte de OSM sin parada GTFS cercana: `osm_id`, `kind`, `osm_name`,
`nearest_stop_m` y posición.)*

---

## 4. route_connectivity.graphml — grafo de la red de rutas

Grafo NO dirigido para Gephi/yEd/networkx. Un nodo = una LÍNEA (no una
parada); una arista = "estas dos líneas conectan".

### Atributos de nodo
| Atributo | Significado |
| --- | --- |
| id del nodo | `route_uid` (`feed:route`) |
| `feed_id`, `route_id` | Procedencia |
| `route_short_name` / `route_long_name` | Etiquetas de la línea |
| `route_type` | Modo GTFS (0 tranvía, 1 metro, 2 tren, 3 autobús, 11 trolebús…) |
| `modalidad` | Sistema operador (agency_name), cuando está disponible |

### Atributos de arista
| Atributo | Significado |
| --- | --- |
| `connection_type` | Evidencia más fuerte de la conexión: `same_stop` (comparten una parada idéntica) > `same_parent_station` (paradas de la misma estación madre, declarada en el GTFS) > `same_station_dedup` (paradas fusionadas por la deduplicación entre feeds — inferida, ver sección 2b) > `proximity` (paradas a ≤ 150 m). `same_station_dedup` y `proximity` son los únicos tipos que cruzan feeds |
| `shared_stop_count` | Número de paradas idénticas compartidas (0 si conectan solo por estación/cercanía) |
| `min_stop_distance_m` | Separación geodésica mínima entre paradas de ambas líneas (0 si comparten parada; NULL para pares solo-estación más lejanos que el umbral) |
| `feed_id_a`, `feed_id_b` | Feeds de cada extremo |

Notas de lectura: los nodos aislados (grado 0) son líneas analizadas sin
conexión con ninguna otra; el grafo NO contiene tiempos — es adyacencia
("¿es posible transbordar?"), no un grafo enrutable. Las estaciones donde
ocurre cada conexión no se conservan en la arista (ver la tabla
`interchanges` para los lugares físicos). Para rutear con tiempos, usar el
grafo de la sección 5.

---

## 5. station_network — la red ENRUTABLE a nivel de parada

Tres formatos con el mismo contenido: `station_network.graphml`
(MultiDiGraph de networkx), `station_network_nodes.csv` +
`station_network_edges.csv` (planos, para pandas/Excel), y la capa
`station_network` del GeoPackage (aristas como líneas, para QGIS).
A diferencia del grafo de rutas (sección 4), aquí un nodo = una PARADA y
cada arista lleva un peso temporal `time_min`, así que
`nx.shortest_path(g, a, b, weight="time_min")` calcula caminos mínimos
directamente — incluso entre feeds.

### Atributos de nodo (una parada)
| Atributo | Significado |
| --- | --- |
| id del nodo / `stop_uid` | Clave global durable (`feed:stop`) |
| `near_id` | El `ID` secuencial de `near_stations.csv` de esta corrida (0 si no aplica) |
| `name` | Nombre de la parada |
| `feed` | Feed de procedencia |
| `canonical_uid` | Estación canónica de la deduplicación (sección 2b); el propio `stop_uid` si no tiene alias |
| `lat`, `lon` | Coordenadas WGS84 |

### Tipos de arista
| `edge_type` | Dirección | `time_min` | Otros atributos |
| --- | --- | --- | --- |
| `in_vehicle` | DIRIGIDA, una por línea y sentido | Minutos EN VEHÍCULO programados (`travel_min`); ausente si el segmento no tiene horario | `linea`, `direction_id`, `route_uid`, `feed_id` |
| `walk` | Recíproca (dos arcos) | Minutos a pie (línea recta, o por calles de OSM con `--network-walk`) | `distance_m`, `walk_source` |
| `same_station` | Recíproca (dos arcos) | `0.0` — alias de la misma estación física entre feeds (sección 2b); es lo que conecta las redes de los dos feeds | — |

Notas: es un MULTIgrafo — un mismo par de paradas puede tener a la vez una
arista a pie y varias en vehículo (una por línea); Gephi las fusiona salvo
que se active "parallel edges" al importar (yEd, igraph y networkx las
leen sin ajuste). En los CSV, los arcos recíprocos se compactan a una fila
por par (`in_vehicle` conserva una fila por sentido).

---

## 6. Paquete neo4j/ — carga directa a Neo4j

Generado con `export-neo4j --report ./out` a partir de los archivos
anteriores (no recalcula nada). Contiene 8 CSVs de datos con el encabezado
de `neo4j-admin` en la primera línea, más tres archivos generados:

| Archivo | Contenido |
| --- | --- |
| `stops.csv` | Nodos `(:Stop)` — `stop_uid:ID(Stop)`, nombre, feed, `near_id`, lat/lon, `canonical_uid` |
| `routes.csv` | Nodos `(:Route)` — metadata de la línea (nombre corto/largo, `route_type`, `modalidad`) |
| `stations.csv` | Nodos `(:Station)` — SOLO grupos de alias con 2+ miembros (`canonical_uid:ID(Station)`, `n_stops`) |
| `rel_in_vehicle.csv` | `[:IN_VEHICLE {time_min, linea, direction_id, route_uid}]` — dirigida |
| `rel_walk.csv` | `[:WALK {time_min, distance_m}]` — se guarda una vez; consultar sin dirección: `-[:WALK]-` |
| `rel_same_station.csv` | `[:SAME_STATION]` — alias entre feeds, con métricas de la deduplicación |
| `rel_serves.csv` | `(:Route)-[:SERVES]->(:Stop)` — derivada de los extremos de `in_vehicle` |
| `rel_part_of.csv` | `(:Stop)-[:PART_OF]->(:Station)` — incluye a la parada canónica |
| `load.cypher` | Script idempotente (`MERGE`, constraints, lotes de 1000) — `cypher-shell -f load.cypher` con los CSV en la carpeta *import* del DBMS |
| `post_import.cypher` | Constraints + propiedad `location` (punto) — para DESPUÉS de un import masivo |
| `admin_import.txt` | El comando `neo4j-admin database import full` documentado (bash y PowerShell) |

Claves del diseño: los espacios de id (`:ID(Stop)` vs `:ID(Station)`)
evitan la colisión — el id de una estación ES el `stop_uid` de su parada
canónica; ningún dato viaja dentro de los `.cypher` (todo va por CSV, así
que nombres con comillas o acentos no rompen el script); recargar el
script es seguro (todo es `MERGE`).
