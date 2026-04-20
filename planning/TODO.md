# TODO

Project-level TODO list. Items not tied to an open plan.

## Recently completed

- Client-side interactive time-slider with free date picker ([plan](time-slider-plan.md))
- OSM opening_hours gating for night-mode venue markers
- Cambridge tree canopy merged into shadow engine (Boston BPDA deferred)
- Live Open-Meteo weather + UV strip for the selected date
- Boston crime heatmap + Vision Zero crash pins (last 2 years, night-only gate) ([plan](extensions-plan.md))
- INITIAL_BBOX hard cutoff and shared zoom / pan limits across every renderer

## Validation

- [ ] **MIT Westgate shadow verification** -- Tang Hall and Westgate have tall buildings that cast prominent shadows in the rendered map. Take real photos on-site (afternoon on a sunny day, sun azimuth roughly matching the default 2026-04-20 14:00 rendering) and compare the observed shadow direction and length against what LightMap draws. This is a ground-truth check for the shadow engine.

## Open follow-ups

- [ ] **Boston BPDA tree canopy** -- 1 GB shapefile (EPSG:2249), reproject and merge with Cambridge layer. Doubles tree coverage across the viewport.
- [ ] **Standalone day map trees** -- `build_day_map` (the plain `--scale N` path) does not yet include tree shadows. Only the time-slider does. Extend `src/shadow/compute.py` to accept a tree list if the day-map consistency matters.
- [ ] **docs/prototype.html refresh** -- the committed main deploy artifact predates the warmer night palette, haloed food markers, and new default bounds. Regenerate at `--scale 100`.
