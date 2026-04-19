# Data Catalog

Last updated: **2026-04-08**. All URLs, record counts, column names, and API responses verified.

## Boston (data.boston.gov)

### 1. Buildings with Height (BPDA)

| Field | Value |
| --- | --- |
| URL | https://data.boston.gov/dataset/boston-buildings-with-roof-breaks |
| API Resource ID | `2c683b81-7b88-4add-80ad-765e177092bf` |
| Records | 128,608 total. 105,121 with height data. 28,152 with geometry (`shape_wkt`) |
| Key columns | `BLDG_HGT_2010` (height in feet), `shape_wkt` (footprint polygon WKT) |
| Format | CSV (24MB), GeoJSON (106MB), Shapefile |
| Data period | 2010 survey |
| CRS | WGS84 |
| Used for | Shadow projection (daytime) |
| Verified | 2026-04-08. Record count confirmed. All columns stored as text type |

### 2. Streetlight Locations

| Field | Value |
| --- | --- |
| URL | https://data.boston.gov/dataset/streetlight-locations |
| API Resource ID | `c2fcc1e3-c38f-44ad-a0cf-e5ea2a6585b5` |
| Records | 74,065 (100% valid coordinates) |
| Key columns | `Lat`, `Long`, `TYPE` |
| Format | CSV, KML, Shapefile |
| Data period | Current as of download |
| CRS | WGS84 |
| Used for | Nighttime brightness heatmap |
| Verified | 2026-04-08. Record count confirmed |

### 3. Tree Canopy Polygons

| Field | Value |
| --- | --- |
| URL | https://data.boston.gov/dataset/tree-canopy-change-assessment |
| Records | 108,131 |
| Format | Shapefile (ZIP, ~1 GB) |
| Data period | 2019 baseline |
| CRS | EPSG:2249 (Massachusetts State Plane). Requires reprojection to WGS84 |
| Used for | Daytime tree shade |
| Verified | 2026-04-08. URL confirmed (old slug `canopy-change-assessment` returns 404). ZIP download only, no API query available |

### 4. Crime Incidents

| Field | Value |
| --- | --- |
| API Resource ID | `b973d8cb-eeb2-4e7e-99da-c92938efc9c0` |
| Records | ~258K (live dataset, growing). 94% with valid coordinates |
| Key columns | `OCCURRED_ON_DATE`, `HOUR` (0-23), `Lat`, `Long` |
| Format | CSV |
| Data period | 2023-01-01 to present |
| Used for | Nighttime safety context (filtered to hours 18-06) |
| Verified | 2026-04-08. 257,954 at time of check. `OFFENSE_CODE_GROUP` and `UCR_PART` columns are unpopulated |

### 5. Crash Records (Vision Zero)

| Field | Value |
| --- | --- |
| API Resource ID | `e4bfe397-6bfc-49c5-9367-c879fac7401d` |
| Records | 42,685 (100% valid coordinates) |
| Key columns | `dispatch_ts` (timestamp), `lat`, `long`, `mode_type` (mv/ped/bike) |
| Format | CSV |
| Data period | 2015-01-01 to 2025-12-31 |
| Used for | Nighttime safety context |
| Verified | 2026-04-08. Record count confirmed |

### 6. Food Establishments (Active Licenses)

| Field | Value |
| --- | --- |
| API Resource ID | `f1e13724-284d-478c-b8bc-ef042aa5b70b` |
| Records | ~3.2K (live dataset, growing). 94% with valid coordinates |
| Key columns | `businessname`, `address`, `latitude`, `longitude` |
| Format | CSV |
| Data period | Active licenses |
| Used for | Nighttime activity markers |
| Verified | 2026-04-08. 3,207 at time of check |

### 7. Flood Complaints (311)

| Field | Value |
| --- | --- |
| Source | Boston 311 yearly CSV datasets on data.boston.gov |
| Records | ~3,500 (aggregated across years 2019-2024) |
| Key columns | `latitude`, `longitude`, `reason`, `type` |
| Filter | `LOWER("type") LIKE '%flood%' OR "reason" = 'Catchbasin'` |
| Format | CSV (one resource per year) |
| Data period | 2019 to 2024 |
| Used for | Flood risk overlay (extension) |
| Note | No single resource ID. Data split across yearly 311 CSVs. New 311 system (2026+) does not include flood categories |
| Verified | 2026-04-08. 3,536 total across 2019-2024 (609+637+546+642+508+594) |

### 8. Ice Complaints (311)

| Field | Value |
| --- | --- |
| Source | Boston 311 yearly CSV datasets on data.boston.gov |
| Records | ~28K (aggregated across years 2018-2024) |
| Key columns | `latitude`, `longitude`, `type` |
| Filter | `"type" = 'Unshoveled Sidewalk' OR "type" = 'Snow/Ice Control'` |
| Format | CSV (one resource per year) |
| Data period | 2018 to 2024 |
| Used for | Ice risk overlay (extension) |
| Note | No single resource ID. Avoid naive `LIKE '%ice%'` filter as it matches "Abandoned Bicycle", "Mice Infestation", etc. 2019-2024 alone = 21,557. Including 2018 (~6,367) brings total to ~28K |
| Verified | 2026-04-08. Per-year: 2024=3,803 / 2023=402 / 2022=5,880 / 2021=2,587 / 2020=2,348 / 2019=6,537 |

---

## Cambridge (data.cambridgema.gov, Cambridge GIS GitHub)

### 9. Buildings with Height

| Field | Value |
| --- | --- |
| URL | https://raw.githubusercontent.com/cambridgegis/cambridgegis_data/main/Basemap/Buildings/BASEMAP_Buildings.geojson |
| Records | 18,234 (100% valid, WGS84) |
| Key columns | `TOP_SL`, `TOP_GL`, `ELEV_SL`, `BASE_ELEV`, `ELEV_GL`, `BldgID`, `TYPE` |
| Format | GeoJSON (19MB) |
| Data period | 2018 flyover |
| CRS | WGS84 |
| Used for | Shadow projection |
| Verified | 2026-04-08. Record count confirmed. No explicit height field. Height = `TOP_GL` or `TOP_SL - BASE_ELEV` |

### 10. Streetlight Locations

| Field | Value |
| --- | --- |
| URL | https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_infra/main/Street_Lights/INFRA_StreetLights.geojson |
| Records | 6,117 (100% valid, WGS84) |
| Key columns | `PoleID`, `FixtureType`, `Description`, `LEDchk`, `StreetName`, `Owner` |
| Format | GeoJSON (2.7MB) |
| Data period | Current as of 2025-10 |
| CRS | WGS84 |
| Used for | Nighttime brightness |
| Verified | 2026-04-08. Record count confirmed |

### 11. Tree Canopy Polygons

| Field | Value |
| --- | --- |
| URL | https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_environmental/main/Tree_Canopy_2018/ENVIRONMENTAL_TreeCanopy2018.topojson |
| Records | 36,266 (100% valid, WGS84) |
| Format | TopoJSON (13.5MB). Needs conversion to GeoJSON |
| Data period | 2018 |
| CRS | WGS84 |
| Used for | Daytime tree shade |
| Verified | 2026-04-08. Record count confirmed. Properties are empty (geometry only, no attributes) |

### 12. Crime Incidents

| Field | Value |
| --- | --- |
| Source | data.cambridgema.gov |
| Socrata ID | `xuad-73uj` |
| Records | 109,214 (109,209 valid coords, ~100%) |
| Key columns | `date_of_report` (timestamp), `crime_date_time` (text), `reporting_area_lat`, `reporting_area_lon` |
| Format | CSV |
| Data period | 2009-01-01 to present |
| Used for | Nighttime safety context |
| Verified | 2026-04-08. Record count confirmed. Use `date_of_report` for timestamps, not `crime_date_time` (text field with date ranges) |

### 13. Crash Records

| Field | Value |
| --- | --- |
| Source | data.cambridgema.gov |
| Socrata ID | `gb5w-yva3` |
| Records | 16,247 (54% with valid coordinates) |
| Key columns | `date_time`, `location` (contains lat/lon), `street_name`, `ambient_light` |
| Format | CSV |
| Data period | 2015-01-01 to 2026-02-28 |
| Used for | Nighttime safety context |
| Verified | 2026-04-08. Record count confirmed. This is the "Police Department Crash Data - Updated" dataset. Two other crash datasets exist (`ybny-g9cv` historical, `h6fp-bp8s` CPD log) but `gb5w-yva3` is the consolidated version |

### 14. Flood Complaints (311)

| Field | Value |
| --- | --- |
| Source | data.cambridgema.gov |
| Socrata ID | `2z9k-mv9g` (Commonwealth Connect Service Requests) |
| Records | ~2,200 (filtered from 144K total 311 records) |
| Key columns | `lat`, `lng`, `issue_type` |
| Filter | `issue_type` = "Manhole/Street Drain Issue" or flood/sewer/catch basin related |
| Format | CSV |
| Data period | 2009-02-10 to present |
| Used for | Flood risk overlay (extension) |
| Verified | 2026-04-08. ~2,211 at time of check. Column names differ from Boston (`lat`/`lng` vs `latitude`/`longitude`) |

### 15. Ice Complaints (311)

| Field | Value |
| --- | --- |
| Source | data.cambridgema.gov |
| Socrata ID | `2z9k-mv9g` (Commonwealth Connect Service Requests) |
| Records | ~13,650 (filtered from 144K total 311 records) |
| Key columns | `lat`, `lng`, `issue_type` |
| Filter | `issue_type` starting with "Icy" or containing "Snowy", "Unshoveled", "Snow" |
| Format | CSV |
| Data period | 2016-01-18 to present |
| Used for | Ice risk overlay (extension) |
| Verified | 2026-04-08. 13,653 at time of check |

---

## Cambridge (additional datasets)

### 27. Park Lights

| Field | Value |
| --- | --- |
| URL | https://raw.githubusercontent.com/cambridgegis/cambridgegis_data/main/Infra/Park_Lights/INFRA_ParkLights.geojson |
| Records | 858 |
| Key columns | `ParkName`, `PoleID`, `Description`, `NumLamps`, `LEDchk` |
| Format | GeoJSON |
| CRS | WGS84 |
| Used for | Nighttime brightness in parks |
| Verified | 2026-04-08 |

### 28. Street Trees

| Field | Value |
| --- | --- |
| URL | https://raw.githubusercontent.com/cambridgegis/cambridgegis_data/main/Environmental/Trees/ENVIRONMENTAL_StreetTrees.geojson |
| Records | 42,711 |
| Key columns | `CommonName`, `ScientificName`, `diameter`, `SolarRating`, `Ownership`, `Neighborhood` |
| Format | GeoJSON |
| CRS | WGS84 |
| Used for | Tree shade analysis. `SolarRating` field directly relevant |
| Verified | 2026-04-08 |

---

## Massachusetts

### 29. MBTA Stop Locations

| Field | Value |
| --- | --- |
| API | https://api-v3.mbta.com/stops |
| GTFS | https://cdn.mbta.com/MBTA_GTFS.zip (30.6MB) |
| Records | 10,271 stops |
| Key columns | `latitude`, `longitude`, `name`, `municipality`, `vehicle_type` |
| Auth | API key recommended (free) but not required for basic use |
| Format | JSON:API (V3 API), GTFS CSV (static feed) |
| Used for | Nighttime safety markers. Transit stops are typically well-lit |
| Verified | 2026-04-08 |

### 30. MassGIS Protected Open Space

| Field | Value |
| --- | --- |
| URL | https://www.mass.gov/info-details/massgis-data-protected-and-recreational-openspace |
| ArcGIS | https://gis.eea.mass.gov/server/rest/services/Protected_and_Recreational_OpenSpace_Polygons/FeatureServer/0 |
| Records | 60,691 polygons |
| Key columns | `SITE_NAME`, `FEE_OWNER`, `OWNER_TYPE`, `PRIM_PURP`, `PUB_ACCESS`, `GIS_ACRES` |
| Format | Shapefile (69MB), File GDB (151MB), ArcGIS Feature Service |
| CRS | EPSG:26986 (Massachusetts State Plane) |
| Used for | Parks and open spaces where people seek shade |
| Verified | 2026-04-08 |

### 31. MassGIS LiDAR Terrain Data

| Field | Value |
| --- | --- |
| URL | https://www.mass.gov/info-details/massgis-data-lidar-terrain-data |
| Format | GeoTIFF DEM (0.5m and 1m resolution), LAS/LAZ point clouds |
| Coverage | All of Massachusetts. Eastern MA at Quality Level 1 from 2021 |
| Used for | Terrain elevation for shadow angle correction |
| Note | Large data volume. Available via NOAA Data Access Viewer or MassMapper |

---

## Real-Time APIs

### 16. Open-Meteo Forecast

| Field | Value |
| --- | --- |
| Endpoint | `https://api.open-meteo.com/v1/forecast?latitude=42.36&longitude=-71.06&current=temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,uv_index` |
| Auth | None |
| Update | Every 1-3 hours |
| Used for | Weather panel (temperature, UV, humidity, wind, precipitation) |
| Verified | 2026-04-08. All 6 current fields returned |

### 17. Open-Meteo Air Quality

| Field | Value |
| --- | --- |
| Endpoint | `https://air-quality-api.open-meteo.com/v1/air-quality?latitude=42.36&longitude=-71.06&current=us_aqi,pm2_5` |
| Auth | None |
| Used for | AQI display |
| Verified | 2026-04-08. Both fields returned. Grid is coarser than forecast API |

### 18. Open-Meteo Hourly Forecast

| Field | Value |
| --- | --- |
| Endpoint | `https://api.open-meteo.com/v1/forecast?latitude=42.36&longitude=-71.06&hourly=precipitation_probability,precipitation,rain,snowfall,weather_code&forecast_days=2` |
| Auth | None |
| Returns | 48 hours hourly data |
| Used for | Rain forecast timeline |
| Verified | 2026-04-08. 48 hours, all 5 fields confirmed |

### 19. RainViewer (Rain Radar)

| Field | Value |
| --- | --- |
| Index | `https://api.rainviewer.com/public/weather-maps.json` |
| Tiles | `https://tilecache.rainviewer.com{path}/256/{z}/{x}/{y}/2/1_1.png` |
| Auth | None |
| Coverage | Global composite radar. ~13 past frames (~2 hours history) |
| Update | Every 10 minutes |
| Used for | Real-time precipitation overlay |
| Verified | 2026-04-08. 13 frames confirmed. Tile URL returns valid PNG |

### 20. IEM NEXRAD (Rain Radar Backup)

| Field | Value |
| --- | --- |
| Tiles | `https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png` |
| Auth | None |
| Coverage | Continental US (NWS base reflectivity) |
| Used for | Fallback if RainViewer unavailable |
| Verified | 2026-04-08. HTTP 200 |

### 21. FEMA Flood Zones (Metro Boston)

| Field | Value |
| --- | --- |
| Endpoint | `https://services.arcgis.com/sFnw0xNflSi8J0uh/arcgis/rest/services/FEMA_2009_DFIRM_100YR_500YR_Clipped_Flood_Zones_Metro_Boston/FeatureServer/0/query?where=1=1&outFields=*&f=geojson` |
| Auth | None |
| Records | 1,036 features |
| Fields | `FLD_ZONE` (AE, X, etc.), `Flood_Zone` (100 YR, 500 YR) |
| Note | Must include `outFields=*` or properties return null. Pagination needed (`exceededTransferLimit`) |
| Used for | Authoritative flood zone polygons |
| Verified | 2026-04-08. Fields confirmed |

### 22. Climate Ready Boston Sea Level Rise

| Field | Value |
| --- | --- |
| Endpoint | `https://services.arcgis.com/sFnw0xNflSi8J0uh/arcgis/rest/services/Climate_Ready_Boston_Sea_Level_Rise_Inundation/FeatureServer/{layer}/query?where=1=1&outFields=*&f=geojson` |
| Auth | None |
| Layers | 0-8 (9"/21"/36" SLR x 10%/1% annual / high tide). Layer 6 recommended (9" SLR, 10% annual) |
| Used for | Sea level rise scenario overlay |
| Verified | 2026-04-08. 9 layers confirmed. Layer 6 = "9inch Sea Level Rise 10pct Annual Flood" |

### 23. NWS API (National Weather Service)

| Field | Value |
| --- | --- |
| Station endpoint | `https://api.weather.gov/stations/KBOS/observations/latest` |
| Hourly forecast | `https://api.weather.gov/gridpoints/BOX/71,90/forecast/hourly` |
| Auth | None (User-Agent header required) |
| Note | Open-Meteo chosen instead. Simpler response format, includes UV and AQI in one call |
| Verified | 2026-04-08. HTTP 200 |

### 24. Open-Meteo Archive API

| Field | Value |
| --- | --- |
| Endpoint | `https://archive-api.open-meteo.com/v1/archive` |
| Auth | None |
| Note | Returns single grid cell for all of Boston. No neighborhood-level resolution |
| Verified | 2026-04-08. HTTP 200 |

### 25. Cambridge FloodViewer

| Field | Value |
| --- | --- |
| URL | `https://www.cambridgema.gov/services/floodmap` |
| Format | GIS web viewer |
| Note | Web viewer only, no downloadable API. FEMA data covers Cambridge area already |
| Verified | 2026-04-08. HTTP 200 |

### 26. MassGIS FEMA National Flood Hazard Layer

| Field | Value |
| --- | --- |
| URL | `https://www.mass.gov/info-details/massgis-data-fema-national-flood-hazard-layer` |
| Download | `https://s3.us-east-1.amazonaws.com/download.massgis.digital.mass.gov/shapefiles/state/nfhl.zip` (193MB) |
| Records | 46,105 polygons statewide |
| Key columns | `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`, `STATIC_BFE` (23 fields total) |
| Format | Shapefile (325MB .shp uncompressed) |
| Note | Boston ArcGIS FEMA endpoint (#21) provides same data via GeoJSON API, easier to integrate |
| Verified | 2026-04-08. Downloaded and inspected. Record count and fields confirmed |

---

## WMO Weather Codes

| Code | Condition | Map Action |
| --- | --- | --- |
| 51/53/55 | Drizzle (light/moderate/dense) | Rain indicator |
| 61/63/65 | Rain (slight/moderate/heavy) | Rain indicator + radar |
| 66/67 | Freezing rain | Rain + ice warning |
| 71/73/75 | Snowfall (slight/moderate/heavy) | Snow indicator |
| 77 | Snow grains | Snow indicator |
| 80-82 | Rain showers | Rain indicator + radar |
| 85/86 | Snow showers | Snow indicator |
| 95/96/99 | Thunderstorm | Rain indicator + radar + alert |

---

## Notes

- Boston and Cambridge 311 complaints use **different column names**: Boston = `latitude`/`longitude`, Cambridge = `lat`/`lng`.
- Boston 311 data is **split across yearly CSV resources** with no single resource ID. New 311 system (2026+) does not include flood/ice categories.
- Boston tree canopy is **EPSG:2249**. Must reproject to WGS84 before use.
- Cambridge tree canopy is **TopoJSON** with **empty properties** (geometry only). Must convert to GeoJSON.
- Boston buildings CSV has 128K records but only **28K have geometry** in `shape_wkt` column. 105K have height data.
- Cambridge buildings have **no explicit height field**. Derive height from `TOP_GL` or `TOP_SL - BASE_ELEV`.
- Cambridge crash data (`gb5w-yva3`) has **low coordinate validity (54%)**. Use with caution.
- Cambridge crime `crime_date_time` is a **text field with date ranges**. Use `date_of_report` for proper timestamps.
- Live datasets (Crime, Food, 311) are **continuously growing**. Record counts are approximate.
- Massachusetts has **no statewide streetlight dataset**. Streetlight data exists only at the municipal level.
- Boston 311 yearly resource IDs: 2024=`dff4d804`, 2023=`e6013a93`, 2022=`81a7b022`, 2021=`f53ebccd`, 2020=`6ff6a6fd`, 2019=`ea2e4696`, 2018=`2be28d90`, 2017=`30022137`, 2016=`b7ea6b1b`, 2015=`c9509ab4`.
