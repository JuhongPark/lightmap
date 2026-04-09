# Technology Research

Last updated: **2026-04-08**.

## Tech Stack

| Layer | Technology | Version | Purpose |
| --- | --- | --- | --- |
| Backend | Python | 3.12 | Runtime (supported until 2028. 3.14 is latest but 3.12 is stable) |
| Backend | FastAPI | >=0.135 | Web framework, API server |
| Backend | uvicorn | >=0.42 | ASGI server |
| Backend | httpx | >=0.28 | HTTP client for weather API calls |
| Shadow engine | pvlib | >=0.15 | Sun position (NREL Solar Position Algorithm) |
| Shadow engine | Shapely | >=2.1 | Geometric projection, polygon union |
| Data processing | GeoPandas | >=1.1 | Bulk spatial data processing |
| Data processing | pandas | >=3.0 | Data manipulation, CSV parsing (requires Python >=3.11) |
| Data processing | numpy | >=2.4 | Numerical computation (requires Python >=3.11) |
| Frontend | MapLibre GL JS | 4.7.1 | WebGL map rendering (v5.x available, evaluate upgrade) |
| Frontend | vanilla JavaScript | -- | UI logic, no framework |
| Base tiles | CARTO Positron | -- | Daytime light base map |
| Base tiles | CARTO Dark Matter | -- | Nighttime dark base map |
| Timezone | ZoneInfo (stdlib) | -- | Boston time (US/Eastern) for time slider |
| Prototype | folium | >=0.20 | Initial static map (replaced by MapLibre) |

### Version Notes

- **pvlib >=0.15** and **folium >=0.20**: Only one release satisfies each constraint (0.15.0, 0.20.0). Consider relaxing if stability is a concern.
- **FastAPI >=0.135** and **httpx >=0.28**: Minimum is very close to the latest release. Tight constraint.
- **pandas >=3.0** and **numpy >=2.4**: These dropped Python 3.10 support. The project effectively requires Python >=3.11.
- **MapLibre GL JS 4.7.1**: Last release in the v4.x line. v5.22.0 is the latest (released 2026-04-03). APIs have evolved between v4 and v5. Evaluate whether to upgrade.

## Technology Decisions

### MapLibre GL JS over Mapbox GL JS

MapLibre GL JS is an open-source fork of Mapbox GL JS (BSD 3-Clause), created after Mapbox switched to a non-OSS license in December 2020. No API key required. Handles 46K building polygons + 80K streetlight heatmap via WebGL.

Note: MapLibre was originally API-compatible with Mapbox GL JS at v1.x, but the APIs have diverged significantly since then. Not a drop-in replacement for current Mapbox versions.

CDN verified:
- `https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js` (HTTP 200)
- `https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js` (HTTP 200)

### MapLibre GL JS over Leaflet

Leaflet cannot efficiently render 46K building polygons. MapLibre uses WebGL with native vector tile support and built-in style switching.

### FastAPI over Flask/Django

FastAPI provides async support, is lightweight, and has built-in API docs. Flask would work but lacks async. Django is too heavyweight for a single-page map app.

### pvlib over suncalc

pvlib (Python) uses the NREL Solar Position Algorithm with high precision. suncalc (JavaScript, by mourner) is lighter but less precise. Server-side computation in Python makes pvlib the better fit.

### 2D geometric projection over 3D WebGL

Shadowmap.org uses Three.js with GPU Shadow Mapping and 3D models. LightMap uses simpler 2D footprint extrusion (height x tan formula). Sufficient for top-down map view with much simpler implementation.

### folium replaced by FastAPI + MapLibre

folium generates static HTML with no real-time interaction. Cannot implement time slider, layer toggles, or dynamic shadow updates. Replaced in early prototyping.

### Vanilla JS over React/Vue

Single-page map app. No build step needed. Framework overhead not justified for this scope.

## Base Tile Verification

| Tile Set | URL Pattern | Auth | Verified |
| --- | --- | --- | --- |
| CARTO Positron | `https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png` | None | HTTP 200 |
| CARTO Dark Matter | `https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png` | None | HTTP 200 |

## Researched but Not Used

### Tippecanoe + MBTiles

Vector tile generation for large datasets. Not needed because MapLibre handles 46K GeoJSON polygons directly via WebGL without pre-tiling.

### Draco Compression

3D geometry compression (Shadowmap.org: Vienna 2GB to 48MB). Not applicable because LightMap uses 2D GeoJSON, not 3D meshes.

### Google 3D Tiles

Photorealistic 3D building rendering used by Shadowmap.org. Paid service. LightMap uses public city building data instead.

### mapbox-gl-shadow-simulator

ShadeMap's shadow engine (npm: `mapbox-gl-shadow-simulator`, v0.68.2). Supports both Mapbox GL JS and MapLibre GL JS. Requires API key from shademap.app. LightMap builds a custom server-side shadow engine instead.

### shadow-mapper

Python project for generating shadow maps from OSM data (GitHub: `perliedman/shadow-mapper`, 71 stars). Not published on PyPI. LightMap built a custom engine using pvlib + Shapely for more control.

### Claude API (anthropic package)

Optional Phase 6 AI guide agent for conversational map insights. PyPI package `anthropic` (latest v0.92.0). Deferred to post-course. Not core functionality.

### PostgreSQL + PostGIS

Spatial database for faster shadow computation and indexing. Current Python in-memory computation with caching works for 46K buildings. PostGIS could enable faster computation at scale. Evaluation planned.

## Competitor Technology Reference

| Competitor | Technology | What LightMap Learned |
| --- | --- | --- |
| ShadeMap | mapbox-gl-shadow-simulator, Mapbox/MapLibre GL JS | Click-to-inspect pattern, heatmap color ramp |
| Shadowmap.org | 3D WebGL (likely Three.js), GPU Shadow Mapping, Google 3D Tiles | Time slider UX, sun path visualization |
| Safest Way | Streetlight density per road segment | "Fully lit = lamp every 30m" brightness classification |
| Light Pollution Map | VIIRS satellite data, Bortle scale | Overlay opacity control (60% default) |
| First Street | Flood risk scoring (1-10 scale) | Simple risk display over numeric scores |
| Climate Ready Boston | ArcGIS, sea level rise layers | Polygon overlay approach for flood zones |

## UI/UX Patterns

### Color Conventions

| Data Type | Color | Reference |
| --- | --- | --- |
| Shadow/shade | Grey / dark | ShadeMap |
| Water / flooding | Blue | Climate Ready Boston, First Street |
| Heat / high risk | Red / orange | Climate Ready Boston |
| Brightness / lit areas | Yellow to white | Safest Way, Light Pollution Map |
| Intensity heatmaps | Dark to blue to green to red | ShadeMap |

### Patterns Adopted

- Full-screen map with floating controls
- Time controls at bottom (horizontal = time)
- Layer controls in collapsible right panel
- Click-to-inspect everywhere
- Overlay opacity control
- Onboarding modal

### Anti-Patterns Avoided

- Inverted time sliders (ShadeMap criticism)
- Non-collapsible controls eating screen space (Shadowmap)
- No map legend (First Street)
- Scientific units without plain language (Light Pollution Map)

## Coordinate Systems

| CRS | Used By | Notes |
| --- | --- | --- |
| WGS84 (EPSG:4326) | All APIs, frontend, most datasets | Primary coordinate system |
| EPSG:2249 (MA State Plane) | Boston tree canopy (TreeTops2019) | Must reproject to WGS84 |
