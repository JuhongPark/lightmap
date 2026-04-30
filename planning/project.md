# Project Description

> Status: landed. This document describes the shipped implementation. History of how we got here lives in `prototype-plan.md`, `scaleup-plan.md`, `time-slider-plan.md`, and `extensions-plan.md`.

## Motivation

I run in Boston. On hot afternoons, there is no way to know which sidewalks are shaded. After dark, there is no map that shows which streets are well-lit. The name LightMap carries a double meaning: during the day, light refers to where sunlight is absent (shade). At night, it refers to where artificial light is present (visibility).

The submitted proposal captures that double meaning as: "Shade by day. Light by night."

The public data already exists. Boston and Cambridge publish building footprints with heights, streetlight locations, and tree canopy polygons. No one has combined these into a single time-aware map.

## Problem

Two questions no map can answer today:

1. Where is shade right now?
2. Where is light right now?

The data is public. Nobody has put it on one map.

## Shipped Features

The single production artifact is `docs/prototype_timeslider.html`. Every feature below is live in that file.

### Daytime shadow map

Sun position comes from pvlib in the Python shadow engine and SunCalc in the browser time-slider. Each building footprint is translated along the opposite azimuth by `height / tan(altitude)` and unioned with the original, producing a 2D shadow polygon. Shadow darkness is mapped from building height. Tree canopy crowns are baked into a static PNG shade overlay so the daytime view combines moving building shade with lightweight tree-canopy shade context.

Coverage: 123K buildings with height (105K Boston + 18K Cambridge) and ~59K per-crown tree polygons inside `INITIAL_BBOX` (MIT, central Cambridge, Back Bay, downtown Boston).

### Nighttime brightness map

After sunset the basemap fades from CARTO Positron to CARTO Dark Matter. 80K streetlights render as a pure-yellow heatmap. ~760 OSM venues (restaurants, bars, cafes) toggle visible based on their `opening_hours` tag via `opening_hours.js`. Historic incident records are kept as an optional reference overlay, not as the main night-mode claim.

### Time slider

A client-side date + time picker scrubs through any date. Building shadows sweep with the sun. Day/night transition triggers on solar altitude crossing `TWILIGHT_START = 0` and fully completes at `DAY_THRESHOLD = 15`. Auto-play advances at a fixed 1 s per slot cadence, independent of per-tick render cost.

### Weather and UV strip

The info panel fetches `temperature_2m_max/min`, `apparent_temperature_max`, and `uv_index_max` from Open-Meteo for the slider's selected date. Forecast API for today + next 16 days, archive API for historical dates. Fetched live from the browser. No auth.

### Heat-response overlay

Three-step fallback for dangerous heat. Daytime shade (buildings + trees) is the default. When the live weather fetch crosses any of `tmax >= 89.6 F (32 C)`, `apparent_temperature_max >= 91.4 F (33 C)`, or `UV >= 8`, the info panel lights a red `HEAT` badge and a cooling-center layer (libraries, community centres, town halls from OSM, the proxy the City of Boston opens during heat emergencies) appears on the map. 24-hour ER markers stay on at all times so the third fallback is always reachable.

### No-data mask

The INITIAL_BBOX is divided into a 25x36 grid (~220 m x ~205 m cells). Cells with no buildings get a translucent dark fill so the viewer can see where the dataset ends instead of treating empty ground as "no activity." All point layers (crime, ER, venue, cooling, streetlight) are filtered server-side against the same grid so the masked area is cleanly empty rather than sprinkled with orphan dots. Interior empty cells fully surrounded by buildings (parks, plazas, parking lots) are flood-filled as covered so only the outer data boundary is masked.

## Shipped Architecture

LightMap is a **static HTML** artifact. No server, no API, no runtime. The Python side is a build-time pipeline. The output is one self-contained file served by GitHub Pages.

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
2. **Shadow projection as a build-time step with PostGIS fallback.** `src/shadow/compute.py` runs in pure Python for 1%-50% scales. `src/shadow/postgis_compute.py` parallelizes the projection as a SQL query for the 100% build. PostGIS auto-detects at build time. Set `LIGHTMAP_NO_POSTGIS=1` to force pure Python.
3. **Pre-computed shadow PNG + deferred vector fetch** (render strategy r13 in `render-optimization-plan.md`). PNG flashes first for fast LCP, then the vector layer streams in for interactive click.
4. **INITIAL_BBOX hard cutoff.** Every dataset is filtered to the Boston + Cambridge core bbox before serialization.
5. **Tree canopy as a baked PNG overlay, not embedded polygons.** ~59K per-crown polygons were rasterized into a single ~760 KB `trees_canopy.png` image. The browser paints one bitmap on pan/zoom instead of tens of thousands of canvas polygons, and the shipped HTML dropped from ~27 MB to ~15 MB.
6. **Coordinate arrays, not GeoJSON, for streetlights.** Flat `[lat, lon]` pairs inside a JS array. Cheaper than `Feature[]` for 80K heatmap points.

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
| Boston violent crime (last 2 years, night hours) | ~830 | data.boston.gov CKAN | Optional historic incident reference pins |
| OSM hospitals (`amenity=hospital` plus `amenity=clinic` with `emergency=yes`) | 16 | OpenStreetMap via Overpass | 24-hour ER markers (`emergency=yes` subset) for the heat-response fallback |
| OSM cooling proxy (`amenity=library`, `community_centre`, `townhall`) | ~136 | OpenStreetMap via Overpass | Cooling-center markers, shown only when HEAT threshold is active |
| Open-Meteo weather + UV | 1 record per slider date | Open-Meteo API | Info-panel strip (live fetch) and HEAT-threshold trigger (tmax, apparent_temperature_max, UV) |

Full catalog with fields, verification dates, and unused datasets: `data-catalog.md`.

## Risks (as mitigated)

| Risk | Actual outcome | What made it workable |
| --- | --- | --- |
| Data freshness (2010-2024 data) | Vintage shown in info panel. | Users see the data year. No live feed was ever promised. |
| Shadow compute at 123K buildings | Build takes ~12 s wall on a loaded host. | PostGIS parallel projection + STRtree coverage + WKB batch decode. See `optimization-plan.md`. |
| Time-slider redraw felt sluggish | Local smoke redraw now lands around ~40 ms for ~6.9K visible shadows. | Replaced per-tick `L.GeoJSON.addData` polygon rebuilding with a direct canvas layer that paints computed shadow rings. See `shadow-lightmap-roadmap.md`. |
| 123K polygons killing browser render | Time-to-interactive ~1.8 s at 100% scale. | Color-batched single `L.polygon` per color + point-in-polygon click handler + PNG preview. See `render-optimization-plan.md`. |
| GitHub Pages deploy size | 9.48 MB gzipped total. | `INITIAL_BBOX` pre-filter + 3 m geometric simplification + 5-decimal coordinate precision. See `deploy-size-trim-plan.md`. |
| 27 MB single HTML from bundled tree canopy | Resolved: baked the canopy into a ~760 KB PNG overlay, so the shipped HTML is ~15 MB. | PNG preserves the canopy boundary at ~1.85 m/pixel inside `INITIAL_BBOX`. Further trim candidates tracked in `TODO.md`. |
