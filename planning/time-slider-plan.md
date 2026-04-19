# Time Slider Plan

Related: [Prototype Plan](prototype-plan.md), [Deploy Size Trim Plan](deploy-size-trim-plan.md), [Render Optimization Plan](render-optimization-plan.md), [Project Description](project.md)

## Goal

Interactive browser control. User picks any date and scrubs through any time of day. Shadow map updates live. When the slider crosses sunrise or sunset, the view swaps to the nighttime brightness layer (streetlight heatmap + food establishments) automatically.

This is the last major UX item from the original proposal (`project.md` Time Slider and Day/Night Auto-Switch sections) and is a strong candidate for the Demo segment (1 minute) of the final presentation video.

## Current state

| Thing | Where | How it works today |
| --- | --- | --- |
| Shadow compute | `src/shadow/compute.py:29-60` (pvlib + Shapely) | Runs at build time, one timestamp per invocation |
| Single-timestamp bake | `src/prototype.py:749-769` | Writes `docs/shadows.geojson` (23 MB at 100%) |
| 6-frame playback | `src/prototype.py:603-670` (`--time-compare` flag) | Bakes 6 hourly frames (7, 9, 11, 13, 15, 17) into one HTML with folium `TimestampedGeoJson`. HTML grows ~6x |
| Dual view | `src/prototype.py:673-723` (`--dual` flag) | Side-by-side day (Positron tiles) vs night (Dark Matter tiles). Night layer is static, no time dependency |
| Day/night logic | `src/prototype.py:750` | `altitude > 0` from pvlib. Hardcoded threshold, set at build time only |
| Night layer | `src/prototype.py:245-306, 572-600` | Static streetlight heatmap + food establishment markers. No time dependency |

## Constraints

1. **Storage on GitHub Pages.** 100%-scale `shadows.geojson` is 23 MB. Naively baking 24 hourly frames gives 550 MB. 48 half-hourly frames gives 1.1 GB. Both are impractical to ship.
2. **Sun path varies by date.** Different dates produce different shadows. Baking many dates multiplies storage further.
3. **Browser perf at 122K buildings.** Client-side shadow projection must be cheap per frame if we go that route.
4. **Final presentation deadline 2026-04-28.** Whatever we build must be stable and demoable by then.

## Options

### Option A. Server-side pre-bake (extend `--time-compare`)

Pre-bake N frames per build, ship as one HTML with the folium slider. Time range and step configurable.

- Pros: minimal JS. Reuses working `TimestampedGeoJson` path.
- Cons: storage grows linearly with frame count. No date picker (one date per build). Night layer stays static.
- Feasibility: 12 hourly daylight frames x 23 MB = 275 MB at 100%. Too big. At 1% scale ~5.5 MB, fine. Viewport-filtered initial only (3.7 MB per frame x 12) ~44 MB, borderline.

### Option B. Client-side shadow projection

Ship building footprints once. Ship a sun-angle lookup table or compute sun position in the browser. Browser computes shadows on the fly.

- Pros: true continuous time. Any date. No storage bloat.
- Cons: requires porting `compute.py` (translate + union) to JS. Perf work needed at 122K buildings. Larger engineering effort.
- Libraries: `suncalc.js` for sun position (~5 KB, well-tested). Turf.js for geometry ops.
- Perf target: smooth drag (30+ fps) or at least sub-200 ms snap-to-frame on slider release.

### Option C. Hybrid (MVP then upgrade)

Phase 1 is Option A at a reduced scale (1% or viewport-filtered) for the demo. Ships within 2 to 3 days. Phase 2 is Option B for full scope (any date, 100% scale). Ships after the presentation.

## Recommendation

Option C. Ship an MVP by the presentation deadline, then upgrade.

### Phase 1 scope (MVP, this sprint)

- Time slider covering one day (sunrise to sunrise next morning, or just daylight hours plus a night sample)
- Date is fixed per build (default: today, or a summer reference date)
- Date picker disabled or hidden in Phase 1
- Auto-swap basemap (Positron to Dark Matter) and layer (shadows off, streetlights on) when sun crosses altitude 0
- 12 to 24 pre-baked daylight frames plus one night frame
- Viewport-filtered shadow sidecar (3.7 MB per frame, not the full 23 MB)

### Phase 2 scope (post-presentation)

- Full client-side sun + shadow computation
- Real date picker (any day, any year)
- Continuous minute-level slider
- 100%-scale without baking

## Phase 1 work breakdown (MVP)

| Step | What | Where | Notes |
| --- | --- | --- | --- |
| 1 | Add `--time-slider` flag | `src/prototype.py` | Mirrors `--time-compare` but full-day range, configurable step |
| 2 | Pre-bake daylight frames | `src/prototype.py` | Sunrise to sunset at step N. Each frame tagged with ISO 8601 timestamp |
| 3 | Ship night layer alongside | `src/prototype.py` | Streetlight heatmap + food establishments bundled in the same HTML |
| 4 | Client-side day/night switch | HTML injection via `m.get_root().html.add_child(...)` | JS listens to slider change. Altitude lookup per frame. Swaps tileset and layer visibility |
| 5 | Custom slider UI | HTML injection | Extend folium `TimestampedGeoJson` or replace with custom slider + play/pause/speed. Current-time display |
| 6 | Size budget check | bench | Verify total HTML + sidecars < 100 MB. If over, drop frames or downsample |
| 7 | Visual validation | manual spot check | Sunrise transition is visible. Shadows sweep smoothly. Night layer lights up |
| 8 | README update | `README.md` | New flag, UX explanation, screenshot |

## Phase 2 work breakdown (future)

| Step | What | Notes |
| --- | --- | --- |
| 1 | Port sun position to JS | Use suncalc.js. Parity-test altitude and azimuth against pvlib output |
| 2 | Port shadow projection to JS | `compute.py:39-60` translate + union logic to Turf.js |
| 3 | Benchmark at 1%, 10%, 50%, 100% | Target smooth drag or fast snap |
| 4 | Simplify building geometry for JS projection | Reduce vertex count where visual fidelity allows |
| 5 | Viewport culling | Only project shadows for buildings currently in view |
| 6 | Date picker | HTML5 `<input type="date">` wired to sun lookup |
| 7 | Retire Phase 1 bake-and-ship path | Code cleanup |

## Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Phase 1 total HTML + sidecars exceed 100 MB | Medium | High | Reduce step to 2 h, or drop to 1% scale for the demo and document clearly |
| Folium `TimestampedGeoJson` unreliable with many frames | Medium | Medium | Fall back to custom slider + manual layer swap |
| Day/night transition looks abrupt | Medium | Low | Cross-fade, or two-minute overlap window |
| Phase 2 client-side compute too slow at 100% | Medium | Medium | Ship Phase 1 as the user-facing deliverable. Phase 2 stays experimental |
| Pre-bake time explodes at fine step | Low | Medium | Cap step at 30 min initially. Extend later |

## Decision points to close before implementing

1. **Phase 1 scope**: accept the MVP path, or delay in order to ship full Phase 2 later?
2. **Pre-bake step**: hourly (12 frames, ~44 MB viewport-filtered) or half-hourly (24 frames, ~88 MB)?
3. **Pre-bake date**: today-by-default at build, fixed summer solstice for photogenic shadows, or a flag?
4. **Date picker in Phase 1**: hide, or show disabled to tease Phase 2?
5. **Scale for the demo**: 100% (needs size trimming), 50%, or 1%? Affects storage budget directly.
