# LightMap

> Shade by day. Light by night.

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

| Dataset | Records | Source |
| --- | --- | --- |
| Buildings | 123K (with height) | BPDA + Cambridge GIS |
| Streetlights | 80K | data.boston.gov + Cambridge GIS |
| Tree canopy | 144K | Boston + Cambridge GIS |

See [docs/data-catalog.md](docs/data-catalog.md) for the full data catalog.

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
| `data/buildings/boston_buildings.geojson` | 141 MB | BPDA |
| `data/cambridge/buildings/buildings.geojson` | 19 MB | Cambridge GIS |
| `data/streetlights/streetlights.csv` | 2 MB | data.boston.gov CKAN |
| `data/cambridge/streetlights/streetlights.geojson` | small | Cambridge GIS |
| `data/safety/food_establishments.csv` | 200 KB | data.boston.gov CKAN |

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

See [docs/optimization-plan.md](docs/optimization-plan.md) for the full v1 through v7 optimization history and bottleneck analysis.

## Documentation

Documentation is being added. This section will be updated as new documents are created.

- [Project Description](docs/project.md) -- Problem, solution plan, architecture, data sources.
- [Data Catalog](docs/data-catalog.md) -- All datasets, APIs, and validation results.
- [Technology Research](docs/tech-research.md) -- Tech stack, decisions, and competitor analysis.
- [Optimization Plan](docs/optimization-plan.md) -- Step-by-step day pipeline optimization from 102s to roughly 12s of compute.
- [Render Optimization Plan](docs/render-optimization-plan.md) -- r0 through r9 browser-side render strategies, from inline SVG that never loads to PNG-then-vector hybrid with a 350 ms preview.
- [Bench Protocol](docs/bench-protocol.md) -- Checklist and red flags for producing trustworthy bench numbers.
- [Course Information](docs/course.md) -- MIT 1.001 course details and grading rubric.

## License

[MIT](LICENSE)
