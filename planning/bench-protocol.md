# Bench Protocol

Checklist for producing trustworthy performance numbers for LightMap.
Must be followed before numbers land in docs, README, or commit messages.

## Before starting

- PostGIS container up: `docker ps | grep lightmap-postgis`
- Gzip-aware server running: `scripts/serve.py 8765`
- `data/buildings.db` present (preprocessed)
- No leftover scale-mismatched sidecar: `ls -la docs/shadows.*`. If a recent regen at a different scale is suspected, delete `docs/shadows.geojson*` and `docs/shadows.png` before running.

## Running

- `render_bench.py --scale 100 --runs 3` is the minimum for claim-grade numbers. Single-run (`--runs 1`) is exploratory only and must be labeled as such in any summary.
- Keep the system quiet. VSCode and Pylance add 2 to 5 times variance at 100 percent scale.
- `render_bench.py` now sanity-checks each regen's sidecar featureCount against the scale. Watch for `[bench] WARNING:` lines — these indicate a stale cache or a sidecar/scale mismatch and must be investigated before accepting the numbers.

## Presenting numbers

- Attach the suite path (`benchmarks/render/<ts>_suite/`) to any number shown to the user or put in a doc. Numbers without a suite path are not reproducible.
- When comparing strategies, prefer within-suite (same session) deltas to cross-session absolute differences. The absolute numbers drift 2 to 5 times with host load, the relative ordering is stable.
- Note the LCP kind (shadows / tile / other) alongside the LCP number. "LCP = 1500 ms" alone is meaningless — LCP for a vector strategy typically measures a CARTO basemap tile, not the shadow layer.

## Red flags

- `featureCount` in a result JSON does not match the regen scale (at 100 percent expect ~123 K, at 1 percent expect ~1230).
- A strategy's total is more than 2 times better or worse than its `expected_total_ms` without an accompanying code change.
- `addedAt` reported but the overlay canvas has zero nonzero pixels. Rendering did not actually happen.
- All strategies in one suite have suspiciously similar numbers. Suggests a shared bottleneck (network, CPU) rather than a real comparison.
