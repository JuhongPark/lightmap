# LightMap

> Shade by day. Light by night.

**Live demos:**

- [Main view](https://juhongpark.github.io/lightmap/) — single-timestamp shadow or brightness map.
- [Interactive time slider](https://juhongpark.github.io/lightmap/prototype_timeslider.html) — scrub through any date and time. Shadows sweep with the sun, basemap fades from dark to light as the sun rises, streetlights switch on at dusk.

An interactive web map that shows where shade falls during the day and where light shines at night in Boston and Cambridge, MA. Uses real-time sun position, building geometry, streetlight locations, and tree canopy data.

Built by **Juhong Park** (System Design and Management, MIT) as a term project for [**MIT 1.001: Engineering Computation and Data Science**](https://student.mit.edu/catalog/search.cgi?search=1.001) (Spring 2026), taught by [Abel Sanchez](https://abel.mit.edu/) and [John R. Williams](https://johntango.github.io/). Course website: [onexi.org](https://onexi.org).

## Preview

| Day (Shadow Map) | Night (Brightness Map) |
| --- | --- |
| <img src="docs/screenshots/day.png" width="400"> | <img src="docs/screenshots/night.png" width="400"> |

123K buildings with shadows across Boston and Cambridge (day). 80K streetlights as a brightness heatmap with 3K food establishment markers (night).

### How It Works

The shadow engine computes sun position (pvlib) and projects each building footprint along the opposite azimuth. The brightness map renders streetlight density as a heatmap. Both scale from a single building to the full dataset:

| Data&nbsp;Ratio | Day | Night |
| :--: | --- | --- |
| 1 each | <img src="docs/screenshots/day_1each.png" width="360"> | <img src="docs/screenshots/night_1each.png" width="360"> |
| 1% | <img src="docs/screenshots/day_1pct.png" width="360"> | <img src="docs/screenshots/night_1pct.png" width="360"> |
| 10% | <img src="docs/screenshots/day_10pct.png" width="360"> | <img src="docs/screenshots/night_10pct.png" width="360"> |
| 50% | <img src="docs/screenshots/day_50pct.png" width="360"> | <img src="docs/screenshots/night_50pct.png" width="360"> |
| 100% | <img src="docs/screenshots/day_100pct.png" width="360"> | <img src="docs/screenshots/night_100pct.png" width="360"> |

## Tech Stack

| Layer | Technologies |
| --- | --- |
| Shadow engine | pvlib (sun position), Shapely (geometry projection) |
| Map generation | folium (Leaflet-based interactive maps) |
| Data pipeline | httpx, pandas, csv |
| Base tiles | CARTO Positron (day), CARTO Dark Matter (night) |

## Data Sources

| Dataset | Records | Source | Used for |
| --- | --- | --- | --- |
| Boston buildings (with height) | 105K with height | [BPDA](https://data.boston.gov/dataset/boston-buildings-with-roof-breaks) (2010 survey) | Shadow projection |
| Cambridge buildings (with height) | 18K | [Cambridge GIS](https://github.com/cambridgegis/cambridgegis_data) (2018 data) | Shadow projection |
| Boston streetlights | 74K | [data.boston.gov CKAN](https://data.boston.gov/dataset/streetlight-locations) | Brightness heatmap |
| Cambridge streetlights | 6K | [Cambridge GIS](https://github.com/cambridgegis/cambridgegis_data_infra) | Brightness heatmap |
| Boston food establishments (active licenses) | 3K | [data.boston.gov CKAN](https://data.boston.gov/dataset/active-food-establishment-licenses) | Standalone night map markers |
| **OSM amenity POIs (with `opening_hours`)** | 760 inside viewport | [OpenStreetMap via Overpass API](https://overpass-turbo.eu/) | Time-slider time-aware venue markers |
| Tree canopy | 244K polygons | Boston + Cambridge GIS | (Planned, not yet rendered) |

### Time-slider data scope

The interactive time slider embeds only the Boston + Cambridge core (a bbox roughly covering MIT, central Cambridge, Back Bay, and downtown Boston). Data outside this bbox is not loaded — this keeps the shipped HTML under 20 MB and the browser scrub responsive. See `src/render/strategies.py` `INITIAL_BBOX` for the exact coordinates.

### How OpenStreetMap powers the venue time-gating

Boston's public licensing dataset does not publish business operating hours — it only lists active licenses. To show which venues are actually open at a given time, the time-slider pulls amenity POIs (restaurant, bar, cafe, fast_food, pub, nightclub) from **OpenStreetMap** via the free, no-auth Overpass API and keeps only those carrying an `opening_hours` tag (about 50% of POIs in the target area). The browser parses that tag with [`opening_hours.js`](https://openingh.openstreetmap.de/) and shows each marker only when the slider's (date, time) is inside the venue's advertised hours.

**Snapshot caveat.** The OSM data is a single snapshot fetched at build time, not a live feed. It encodes the **current** advertised weekly pattern for each venue. When you scrub the slider to a past date, the weekday-pattern logic still applies correctly (Monday-at-08:00 a cafe whose tag is "Mo-Fr 07:00-15:00" shows open), but **specific historical events are not reflected**. For example:

- A restaurant that closed permanently last year is gone from the snapshot. It will not appear even if you pick a date when it was actually open.
- A venue that changed its hours in 2024 will show its post-2024 hours for every date, including dates before the change.
- Historical public-holiday closures in specific years are not captured unless the tag literally encodes them (rare in practice).

Rerun `scripts/download_osm_pois.py` to refresh the snapshot.

See [planning/data-catalog.md](planning/data-catalog.md) for the full data catalog.

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

The time-slider adds one more dataset. Pull it with a separate script so the Overpass API is hit only when needed:

```
.venv/bin/python scripts/download_osm_pois.py
```

| File | Size | Source |
| --- | --- | --- |
| `data/osm/pois.geojson` | ~150 KB | OpenStreetMap via Overpass API |

### 3. Pre-process buildings into SQLite

Converts the raw GeoJSON into a compact `data/buildings.db` with WKB blobs and a spatial bounding box index. Speeds up every subsequent run.

```
.venv/bin/python scripts/preprocess_buildings.py
```

### 4. Run the prototype

```
.venv/bin/python src/prototype.py --scale 10
```

Opens `docs/prototype.html` in your browser. Available flags:

| Flag | Effect |
| --- | --- |
| `--scale N` | Percent of data to render. Valid values: 0, 1, 10, 50, 100. Default 1. |
| `--night` | Force night mode instead of the default afternoon time. |
| `--time "YYYY-MM-DD HH:MM"` | Render a specific timestamp. |
| `--dual` | Render day and night side by side on one synchronized map. |
| `--time-compare` | Shadow animation across six hours with a playback slider. |
| `--time-slider` | 24-hour playback. Shadows move with the sun during the day. Streetlights and food-establishment markers appear automatically after sunset. |

### 5. Run tests

```
PYTHONPATH=src .venv/bin/python -m unittest tests.test_shadow
```

## Running at full 100% scale

The 100% scale run processes 123K buildings. Rendering it in pure Python works but takes around 15-25 seconds. A PostGIS backend brings that down to well under 10 seconds by running shadow projection as a parallel SQL query.

### 1. Start the PostGIS container

Requires Docker.

```
docker run -d --name lightmap-postgis -e POSTGRES_PASSWORD=lightmap -e POSTGRES_USER=lightmap -e POSTGRES_DB=lightmap -p 5432:5432 -v lightmap_pgdata:/var/lib/postgresql/data postgis/postgis:16-3.4
```

Stop later with `docker stop lightmap-postgis`. Restart with `docker start lightmap-postgis`.

### 2. Load the buildings into PostGIS

Creates the schema, inserts 123K buildings, builds a GiST spatial index, and enables 8 parallel workers on the buildings table.

```
.venv/bin/python scripts/preprocess_postgis.py
```

### 3. Run at 100% scale

```
.venv/bin/python src/prototype.py --scale 100
```

The code automatically detects the PostGIS container and uses it for the 100% path. Set the environment variable `LIGHTMAP_NO_POSTGIS=1` to force the pure Python fallback.

## Benchmarking

Measures each stage of the day pipeline at 100% scale, best-of-5 runs.

```
.venv/bin/python scripts/benchmark.py
```

See [planning/optimization-plan.md](planning/optimization-plan.md) for the full v1 through v7 optimization history and bottleneck analysis.

## Documentation

Documentation is being added. This section will be updated as new documents are created.

- [Project Description](planning/project.md) -- Problem, solution plan, architecture, data sources.
- [Data Catalog](planning/data-catalog.md) -- All datasets, APIs, and validation results.
- [Technology Research](planning/tech-research.md) -- Tech stack, decisions, and competitor analysis.
- [Optimization Plan](planning/optimization-plan.md) -- Step-by-step day pipeline optimization from 102s to roughly 12s of compute.
- [Render Optimization Plan](planning/render-optimization-plan.md) -- r0 through r13 browser-side render strategies, from inline SVG that never loads to PNG-raster-primary with merged shadows.
- [Deploy Size Trim Plan](planning/deploy-size-trim-plan.md) -- Coordinate precision trim, bucket merge, raster-primary rendering experiments.
- [Bench Protocol](planning/bench-protocol.md) -- Checklist and red flags for producing trustworthy bench numbers.
- [Course Information](planning/course.md) -- MIT 1.001 course details and grading rubric.

## License

[MIT](LICENSE)
