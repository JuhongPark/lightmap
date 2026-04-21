# LightMap Scale-Up Plan

Related: [Prototype Plan](prototype-plan.md) - Section 3 (Scale Up stages table)

## Context

Prototype validated shadow engine and night mode with 1 sample per city.
This plan scales both day and night modes from 1% to 100% of total data.

## Current Data Inventory

| Dataset | Total Records | Status |
|---------|--------------|--------|
| Cambridge buildings (TOP_GL > 0) | 17,986 | downloaded |
| Boston buildings (BLDG_HGT_2010 > 0) | ~28,000* | not downloaded |
| Boston streetlights | 74,065 | downloaded |
| Cambridge streetlights | 6,117 | downloaded |
| Food establishments | 3,207 | downloaded |

*Boston buildings count to be confirmed after download.

## Scale Stages

| Stage | Cam Buildings | Bos Buildings | Cam Streetlights | Bos Streetlights | Food |
|-------|-------------|-------------|-----------------|-----------------|------|
| ~~1 each~~ (done) | 1 | 1 (hardcoded) | 1 | 1 | 1 |
| 1% | 180 | ~280 | 61 | 741 | 32 |
| 10% | 1,799 | ~2,800 | 612 | 7,407 | 321 |
| 50% | 8,993 | ~14,000 | 3,059 | 37,033 | 1,604 |
| 100% | 17,986 | ~28,000 | 6,117 | 74,065 | 3,207 |

## Preparation (before 1% stage)

### 1. Download Boston buildings

Add to `scripts/download_data.py`:
- Source: `https://data.boston.gov/dataset/boston-buildings-with-roof-breaks` (GeoJSON, ~106MB)
- Save to: `data/buildings/boston_buildings.geojson`
- Height field: `BLDG_HGT_2010` (feet, already matches compute.py)

### 2. Add --scale parameter to prototype.py

`--scale N` where N = 1, 10, 50, 100 (percent).

Day mode:
- Load Cambridge buildings GeoJSON, sample N% of those with TOP_GL > 0
- Load Boston buildings GeoJSON, sample N% of those with BLDG_HGT_2010 > 0
- Convert Cambridge TOP_GL (meters) to feet
- Write combined sample to temp GeoJSON, pass to compute_all_shadows

Night mode:
- Load N% of Boston streetlights from CSV
- Load N% of Cambridge streetlights from GeoJSON
- Load N% of food establishments from CSV

Sampling: use random.sample with seed=42 for reproducibility.

### 3. Add --scale parameter to screenshot.py

Pass --scale through to prototype.py.
`--both --scale 10` generates day_10.png and night_10.png.

## Execution

Each stage follows the same loop:

```
1. .venv/bin/python scripts/screenshot.py --both --scale N
2. Visually verify day + night screenshots
3. Note rendering time and any issues
4. Commit: "feat: scale prototype to N%"
```

### Stage: 1%

Expected:
- Day: ~460 buildings with shadows across Boston + Cambridge
- Night: ~800 streetlight heatmap points, ~32 food markers
- HTML file size: moderate
- Rendering: should be fast

Verify:
- Shadows visible across both cities
- Heatmap shows sparse but distributed points
- No rendering errors or missing data

### Stage: 10%

Expected:
- Day: ~4,600 buildings with shadows
- Night: ~8,000 streetlight points, ~321 food markers
- Heatmap starts showing street patterns

Verify:
- Shadow coverage visible across neighborhoods
- Heatmap shows recognizable street grid patterns
- Rendering time acceptable (< 30s for HTML generation)

### Stage: 50%

Expected:
- Day: ~23,000 buildings with shadows
- Night: ~40,000 streetlight points, ~1,600 food markers
- Heatmap clearly shows major vs minor streets

Verify:
- Dense shadow coverage
- Heatmap clearly distinguishes bright/dark areas
- HTML file size manageable (check if > 50MB)
- If too slow: consider reducing shadow detail or splitting layers

### Stage: 100%

Expected:
- Day: ~46,000 buildings with full shadow coverage
- Night: ~80,000 streetlight points, ~3,200 food markers
- Complete heatmap of Boston + Cambridge

Verify:
- Full coverage, no gaps
- Heatmap matches expected city lighting patterns
- HTML file size and load time acceptable
- If HTML too large for folium: note as limitation, consider vector tiles for interactive app

## Risks

| Risk | Stage | Mitigation |
|------|-------|------------|
| Boston buildings download fails (106MB) | prep | Retry. Fall back to CKAN CSV (28K with WKT). |
| Shadow computation too slow at 50%+ | 50% | Profile. Skip buildings below height threshold. |
| folium HTML too large at 100% | 100% | Shipped anyway. INITIAL_BBOX pre-filter + 3 m simplify + 5-decimal coords + per-strategy render optimizations kept the time-slider artifact at ~27 MB. See `deploy-size-trim-plan.md`. |
| Memory issues with large GeoJSON | 50%+ | Process in chunks. Reduce precision of coordinates. |

## File Changes

| File | Change |
|------|--------|
| `scripts/download_data.py` | Add Boston buildings download |
| `src/prototype.py` | Add --scale parameter, load both cities |
| `scripts/screenshot.py` | Pass --scale through |
| `planning/prototype-plan.md` | Update stage status as completed |
