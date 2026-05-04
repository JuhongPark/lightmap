# Contributing City Data

LightMap welcomes PRs that add reusable city profiles or improve city data
adapters.

A city-data PR should include:

- `cities/<city-id>.json` with timezone, center, bbox, data paths, building
  height fields, and streetlight source definitions.
- Public source notes for each dataset.
- Small downloader or adapter scripts when a source needs repeatable
  processing before it lands in `data/cities/<city-id>/...`.
- Focused tests when the adapter changes parsing logic.

Keep raw datasets out of git. The `data/` directory is gitignored. Only commit a
data fixture when it is tiny, public, license-clean, and needed for a test.

Build a city profile locally with:

```
.venv/bin/python scripts/preprocess_buildings.py --city your-city
.venv/bin/python src/prototype.py --city your-city --time-slider --out LightMap-your-city.html --scale 100
```

Use public wording around shade, brightness, visibility, lighting context, and
historic incident reference data. Do not make route safety claims.
