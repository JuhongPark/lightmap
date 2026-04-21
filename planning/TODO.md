# TODO

Project-level TODO list. Items not tied to an open plan.

## Recently completed

- Plain prototype retired -- the time-slider at `docs/prototype_timeslider.html` is the single production artifact, `/` redirects to it.
- Violent-crime red-diamond pins replace the old Vision Zero crash pins in the time-slider night layer (filters `data/safety/crime.geojson` on murder, aggravated assault, robbery, sexual offenses, firearm and weapon).
- Per-crown tree canopy with OSM water clip replaces the buffer-union shade-patch approach. `scripts/download_water.py` + `scripts/clip_trees_by_water.py` handle the clipping step.
- Streetlight heatmap gradient shifted from amber-to-white to pure yellow so the green venue dots and red violent-crime diamonds read against it.
- OSM venue POI markers moved from the shared shadow canvas onto `markerPane` (L.marker + DivIcon), so they stay legible above the streetlight heatmap.
- Time-slider auto-play locked to a fixed 1 s per slot cadence, independent of per-tick render cost.
- Client-side interactive time-slider with free date picker ([plan](time-slider-plan.md)).
- OSM opening_hours gating for night-mode venue markers.
- Cambridge tree canopy merged into shadow engine.
- Live Open-Meteo weather + UV strip for the selected date.
- Boston crime heatmap (last 2 years, night-only gate) ([plan](extensions-plan.md)).
- INITIAL_BBOX hard cutoff and shared zoom / pan limits across every renderer.

## Validation

- [ ] **MIT Westgate shadow verification** -- Tang Hall and Westgate have tall buildings that cast prominent shadows in the rendered map. Take real photos on-site (afternoon on a sunny day, sun azimuth roughly matching the default 14:00 rendering) and compare the observed shadow direction and length against what LightMap draws. This is a ground-truth check for the shadow engine.

## Open follow-ups

- [ ] **Integrate water clip into `download_trees.py`** -- today the clip is a separate post-processing step. Fold `clip_trees_by_water.py` into the tree-canopy pipeline so the invariant "canopy polygons never sit over water" holds without an extra manual step.
- [ ] **Expand OSM water coverage** -- a spot-check of Charles-River centerline points inside INITIAL_BBOX shows only 1 of 7 lands inside the current water union, because the Overpass query misses some multi-polygon relations. Broaden the query (`natural=coastline`, `waterway=canal`, `water=river` on relations) and re-stitch relation outers with proper ring nesting.
- [ ] **Time-slider payload trim** -- the per-crown canopy pushed `prototype_timeslider.html` to ~27 MB. Explore tighter canopy simplification (2 m → 3-5 m tolerance) or alpha-shape clustering before falling back to architectural changes (binary sidecar, tile grid).
- [ ] **Full scale=100 best-of-3 bench refresh** -- the committed benchmark snapshots pre-date the tree-canopy + violent-crime changes. Re-run `scripts/benchmark.py --runs 3` once the host has spare capacity so the current baseline is recorded.
