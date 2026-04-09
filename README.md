# LightMap

> Shade by day. Light by night.

An interactive web map that shows where shade falls during the day and where light shines at night in Boston and Cambridge, MA. Uses real-time sun position, building geometry, streetlight locations, and tree canopy data.

Built by **Juhong Park** (System Design and Management, MIT) as a term project for [**MIT 1.001: Engineering Computation and Data Science**](https://student.mit.edu/catalog/search.cgi?search=1.001) (Spring 2026), taught by [Abel Sanchez](https://abel.mit.edu/) and [John R. Williams](https://johntango.github.io/). Course website: [onexi.org](https://onexi.org).

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
