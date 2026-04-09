# LightMap Prototype Implementation Plan

## Rough Roadmap (for reference only)

1. ~~1 each: Shadow + Night validation~~ (done)
2. 1%: ~280 buildings, ~800 streetlights
3. 10%: ~2.8K buildings, ~8K streetlights
4. 50%: ~14K buildings, ~40K streetlights
5. 100%: ~28K buildings, ~80K streetlights (+ Boston buildings download)
6. Safety incident data overlay (later)
7. Optimization and extensions (later)

---

## 1. Shadow Engine Prototype (done)

Shadow direction, length, building connectivity validated.

Files created:
- `src/shadow/compute.py` — get_sun_position, compute_shadow, compute_all_shadows
- `src/prototype.py` — folium HTML generator (day mode)
- `scripts/download_data.py` — Cambridge buildings download
- `requirements.txt` — pvlib, shapely, pandas, numpy, httpx, folium, tzdata

Validation results:
- July 15 2pm: altitude=64.5, azimuth=220.3, shadow ~4.8m (correct)
- Jan 15 2pm: altitude=20.3, azimuth=211.4, shadow longer (correct)
- 2 buildings (Cambridge real + Boston hardcoded) both render correctly

---

## 2. Nighttime Brightness Map

### Goal

Add night mode to prototype. When sun is below horizon, render streetlight heatmap + food establishment markers instead of shadows.

Validate: heatmap shows bright clusters along major streets. Food markers visible.

### Data to Download

| Dataset | Method | Source | Records | Output |
|---------|--------|--------|---------|--------|
| Boston Streetlights | CKAN API | resource_id: `c2fcc1e3-c38f-44ad-a0cf-e5ea2a6585b5` | 74K | `data/streetlights/streetlights.csv` |
| Cambridge Streetlights | GitHub raw | `cambridgegis_data_infra/.../INFRA_StreetLights.geojson` | 6K | `data/cambridge/streetlights/streetlights.geojson` |
| Food Establishments | CKAN API | resource_id: `f1e13724-284d-478c-b8bc-ef042aa5b70b` | 3.2K | `data/safety/food_establishments.csv` |

CKAN base URL: `https://data.boston.gov/api/3/action/datastore_search?resource_id={id}&limit=32000`

Cambridge streetlights URL:
```
https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_infra/main/Street_Lights/INFRA_StreetLights.geojson
```

### Implementation Steps

#### Step 1: Expand download_data.py

Add download functions for:
- Boston streetlights (CKAN API, columns: Lat, Long)
- Cambridge streetlights (GitHub raw GeoJSON)
- Boston food establishments (CKAN API, columns: latitude, longitude, businessname)

CKAN returns JSON with `result.records` array. Extract relevant columns, write as CSV.

#### Step 2: Update prototype.py

Add night mode:
1. Check `is_day = altitude > 0`
2. If night: switch to CartoDB dark_matter tiles
3. Load streetlights (1 per city for prototype):
   - Boston CSV (Lat, Long columns) -> 1 point
   - Cambridge GeoJSON (extract coordinates from Point features) -> 1 point
4. Add as `folium.plugins.HeatMap` (radius=12, blur=20)
   - Gradient: 0.2=#1e3a5f, 0.4=#2563eb, 0.6=#60a5fa, 0.8=#fbbf24, 1.0=#ffffff
5. Load food establishments (1 for prototype)
6. Add as `folium.CircleMarker` (radius=3, color=#fbbf24)
7. Add LayerControl

CLI: add `--night` flag (forces 2026-07-15 22:00)

#### Step 3: Validate

| Check | Expected |
|-------|----------|
| Heatmap renders | Visible around sample points |
| Food markers | Yellow dot at business location |
| Streetlight count | 2 (1 Boston + 1 Cambridge) |
| Day/night switch | --night shows heatmap, default shows shadows |

### Risks

| Risk | Mitigation |
|------|------------|
| CKAN API limits per-request | Start with limit=32000. Streetlights (74K) may need 3 pages. |

---

## 3. Scale Up (5 stages)

See [Scale-Up Plan](scaleup-plan.md) for full details.

| Stage | Status |
|-------|--------|
| 1 each | done |
| 1% | pending |
| 10% | pending |
| 50% | pending |
| 100% | pending |

### Next steps (later)
- Safety incident data overlay (crime heatmap)
- Interactive app: FastAPI backend + MapLibre frontend

## URL Corrections (for future reference)

| Dataset | Verified URL |
|---------|-------------|
| Cambridge Buildings | `https://raw.githubusercontent.com/cambridgegis/cambridgegis_data/main/Basemap/Buildings/BASEMAP_Buildings.geojson` |
| Cambridge Streetlights | `https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_infra/main/Street_Lights/INFRA_StreetLights.geojson` |
| Cambridge Tree Canopy | `https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_environmental/main/Tree_Canopy_2018/ENVIRONMENTAL_TreeCanopy2018.topojson` |
| Cambridge Crime | Socrata ID: `xuad-73uj` |
