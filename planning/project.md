# Project Description

> Status: landed. This document describes the shipped implementation. History of how we got here lives in `prototype-plan.md`, `scaleup-plan.md`, `time-slider-plan.md`, and `extensions-plan.md`.

## Motivation

I run in Boston. On hot afternoons, there is no way to know which sidewalks are shaded. After dark, there is no map that shows which streets are well-lit. The name LightMap carries a double meaning: during the day, light refers to where sunlight is absent (shade). At night, it refers to where artificial light is present (safety).

The public data already exists. Boston and Cambridge publish building footprints with heights, streetlight locations, and tree canopy polygons. No one has combined these into a single time-aware map.

## Problem

Two questions no map can answer today:

1. Where is shade right now?
2. Where is light right now?

The data is public. Nobody has put it on one map.

## Shipped Features

The single production artifact is `docs/prototype_timeslider.html`. Every feature below is live in that file.

### Daytime shadow map

Sun position comes from pvlib (NREL Solar Position Algorithm). Each building footprint is translated along the opposite azimuth by `height / tan(altitude)` and unioned with the original, producing a 2D shadow polygon. Shadow color is mapped from building height. Tree canopy crowns are projected with the same sun angle and an assumed 10 m canopy height, so the daytime view combines building shade and tree shade in one pass.

Coverage: 123K buildings with height (105K Boston + 18K Cambridge) and ~59K per-crown tree polygons inside `INITIAL_BBOX` (MIT, central Cambridge, Back Bay, downtown Boston).

### Nighttime brightness map

After sunset the basemap fades from CARTO Positron to CARTO Dark Matter. 80K streetlights render as a pure-yellow heatmap. ~760 OSM venues (restaurants, bars, cafes) toggle visible based on their `opening_hours` tag via `opening_hours.js`. ~830 violent-crime red-diamond pins (last 2 years, night hours, INITIAL_BBOX) sit on top.

### Time slider

A client-side date + time picker scrubs through any date. Shadows sweep with the sun. Day/night transition triggers on solar altitude crossing `TWILIGHT_START = 0` and fully completes at `DAY_THRESHOLD = 15`. Auto-play advances at a fixed 1 s per slot cadence, independent of per-tick render cost.

### Weather and UV strip

The info panel fetches `temperature_2m_max/min` and `uv_index_max` from Open-Meteo for the slider's selected date. Forecast API for today + next 16 days, archive API for historical dates. Fetched live from the browser. No auth.

## Shipped Architecture

LightMap is a **static HTML** artifact. No server, no API, no runtime. The Python side is a build-time pipeline; the output is one self-contained file served by GitHub Pages.

```
Build time (Python)                       Run time (browser)
-------------------                       ------------------
data/ raw GeoJSON + CSV                   docs/prototype_timeslider.html
  |                                         |
scripts/download_*.py --------+             +-- Leaflet + folium runtime
scripts/preprocess_buildings.py             +-- opening_hours.js
  |                                         +-- PNG shadow preview
src/shadow/compute.py (pvlib, Shapely)      +-- GeoJSON sidecar (gzip)
src/shadow/postgis_compute.py (optional)    +-- Client-side:
src/render/strategies.py                         - sun position lookup
src/prototype.py (folium HTML emitter)           - day/night blend
  |                                              - Open-Meteo fetch
docs/prototype_timeslider.html --------------> browser
docs/shadows*.geojson.gz
docs/buildings*.geojson.gz
```

### Design Decisions (as shipped)

1. **Static HTML over client-server.** The original proposal called for FastAPI + MapLibre GL JS. That path was dropped after the folium + Leaflet prototype hit all functional targets at 100% scale. No server avoids hosting cost, deploy complexity, and the "what runs when the demo grader opens it in 6 months" problem. See `tech-research.md` for the deferral rationale.
2. **Shadow projection as a build-time step with PostGIS fallback.** `src/shadow/compute.py` runs in pure Python for 1%-50% scales. `src/shadow/postgis_compute.py` parallelizes the projection as a SQL query for the 100% build. PostGIS auto-detects at build time; set `LIGHTMAP_NO_POSTGIS=1` to force pure Python.
3. **Pre-computed shadow PNG + deferred vector fetch** (render strategy r13 in `render-optimization-plan.md`). PNG flashes first for fast LCP, then the vector layer streams in for interactive click.
4. **INITIAL_BBOX hard cutoff.** Every dataset is filtered to the Boston + Cambridge core bbox before serialization. Keeps `prototype_timeslider.html` at ~27 MB even with per-crown tree canopy bundled in.
5. **Coordinate arrays, not GeoJSON, for streetlights.** Flat `[lat, lon]` pairs inside a JS array. Cheaper than `Feature[]` for 80K heatmap points.

## Data Sources (shipped)

| Dataset | Records (inside bbox) | Source | Role |
| --- | --- | --- | --- |
| Boston buildings + height | 105K | BPDA (2010 survey) | Shadow projection |
| Cambridge buildings + height | 18K | Cambridge GIS (2018) | Shadow projection |
| Boston streetlights | 74K | data.boston.gov CKAN | Brightness heatmap |
| Cambridge streetlights | 6K | Cambridge GIS | Brightness heatmap |
| Tree canopy (Cambridge 2018 + Boston 2019-2024) | ~59K per-crown | Cambridge GIS + BPDA Tree Canopy | Daytime tree shade |
| OSM water | 175 features | OpenStreetMap via Overpass | Mask so tree canopy never floats over water |
| OSM amenity POIs with `opening_hours` | 760 | OpenStreetMap via Overpass | Night-mode venue dots |
| Boston violent crime (last 2 years, night hours) | ~830 | data.boston.gov CKAN | Night-mode red-diamond pins |
| Open-Meteo weather + UV | 1 record per slider date | Open-Meteo API | Info-panel strip (live fetch) |

Full catalog with fields, verification dates, and unused datasets: `data-catalog.md`.

## Risks (as mitigated)

| Risk | Actual outcome | What made it workable |
| --- | --- | --- |
| Data freshness (2010-2024 data) | Vintage shown in info panel. | Users see the data year. No live feed was ever promised. |
| Shadow compute at 123K buildings | Build takes ~12 s wall on a loaded host. | PostGIS parallel projection + STRtree coverage + WKB batch decode. See `optimization-plan.md`. |
| 123K polygons killing browser render | Time-to-interactive ~1.8 s at 100% scale. | Color-batched single `L.polygon` per color + point-in-polygon click handler + PNG preview. See `render-optimization-plan.md`. |
| GitHub Pages deploy size | 9.48 MB gzipped total. | `INITIAL_BBOX` pre-filter + 3 m geometric simplification + 5-decimal coordinate precision. See `deploy-size-trim-plan.md`. |
| 27 MB single HTML from bundled tree canopy | Still under GitHub Pages limits; open follow-up in `TODO.md`. | Per-crown polygons kept for accurate boundary. Further trim is tracked, not blocking. |
