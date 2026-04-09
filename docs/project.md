# Project Description

## Motivation

I run in Boston. On hot afternoons, there is no way to know which sidewalks are shaded. After dark, there is no map that shows which streets are well-lit. The name LightMap carries a double meaning: during the day, light refers to where sunlight is absent (shade). At night, it refers to where artificial light is present (safety).

The public data already exists. Boston and Cambridge publish building footprints with heights, streetlight locations, and tree canopy polygons. No one has combined these into a single time-aware map.

## Problem

Two questions no map can answer today:

1. Where is shade right now?
2. Where is light right now?

The data is public. Nobody has put it on one map.

## Planned Features

The features below are planned but not yet implemented.

### Daytime Shadow Map

Calculate shadow positions from sun angle and building heights using pvlib and Shapely. The dataset covers 122K buildings and 138K tree canopy polygons. Shadows will be projected as 2D polygons on the map surface, updating as the user moves through time.

### Nighttime Brightness Map

Visualize 80K streetlight locations as a heatmap layer. Display 3K stores as activity markers. Click to inspect brightness level.

### Day/Night Auto-Switch

Automatically switch the map theme based on solar elevation. CARTO Positron for daytime, CARTO Dark Matter for nighttime. The transition will follow the actual sunrise and sunset times for the current date.

### Time Slider

A 24-hour slider with 30-minute steps. Users can scrub through the day to observe how shadows shift as the sun moves across the sky. The slider will also trigger the day/night theme transition at the appropriate solar elevation.

### Interactions

- **Click-to-inspect** -- Click any point to see shadow status (day) or brightness level (night).
- **Layer toggles** -- Show/hide buildings, shadows, streetlights, tree canopy, stores independently.
- **Onboarding modal** -- First-visit walkthrough explaining map controls and data layers.
- **About panel** -- Full data source attribution, last-updated dates, and known limitations for transparency.

## Planned Architecture

The system is designed as a two-tier application: a Python backend that performs computations and serves data, and a browser frontend that renders the map.

```
Browser                          Server
------                          ------
MapLibre GL JS                  FastAPI (uvicorn)
  + Time slider        --->       /api/shadows (pvlib + Shapely)
  + Layer controls      --->       /api/data/* (static GeoJSON/CSV)
                                data/
                                  buildings.geojson
                                  streetlights.csv
                                  trees.geojson
```

### Design Decisions

1. **Server-side shadow calculation** -- Shadows are computed on the backend with pvlib and Shapely, not in the browser with WebGL. This keeps the frontend simple and avoids shipping a geometry engine to the client.
2. **Coordinate arrays for streetlights** -- Streetlight positions are sent as flat coordinate arrays instead of GeoJSON to reduce payload size for 80K points.
3. **MapLibre GL JS** -- WebGL-based renderer capable of handling large vector datasets without performance degradation.
4. **Vanilla JavaScript** -- No frontend framework. The UI is simple enough that a framework would add complexity without benefit.

### Data Directory

The `data/` directory will contain GeoJSON, CSV, and JSON files. These files are gitignored and downloaded via data preparation scripts.

## Data Sources

| Dataset | Records | Source |
| --- | --- | --- |
| Buildings | 122K | BPDA + Cambridge GIS |
| Streetlights | 80K | data.boston.gov + Cambridge GIS |
| Tree canopy | 138K | Boston + Cambridge GIS |
| Stores | 3K | data.boston.gov |

## Risks

| Risk | Problem | Mitigation |
| --- | --- | --- |
| Data freshness | Boston building heights from 2010, Cambridge from 2018. New buildings will be missing. | Show data year in the app. Users see what they are looking at. |
| Computing speed | 122K buildings per time slot. Shadow projection is computationally expensive. | Server-side cache. Compute once per slot, then reuse instantly. |

All data sources, timestamps, and limitations will be displayed in the About panel so users can judge the reliability of what they see.
