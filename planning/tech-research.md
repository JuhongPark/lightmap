# Technology Research

Initial research: **2026-04-08**. Final tech stack revised: **2026-04-21**.

## Tech Stack (as shipped)

The shipped product is a single static HTML file. No server runtime.

| Layer | Technology | Version | Purpose |
| --- | --- | --- | --- |
| Build runtime | Python | >=3.12 | Offline pipeline. Requires >=3.11 transitively via pandas/numpy |
| Shadow engine | pvlib | >=0.15 | Sun position (NREL Solar Position Algorithm) |
| Shadow engine | Shapely | >=2.1 | Geometric projection, polygon union, WKB batch decode |
| Shadow engine (100% path) | PostgreSQL + PostGIS | 16 / 3.4 | Parallel projection as a SQL query. Optional, auto-detected at build |
| Data processing | pandas | >=3.0 | CSV parsing, tabular prep |
| Data processing | numpy | >=2.4 | Backs shapely / pvlib ufuncs |
| Raster coverage | rasterio | >=1.5 | Shadow coverage area via rasterization (v7e in `optimization-plan.md`) |
| Download | httpx | >=0.28 | HTTP client for data ingestion scripts |
| Map emitter | folium | >=0.20 | Generates the final Leaflet HTML at build time |
| Browser renderer | Leaflet (via folium) | 1.9.x | 2D map, canvas overlay, tile layers |
| Browser helper | opening_hours.js | latest CDN | Parses OSM `opening_hours` tags for venue time-gating |
| Browser helper | Open-Meteo | -- | Live weather + UV fetch per slider date, no auth |
| Base tiles | CARTO Positron | -- | Daytime basemap |
| Base tiles | CARTO Dark Matter | -- | Nighttime basemap |
| Timezone | ZoneInfo (stdlib) | -- | Boston time (US/Eastern) for slider |
| DB driver | psycopg2-binary | >=2.9 | Only used when PostGIS is available |

### Version Notes

- **pvlib >=0.15** and **folium >=0.20**: only one release satisfies each constraint (0.15.0, 0.20.0). Relax if stability becomes an issue.
- **pandas >=3.0** and **numpy >=2.4**: dropped Python 3.10 support. The project effectively requires Python >=3.11.
- **PostGIS is optional.** Build auto-detects the container. Set `LIGHTMAP_NO_POSTGIS=1` to force the pure Python fallback. The fallback produces identical output at measurable but tolerable cost at 100% scale.

## Technology Decisions (as shipped)

### Static HTML over FastAPI + MapLibre

Early research planned a FastAPI backend serving `/api/shadows` to a MapLibre GL JS frontend. During the prototype phase the folium + Leaflet static HTML hit every functional target (interactive time slider, layer toggles, click-to-inspect, day/night auto-switch) without the operational cost of hosting a server. We kept static HTML as the shipping format. FastAPI + MapLibre GL JS is noted as a future consideration in "Researched but Not Used" below.

### folium emitter over hand-rolled Leaflet

folium produces well-formed Leaflet HTML with the right viewport clamp, base layers, and plugin wiring. Hand-rolling the same HTML would save no meaningful bytes and cost more maintenance. Custom JS is injected via `m.get_root().html.add_child(Element(...))` where folium's defaults are insufficient (custom slider, client-side sun position, day/night blend).

### pvlib over suncalc.js

pvlib uses the NREL Solar Position Algorithm with high precision. The altitude + azimuth lookup is pre-computed at build time and baked into a JS table, so the browser never runs a sun-position library. suncalc.js stays in scope for a future client-side shadow engine if we ever revive Phase 2 of `time-slider-plan.md`.

### 2D geometric projection over 3D WebGL

Shadowmap.org uses Three.js with GPU Shadow Mapping and 3D models. LightMap uses simpler 2D footprint extrusion (`height * tan(altitude)` translate + union). Top-down map view does not benefit enough from 3D to justify the complexity.

### Color-batched polygons over per-feature Leaflet objects

`L.GeoJSON` creates one `L.Polygon` per feature. At 123K features the per-object overhead dominates render time. Strategy r12 in `render-optimization-plan.md` batches shadows into ~4 `L.polygon` objects (one per color bin). r13 adds a ray-cast point-in-polygon click handler with a pre-computed bbox index so clickability is preserved. 2.7x faster than the per-feature path.

### PostgreSQL + PostGIS as an optional fast path

At 100% scale the Python shadow projection dominates build time. PostGIS with 8 parallel workers on an indexed table turns projection into a parallel SQL query, roughly halving wall time on a loaded host. Kept optional because the pure Python path is sufficient for demo and course submission.

### Vanilla JS over React/Vue

Single-page map app. No build step needed. Framework overhead not justified for this scope.

## Base Tile Verification

| Tile Set | URL Pattern | Auth | Verified |
| --- | --- | --- | --- |
| CARTO Positron | `https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png` | None | HTTP 200 |
| CARTO Dark Matter | `https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png` | None | HTTP 200 |

## Researched but Not Used

### Tippecanoe + MBTiles

Vector tile generation for large datasets. Not needed because the color-batched Leaflet renderer draws all 123K shadow features in a single canvas without pre-tiling.

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

### FastAPI + uvicorn backend

Originally planned to serve `/api/shadows?time=...` and static GeoJSON endpoints. Dropped when the static-HTML prototype hit every functional target. Reintroducing a backend would unlock continuous-minute slider and any-date queries without pre-bake, but at hosting + deploy cost. Candidate for a future Phase 2 of `time-slider-plan.md`.

### MapLibre GL JS frontend

Paired with the FastAPI backend in the original plan. WebGL-based, handles large vector datasets natively. Not needed because Leaflet + color-batched polygons + canvas overlay render 123K shadow features in ~1.8 s on the final strategy (`render-optimization-plan.md` r13). Reintroduce if future features need 3D extrusion, vector tiles with feature state, or GPU-side styling.

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
| EPSG:2249 (MA State Plane) | Boston tree canopy (BPDA TreeTops 2019-2024) | Reprojected to WGS84 inside `scripts/download_trees.py` via ogr2ogr |
