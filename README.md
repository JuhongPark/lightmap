# LightMap

> Shade by day. Light by night.

**Live demo:** [LightMap](https://juhongpark.github.io/lightmap/LightMap.html) — scrub through any date and time. Shade moves with the sun, twilight phases mark dawn and dusk, and streetlights plus open-venue activity switch on at night.

An interactive web map that shows where shade falls during the day and where light shines at night. The shipped profile covers Boston and Cambridge, MA. City profiles under `cities/` make the time-slider reusable for other cities with public building, streetlight, venue, and canopy data.

Built by **Juhong Park** (System Design and Management, MIT) as a term project for [**MIT 1.001: Engineering Computation and Data Science**](https://student.mit.edu/catalog/search.cgi?search=1.001) (Spring 2026), taught by [Abel Sanchez](https://abel.mit.edu/) and [John R. Williams](https://johntango.github.io/). Course website: [onexi.org](https://onexi.org).

## Preview

| Day (Shade Map) | Night (Light Map) |
| --- | --- |
| <img src="docs/screenshots/day.png" width="400"> | <img src="docs/screenshots/night.png" width="400"> |

31K buildings with moving shade and ~59K tree-canopy crowns as static shade context (day). 32K streetlights render as a night light heatmap with 688 time-gated OSM venue dots (night).

### How It Works

The shadow engine computes sun position (pvlib) and projects each building footprint along the opposite azimuth. The brightness map renders streetlight density as a heatmap. Both scale from a single building to the full dataset:

| Data Ratio | Day | Night |
| :--: | --- | --- |
| 1 each | <img src="docs/screenshots/day_1each.png" width="360"> | <img src="docs/screenshots/night_1each.png" width="360"> |
| 1% | <img src="docs/screenshots/day_1pct.png" width="360"> | <img src="docs/screenshots/night_1pct.png" width="360"> |
| 10% | <img src="docs/screenshots/day_10pct.png" width="360"> | <img src="docs/screenshots/night_10pct.png" width="360"> |
| 50% | <img src="docs/screenshots/day_50pct.png" width="360"> | <img src="docs/screenshots/night_50pct.png" width="360"> |
| 100% | <img src="docs/screenshots/day_100pct.png" width="360"> | <img src="docs/screenshots/night_100pct.png" width="360"> |

### LightTime Agent

LightTime Agent is the button-driven assistant layer inside the time-slider.
After the user clicks a point on the map, it answers four timing questions:

- `Shade Time`: finds when the local check ring is covered by building shade.
- `Sunny Time`: finds when the same point is open to direct sun.
- `Active Time`: finds night slots with nearby open venues.
- `Inactive Time`: finds night slots with little or no open-venue activity.

The visible timing buttons use LightMap's computed map evidence: date, time,
clicked point, building-shadow coverage, streetlight context, and OSM
`opening_hours`. The UI draws a scan effect, moves the time slider to the
selected slot, and highlights the checked area on the map.

When the app is served through `scripts/serve_agent.py`, LightMap also exposes
a local `/api/agent` endpoint for OpenAI-backed map explanations. The browser
sends compact map context to the local server, and the OpenAI API key stays on
the server. It is never embedded in `docs/LightMap.html`.

## Tech Stack

| Layer | Technologies |
| --- | --- |
| Shadow engine | pvlib (sun position), Shapely (geometry projection) |
| Map generation | folium (Leaflet-based interactive maps) |
| Data pipeline | httpx, pandas, csv |
| LightTime Agent | Browser map context, local `/api/agent`, OpenAI Responses API |
| Base tiles | CARTO Positron (day), CARTO Dark Matter (night) |

## Data Sources

| Dataset | Records | Source | Used for |
| --- | --- | --- | --- |
| Boston buildings (with height) | 105K with height | [BPDA](https://data.boston.gov/dataset/boston-buildings-with-roof-breaks) (2010 survey) | Shadow projection |
| Cambridge buildings (with height) | 18K | [Cambridge GIS](https://github.com/cambridgegis/cambridgegis_data) (2018 data) | Shadow projection |
| Boston streetlights | 74K | [data.boston.gov CKAN](https://data.boston.gov/dataset/streetlight-locations) | Brightness heatmap |
| Cambridge streetlights | 6K | [Cambridge GIS](https://github.com/cambridgegis/cambridgegis_data_infra) | Brightness heatmap |
| Boston food establishments (active licenses) | 3K | [data.boston.gov CKAN](https://data.boston.gov/dataset/active-food-establishment-licenses) | Legacy standalone night map markers. The current time-slider uses OSM venue hours instead. |
| **OSM amenity POIs (with `opening_hours`)** | 688 shown in the current time-slider | [OpenStreetMap via Overpass API](https://overpass-turbo.eu/) | Time-gated venue markers |
| **Tree canopy (Cambridge 2018 + Boston 2019-2024)** | ~59K per-crown polygons inside viewport | [Cambridge GIS](https://github.com/cambridgegis/cambridgegis_data_environmental) + [Boston BPDA Tree Canopy Change Assessment](https://data.boston.gov/dataset/tree-canopy-change-assessment) | Static daytime shade context in the time-slider. The source polygons are baked into `docs/trees_canopy.png` so the browser paints one image overlay instead of redrawing tens of thousands of crowns per tick. Boston LiDAR crowns are streamed in-place from a 1 GB ZIP via `ogr2ogr`, simplified, height-clamped, and water-clipped before rasterization. |
| **Weather + UV (Open-Meteo)** | 1 daily record per slider date | [Open-Meteo API](https://open-meteo.com/) | Info panel temperature range + max UV for the slider's currently selected date. Free and no auth. Fetched live from the browser: forecast API for today-to-future-16-days, archive API for historical dates. |
| **Boston crime incidents (last 2 years, night hours)** | ~19K inside viewport | [data.boston.gov CKAN](https://data.boston.gov/dataset/crime-incident-reports-august-2015-to-date-source-new-system) | Downloaded historic reference data. Not rendered in the current time-slider and not used as a safety prediction. |
| **Boston crime incidents -- violent subset (last 2 years)** | ~830 inside viewport | Filtered from the crime dataset above | Downloaded research subset. Not rendered in the current time-slider. |
| **OSM hospitals (emergency rooms)** | 16 inside viewport (9 tagged `emergency=yes`) | [OpenStreetMap via Overpass API](https://overpass-turbo.eu/) | Downloaded heat-response reference data. Not rendered in the current time-slider. |
| **OSM cooling proxy (libraries, community centres, town halls)** | ~136 inside viewport | [OpenStreetMap via Overpass API](https://overpass-turbo.eu/) | Cooling-center markers. Visible only when the live Open-Meteo fetch crosses a heat threshold (`tmax >= 89.6 F` or `apparent_max >= 91.4 F` or `UV >= 8`). Boston opens these during heat emergencies. |
| **OpenStreetMap water polygons** | 175 features inside viewport | [OpenStreetMap via Overpass API](https://overpass-turbo.eu/) | Mask used by `scripts/clip_trees_by_water.py` so the tree-canopy layer never extends over the Charles, Fort Point Channel, or the harbor. |

### Time-slider data scope

The interactive time slider embeds only the Boston + Cambridge core (a bbox roughly covering MIT, central Cambridge, Back Bay, and downtown Boston). Data outside this bbox is not loaded. The shipped HTML is ~15 MB at 100% scale. The tree canopy ships as a single ~760 KB baked PNG overlay (`docs/trees_canopy.png`) rather than per-crown polygons, which keeps the HTML small and removes the per-tick canvas paint cost for ~59K crowns. See `src/render/strategies.py` `INITIAL_BBOX` for the exact coordinates.

### How OpenStreetMap powers the venue time-gating

Boston's public licensing dataset does not publish business operating hours — it only lists active licenses. To show which venues are actually open at a given time, the time-slider pulls amenity POIs (restaurant, bar, cafe, fast_food, pub, nightclub) from **OpenStreetMap** via the free, no-auth Overpass API and keeps only those carrying an `opening_hours` tag (about 50% of POIs in the target area). The browser parses that tag with [`opening_hours.js`](https://openingh.openstreetmap.de/) and shows each marker only when the slider's (date, time) is inside the venue's advertised hours.

**Snapshot caveat.** The OSM data is a single snapshot fetched at build time, not a live feed. It encodes the **current** advertised weekly pattern for each venue. When you scrub the slider to a past date, the weekday-pattern logic still applies correctly (Monday-at-08:00 a cafe whose tag is "Mo-Fr 07:00-15:00" shows open), but **specific historical events are not reflected**. For example:

- A restaurant that closed permanently last year is gone from the snapshot. It will not appear even if you pick a date when it was actually open.
- A venue that changed its hours in 2024 will show its post-2024 hours for every date, including dates before the change.
- Historical public-holiday closures in specific years are not captured unless the tag literally encodes them (rare in practice).

Rerun `scripts/download_osm_pois.py` to refresh the snapshot.

See [planning/data-catalog.md](planning/data-catalog.md) for the full data catalog.

## Adding City Data

LightMap reads its production time-slider scope from `cities/<city-id>.json`.
The default profile is `cities/boston-cambridge.json`. A new profile defines:

- `timezone`, `center`, and `bbox`.
- Building GeoJSON sources, including the height field and unit.
- Streetlight sources as CSV or GeoJSON points.
- Optional layer paths for OSM POIs, tree canopy, water, medical, cooling, crime, and crash reference data.

Raw datasets stay under `data/`, which is gitignored. For a new city, use
`data/cities/<city-id>/...` unless the profile points somewhere else.

Generic OSM-backed layers can be pulled with the profile bbox:

```
.venv/bin/python scripts/download_osm_pois.py --city your-city
.venv/bin/python scripts/download_water.py --city your-city
.venv/bin/python scripts/download_cooling.py --city your-city
.venv/bin/python scripts/download_medical.py --city your-city
```

Then pre-process buildings and build the time-slider:

```
.venv/bin/python scripts/preprocess_buildings.py --city your-city
.venv/bin/python src/prototype.py --city your-city --time-slider --out LightMap-your-city.html --scale 100
```

City-data PRs are welcome. A good PR adds a `cities/<city-id>.json` profile,
documents public data sources, and includes any small downloader or adapter
needed to reproduce the local `data/cities/<city-id>/...` files. Do not commit
large raw datasets unless they are tiny, public, license-clean, and necessary
for a test fixture. Keep public copy framed around shade, brightness,
visibility, lighting context, and historic incident reference data. Do not make
route safety claims.

## Getting Started

### Prerequisites

- Python 3.12 or newer
- About 300 MB of disk space for the raw datasets
- Optional but recommended for 100% scale runs: Docker, for the PostGIS container

### 1. Clone and set up the Python environment

```
git clone https://github.com/JuhongPark/lightmap.git
cd lightmap
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Download the raw datasets

Every dataset is fetched from public Boston and Cambridge APIs. The script writes into `data/`, which is gitignored so nothing ever leaks back into the repo.

```
.venv/bin/python scripts/download_data.py
```

This downloads:

| File | Size | Source |
| --- | --- | --- |
| `data/buildings/boston_buildings.geojson` | 147 MB | BPDA |
| `data/cambridge/buildings/buildings.geojson` | 19 MB | Cambridge GIS |
| `data/streetlights/streetlights.csv` | 2.2 MB | data.boston.gov CKAN |
| `data/cambridge/streetlights/streetlights.geojson` | 2.7 MB | Cambridge GIS |
| `data/safety/food_establishments.csv` | 180 KB | data.boston.gov CKAN |

The time-slider adds several more datasets. Pull them with separate scripts so the external APIs are hit only when needed:

```
.venv/bin/python scripts/download_osm_pois.py
.venv/bin/python scripts/download_trees.py
.venv/bin/python scripts/download_water.py
.venv/bin/python scripts/clip_trees_by_water.py
.venv/bin/python scripts/download_cooling.py
```

Historic incident and medical datasets are available through
`scripts/download_safety.py` and `scripts/download_medical.py`, but they are not
required for the current public time-slider.

| File | Size | Source |
| --- | --- | --- |
| `data/osm/pois.geojson` | ~150 KB | OpenStreetMap via Overpass API |
| `data/trees/trees.geojson` | ~14 MB (Boston + Cambridge, per-crown) | Cambridge GIS TopoJSON + Boston BPDA TreeTops2024 (streamed from a 1 GB ZIP via ogr2ogr, simplified to ~2 m tolerance) |
| `data/water/water.geojson` | ~350 KB | OpenStreetMap via Overpass API (`natural=water`, `waterway=riverbank|river`) |
| `data/cooling/cooling.geojson` | ~36 KB | OpenStreetMap via Overpass API (`amenity=library`, `community_centre`, `townhall`, INITIAL_BBOX filtered) |

`clip_trees_by_water.py` is a post-processing step that subtracts the water union from `trees.geojson` in place. Run it after every `download_trees.py --force` so the canopy layer never floats over water.

### 3. Pre-process buildings into SQLite

Converts the raw GeoJSON into a compact `data/buildings.db` with WKB blobs and a spatial bounding box index. Speeds up every subsequent run.

```
.venv/bin/python scripts/preprocess_buildings.py
```

### 4. Build the time-slider

The time-slider is the single production artifact. Build it with:

```
.venv/bin/python src/prototype.py --time-slider --out LightMap.html --scale 100
```

Opens `docs/LightMap.html` in your browser. On load, the slider resets to the current Boston date and the nearest past hourly slot. During the day, building shade moves with the sun and a static green tree-canopy overlay fills in the rest of the shade. Click a point to ask when the local 17 m check ring is covered by building shadow or open to direct sun. Dawn and dusk use a distinct twilight theme. After sunset the basemap turns dark, the streetlight heatmap switches on as a bright-yellow glow, and OSM venues turn on one by one based on their real `opening_hours` tag. Weather and UV for the selected date are fetched live from Open-Meteo. Auto-play advances one hourly slot every 0.5 seconds.

Available flags:

| Flag | Effect |
| --- | --- |
| `--time-slider` | Build the interactive time-slider HTML. This is the only supported output. |
| `--city ID` | Use a city profile from `cities/`. Default `boston-cambridge`. |
| `--scale N` | Percent of data to render. Valid values: 0, 1, 10, 50, 100. Default 1. Use 100 for the shipping build. |
| `--out NAME` | Output filename under `docs/`. Default `LightMap.html` for the time-slider. |
| `--time "YYYY-MM-DD HH:MM"` | Starting timestamp the slider opens at. |

### 5. Run tests

```
PYTHONPATH=src .venv/bin/python -m unittest discover tests
```

### Optional local AI agent

The time-slider can be served with a local LightTime Agent endpoint for
OpenAI-backed map explanations. The four visible timing buttons still use
LightMap's computed evidence so they remain fast and deterministic.

Paste the key into `.env`:

```
OPENAI_API_KEY=your_key
```

See `.env.example` for optional model settings.

Start the local server:

```
.venv/bin/python scripts/serve_agent.py 8765 docs
```

Then open:

```
http://localhost:8765/LightMap.html
```

Optional settings:

| Variable | Default | Effect |
| --- | --- | --- |
| `LIGHTMAP_OPENAI_MODEL` | `gpt-5.4-mini` | Model used by the local agent. |
| `LIGHTMAP_REASONING_EFFORT` | `medium` | Reasoning effort sent to the Responses API. |

## Running at full 100% scale

The 100% scale path starts from 123K source buildings before the time-slider bbox is applied. The current public time-slider embeds about 31K buildings in the Boston + Cambridge core. Rendering the full source set in pure Python works but takes around 15-25 seconds. A PostGIS backend brings that down to well under 10 seconds by running shadow projection as a parallel SQL query.

### 1. Start the PostGIS container

Requires Docker.

```
docker run -d --name lightmap-postgis -e POSTGRES_PASSWORD=lightmap -e POSTGRES_USER=lightmap -e POSTGRES_DB=lightmap -p 5432:5432 -v lightmap_pgdata:/var/lib/postgresql/data postgis/postgis:16-3.4
```

Stop later with `docker stop lightmap-postgis`. Restart with `docker start lightmap-postgis`.

### 2. Load the buildings into PostGIS

Creates the schema, inserts 123K source buildings, builds a GiST spatial index, and enables 8 parallel workers on the buildings table.

```
.venv/bin/python scripts/preprocess_postgis.py
```

### 3. Build the time-slider at 100% scale

```
.venv/bin/python src/prototype.py --time-slider --out LightMap.html --scale 100
```

The code automatically detects the PostGIS container and uses it for the 100% path. Set the environment variable `LIGHTMAP_NO_POSTGIS=1` to force the pure Python fallback.

## Benchmarking

Measures each stage of the shadow pipeline at 100% scale, best-of-5 runs.

```
.venv/bin/python scripts/benchmark.py
```

See [planning/optimization-plan.md](planning/optimization-plan.md) for the full v1 through v7 optimization history and bottleneck analysis.

## Documentation

- [Project Description](planning/project.md) -- Shipped features, architecture, data sources, and as-mitigated risks.
- [Presentation Narrative](planning/presentation-narrative.md) -- Talk track that keeps "Shade by day. Light by night." as the public-facing catchphrase.
- [Narrative Evaluation](planning/narrative-evaluation.md) -- Evidence-backed critique of the current story, with proposal alignment and feedback points.
- [Shade by Day, Light by Night Roadmap](planning/shadow-lightmap-roadmap.md) -- Active local-first roadmap, narrative rules, time-slider speed pass, and next performance candidates.
- [AI + Local Development Plan](planning/ai-local-development-plan.md) -- Guardrails and staged AI features that explain computed map evidence without making safety claims.
- [Data Catalog](planning/data-catalog.md) -- Every dataset researched. Labels which entries are shipped vs downloaded-but-unused vs researched-only.
- [Technology Research](planning/tech-research.md) -- Shipped tech stack, design decisions, and competitor analysis. Notes why FastAPI + MapLibre were deferred.
- [Prototype Plan](planning/prototype-plan.md) -- Historical roadmap for the 1-each through 100% scale prototype.
- [Scale-Up Plan](planning/scaleup-plan.md) -- Per-stage targets and verification for the 1% through 100% scale-up.
- [Time Slider Plan](planning/time-slider-plan.md) -- Phase 1 MVP that shipped. Phase 2 (full client-side shadow compute) left as future work.
- [Extensions Plan](planning/extensions-plan.md) -- Tree canopy, weather, and safety-overlay rollout with per-phase outcomes.
- [Optimization Plan](planning/optimization-plan.md) -- Step-by-step day pipeline optimization from 102s to roughly 12s of compute.
- [Render Optimization Plan](planning/render-optimization-plan.md) -- r0 through r13 browser-side render strategies, from inline SVG that never loads to PNG-raster-primary with merged shadows.
- [Deploy Size Trim Plan](planning/deploy-size-trim-plan.md) -- Coordinate precision trim, bucket merge, raster-primary rendering experiments.
- [Bench Protocol](planning/bench-protocol.md) -- Checklist and red flags for producing trustworthy bench numbers.
- [Project TODOs](planning/TODO.md) -- Recently completed items and open follow-ups.
- [Course Information](planning/course.md) -- MIT 1.001 course details and grading rubric.

## License

[MIT](LICENSE)
