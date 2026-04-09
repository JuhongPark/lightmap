# LightMap Prototype Implementation Plan

## 1. Context

The prototype validates the shadow computation engine.
Generate `docs/prototype.html` and check: shadow direction, length, building connectivity.

---

## 2. Files to Create

```
lightmap/
  requirements.txt
  src/
    __init__.py
    shadow/
      __init__.py
      compute.py          # Shadow engine (~120 lines)
    prototype.py          # folium HTML generator (~80 lines)
  scripts/
    download_data.py      # Building data download
```

Generated output (gitignored):
```
  data/
    cambridge/
      buildings/buildings.geojson   # Cambridge 18K buildings
  docs/
    prototype.html
```

---

## 3. Implementation Steps

### Step 1: Scaffolding

- Create `src/shadow/`, `scripts/`, empty `__init__.py` files
- Write `requirements.txt`: pvlib, shapely, pandas, numpy, httpx, folium
- Add `data/`, `docs/prototype.html` to `.gitignore`
- Create venv and install

### Step 2: Shadow Engine (src/shadow/compute.py)

#### get_sun_position(dt, lat=42.36, lon=-71.06)

- pvlib.solarposition.get_solarposition with pd.DatetimeIndex
- If dt is naive, assume US/Eastern
- Return (apparent_elevation, azimuth)

#### compute_shadow(building_polygon, height_ft, sun_altitude, sun_azimuth)

1. If sun_altitude <= 0: return None
2. shadow_length_m = (height_ft * 0.3048) / tan(radians(sun_altitude)), cap 500m
3. shadow_direction = radians(sun_azimuth + 180)
4. dx_m = length * sin(direction), dy_m = length * cos(direction)
5. Convert to degrees (m_per_deg_lat=111320, m_per_deg_lon=111320*cos(rad(42.36)))
6. Translate polygon by (dx_deg, dy_deg) via shapely.affinity.translate
7. unary_union([building, translated]).convex_hull
8. Fix invalid with buffer(0)

#### compute_all_shadows(geojson_path, dt)

- Cache parsed buildings by path
- Filter height > 0, handle Polygon/MultiPolygon/GeometryCollection
- Return (shadow_features_list, altitude, azimuth)

### Step 3: Download Data (scripts/download_data.py)

Download Cambridge buildings GeoJSON (19MB):
```
https://raw.githubusercontent.com/cambridgegis/cambridgegis_data/main/Basemap/Buildings/BASEMAP_Buildings.geojson
```
Save to `data/cambridge/buildings/buildings.geojson`.

- Skip if file already exists
- Print download progress and file size
- Height field: `TOP_GL` (meters)

### Step 4: Prototype Script (src/prototype.py)

1. Load Cambridge buildings GeoJSON, pick 1 building with TOP_GL > 0
2. Load 1 Boston building as hardcoded test polygon (known height, e.g. Prudential Tower)
3. Convert height to feet for compute_shadow
4. Compute shadows for both buildings
5. Render with folium: buildings (#64748b) + shadows (#1e293b), CartoDB positron
6. Add info overlay (time, sun altitude/azimuth)
7. Save to docs/prototype.html

CLI: `--time "YYYY-MM-DD HH:MM"` (default: 2026-07-15 14:00 US/Eastern)

### Step 5: Validate

| Check | Expected |
|-------|----------|
| Shadow direction (July 2pm) | Northeast of buildings (sun azimuth ~230) |
| Shadow direction (Jan 2pm) | North-northeast (sun azimuth ~195) |
| Shadow length (10m building, July) | ~4.8m |
| Shadow length (10m building, Jan) | ~27m |
| Shadow shape | Connected to building, no gaps |
| Different times | Shadows change when --time changes |
| Two buildings | Cambridge (real data) + Boston (hardcoded) both render correctly |

---

## 4. Risks

| Risk | Mitigation |
|------|------------|
| Cambridge TOP_GL has nulls/zeros | Filter TOP_GL > 0 |
| convex_hull oversimplifies L-shaped buildings | Acceptable for prototype |

---

## Next Steps (after prototype is validated)

- Expand download_data.py: Boston buildings (106MB), streetlights, crime, food, canopy
- Merge Boston + Cambridge buildings -> all_buildings.geojson (46K)
- CKAN API pagination for large Boston datasets (258K crime records)
- Boston buildings GeoJSON direct download URL (needs discovery from dataset page)
- Night mode: streetlight heatmap + food establishment markers
- Interactive app: FastAPI backend + MapLibre frontend

## URL Corrections (for future reference)

| Dataset | Verified URL |
|---------|-------------|
| Cambridge Buildings | `https://raw.githubusercontent.com/cambridgegis/cambridgegis_data/main/Basemap/Buildings/BASEMAP_Buildings.geojson` |
| Cambridge Streetlights | `https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_infra/main/Street_Lights/INFRA_StreetLights.geojson` |
| Cambridge Tree Canopy | `https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_environmental/main/Tree_Canopy_2018/ENVIRONMENTAL_TreeCanopy2018.topojson` |
| Cambridge Crime | Socrata ID: `xuad-73uj` |

## Rough Roadmap (for reference only)

1. Shadow engine prototype (this document)
2. Nighttime brightness map
3. Safety incident data overlay
4. Optimization and extensions
