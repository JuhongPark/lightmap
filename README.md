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

| Scale | Day (Shadow Map) | Night (Brightness Map) |
| --- | --- | --- |
| 1 each | <img src="docs/screenshots/day_1each.png" width="360"> | <img src="docs/screenshots/night_1each.png" width="360"> |
| 1% | <img src="docs/screenshots/day_1pct.png" width="360"> | <img src="docs/screenshots/night_1pct.png" width="360"> |
| 10% | <img src="docs/screenshots/day_10pct.png" width="360"> | <img src="docs/screenshots/night_10pct.png" width="360"> |
| 50% | <img src="docs/screenshots/day_50pct.png" width="360"> | <img src="docs/screenshots/night_50pct.png" width="360"> |
| 100% | <img src="docs/screenshots/day_100pct.png" width="360"> | <img src="docs/screenshots/night_100pct.png" width="360"> |

## Tech Stack

| Layer | Technologies |
| --- | --- |
| Backend | FastAPI, uvicorn |
| Shadow engine | pvlib (sun position), Shapely + GeoPandas (geometry) |
| Frontend | MapLibre GL JS, vanilla JavaScript |
| Base tiles | CARTO Positron (day), CARTO Dark Matter (night) |

## Data Sources

| Dataset | Records | Source |
| --- | --- | --- |
| Buildings | 123K (with height) | BPDA + Cambridge GIS |
| Streetlights | 80K | data.boston.gov + Cambridge GIS |
| Tree canopy | 144K | Boston + Cambridge GIS |

See [docs/data-catalog.md](docs/data-catalog.md) for the full data catalog.

## Documentation

Documentation is being added. This section will be updated as new documents are created.

- [Project Description](docs/project.md) -- Problem, solution plan, architecture, data sources.
- [Data Catalog](docs/data-catalog.md) -- All datasets, APIs, and validation results.
- [Technology Research](docs/tech-research.md) -- Tech stack, decisions, and competitor analysis.
- [Course Information](docs/course.md) -- MIT 1.001 course details and grading rubric.

## License

[MIT](LICENSE)
