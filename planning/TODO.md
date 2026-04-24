# TODO

Project-level TODO list. Items not tied to an open plan.

## Recently completed

- Heat-response overlay. 24-hour ER markers plus cooling-center markers (OSM proxy: libraries, community centres, town halls). Cooling layer toggles on when the live weather fetch crosses any of `tmax >= 89.6 F` / `apparent_max >= 91.4 F` / `UV >= 8`. Info panel shows a red `HEAT` badge while active. `scripts/download_medical.py` and `scripts/download_cooling.py` populate `data/osm/medical.geojson` and `data/cooling/cooling.geojson`.
- Tree canopy rendered as a single baked PNG overlay (`docs/trees_canopy.png`) instead of ~59K per-crown canvas polygons. One image paint on pan/zoom versus tens of thousands. Cut the shipped HTML from ~27 MB to ~15 MB.
- No-data mask. Cells inside `INITIAL_BBOX` that contain no buildings get a translucent dark fill, and every point layer (crime, ER, venue, cooling, streetlight) is pre-filtered server-side to drop anything in those cells so the masked area is cleanly empty.
- Streetlight heatmap re-tuned. Stops shifted (0.35-1.0 instead of 0.2-1.0), top stop capped at warm yellow (no white washout), `max=5` so only genuinely dense clusters glow, `max_zoom=14` so intensity no longer scales on zoom-in. Crime heatmap also pinned to `max_zoom=14`.
- OSM venue markers enlarged (10x10 green dots -> 16x16 yellow dots with dark border and glow) so they stay legible above the yellow streetlight heatmap.
- Building coord precision trimmed from 6 to 5 decimals (~1 m), building height rounded to 1 m. No visual change at zoom 15-18 and the HTML shrinks proportionally.
- Plain prototype retired -- the time-slider at `docs/prototype_timeslider.html` is the single production artifact, `/` redirects to it.
- Violent-crime red-diamond pins replace the old Vision Zero crash pins in the time-slider night layer (filters `data/safety/crime.geojson` on murder, aggravated assault, robbery, sexual offenses, firearm and weapon).
- Per-crown tree canopy with OSM water clip replaces the buffer-union shade-patch approach. `scripts/download_water.py` + `scripts/clip_trees_by_water.py` handle the clipping step.
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
- [ ] **Further time-slider payload trim** -- the PNG swap brought `prototype_timeslider.html` from ~27 MB to ~15 MB. Remaining size is mostly building shadow geometry. Next candidates: 3 m simplification tolerance (currently 2 m), flatter coord encoding for crime/violent-crime/POI arrays (object -> `[lat, lon]`).
- [ ] **Full scale=100 best-of-3 bench refresh** -- the committed benchmark snapshots pre-date the tree-canopy + violent-crime changes. Re-run `scripts/benchmark.py --runs 3` once the host has spare capacity so the current baseline is recorded.
