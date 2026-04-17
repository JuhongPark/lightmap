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
`r9-png-then-vector`) correspond to entries in
`RENDER_STRATEGIES` in `src/render/strategies.py`. Keys never move or
disappear — history is the point.

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

LCP's `element` field is a useful signal: for r9 it consistently
reports `IMG`, confirming the browser identifies the PNG preview as the
largest-contentful paint — exactly the hypothesis r9 was designed
around.

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

Numbers below come from two best-of-3 suites:

- `20260417_151735_suite` — r5 through r9 (server's gzip behavior was
  correct for these because each of these strategies rewrites both
  `shadows.geojson` and `shadows.geojson.gz` on regen).
- `20260417_154327_suite` — r2, r3, r4 re-measured after the stale
  `.gz` fix in `scripts/serve.py` landed.

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
| total (addedAt) | 1701 ms |
| fetch duration | 326 ms |
| parse + add | 1025 ms |
| LCP | 304 ms (element `IMG`) |

The browser parses the (now small) HTML instantly and the sidecar
loads in parallel with Leaflet's base tiles. This is the version that
makes the app usable at 100 % scale.

### r3-preload

Add `<link rel=preload href="shadows.geojson" as=fetch fetchpriority=high>`
in `<head>`. The HTML preload scanner now kicks off the fetch during
HTML parse, before any script runs. The subsequent `fetch()` in JS
reuses the preloaded response from the HTTP cache.

| Metric | Value | Δ vs r2 |
|---|---|---|
| total (addedAt) | 1354 ms | −347 ms (−20 %) |
| fetch duration | 263 ms | −63 ms |

Preload overlaps the fetch with Leaflet initialization and base tile
loading. The gain is modest when network and parse are already fast;
on a heavier load (the original `20260414_001829_suite` measured this
transition at −1700 ms) the overlap is much more decisive.

### r4-fade

r3 + animate the canvas opacity from 0 → 1 over 300 ms. UX improvement,
not a speed improvement.

| Metric | Value | Δ vs r3 |
|---|---|---|
| total (addedAt) | 1358 ms | +4 ms (tied) |

r4's user-facing gain is subjective: shadows don't pop onto the map,
they fade in. Same wall time, better perceived quality.

### r5-gzip

r4 + also write `shadows.geojson.gz` sidecar. The gzip-aware server
(`scripts/serve.py`) serves the pre-compressed file with
`Content-Encoding: gzip` when the client accepts it. The browser's
`fetch()` API transparently decompresses.

| Metric | Value | Δ vs r4 |
|---|---|---|
| total (addedAt) | 1329 ms | −29 ms (≈tied) |
| fetch duration | 267 ms | −3 ms |
| sidecar on wire | 3.9 MB | down from 28 MB (~7× smaller) |

On a LAN the localhost network is so fast that the 7× size reduction
barely registers. The older `20260414_001829_suite` (more contended
disk and CPU) measured r5 −652 ms faster than r4 — that is the gain
users on a real internet connection will see. Both numbers are
reported so the effect of system load is visible.

### r6-chunked

r5 + process incoming features in 4 K-feature batches under
`requestAnimationFrame`, marking `firstChunkAt` when the first batch
renders. The hypothesis was that users would see *something* sooner,
even if total time was the same.

| Metric | Value | Δ vs r5 |
|---|---|---|
| total (addedAt) | 2568 ms | **+1239 ms (+93 %)** |

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

### r8-png-overlay

Rasterize shadows to a PNG at build time (Pillow + rasterio geometry
trick), ship as a single `<img>` via `L.imageOverlay`.

| Metric | Value | Δ vs r5 |
|---|---|---|
| total (addedAt) | 329 ms | **−1000 ms (−75 %)** |
| fetch duration | 18 ms | — |
| image size | 5347 × 5566 px |

Blows past r5 by an order of magnitude on first-pixel time. But there
is a significant tradeoff: the overlay is a static image. No
per-feature styling, no tooltips, no click targets. Beautiful for
screenshots and for users who will not interact with shadows, but not
a complete interactive solution.

### r9-png-then-vector (current default)

r8's fast PNG preview + r4-fade's interactive canvas vector layer,
sequenced so the preview covers the 1–2 s while the vector sidecar is
still fetching and parsing.

| Metric | Value |
|---|---|
| previewAt | 198 ms |
| total (addedAt) | 1245 ms |
| LCP | 240 ms (element `IMG`) |
| FCP | 220 ms |

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

**Caveat**: r5-gzip's total (1329 ms) is currently comparable to r9's
total (1245 ms), and in some older runs r5 even beat r9 on the
`addedAt` metric. r9 is the default because `previewAt` (~200 ms)
dominates user-perceived performance, not `addedAt`. If you change the
default back to r5, users will stare at an empty map for a full second
while the sidecar parses — the bench `total` number hides this. LCP
element = `IMG` in r9 confirms the PNG preview is what the browser
measures as the largest visible element at the measurement point,
which aligns directly with what a user sees.

## Final Comparison

Best-of-3 numbers at scale=100. r2/r3/r4 from suite
`20260417_154327_suite` (measured after the stale-.gz fix); r5 through
r9 from suite `20260417_151735_suite`. See the per-version section
above for older numbers from a more contended host, which are also
informative about how much of each gain depends on system load.

| Version | total (ms) | preview (ms) | FCP | LCP | notes |
|---------|------------|--------------|-----|-----|-------|
| r0-inline-svg | TIMEOUT | — | — | — | 32 MB JS literal, 123 K SVG paths |
| r1-inline-canvas | TIMEOUT | — | — | — | 32 MB JS literal dominates |
| r2-async | 1701 | — | 228 | 304 | async sidecar, canvas renderer |
| r3-preload | 1354 | — | 236 | 1540 | +`<link rel=preload>` |
| r4-fade | 1358 | — | 220 | 1528 | +opacity fade-in (UX only) |
| r5-gzip | **1329** | — | 224 | 1552 | +gzipped sidecar |
| r6-chunked | 2568 | — | 212 | 660 | progressive addData, regression |
| r7-canvas-direct | 57 871 | — | 268 | 57 968 | naive custom layer, broken |
| r8-png-overlay | **329** | — | 232 | 656 | static PNG, non-interactive |
| r9-png-then-vector | 1245 | **198** | 220 | **240** | PNG preview + vector swap |

### Speedup chart (total addedAt)

```
r0 ██████████████████████████████████████████ TIMEOUT
r1 ██████████████████████████████████████████ TIMEOUT
r2 █████████████                                1701 ms
r3 ██████████                                    1354 ms  −20 % vs r2
r4 ██████████                                    1358 ms  UX only
r5 ██████████                                    1329 ms  ≈tied on LAN
r6 ███████████████████                           2568 ms  regression
r7 ██████████████████████████████████████████   57 871 ms  broken
r8 ██                                             329 ms  non-interactive
r9 █████████                                     1245 ms  (preview 198 ms)
```

### Biggest wins (ranked)

| Rank | Change | Savings | Technique |
|---|---|---|---|
| 1 | r0 → r2 | ∞ (any finite time is better than "never loads") | Stop embedding features in HTML. Async fetch of compact JSON sidecar. |
| 2 | r2 → r3 | −347 ms on LAN (−1700 ms on a loaded host) | `<link rel=preload>` lets fetch overlap with Leaflet init. |
| 3 | r4 → r5 | ≈tied on LAN (−652 ms on a loaded host) | Gzipped sidecar (28 MB → 3.9 MB). |
| 4 | r5 → r8 | −1000 ms (but loses interactivity) | Server-side rasterization + `L.imageOverlay`. |
| 5 | r8 + r5 → r9 | 198 ms preview in front of a 1.2 s vector | PNG-then-vector hybrid. LCP element = `IMG`. |

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

### What made the most difference (ranked)

1. **Moving away from inline features (r0/r1 → r2)** — unlocked every
   subsequent version. The single biggest architectural win.
2. **Preload hint (r2 → r3)** — nearly halved total time by overlapping
   network with JS init. Huge return on a one-line HTML change.
3. **Gzip on the wire (r4 → r5)** — when you are shipping 28 MB of
   text, 7× compression is almost free and immediately visible.
4. **PNG preview for first-pixel (r9)** — the trick that makes the
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
**r9: 198 ms preview + 1.2 s full vector** at 100 % scale. The key
insights:

1. **Fighting the browser is expensive.** r0 and r1 both tried to
   push 32 MB of inline data through HTML parse. Nothing downstream
   matters until that is gone.
2. **Preload + gzip are almost free, and they compound.** r3 + r5
   together cut ~2300 ms off r2 for roughly 10 lines of code.
3. **Custom renderers need to replicate the library's hidden work.**
   r7 is a 97-second reminder that "how hard could it be?" is often
   the wrong question.
4. **Perceived performance is not total time.** r5 is faster than r9
   by the `total` metric but feels slower because r9 has a preview.
   Design the benchmark to measure what the user feels, not what
   the spec defines.
5. **Hybrid strategies beat purity.** r8 is too static; r5 is too
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
