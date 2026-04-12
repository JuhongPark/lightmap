# v7 Optimization Plan

## Goal

Push LightMap's 100% scale day pipeline from v6's **18.84s below 5s** (stretch: below 3s). Each optimization is implemented as a separate sub-version so we can attribute the speedup to a specific technique and compare all of them in the final report.

## Baseline

v6 numbers, best of 3, 100% scale, from commit `c2d7d07`:

| Stage | v6 (s) | Notes |
|-------|--------|-------|
| load_buildings | 5.54 | PostGIS query + Python WKB decode |
| shadow_projection | 6.00 | PostGIS ST_Translate + ST_ConvexHull |
| compute_coverage | 7.26 | Python STRtree-batched union |
| folium_render_day | 0.04 | Negligible |
| **Day pipeline** | **18.84** | |
| Memory peak | 1,344 MB | |

### Profile findings (what the plan is based on)

1. **`compute_shadow_coverage`**: of 9.9s profiled, **4.67s (47%) is wasted re-parsing GeoJSON dicts back into Shapely polygons**. The shadow polygons were just freshly created by postgis_compute from WKB — we serialize them to GeoJSON-like dicts, then re-parse them in coverage. Pure waste.
2. **`compute_all_shadows_postgis`**: of 8.7s profiled, 3.36s is the actual DB query. The remaining 5.3s is Python-side marshaling — iterating coordinates, building dicts for folium.
3. **PostgreSQL `max_parallel_workers_per_gather = 2`** on the running container. Server has 8 cores available. Current shadow projection is leaving 75% of the parallelism on the floor.
4. **PostGIS GEOS version is 3.9.0**, Python side has GEOS 3.13.1 (4 major versions newer, with significant union optimizations). Python should beat PostGIS for union operations.
5. **Research indicates rasterization** (stamp shadows into a numpy raster and count) is the theoretically fastest approach for "area of union of N polygons" when you only need the area, not the geometry. Expected < 1s.

## Sub-versions

Each layered on top of the previous one. Every variant gets its own commit and benchmark row.

### v7a — Eliminate shapely ↔ dict round-trip in coverage

The biggest profile-indicated win. `compute_all_shadows_postgis` already has shapely polygons from WKB decoding. Instead of throwing them away after building feature dicts, pass them alongside to the coverage function.

**Change**:
- `compute_all_shadows_postgis` returns `(features, polygons, altitude, azimuth)` instead of `(features, altitude, azimuth)`
- New `compute_shadow_coverage_from_polys(polys)` that skips `shape(feat["geometry"])`
- `prototype.py` `_load_buildings_and_shadows` uses the new path when PostGIS is active
- `benchmark.py` measures the new coverage path

**Expected**: coverage 7.26s → ~3.5s

**Dependencies**: none

**Risk**: other callers (Python v5 path) need the existing function; add a shim so the old signature still works.

---

### v7b — PostgreSQL parallel workers

PostgreSQL caps parallel workers at 2 by default. Our workload (per-row ST_Translate + ST_ConvexHull on 123K rows) is embarrassingly parallel and benefits almost linearly up to 8 workers.

**Change**: Prepend to the shadow query in `compute_all_shadows_postgis`:

```sql
SET LOCAL max_parallel_workers_per_gather = 8;
SET LOCAL parallel_tuple_cost = 0.001;
SET LOCAL parallel_setup_cost = 10;
SET LOCAL min_parallel_table_scan_size = '1MB';
```

All `SET LOCAL` — no container or global change.

**Expected**: shadow_projection 6.00s → ~3.0s

**Dependencies**: none

**Risk**: Planner may refuse parallel plans; verify with `EXPLAIN (ANALYZE, VERBOSE)` that `Workers Launched: 8` appears. If not, lower `parallel_tuple_cost` further.

---

### v7c — Vectorized Python-side marshaling

The per-row `wkb_loads(bytes(wkb))` + `list(poly.exterior.coords)` loop eats ~3s of post-DB time in `compute_all_shadows_postgis`. Shapely 2.x has vectorized ufuncs that run in C with the GIL released.

**Change**: Batch decode via `shapely.from_wkb(numpy_array)` and `shapely.get_coordinates` for bulk coordinate extraction. Features for folium can be constructed lazily.

**Expected**: shadow_projection (Python-side) 5.3s → ~1s. Combined with v7b's DB speedup, shadow_projection target ~2.5s.

**Dependencies**: none (shapely 2.1.2 already installed)

**Risk**: Output must still be dicts for folium. Keep a lazy/vectorized path for the compute pipeline and a dict path only when folium actually renders.

---

### v7d — disjoint_subset_union_all

Shapely 2.1's `shapely.disjoint_subset_union_all` was designed exactly for large polygon sets with many disconnected clusters. Shadow sets over city blocks fit that structure. Drop-in comparison vs our current 50×50 STRtree batching.

**Change**: Add `compute_shadow_coverage_disjoint()` as an alternative to `compute_shadow_coverage`. Benchmark both. Keep the faster one.

**Expected**: possibly -1 to -2s on coverage. May be slower on dense downtown clusters. Empirical.

**Dependencies**: none (shapely 2.1.2)

**Risk**: If slower, keep v4 STRtree path. Only adopt if it wins.

---

### v7e — Rasterization of coverage

The theoretical optimum for "area of union of N polygons". Stamp each shadow onto a 2400×2400 (5m resolution) numpy uint8 raster, then `mask.sum() × cell_area`. Skips vector union entirely.

**Change**: New `compute_shadow_coverage_raster()` using `rasterio.features.rasterize`. Reprojects study area bounds to a raster transform at 5m resolution in lat/lon degrees. Validates numerical agreement with current 22.637% to within 0.01%.

**Expected**: **coverage 7.26s → 0.3–0.8s**

**Dependencies**: `rasterio 1.5.0` — **already installed** (verified)

**Risk**:
- Accuracy must match Shapely union to ≤ 0.01% (5m resolution vs a ~12km × 12km area)
- Set `GDAL_CACHEMAX` env var ≥ raster size or rasterio degrades to quadratic

---

## Execution order

Strictly sequential. Each step depends on the previous variant for consistent measurement.

1. **v7a** — implement, benchmark, commit
2. **v7b** — implement, benchmark, commit (built on v7a)
3. **v7c** — implement, benchmark, commit (built on v7a+v7b)
4. **v7d** — implement, benchmark, commit (built on v7a+v7b+v7c)
5. **v7e** — implement, benchmark, commit (built on v7a+v7b+v7c+v7d, rasterio path becomes default)
6. **v7 comparison** — append full table to `docs/optimization-plan.md`

After each step: run `.venv/bin/python scripts/benchmark.py`, run unit tests, `git commit -m "perf(v7x): ..."`.

## Rules

- Run every benchmark at **100% scale** (123,105 buildings)
- Best-of-3 runs per stage
- Coverage result must stay at **22.637% ± 0.01%** across all variants (correctness check)
- All 14 unit tests must pass
- Do not proceed to v7(n+1) until v7n benchmark is recorded
- If a variant makes things worse, commit the result anyway (we want the data) and note in the commit message

## Dependencies status

| Package | Required by | Status |
|---------|-------------|--------|
| rasterio 1.5.0 | v7e | Installed |
| shapely 2.1.2 (GEOS 3.13) | v7c, v7d | Installed |
| psycopg2 2.9.9 | v7b | Installed |
| numpy 2.4.4 | v7c, v7e | Installed |

**Nothing left to install.**

## Expected final outcome

| Stage | v6 | v7a | v7b | v7c | v7d | v7e | Tier |
|-------|----|----|----|----|----|----|----|
| load_buildings | 5.54 | ~5.5 | ~5.5 | ~5.5 | ~5.5 | ~5.5 | flat |
| shadow_projection | 6.00 | ~6.0 | ~3.0 | ~2.5 | ~2.5 | ~2.5 | -58% |
| compute_coverage | 7.26 | ~3.5 | ~3.5 | ~3.5 | ~2.5 | ~0.5 | -93% |
| render | 0.04 | 0.04 | 0.04 | 0.04 | 0.04 | 0.04 | flat |
| **Day pipeline** | **18.84** | **~15** | **~12** | **~11.5** | **~10.5** | **~8.5** | |

If the vectorization in v7c also improves `load_buildings`, we'd see the total drop another 2-3s to roughly **~5-6s**. With everything optimized and if PostgreSQL parallelism lands well, we could plausibly hit **~4s**.

## What happens after v7e

After the full v7 series is done we produce a consolidated comparison and conclusions section in `docs/optimization-plan.md`:
- Total speedup from v2 to v7e
- Which technique mattered most
- Which didn't move the needle
- Honest lessons (what we expected to work that didn't)
