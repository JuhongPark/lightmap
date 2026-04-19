# LightMap Extensions Plan

Related: [Project Description](project.md), [Prototype Plan](prototype-plan.md), [Time Slider Plan](time-slider-plan.md), [Data Catalog](data-catalog.md), [Render Optimization Plan](render-optimization-plan.md)

## Goal

Land three additional data layers promised (or close to) in the proposal, plus a round of client-side render speedups so all these new features stay responsive inside the viewport.

## Sequencing

The three layers are sequenced deliberately. Tree canopy is highest value (completes the "Shade by day" story and is on the proposal title slide). Weather is second (proposal Technology slide, low implementation cost). Safety overlay is third (internal roadmap only, optional).

| # | Layer | In proposal? | Rough effort | Unlocks |
| --- | --- | :---: | --- | --- |
| 1 | Tree canopy | ✅ Title slide + Next Steps "Trees + Weather" | Large | "Shade by day" completeness, visual richness |
| 2 | Weather / UV | ✅ Technology slide (Open-Meteo) | Small | Contextual info panel, "real-world" framing |
| 3 | Safety (crime + crash) | ❌ Internal roadmap only (`prototype-plan.md`) | Medium | "Light by night" richness, optional |
| X | Speed improvements | Everywhere | Medium | Required so the three layers do not regress slider perf |

The speed pass is interleaved: profile after each layer lands, fix the regression before moving on.

## Hard rules carried over

- **INITIAL_BBOX cutoff** for every new dataset. Drop anything outside before it hits the client.
- **Free and no-auth data sources only** (per user directive).
- **No Google Maps scraping.** All data must be from public, terms-compatible APIs.
- **Day/night gating** via the existing `TWILIGHT_START = 0` and `DAY_THRESHOLD = 15` thresholds.
- **Same zoom / pan clamp** (`INITIAL_BBOX` + zoom 15-18) on every map, including overlays.
- **Commit discipline**: one purpose per commit, short subject, no body.

---

## Phase 1. Tree canopy

### Why it is top priority

The proposal title slide shows "138K Trees" as one of the three pillars. `project.md` lists tree canopy in the Data Sources table and in the Layer Toggle list. Without it the "Shade by day" headline is incomplete. Trees also fill the visual gap between building shadows (which give sharp, angular coverage) and open blocks (which look barren at midday).

### Data

| Source | Records | Format | Notes |
| --- | ---: | --- | --- |
| Boston tree canopy (BPDA) | ~108K | Shapefile ZIP (~1 GB download) | EPSG:2249 → reproject to WGS84 |
| Cambridge tree canopy | ~144K | TopoJSON | Empty properties, convert to GeoJSON |

Combined: ~252K polygons. Inside `INITIAL_BBOX` probably ~30 to 60K (similar ratio to buildings).

### Scope decisions to close before coding

1. **Visual model**: render tree polygons as flat semi-transparent green overlays (simple), OR project tree shadows along the same sun angle as buildings (faithful, expensive).
2. **Tree height**: BPDA shapefile has canopy height in one attribute. Cambridge has no height. Either assume a flat 10 m for shadow projection, or skip shadow projection entirely and ship tree outlines only.
3. **Simplification tolerance**: 1 m, 3 m, or 5 m. Affects file size vs visual fidelity.
4. **When to show**: always, or only during day (altitude > 0)? Most useful as a day layer.

### Tasks

1. **Downloader**: `scripts/download_tree_canopy.py`.
   - Boston: fetch shapefile ZIP, unzip, read with `geopandas`, reproject to EPSG:4326, filter to `INITIAL_BBOX`, simplify, write `data/trees/boston_trees.geojson`.
   - Cambridge: fetch TopoJSON, convert to GeoJSON with `topojson` Python module (or `shapely` + inline converter), filter to bbox, simplify, write `data/trees/cambridge_trees.geojson`.
   - Merge into `data/trees/trees.geojson`.
   - Expected final size: under 5 MB after simplification at tolerance ~3 m.
2. **Shadow engine support** (optional, Phase 1 can skip):
   - Extend `src/shadow/compute.py` to accept a list of (polygon, height) for trees alongside buildings.
   - Trees use an assumed uniform height (~10 m).
   - Decision: either merge with building shadows into one GeoJSON, or keep trees as a separate `docs/trees.geojson` sidecar.
3. **Standalone day map**: `build_day_map` adds a `folium.GeoJson` layer for trees, styled `fillColor="#10b981"`, `fillOpacity=0.2`, `weight=0`.
4. **Time slider**: embed tree footprints alongside buildings, project tree shadows client-side if the "faithful" path is chosen.
5. **README**: add tree canopy row to Data Sources table with real size and license.

### Performance risk

At 100% scale with client-side rendering, adding ~50K tree polygons to the shadow layer nearly doubles the work per tick. Mitigations:

- Aggressive simplification (3 m tolerance trims ~70% of vertices).
- Viewport culling (already in place for buildings).
- Consider separate tree layer so shadow cache logic stays simple.

If render regresses past the tolerable bar at zoom 15, stop and do Phase X optimizations before adding more.

### Expected outputs

- `data/trees/trees.geojson` (gitignored, under 10 MB after simplification)
- `scripts/download_tree_canopy.py`
- Updated `build_day_map`, `build_time_slider_map`
- Updated README

---

## Phase 2. Weather and UV

### Why it is next

Tiny implementation cost, explicit proposal promise (Technology slide: Open-Meteo API for "Temperature, UV index, air quality").

### Data

**Open-Meteo API**, free, no auth required.

Endpoints planned to use:

- Current + forecast: `https://api.open-meteo.com/v1/forecast?latitude=42.36&longitude=-71.09&current=temperature_2m,uv_index,weather_code&hourly=temperature_2m,uv_index`
- Historical: `https://archive-api.open-meteo.com/v1/archive?latitude=42.36&longitude=-71.09&start_date=2020-01-01&end_date=2020-01-31&hourly=temperature_2m,uv_index`
- Air quality: `https://air-quality-api.open-meteo.com/v1/air-quality?latitude=42.36&longitude=-71.09&current=us_aqi`

Returns JSON, sub-5 KB per request.

### Scope decisions

1. **Fetch timing**: live on page load (always current conditions) OR baked at build time (static snapshot).
2. **Time slider interaction**: slider on a past date → fetch archive API. Slider on future date (<= 16 days out) → fetch forecast. Beyond that → grey out.
3. **Visual model**: small info widget in the corner? Colour tint over the map? Icon pills next to the date label?
4. **Fields to show**: temperature, UV index, air quality (US AQI). Precipitation optional.

### Tasks

1. **Client-side fetch module** (pure JS, no Python changes): make the request when slider date or current time changes. Cache by `(date, hour)` in a `Map`.
2. **Info widget**: small rounded card next to the slider with `🌡 22°C · UV 6 · AQI 42`. Night slots show moon phase instead of UV.
3. **Debounce**: only call Open-Meteo once per slider settle (on `change`, not `input`), since each call is ~200 ms and slider scrubs at 2+ Hz.
4. **Fallback**: on fetch failure show "weather unavailable" and proceed.
5. **README**: add Open-Meteo to Data Sources with attribution ("Weather data by Open-Meteo.com").

### Performance risk

Low. A single HTTP call per date change, subsecond. No render cost.

### Expected outputs

- New JS module in `build_time_slider_map` injected script (or a small Python helper to emit the widget HTML + JS).
- Updated README.

---

## Phase 3. Safety overlay (crime + crash)

### Why it is optional

Not in the submitted proposal. On the internal roadmap in `prototype-plan.md` as "Safety incident data overlay (crime heatmap)" and the `data/safety/` directory is already reserved. Proceeds only if Phases 1 and 2 landed without speed regression.

### Data

| Source | Records | Filter before ship | Used for |
| --- | ---: | --- | --- |
| Boston crime (data.boston.gov CKAN `b973d8cb-...`) | ~258K | Last 2 years + night hours (18-06) + bbox | Nighttime safety heatmap |
| Boston crashes (data.boston.gov CKAN `e4bfe397-...`) | ~43K | Last 2 years + bbox | Night-mode pin markers |
| Cambridge crime (Socrata `xuad-73uj`) | TBD | Same filters | Cambridge coverage |
| Cambridge crashes (Socrata `gb5w-yva3`) | TBD | 54% coord-valid, use with caution | Cambridge coverage |

After filters, expected under 20 MB of raw points for last 2 years × night hours × bbox.

### Scope decisions

1. **Visualization**: heatmap (abstract density) vs per-point dots (specific incidents). Heatmap is less stigmatizing. Pick heatmap for crime, pin markers for crashes.
2. **Time filter**: static "last 2 years" aggregate, OR date-scoped to the slider's current date.
3. **Privacy framing**: add explicit attribution and time period. Include "incidents are historic aggregates, not current conditions" note in the info panel.
4. **Category split**: crime vs crash as separate toggles, or merged "safety concern" layer.

### Tasks

1. **Downloader**: `scripts/download_safety.py`.
   - Fetch crime + crash CSVs from CKAN / Socrata.
   - Filter by date range + coordinates + bbox.
   - Write `data/safety/crime.geojson`, `data/safety/crashes.geojson`.
2. **Night layer**: in `build_time_slider_map`, load both files. Add as folium heatmap (crime, subtle red gradient, low opacity) and folium markers (crashes, orange pins with popup).
3. **Gating**: same as other night layers. On when `nightMix > 0.5`, off otherwise.
4. **Legend**: add red crime gradient to the legend block.
5. **README**: add both datasets, make the historic / aggregate framing explicit.

### Performance risk

Crime heatmap with 50K points is doable but heavy at full scale. Mitigations:

- Pre-aggregate to grid cells server-side (Python) before ship.
- Limit to last year if size or perf is an issue.

### Expected outputs

- `data/safety/crime.geojson`, `data/safety/crashes.geojson`
- `scripts/download_safety.py`
- Updated `build_time_slider_map`, `build_night_map`
- Updated README with clear historic-data disclosure

---

## Phase X. Speed improvements

Runs in parallel and after each layer addition. Budget: before 2026-04-28.

### Current perf baseline (100% scale time-slider, zoom 15)

| Metric | Value |
| --- | ---: |
| Tick median | 250 ms |
| Tick p95 | 375 ms |
| First shadow | 2.1 s |
| File size | 14 MB |

The bottleneck already profiled: `L.GeoJSON.addData` rebuilding ~20K L.Polygon children every tick, ~150 ms per tick. The convex-hull compute is only ~30 ms of that.

### Ranked candidate speedups

1. **Custom `L.CanvasLayer` that fills polygons directly (big win).** Draw shadow polygons onto one `<canvas>` without going through L.Polygon objects. Estimated 3 to 5x faster on addData-bound ticks. Effort: half day.
2. **Web Worker for convex-hull compute (moderate win).** Offload the projection loop. UI thread stays responsive even if render is slow. Effort: half day.
3. **Level-of-detail (LOD) geometry (moderate win).** Ship two building geometry sets — simplified (for zoom 15) and full (for zoom 17-18). Halves polygon count at wide zoom. Effort: 1 day.
4. **Shadow polygon simplification on emit (small win).** Round to 5 decimals, drop collinear points. Effort: a few hours.
5. **Deferred first render (UX win, not raw perf).** Show a loading spinner during the 2-second first-shadow pass so it feels intentional rather than laggy.

### Tasks (order by ROI)

1. Prototype candidate #1 in an isolated branch. Bench with the existing `/tmp/bench_timeslider.py` harness. Decide keep vs drop.
2. If #1 alone is enough, stop and document. Otherwise layer in #3 (LOD) for a second boost.
3. Only pursue #2 (Web Worker) if both #1 and #3 fail to restore smoothness at zoom 15 after Phase 1 tree canopy lands.

### Decision gate

After every major data layer lands, rerun the bench. If tick median at zoom 15 exceeds 300 ms, pause feature work and execute the next speedup.

---

## Risks, shared across phases

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Boston tree shapefile is 1 GB and slow to download | Medium | Medium | Cache the shapefile locally; document the one-time cost in README |
| Cambridge tree TopoJSON has empty properties, breaks some converters | Medium | Low | Use explicit converter that accepts empty properties |
| Adding tree shadows doubles convex-hull work per tick | Medium | High | Phase X optimization runs before shipping 100% scale with trees |
| Open-Meteo historical API rate-limits for aggressive scrubbing | Low | Low | Debounce to `change` event only, cache locally |
| Crime heatmap is perceived as stigmatizing | Medium | Low | Clear historic-aggregate framing in info panel and README |
| File-size budget at GitHub Pages exceeded after adding layers | Medium | Medium | Gzip already applied. Stage: prune to INITIAL_BBOX + simplify + LOD |

---

## Decisions to close before Phase 1 starts

1. **Tree visual model**: flat canopy polygons OR projected tree shadows?
2. **Tree height assumption**: per-feature (Boston has the field) or flat 10 m?
3. **Weather widget placement**: next to slider, or in the existing info panel?
4. **Weather historical/forecast timing**: auto-fetch on date change, or only on explicit button?
5. **Safety visualization**: heatmap for crime + pins for crashes, or combined?
6. **Safety date window**: last 1 year vs 2 years vs all?
7. **Speed branch order**: try custom canvas layer first (highest ROI) or LOD first (safer)?

Once these are answered, this plan becomes executable task-by-task without further mid-stream design calls.
