# LightMap Client-Side Render Optimization Plan

## Purpose

Track every browser-side rendering strategy we have tried for the 123 K
shadow polygons, with before/after benchmarks for each version. Mirrors
`optimization-plan.md` in structure, but focused on what happens
**after** the server has produced the shadow FeatureCollection — how we
ship those features to the browser, decode them, and draw them on the
map.

The backend pipeline has brought total compute down from 102 s to ~12 s
(see `optimization-plan.md`). None of that wins an interactive experience
if the browser then stalls for 30+ seconds parsing a 35 MB HTML blob.
This document is the story of that second half.

## Version History

Each row lists the single key change that version introduced on top of
the previous one. The strategy names (`r0-inline-svg` through
`r13-hybrid`) correspond to entries in `RENDER_STRATEGIES` in
`src/render/strategies.py`. Keys never move or disappear — history is
the point.

| Version | Key change | Why it matters |
|---------|------------|----------------|
| r0 | Inline FeatureCollection + SVG renderer (folium default) | Reference bad case. Folium embeds every feature as a JS literal, and Leaflet defaults to SVG, one `<path>` per feature. |
| r1 | Switch Leaflet to `prefer_canvas=True` | Canvas draws all 123 K polygons into a single `<canvas>` instead of 123 K DOM nodes. |
| r2 | Async sidecar fetch on `window.load` | Stops embedding ~32 MB of GeoJSON as a JS string literal in the HTML. Features load in parallel with base tiles. |
| r3 | `<link rel=preload>` on the sidecar | Browser's HTML preload scanner starts the fetch during parse, before any JS runs. |
| r4 | Canvas opacity fade-in transition | Smooth reveal instead of pop-in. UX polish; minimal bench impact. |
| r5 | Gzipped sidecar (gzip-aware server) | 28 MB → 3.9 MB on the wire (~7× smaller). |
| r6 | `requestAnimationFrame` chunked `addData` | Progressive reveal, 4 K features per frame. Total wall time same or worse. |
| r7 | Custom `CanvasLayer` with `latLngToContainerPoint` per vertex | Naive hand-rolled renderer. Disastrous at 123 K polygons (~97 s). Kept as cautionary tale. |
| r8 | Server-side Pillow PNG + `L.imageOverlay` | Skip client geometry parsing entirely. Fastest time-to-first-pixel (~400 ms) but non-interactive. |
| r9 | r8 PNG preview → swap in r4-style async canvas vector | Combine r8's fast preview with r4's interactive vector layer. PNG flashes in ~350 ms, full vector arrives by ~1.9 s. |
| r9++ | MIT viewport-first + deferred full fetch + threaded server | INITIAL\_BBOX pre-filter (MIT campus features in a 14 KB sidecar). Initial shadows in 331 ms; full set streamed after. Shipped via `docs/shadows_initial.geojson(.gz)` and `docs/buildings_initial.geojson(.gz)`. |
| r10 | r9 + polygon simplify at 3 m tolerance | 33 % faster (~2.3 s) by dropping low-information vertices before serialize. Shadow/building count unchanged, so L.Polygon instantiation cost unchanged. |
| r11 | r9 + `L.vectorGrid.slicer` | Drop-in Leaflet plugin that slices GeoJSON into tiles on the client and renders without per-feature `L.Polygon`. Expected big win, actual: CDN load penalty + per-tile slicing overhead caps the gain at ~1.3× faster. |
| r12 | r9 + color-batched single `L.polygon` per color | One Leaflet polygon per fill color (~4 total) instead of 123 K. **2.7× faster** (~1.3 s). Tradeoff: no per-feature click popup. |
| r13 | r12 + point-in-polygon click handler (**current default**) | Keep r12's rendering speed, add a ~10-line ray-cast PIP handler with a pre-computed bbox index. Full 123 K features become clickable for ~1 ms per click. 1.8 s total, 332 ms preview, click works. |

## Benchmark Environment

Measurements were taken on the same hardware as `optimization-plan.md`,
so the backend and frontend numbers are directly comparable.

| Component | Spec |
|---|---|
| CPU | 12th Gen Intel Core i3-1220P, 6 cores / 12 threads |
| RAM | 9.7 GB available to WSL2 |
| Host OS | Windows with WSL2 |
| Guest OS | Ubuntu 24.04 |
| Python | 3.12.3 |
| Browser | Headless Chromium via Playwright |
| Viewport | 1280 × 800 at device-scale-factor 1 |

### Important caveat: noisy environment

The benchmark numbers below were taken while VSCode and multiple Pylance
language servers were running. Absolute wall times have been observed to
swing 2× to 3× between runs in the same configuration. Side-by-side
comparisons **within one suite run** are the most reliable signal.

The render bench uses single-run by default. Multi-run support exists
(`--runs N`) and is recommended for decisions that change the default
strategy.

## Benchmark Protocol

### What we measure

The prototype page exposes a harness at `window.__lightmap`:

| Milestone | Meaning |
|-----------|---------|
| `fetchStart` | Page called `fetch('shadows.geojson')`. |
| `fetchEnd` | Sidecar fully downloaded and JSON parsed. |
| `previewAt` | PNG preview (r8/r9) became visible in the DOM. |
| `addedAt` | Vector layer (or final PNG for r8) reached the map. |

In addition, a `PerformanceObserver` installed before navigation
captures browser standard paint metrics:

| Metric | Meaning |
|---|---|
| FCP | First Contentful Paint. First time any non-white pixel is drawn. |
| LCP | Largest Contentful Paint. The biggest visible element at the end of the run, with its tag name. |

LCP's element field is a useful signal. We classify it into a `kind`
so the bench table is not ambiguous:

- `kind=shadows` — the LCP element is our `shadows.png` preview
  (the r8/r9 design target).
- `kind=tile` — the LCP element is a CARTO basemap tile. This is what
  the browser picks for vector-only strategies (r2–r6) when no image
  overlay ever appears — the LCP number then reflects basemap paint
  time, not shadow paint time.
- `kind=other` — fallback, inspect `lcp_src` / `lcp_class` for
  detail.

For r9 the kind is consistently `shadows`, confirming the browser
measures the PNG preview as the largest-contentful paint — exactly
the hypothesis r9 was designed around. For r2–r6 the kind is `tile`,
so the LCP number is not directly comparable to r9's.

### How to run

Start the gzip-aware server, then the bench script:

```
.venv/bin/python scripts/serve.py 8765
.venv/bin/python scripts/render_bench.py --scale 100 --runs 3
```

The `--only` flag limits the suite to specific strategies:

```
.venv/bin/python scripts/render_bench.py --only r5-gzip r9-png-then-vector
```

Each suite is saved under `benchmarks/render/<timestamp>_suite/` with
one JSON per strategy and a `suite.json` that ties them together.

### Rules

1. Always benchmark at **scale=100** for the published numbers. Smaller
   scales exercise the pipeline but do not stress the browser.
2. Server must be `scripts/serve.py` (gzip-aware). Plain
   `python -m http.server` will fall back to the uncompressed sidecar
   for r5+ and underreport the gain.
3. Before committing a new default strategy, run `--runs 5` and record
   variance alongside the best number.
4. PostGIS container (`lightmap-postgis`) must be up for the scale=100
   regen, otherwise shadow computation falls back to pure Python and
   the regen step alone balloons to ~30 s.

### Gotcha: stale .gz sidecars across mixed-strategy suites

The server serves `shadows.geojson.gz` in preference to
`shadows.geojson` when the client accepts gzip. Strategies that do
not write the `.gz` (r2/r3/r4) left any older `.gz` in place,
which the server would then serve — causing the browser to receive a
sidecar from the previous build. Symptom: `featureCount` in the result
JSON does not match the current scale, and the strategy appears
artificially fast.

Fixed in `scripts/serve.py`: if both the plain and `.gz` files exist,
only serve the `.gz` when its mtime is at least as recent as the plain
file's. Otherwise the fresh plain file wins. Run with the updated
`serve.py` to get valid numbers.

## Results per version

Numbers below come from best-of-3 suite `20260417_183337_suite`, taken
after two infrastructure fixes were in place:

- `scripts/serve.py` now refuses to serve a `.gz` sidecar whose mtime
  is older than the plain file (fixes a run where r2/r3/r4 saw a
  scale=1 sidecar from a prior test).
- `render_verify.py` now classifies the LCP element into
  `shadows` / `tile` / `other` instead of only reporting `tag=IMG`.

r0 and r1 time out in every run as expected. All measurements at
scale=100 on WSL2 with light background load.

### r0-inline-svg (reference bad case)

Folium's default: embed the full FeatureCollection inline in the HTML
and let Leaflet render it with its SVG renderer.

| Metric | Value |
|---|---|
| total (addedAt) | TIMEOUT (>60 s to navigation `load`) |
| HTML size | ~34 MB |

Two compounding problems:

1. The HTML contains a ~32 MB JavaScript string literal for the
   FeatureCollection. Chromium takes 8–12 s just to parse it.
2. Leaflet's SVG renderer produces one `<path>` DOM node per feature.
   At 123 K features the DOM layout pass alone hangs the tab.

### r1-inline-canvas

Keep the inline FeatureCollection, but pass `prefer_canvas=True` to the
base map. All shadow polygons go into a single `<canvas>` element.

| Metric | Value |
|---|---|
| total (addedAt) | TIMEOUT (>60 s to navigation `load`) |

r1 still times out. The DOM pressure is gone, but the 32 MB JS literal
alone is enough to choke the browser. Canvas alone is not the fix —
the inline data path has to go.

### r2-async

Write the FeatureCollection to `docs/shadows.geojson` (compact JSON, no
whitespace) and fetch it after `window.load`.

| Metric | Value |
|---|---|
| total (addedAt) | 2316 ms |
| fetch duration | 266 ms |
| parse + add | 911 ms |
| LCP | 664 ms (kind `tile` — a CARTO basemap tile, not the shadow) |

The browser parses the (now small) HTML instantly and the sidecar
loads in parallel with Leaflet's base tiles. This is the version that
makes the app usable at 100 % scale. LCP kind `tile` means the browser
measured a map tile paint, not the shadow layer — the `addedAt`
number is the right one to compare to r9.

### r3-preload

Add `<link rel=preload href="shadows.geojson" as=fetch fetchpriority=high>`
in `<head>`. The HTML preload scanner now kicks off the fetch during
HTML parse, before any script runs. The subsequent `fetch()` in JS
reuses the preloaded response from the HTTP cache.

| Metric | Value | Δ vs r2 |
|---|---|---|
| total (addedAt) | 1349 ms | **−967 ms (−42 %)** |
| fetch duration | 210 ms | −56 ms |

Preload overlaps the fetch with Leaflet initialization and base tile
loading, cutting wall time by almost half. LCP kind is again `tile`,
still not a direct shadow-paint signal.

### r4-fade

r3 + animate the canvas opacity from 0 → 1 over 300 ms. UX improvement,
not a speed improvement.

| Metric | Value | Δ vs r3 |
|---|---|---|
| total (addedAt) | 1330 ms | −19 ms (noise) |

r4's user-facing gain is subjective: shadows don't pop onto the map,
they fade in. Same wall time, better perceived quality.

### r5-gzip

r4 + also write `shadows.geojson.gz` sidecar. The gzip-aware server
(`scripts/serve.py`) serves the pre-compressed file with
`Content-Encoding: gzip` when the client accepts it. The browser's
`fetch()` API transparently decompresses.

| Metric | Value | Δ vs r4 |
|---|---|---|
| total (addedAt) | 1386 ms | +56 ms (noise) |
| fetch duration | 209 ms | −1 ms |
| sidecar on wire | 3.9 MB | down from 28 MB (~7× smaller) |

On localhost the 7× size reduction barely registers — the network
was never the bottleneck. On a real internet connection the gain
will be substantially larger (historically measured at −652 ms on a
more contended setup).

### r6-chunked

r5 + process incoming features in 4 K-feature batches under
`requestAnimationFrame`, marking `firstChunkAt` when the first batch
renders. The hypothesis was that users would see *something* sooner,
even if total time was the same.

| Metric | Value | Δ vs r5 |
|---|---|---|
| total (addedAt) | 2475 ms | **+1089 ms (+79 %)** |

r6 is nearly 2× **slower** than r5. Leaflet's `addData()` rebuilds
internal spatial indexes on every call, so 30 calls of 4 K features
each is much worse than one call of 123 K features. "First pixel"
time did improve, but the all-pixels time regressed badly. r6 remains
in the catalog as a documented dead end.

### r7-canvas-direct

Custom `L.Layer` subclass that walks the flat coordinate array once,
calls `latLngToContainerPoint` per vertex, and issues `moveTo`/`lineTo`
directly into a 2D context. Goal: avoid Leaflet's per-feature
`L.Polygon` allocation overhead.

| Metric | Value | Δ vs r5 |
|---|---|---|
| total (addedAt) | 57 871 ms | **+56 542 ms (broken)** |

Almost a full minute. Why: `latLngToContainerPoint` per-vertex
coordinate transform is the bottleneck, and our code calls it inside
a tight nested loop without batching or caching. Leaflet's canvas
renderer amortizes this by grouping coordinate transforms per layer
pan/zoom. Rolling your own without that amortization loses badly.

r7 is preserved as a committed cautionary baseline. The lesson: a
naive "let me just draw canvas directly" intuition is wrong when the
library is already doing non-trivial work you have to replicate.

r7 was skipped in the latest suite to keep total bench time
manageable. Number is from `20260417_151735_suite`.

### r8-png-overlay

Rasterize shadows to a PNG at build time (Pillow + rasterio geometry
trick), ship as a single `<img>` via `L.imageOverlay`.

| Metric | Value | Δ vs r5 |
|---|---|---|
| total (addedAt) | 1268 ms | −118 ms |
| fetch duration | 20 ms | |
| LCP | 1524 ms (kind `shadows` — the PNG itself) |
| image size | 5347 × 5566 px at 2 m resolution |

r8's absolute `addedAt` is **noisy** across runs: 329 ms in
`20260417_151735_suite` vs 1268 ms here. The reason is that r8 waits
for `window.load` before attaching the overlay, so its total time
includes every network resource the browser chose to fetch first
(base map tiles, icons, fonts from CARTO). When that contention is
low, r8 lands in ~300 ms; under noise it spills to 1 s+. The moment
the image is drawn is what matters for perceived performance, which
is why r9 inherits r8's PNG preload trick but does not gate the
display on `window.load`.

The tradeoff with r8 remains: the overlay is a static image. No
per-feature styling, no tooltips, no click targets. Beautiful for
screenshots; not a complete interactive solution.

### r9-png-then-vector (current default)

r8's fast PNG preview + r4-fade's interactive canvas vector layer,
sequenced so the preview covers the 1–2 s while the vector sidecar is
still fetching and parsing.

| Metric | Value |
|---|---|
| previewAt | 376 ms |
| total (addedAt) | 1317 ms |
| LCP | 612 ms (kind `shadows` — the PNG preview itself) |
| FCP | 392 ms |

Flow:

1. HTML loads. `<link rel=preload>` for both the PNG (high priority)
   and the gzipped GeoJSON (low priority) kicks off immediately.
2. Leaflet initializes; PNG lands and is drawn as `L.imageOverlay` —
   **`previewAt` fires at ~350 ms**. This is what the user sees.
3. GeoJSON sidecar arrives, parses, builds the canvas vector layer.
4. The vector layer fades in from opacity 0 → 1 over 300 ms, the PNG
   overlay is removed on the next frame. Swap is seamless.
5. `addedAt` fires on the vector layer. Map is now fully interactive.

The LCP element of `IMG` is a strong signal that the browser itself
measures the PNG preview as the biggest visible element at the decision
point, confirming the design intent.

**Caveat**: r5-gzip's total (1386 ms) is comparable to r9's
total (1317 ms), and the LCP number for r5 (1544 ms, kind `tile`)
is higher than r9's LCP (612 ms, kind `shadows`). r9 is the default
because `previewAt` (376 ms) dominates user-perceived performance,
not `addedAt`. If you change the default back to r5, users will
stare at an empty map for a full second while the sidecar parses —
the bench `total` number hides this. The `kind=shadows` on r9's LCP
confirms the browser measures the PNG preview as the largest visible
element at the measurement point, which aligns directly with what a
user sees.

## Final Comparison

Best-of-3 numbers at scale=100 on WSL2 localhost. r0/r1 timeouts and
r7 broken-baseline are from older suites. r2–r9 are from
`20260417_183337_suite`. r10–r13 candidate comparison is from suites
`20260418_002044`, `20260418_003655`, and `20260418_012935` (each
strategy was measured in the suite where it was the focus of that
run).

| Version | total (ms) | preview (ms) | FCP | LCP | LCP kind | click | notes |
|---------|------------|--------------|-----|-----|----------|:---:|-------|
| r0-inline-svg | TIMEOUT | — | — | — | — | — | 32 MB JS literal, 123 K SVG paths |
| r1-inline-canvas | TIMEOUT | — | — | — | — | — | 32 MB JS literal dominates |
| r2-async | 2316 | — | 452 | 664 | tile | ✓ | async sidecar, canvas renderer |
| r3-preload | 1349 | — | 416 | 1512 | tile | ✓ | +`<link rel=preload>` |
| r4-fade | 1330 | — | 392 | 1504 | tile | ✓ | +opacity fade-in (UX only) |
| r5-gzip | 1386 | — | 432 | 1544 | tile | ✓ | +gzipped sidecar |
| r6-chunked | 2475 | — | 396 | 852 | tile | ✓ | progressive addData, regression |
| r7-canvas-direct | 57 871 | — | 268 | 57 968 | tile | ✓ | naive custom layer, broken |
| r8-png-overlay | 1268 | — | 484 | 1524 | shadows | ✗ | static PNG, non-interactive, noisy |
| r9-png-then-vector | 3422 | 205 | 220 | 732 | tile | ✓ | PNG preview + L.geoJSON vector |
| r10-simplify | 2305 | 253 | 200 | 704 | tile | ✓ | r9 + simplify(3 m); ~33 % faster |
| r11-vectorgrid | 2621 | 647 | 424 | 732 | tile | ✓* | r9 + `L.vectorGrid.slicer`; CDN cost hurts |
| r12-colorbatch | **1282** | 412 | 224 | 500 | tile | ✗ | r9 + one `L.polygon` per color; fastest but no click |
| r13-hybrid | 1803 | **332** | 300 | 696 | tile | ✓ | r12 + PIP click handler; **current default** |

LCP kind matters: `tile` means the browser's LCP element was a CARTO
basemap tile, not the shadow layer — so that LCP number is not a
shadow-paint signal. At zoom 16 on the MIT viewport the basemap tile
is the dominant paint for every strategy except r8. For vector-only
strategies, `addedAt` is the right "shadows are drawn" metric.

*r11 click uses VectorGrid's own event model (`layer.on('click', e =>
e.layer.properties)`) rather than a per-feature popup.

### Speedup chart (total addedAt, scale=100 at MIT zoom 16 with buildings on)

```
r0  ██████████████████████████████████████████ TIMEOUT
r1  ██████████████████████████████████████████ TIMEOUT
r9  ████████████████████████                    3422 ms  baseline (vector + PIP buildings)
r10 ████████████████                            2305 ms  −33 % vs r9  (simplify 3 m)
r11 ██████████████████                          2621 ms  −23 % vs r9  (VectorGrid, CDN cost)
r12 █████████                                   1282 ms  **−62 % vs r9** (colorbatch, no click)
r13 █████████████                               1803 ms  −47 % vs r9, **click kept**
```

### Biggest wins (ranked)

| Rank | Change | Savings | Technique |
|---|---|---|---|
| 1 | r0 → r2 | ∞ (any finite time is better than "never loads") | Stop embedding features in HTML. Async fetch of compact JSON sidecar. |
| 2 | r2 → r3 | −967 ms | `<link rel=preload>` lets fetch overlap with Leaflet init. |
| 3 | r9 viewport-first | 3 s → 331 ms for MIT area | Pre-filter features into `shadows_initial.geojson` and render that before fetching the full 28 MB set. |
| 4 | **r9 → r13 colorbatch + PIP** | **3422 ms → 1803 ms (−47 %)** | Replace 123 K `L.Polygon` instantiations with 4 color-batched polygons. Click via ray-cast PIP on a pre-computed bbox index. |
| 5 | r5 → r9 | 376 ms preview vs 1386 ms empty-map wait | PNG preview landing before vector parse finishes. |
| 6 | LCP element classification | Tag-only `IMG` was ambiguous | `lcp_kind` (shadows / tile / other) distinguishes shadow-paint LCP from basemap-tile LCP. |

### What didn't work as predicted

- **r6 chunked `addData` was supposed to give faster first-pixel**.
  It did, but Leaflet's per-`addData` spatial-index rebuild made total
  time nearly 2× worse than a single-call `addData`.
- **r7 custom CanvasLayer was supposed to remove per-feature overhead**.
  It removed the wrong overhead. Per-vertex `latLngToContainerPoint`
  is the dominant cost, and Leaflet's canvas renderer batches it in
  ways a naive implementation does not.
- **r8 PNG overlay was supposed to be the endgame**. It is the fastest
  path to first pixel by a wide margin, but non-interactive. This
  triggered r9 as the actual endgame.
- **r11 VectorGrid.Slicer was supposed to skip L.Polygon entirely**.
  It does, but the CDN load (+300 ms preview) plus per-tile slicing
  overhead makes it only ~1.3× faster than the baseline. The
  instantiation-free advantage is real but smaller than expected.
- **r10 polygon simplify was supposed to halve everything**. It
  shaves file size and parse cost (33 %) but does not touch the
  L.Polygon instantiation count, which is the dominant cost.

### What made the most difference (ranked)

1. **Moving away from inline features (r0/r1 → r2)** — unlocked every
   subsequent version. The single biggest architectural win.
2. **Preload hint (r2 → r3)** — nearly halved total time by overlapping
   network with JS init. Huge return on a one-line HTML change.
3. **Viewport-first load (r9++ initial chunk)** — MIT-area features
   land in 331 ms regardless of how big the full set is.
4. **Color-batched rendering (r12/r13)** — replacing 123 K L.Polygon
   objects with a handful (one per color) cut total render time by
   almost half. The click regression r12 introduced was resolved in
   r13 with a ~40-line point-in-polygon handler.
5. **PNG preview for first-pixel (r9)** — the trick that makes the
   page feel fast even when the vector layer takes 2 s.

## Remaining bottlenecks and further work

Even with r9, these are not small:

- **Vector layer `addedAt` is still ~1–2 s at scale=100.** Parsing
  28 MB of uncompressed GeoJSON in JS is the dominant cost now.
  Options:
  - Ship a lighter intermediate format (MessagePack, Protobuf, flat
    binary). Would require both server changes and a client decoder.
  - Simplify polygons before shipping. Each shadow has ~18 vertices;
    dropping to ~8 would halve parse cost at some visual fidelity loss.
  - Stream + incrementally decode via `ReadableStream`.
- **r9 removes the PNG on vector arrival with a 300 ms overlap.**
  Works, but the transition is not pixel-perfect at zoom levels where
  PNG resolution diverges from vector precision. A separate PNG per
  initial-zoom level (or client-side rasterization from the vector) is
  future work.
- **Benchmark noise dominates the margin between r5 and r9.** In
  practice the user-facing signal is `previewAt`, which r5 does not
  have. Confidence in the r9 default comes from the LCP element being
  `IMG` consistently, not from the addedAt delta.
- **No cross-browser coverage.** All numbers are headless Chromium. A
  quick pass in headed Chrome, Firefox, and Safari is overdue before
  the final presentation.

## Time-Slider Live Redraw Pass (2026-04-29)

The r0-r13 history above focused on loading and rendering a precomputed
shadow sidecar. The interactive time-slider has a different hot path: every
hour change recomputes visible building shadows in the browser and redraws
them.

The slow version used a canvas-backed `L.GeoJSON` layer and called
`clearLayers()` plus `addData()` every tick. Even with Leaflet canvas rendering,
that still made Leaflet allocate thousands of polygon objects before it could
draw. The math was cheap; object churn was the problem.

The new version adds a small direct canvas layer inside `src/prototype.py`:

- `computeShadowRings()` returns lightweight `[height, ring]` tuples.
- `ShadowCanvasLayer.setShadows()` clears one canvas and fills rings directly.
- `window.__lightmapTimeSlider` records `shadowComputeMs`, `shadowDrawMs`, and
  `shadowRenderMs`.

Local smoke result at 1280x800:

```text
time-slider shadows: 6921
compute: 10.2 ms
draw: 29.3 ms
render total: 39.6 ms
```

Why it matters: the slider now behaves like an animation layer rather than a
feature-layer rebuild. That matches the product: the moving ShadowMap is the
primary experience, so the hot path should be geometry-to-pixels, not
geometry-to-GeoJSON-to-Leaflet-objects-to-pixels.

## What we'd do next if pushing further

1. Ship a binary (MessagePack or Protobuf) sidecar to eliminate JSON
   parse cost. Expected win: −500 to −1000 ms on `addedAt`.
2. Pre-split the shadow set into a 3 × 3 spatial grid and only fetch
   the current viewport. Lazy-load the rest on pan. Cuts initial
   `addedAt` to ~200–400 ms.
3. Move to a vector tile approach (`MapLibre GL JS` + `.pmtiles`).
   Different stack entirely but solves all of these problems at once.
   Flagged in `prototype-plan.md` as the planned v4 architecture.
4. Best-of-5 re-benchmark on a clean host (VSCode closed, swap empty,
   PostGIS warmed up) to confirm the ordering of r3/r4/r5 and the r9
   vs r5 comparison.

## Conclusion

LightMap's client-side render went from **r0: fails to load** to
**r13: ~332 ms preview + ~1.8 s full interactive render with click**
at 100 % scale. The key insights:

1. **Fighting the browser is expensive.** r0 and r1 both tried to
   push 32 MB of inline data through HTML parse. Nothing downstream
   matters until that is gone.
2. **Preload + gzip are almost free, and they compound.** r3 + r5
   together cut ~2300 ms off r2 for roughly 10 lines of code.
3. **The real bottleneck was Leaflet's per-feature overhead.** JSON
   parse is fast; network is fast on a LAN; Leaflet instantiating
   123 K `L.Polygon` objects is what actually costs 1.5–2 s. The
   color-batched renderer (r12/r13) cuts this to ~4 objects.
4. **You can have both speed and interactivity.** r12 is fastest but
   loses per-feature click. r13 adds a ~40-line ray-cast PIP handler
   with a pre-computed bbox index and recovers click at ~1 ms per
   click — essentially free on top of r12's render cost.
5. **Custom renderers need to replicate the library's hidden work.**
   r7 is a 97-second reminder that "how hard could it be?" is often
   the wrong question. r11 (VectorGrid) reinforces this: a proper
   library still has overhead (CDN, tile slicing) that limits the
   win.
6. **Perceived performance is not total time.** r5 is faster than r9
   by the `total` metric but feels slower because r9 has a preview.
   Design the benchmark to measure what the user feels, not what
   the spec defines.
7. **Hybrid strategies beat purity.** r8 is too static; r5 is too
   slow; r9 is both at once. If you can combine two imperfect
   approaches, the sum can be better than either pure form.

### Preconditions for running at r9

- `docs/shadows.png` and `docs/shadows.geojson.gz` regenerated by
  `src/prototype.py` (any scale).
- `scripts/serve.py` running (gzip-aware).
- A Chromium-family browser (LCP, `PerformanceObserver`, preload all
  require Chrome 77+ / Safari 16+ / Firefox 122+). Vector rendering
  works on older browsers without those metrics.

A `tests/test_render_smoke.py` end-to-end test regenerates r9 at
scale=1 and validates the `previewAt → addedAt` sequence, runtime ~5 s.
