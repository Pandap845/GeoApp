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
| `frequencies.txt` | Servicio por intervalos; se reporta pero NO se expande en headways (limitación documentada) |
| `feed_info.txt` | Editor, versión y vigencia del feed (referencias) |

### Identificadores creados por el pipeline
| Campo | Significado |
| --- | --- |
| `feed_id` | Slug determinístico del archivo del feed; clave de procedencia en todas las tablas |
| `stop_uid` / `route_uid` | `"{feed_id}:{id}"` — claves globales que nunca chocan entre feeds |
| `stop_num_id` | Alias numérico estable de `stop_uid` (48 bits de sha256); clave máquina en la base de datos |

---

## 2. near_stations.csv — una fila por parada

| Columna | Significado |
| --- | --- |
| `ID` | Id secuencial de nodo **de este archivo** (1..N en orden de fila). Es la clave que usan todas las columnas de relación. No es estable entre corridas con feeds distintos; para cruces durables usar `stop_uid` |
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
| `connection_type` | Evidencia más fuerte de la conexión: `same_stop` (comparten una parada idéntica) > `same_parent_station` (paradas de la misma estación madre) > `proximity` (paradas a ≤ 150 m; único tipo que cruza feeds) |
| `shared_stop_count` | Número de paradas idénticas compartidas (0 si conectan solo por estación/cercanía) |
| `min_stop_distance_m` | Separación geodésica mínima entre paradas de ambas líneas (0 si comparten parada; NULL para pares solo-estación más lejanos que el umbral) |
| `feed_id_a`, `feed_id_b` | Feeds de cada extremo |

Notas de lectura: los nodos aislados (grado 0) son líneas analizadas sin
conexión con ninguna otra; el grafo NO contiene tiempos — es adyacencia
("¿es posible transbordar?"), no un grafo enrutable. Las estaciones donde
ocurre cada conexión no se conservan en la arista (ver la tabla
`interchanges` para los lugares físicos).
