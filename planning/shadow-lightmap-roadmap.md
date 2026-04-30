# Shade by Day, Light by Night Roadmap

> Updated 2026-04-29. This is the active narrative and development direction
> for the next local-first phase.

## Narrative

The proposal-level slogan remains:

> Shade by day. Light by night.

That public phrase should stay on top. Under it, the implementation story starts
with the daytime problem:

> Where is shade right now?

The primary experience is a **ShadowMap**. A user scrubs time and sees building
shadows move with the sun while tree canopy fills in static shade coverage.

The paired nighttime experience is a **LightMap**:

> Where is light right now?

At night, the map emphasizes streetlight density and open, time-aware venues.
Historic incident data is not the thesis. It stays as an optional reference
toggle for users who explicitly want that context.

## Product Rules

- Preserve "Shade by day. Light by night." as the presentation catchphrase.
- Do not replace the catchphrase with an internal technical slogan.
- Keep shadow-engine and light-layer wording as implementation details.
- Lead with day shadows in demos, docs, and AI summaries.
- Explain night mode as brightness/visibility, not a personal safety guarantee.
- Keep incident history default-off and explicitly historic.
- Avoid comparing against other map products until the internal narrative is
  stable.

## Performance Focus

The current bottleneck is interactive shadow redraw in the time-slider. The old
path rebuilt Leaflet GeoJSON polygons on every slider tick, creating thousands
of objects and making the app feel sluggish.

The immediate speed pass is:

1. Render building shadows through a direct canvas layer instead of per-tick
   `L.GeoJSON.addData`.
2. Keep incident overlays optional so night mode defaults to light, not safety.
3. Measure local performance before further feature work.

## 2026-04-29 Speed Pass

Implemented:

- Replaced the time-slider shadow layer's per-tick GeoJSON rebuild with a
  direct canvas layer.
- Changed the shadow cache payload from GeoJSON features to `[height, ring]`
  tuples.
- Added `window.__lightmapTimeSlider` timing output for browser verification.
- Added an `Incidents` checkbox so historic records stay default-off.

Local browser check:

```text
RENDER timeslider-canvas-timing ok=True
time-slider shadows: 6921
compute: 10.2 ms
draw: 29.3 ms
render total: 39.6 ms
```

This run loaded `docs/prototype_timeslider.html` through Playwright using a
`file://` URL at 1280x800. It is not a complete production benchmark, but it
confirms the new draw path renders nonblank canvas shadows and removes the old
per-tick Leaflet polygon allocation from the hot path.

### Why this was a strong result

This speedup worked because it attacked the right layer of the stack.
The slow part was not the sun math. It was not the convex hull calculation.
It was the browser repeatedly rebuilding thousands of Leaflet polygon objects
every time the slider moved.

Before the change, each time tick did roughly this:

1. Compute shadow rings.
2. Convert them into GeoJSON Feature objects.
3. Call `L.GeoJSON.addData`.
4. Let Leaflet allocate and register thousands of `L.Polygon` children.
5. Ask Leaflet to draw those polygons onto canvas.

After the change, each time tick does this:

1. Compute shadow rings.
2. Keep them as lightweight `[height, ring]` tuples.
3. Clear one canvas.
4. Fill the rings directly with the 2D canvas API.

That cuts out the expensive middle layer: per-feature GeoJSON parsing inside
Leaflet and per-polygon object creation. The result is exactly the kind of
optimization that matters for an interactive time slider: fewer allocations,
less framework overhead, and direct drawing in the hot path.

Presentation version:

> The first version treated every shadow as a map feature. That was correct,
> but slow for animation. The faster version treats the moving shadow layer as
> a drawing problem: compute the same geometry, then paint it directly onto one
> canvas. This preserves the ShadowMap story while making the slider feel live.

Shorter version:

> We did not make the math simpler. We removed the rendering overhead between
> the math and the pixels.

Numbers to cite:

| Metric | Before | After |
| --- | ---: | ---: |
| Time-slider tick median | ~250 ms | ~40 ms local smoke |
| Time-slider tick p95 | ~375 ms | Not rebenchmarked yet |
| Shadow compute | ~30 ms historical estimate | 10.2 ms in smoke |
| Shadow draw | Leaflet object rebuild dominated | 29.3 ms direct canvas |
| Rendered shadows in smoke | -- | 6,921 |

Use the numbers carefully: the after value is from a local Playwright smoke,
not a full best-of-N benchmark. The defensible claim is that the hot path moved
from Leaflet object reconstruction to direct canvas drawing, and the observed
interactive redraw dropped to roughly 40 ms in the smoke run.

Next candidates after this pass:

- Spatially bucket buildings in the browser so viewport culling does not scan
  the whole building array every tick.
- Add a level-of-detail building ring for zoom 16 and keep full geometry for
  zoom 17-18.
- Move shadow computation into a Web Worker if UI thread blocking remains.
- For the local-first app phase, consider serving bbox-specific building chunks
  from a local API instead of embedding all geometry in one HTML file.

## AI Feature Order

AI comes after the speed baseline is acceptable.

1. Explain This View: summarize the current shade, heat, light, and data limits.
2. Find Shade Near Me: use location or a dropped pin to recommend nearby shaded
   blocks from computed shade and tree canopy.
3. Where Is It Bright At Night: use streetlight density and open venues to
   identify brighter nearby corridors after dark.
4. Route brief: summarize shade and light tradeoffs for a proposed walk.
5. Planner mode: explain data gaps, lighting clusters, and shade coverage.
6. Validation assistant: compare field photos against rendered shadow direction.

AI answers must stay evidence-bound: name the active layer, mention data vintage
or limits where relevant, and avoid route safety claims.
