import argparse
import csv
import json
import os
import random
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
import branca.colormap as cm
import shapely as shapely_module
from folium.plugins import (
    DualMap, Fullscreen, HeatMap, MiniMap, MousePosition,
    TimestampedGeoJson,
)
from shapely.geometry import mapping
from shapely.wkb import loads as wkb_loads


def wkb_loads_batch(wkb_blobs):
    """Decode many WKB blobs in one shapely ufunc call."""
    return shapely_module.from_wkb([bytes(b) for b in wkb_blobs]).tolist()

from city_config import (
    DEFAULT_CITY_ID,
    height_from_properties_ft,
    load_city_profile,
    point_in_bbox,
    profile_data_path,
)
from shadow.compute import (
    compute_all_shadows,
    compute_shadow_coverage,
    compute_shadow_coverage_from_polys,
    compute_shadow_coverage_raster,
    get_sun_position,
    parse_building_features,
    render_shadows_png,
)
from render.strategies import (
    DEFAULT_RENDER_STRATEGY,
    RENDER_STRATEGIES,
    SHADOW_CMAP_COLORS,
    add_building_layer,
    add_shadow_layer,
)

try:
    from shadow.postgis_compute import (
        compute_all_shadows_postgis,
        get_connection as get_postgis_connection,
        load_buildings_postgis,
    )
    _POSTGIS_AVAILABLE = True
except ImportError:
    _POSTGIS_AVAILABLE = False


def _postgis_enabled():
    """PostGIS is used only if the driver imports AND the connection works."""
    if CITY.id != DEFAULT_CITY_ID:
        return False
    if not _POSTGIS_AVAILABLE:
        return False
    if os.environ.get("LIGHTMAP_NO_POSTGIS"):
        return False
    try:
        conn = get_postgis_connection()
        conn.close()
        return True
    except Exception:
        return False

CITY = load_city_profile(DEFAULT_CITY_ID)
LOCAL_TZ = ZoneInfo(CITY.timezone)
MAP_CENTER = list(CITY.center)
INITIAL_BBOX = CITY.bbox
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")

BUILDINGS_DB_PATH = profile_data_path(CITY, "buildings_db", "buildings.db")
OSM_POIS_PATH = profile_data_path(CITY, "osm_pois", "osm", "pois.geojson")
MEDICAL_PATH = profile_data_path(CITY, "medical", "osm", "medical.geojson")
COOLING_PATH = profile_data_path(CITY, "cooling", "cooling", "cooling.geojson")
TREES_PATH = profile_data_path(CITY, "trees", "trees", "trees.geojson")
CRIME_PATH = profile_data_path(CITY, "crime", "safety", "crime.geojson")
CRASH_PATH = profile_data_path(CITY, "crashes", "safety", "crashes.geojson")
FOOD_ESTABLISHMENTS_PATH = profile_data_path(
    CITY, "food_establishments", "safety", "food_establishments.csv"
)
TREES_CANOPY_PNG = (
    "trees_canopy.png"
    if CITY.id == DEFAULT_CITY_ID
    else f"trees_canopy_{CITY.id}.png"
)


def set_active_city(city):
    global CITY, LOCAL_TZ, MAP_CENTER, INITIAL_BBOX
    global BUILDINGS_DB_PATH, OSM_POIS_PATH, MEDICAL_PATH, COOLING_PATH
    global TREES_PATH, CRIME_PATH, CRASH_PATH, FOOD_ESTABLISHMENTS_PATH
    global TREES_CANOPY_PNG

    CITY = city
    LOCAL_TZ = ZoneInfo(city.timezone)
    MAP_CENTER = list(city.center)
    INITIAL_BBOX = city.bbox
    BUILDINGS_DB_PATH = profile_data_path(city, "buildings_db", "buildings.db")
    OSM_POIS_PATH = profile_data_path(city, "osm_pois", "osm", "pois.geojson")
    MEDICAL_PATH = profile_data_path(city, "medical", "osm", "medical.geojson")
    COOLING_PATH = profile_data_path(city, "cooling", "cooling", "cooling.geojson")
    TREES_PATH = profile_data_path(city, "trees", "trees", "trees.geojson")
    CRIME_PATH = profile_data_path(city, "crime", "safety", "crime.geojson")
    CRASH_PATH = profile_data_path(city, "crashes", "safety", "crashes.geojson")
    FOOD_ESTABLISHMENTS_PATH = profile_data_path(
        city, "food_establishments", "safety", "food_establishments.csv"
    )
    TREES_CANOPY_PNG = (
        "trees_canopy.png"
        if city.id == DEFAULT_CITY_ID
        else f"trees_canopy_{city.id}.png"
    )

# Heat-response thresholds (matches scripts/download_medical.py scope).
# Open-Meteo returns temperatures in Fahrenheit because the info panel
# displays them that way. 32 C = 89.6 F, 33 C = 91.4 F.
HEAT_TMAX_F = 89.6
HEAT_APPARENT_F = 91.4
HEAT_UV = 8

HEATMAP_GRADIENT = {
    # Shifted stops + capped at warm yellow (no white). Below 0.35
    # density renders fully transparent so empty streets don't pick
    # up a glow from the surrounding cluster. Top stop is light
    # yellow rather than white so even the brightest cluster reads
    # as a bright point, not a saturated washout.
    0.35: "#854d0e", 0.55: "#ca8a04", 0.75: "#facc15",
    1.0: "#fde047",
}
TIME_STEPS = [7, 9, 11, 13, 15, 17]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _sample_count(total, scale_pct):
    if scale_pct == 0:
        return 1
    if scale_pct >= 100:
        return total
    return max(1, int(total * scale_pct / 100))


def _pick_tallest_near_center(valid_features, center_lat=42.36, center_lon=-71.08):
    from shapely.geometry import shape
    best = None
    best_score = -1
    for feat in valid_features:
        h = feat["properties"].get("BLDG_HGT_2010", 0)
        try:
            geom = shape(feat["geometry"])
            c = geom.centroid
            dist = abs(c.y - center_lat) + abs(c.x - center_lon)
            if dist > 0.05:
                continue
            score = h * (geom.area ** 0.5)
            if score > best_score:
                best_score = score
                best = feat
        except Exception:
            continue
    return best


def _source_id(source):
    return source.get("id") or source.get("label") or "source"


def _source_label(source):
    return source.get("label") or _source_id(source).replace("_", " ").title()


def _feature_with_height(feat, source):
    props = feat.get("properties") or {}
    height_ft = height_from_properties_ft(props, source)
    if height_ft is None or height_ft <= 0:
        return None
    return {
        "type": "Feature",
        "properties": {
            **props,
            "BLDG_HGT_2010": height_ft,
            "height_ft": height_ft,
            "source": _source_id(source),
        },
        "geometry": feat.get("geometry"),
    }


def _load_building_source_features(source):
    path = source.get("path")
    label = _source_label(source)
    if not path or not os.path.exists(path):
        print(f"  {label}: not downloaded")
        return []
    source_format = source.get("format") or "geojson"
    if source_format != "geojson":
        print(f"  {label}: unsupported building format {source_format}")
        return []
    with open(path) as f:
        data = json.load(f)
    valid = []
    for feat in data.get("features", []):
        normalized = _feature_with_height(feat, source)
        if normalized is not None:
            valid.append(normalized)
    return valid


def _load_buildings_from_db(scale_pct):
    """Load buildings from the pre-processed SQLite database.

    Returns (geojson_dict, parsed_tuples). parsed_tuples is a list of
    (shapely_polygon, height_ft), usable directly by compute_all_shadows
    without any further parsing.
    """
    random.seed(42)
    conn = sqlite3.connect(BUILDINGS_DB_PATH)
    c = conn.cursor()

    features = []
    parsed = []
    total_rows_loaded = 0
    sources = CITY.building_sources
    if not sources:
        rows = c.execute("SELECT DISTINCT city FROM buildings").fetchall()
        sources = tuple({"id": row[0], "label": row[0]} for row in rows)

    for source in sources:
        source_id = _source_id(source)
        label = _source_label(source)
        rows = c.execute(
            "SELECT height_ft, geom FROM buildings WHERE city = ?", (source_id,)
        ).fetchall()
        total_rows_loaded += len(rows)

        if scale_pct == 0:
            # Still support "1 each" mode -- need JSON path for tallest-near-center
            # Fall back to JSON for scale=0 since it's a rarely used debug mode
            conn.close()
            return None

        n = _sample_count(len(rows), scale_pct)
        sampled = random.sample(rows, min(n, len(rows)))
        print(f"  {label}: {len(sampled)}/{len(rows)}")
        wkb_blobs = [row[1] for row in sampled]
        if wkb_blobs:
            polys = wkb_loads_batch(wkb_blobs)
        else:
            polys = []
        # Simplify + coordinate rounding to keep the rendered HTML small.
        simp_polys = shapely_module.simplify(polys, 5e-5, preserve_topology=True).tolist() if polys else []
        for i, (height_ft, _wkb) in enumerate(sampled):
            full = polys[i]
            parsed.append((full, float(height_ft)))
            display = simp_polys[i] if (simp_polys and not simp_polys[i].is_empty) else full
            coords = [
                (round(x, 6), round(y, 6))
                for x, y in display.exterior.coords
            ]
            features.append({
                "type": "Feature",
                "properties": {
                    "BLDG_HGT_2010": round(float(height_ft), 1),
                    "height_ft": round(float(height_ft), 1),
                    "source": source_id,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords],
                },
            })

    conn.close()
    if total_rows_loaded == 0:
        return None
    return {"type": "FeatureCollection", "features": features}, parsed


def load_buildings(scale_pct):
    # Prefer pre-processed SQLite DB (v5)
    if os.path.exists(BUILDINGS_DB_PATH) and scale_pct != 0:
        result = _load_buildings_from_db(scale_pct)
        if result is not None:
            return result[0]

    # Legacy GeoJSON path (also used by scale=0 tallest-near-center mode)
    random.seed(42)
    features = []

    for source in CITY.building_sources:
        valid = _load_building_source_features(source)
        if not valid:
            continue
        if scale_pct == 0:
            tallest = _pick_tallest_near_center(
                valid, center_lat=MAP_CENTER[0], center_lon=MAP_CENTER[1]
            )
            sampled = [tallest] if tallest is not None else []
        else:
            n = _sample_count(len(valid), scale_pct)
            sampled = random.sample(valid, min(n, len(valid)))
        features.extend(sampled)
        print(f"  {_source_label(source)}: {len(sampled)}/{len(valid)}")

    return {"type": "FeatureCollection", "features": features}


def load_buildings_with_parsed(scale_pct):
    """Load buildings returning both GeoJSON (for folium) and parsed tuples
    (for shadow computation). Uses SQLite DB if available to skip re-parsing."""
    if os.path.exists(BUILDINGS_DB_PATH) and scale_pct != 0:
        result = _load_buildings_from_db(scale_pct)
        if result is not None:
            return result
    # Fallback: load via GeoJSON then parse
    building_data = load_buildings(scale_pct)
    parsed = parse_building_features(building_data["features"])
    return building_data, parsed


def load_streetlights(scale_pct):
    random.seed(42)
    coords = []

    for source in CITY.streetlight_sources:
        path = source.get("path")
        label = _source_label(source)
        if not path or not os.path.exists(path):
            print(f"  {label}: not downloaded")
            continue
        source_format = source.get("format") or "geojson"
        all_points = []
        if source_format == "csv":
            lat_field = source.get("lat_field") or "lat"
            lon_field = source.get("lon_field") or "lon"
            with open(path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        lat = float(row[lat_field])
                        lon = float(row[lon_field])
                    except (ValueError, KeyError):
                        continue
                    if point_in_bbox(lat, lon, INITIAL_BBOX):
                        all_points.append([lat, lon])
        elif source_format == "geojson":
            with open(path) as f:
                data = json.load(f)
            for feat in data.get("features", []):
                geom = feat.get("geometry", {})
                if geom.get("type") != "Point":
                    continue
                coords_raw = geom.get("coordinates") or []
                if len(coords_raw) < 2:
                    continue
                lon, lat = coords_raw[:2]
                if point_in_bbox(lat, lon, INITIAL_BBOX):
                    all_points.append([lat, lon])
        else:
            print(f"  {label}: unsupported streetlight format {source_format}")
            continue
        n = _sample_count(len(all_points), scale_pct)
        sampled = random.sample(all_points, min(n, len(all_points)))
        coords.extend(sampled)
        print(f"  {label}: {len(sampled)}/{len(all_points)}")

    print(f"  Total streetlights: {len(coords)}")
    return coords


def load_safety_crime():
    """Load night-hour crime incidents as [[lat, lon], ...] for a
    Leaflet heatmap. Each point is a recorded incident during hours
    18-05 within INITIAL_BBOX over the past two years."""
    if not os.path.exists(CRIME_PATH):
        print(f"  Crime file not found: {CRIME_PATH}")
        print(f"  Run scripts/download_safety.py to populate it")
        return []
    with open(CRIME_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        coords = feat.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        out.append([coords[1], coords[0]])  # [lat, lon] for HeatMap
    print(f"  Crime points: {len(out)}")
    return out


def load_safety_crashes():
    """Load crash records as list of dicts with lat/lon/mode."""
    if not os.path.exists(CRASH_PATH):
        print(f"  Crashes file not found: {CRASH_PATH}")
        return []
    with open(CRASH_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        coords = feat.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties") or {}
        out.append({
            "lat": coords[1], "lon": coords[0],
            "mode": props.get("mode") or "",
        })
    print(f"  Crash records: {len(out)}")
    return out


# Offense-description keywords that define the "violent crime" (강력범죄)
# bucket surfaced in the time-slider: murder/manslaughter, aggravated
# assault, robbery, sexual offenses, and firearm/weapon involvement.
# Simple assault is intentionally excluded — it dominates the feed with
# verbal/minor incidents that would swamp the marker layer.
_VIOLENT_KEYWORDS = (
    "MURDER", "MANSLAUGHTER", "HOMICIDE",
    "ASSAULT - AGGRAVATED",
    "ROBBERY",
    "RAPE", "SEXUAL",
    "FIREARM", "WEAPON", "SHOOTING",
)


def load_violent_crime():
    """Load violent-crime incidents as list of dicts with lat/lon/type.

    Filters `data/safety/crime.geojson` down to the "강력범죄" set.
    These are rendered as distinctive diamond markers (not a heatmap)
    so individual incidents stand out on top of the streetlight glow.
    """
    if not os.path.exists(CRIME_PATH):
        print(f"  Crime file not found: {CRIME_PATH}")
        return []
    with open(CRIME_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        coords = feat.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties") or {}
        desc = (props.get("descript") or "").upper()
        if not any(k in desc for k in _VIOLENT_KEYWORDS):
            continue
        out.append({
            "lat": coords[1], "lon": coords[0],
            "type": props.get("descript") or "",
        })
    print(f"  Violent crime incidents: {len(out)}")
    return out


def load_trees():
    """Load tree canopy polygons for the static time-slider shade overlay.

    Each feature keeps its outer ring + a `height_m` property. The
    current time-slider rasterizes these crowns into one PNG overlay
    instead of projecting them on every slider tick.
    """
    if not os.path.exists(TREES_PATH):
        print(f"  Tree canopy file not found: {TREES_PATH}")
        print(f"  Run scripts/download_trees.py to populate it")
        return []
    with open(TREES_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        rings = geom.get("coordinates") or []
        if not rings or not rings[0]:
            continue
        props = feat.get("properties") or {}
        try:
            h_m = float(props.get("height_m") or 10.0)
        except (TypeError, ValueError):
            h_m = 10.0
        out.append({"ring": rings[0], "h_m": h_m})
    print(f"  Tree canopy features: {len(out)}")
    return out


def load_osm_pois():
    """Load OSM amenity POIs that carry an opening_hours tag.

    These are the venues the time-slider can actually toggle on/off per
    (date, time). The download script (scripts/download_osm_pois.py)
    fetches them from the Overpass API and writes the GeoJSON. If the
    file is missing, the time-slider will silently degrade (no POI
    markers), so the user gets a clear hint to run the download step.
    """
    if not os.path.exists(OSM_POIS_PATH):
        print(f"  OSM POIs file not found: {OSM_POIS_PATH}")
        print(f"  Run scripts/download_osm_pois.py to populate it")
        return []
    with open(OSM_POIS_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        coords = feat.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties", {})
        hours = props.get("opening_hours")
        if not hours:
            continue
        out.append({
            "lon": coords[0], "lat": coords[1],
            "name": props.get("name", ""),
            "amenity": props.get("amenity", ""),
            "hours": hours,
        })
    print(f"  OSM POIs with opening_hours: {len(out)}")
    return out


def load_medical():
    """Load hospitals from OSM. Includes ALL hospitals in the dataset
    (not just emergency=yes) so the map surfaces the full medical
    footprint. The `is_er` flag distinguishes 24-hour emergency
    departments at render time so they get a more prominent style.
    """
    if not os.path.exists(MEDICAL_PATH):
        print(f"  Medical file not found: {MEDICAL_PATH}")
        print(f"  Run scripts/download_medical.py to populate it")
        return []
    with open(MEDICAL_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        coords = feat.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties", {}) or {}
        out.append({
            "lon": coords[0], "lat": coords[1],
            "name": props.get("name") or "Hospital",
            "addr": props.get("addr_street") or "",
            "phone": props.get("phone") or "",
            "is_er": props.get("emergency") == "yes",
        })
    er_count = sum(1 for m in out if m["is_er"])
    print(f"  Hospitals: {len(out)} ({er_count} with emergency=yes)")
    return out


def load_cooling_centers():
    """Load cooling-center candidates from OSM proxy (libraries,
    community centres, town halls). Visible only when heat advisory
    is active. See scripts/download_cooling.py for the proxy rationale.
    """
    if not os.path.exists(COOLING_PATH):
        print(f"  Cooling file not found: {COOLING_PATH}")
        print(f"  Run scripts/download_cooling.py to populate it")
        return []
    with open(COOLING_PATH) as f:
        gj = json.load(f)
    out = []
    for feat in gj.get("features", []):
        coords = feat.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties", {}) or {}
        out.append({
            "lon": coords[0], "lat": coords[1],
            "name": props.get("name") or (props.get("amenity") or "cooling"),
            "amenity": props.get("amenity") or "",
        })
    print(f"  Cooling proxy centers: {len(out)}")
    return out


def load_food_establishments(scale_pct):
    random.seed(42)
    path = FOOD_ESTABLISHMENTS_PATH
    if not os.path.exists(path):
        print("  Food establishments file not found.")
        return []

    all_places = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
                name = row.get("businessname", "")
                if point_in_bbox(lat, lon, INITIAL_BBOX):
                    all_places.append({"lat": lat, "lon": lon, "name": name})
            except (ValueError, KeyError):
                continue

    n = _sample_count(len(all_places), scale_pct)
    sampled = random.sample(all_places, min(n, len(all_places)))
    print(f"  Food establishments: {len(sampled)}/{len(all_places)}")
    return sampled


def _load_buildings_and_shadows(scale_pct, target_time):
    # v6/v7: PostGIS path (only at 100% scale for now)
    if scale_pct == 100 and _postgis_enabled():
        conn = get_postgis_connection()
        try:
            print("Computing shadows (PostGIS)...")
            shadows, shadow_polys, _, _ = compute_all_shadows_postgis(
                conn, target_time, return_polygons=True,
            )
            print(f"  Shadows computed: {len(shadows)}")
        finally:
            conn.close()

        # Load building footprints from the SQLite db alongside the
        # PostGIS-computed shadows. The async building layer renders
        # these as a separate canvas layer (see strategies.py), so the
        # user can actually click a building to see its height — the
        # shadow alone is a ConvexHull and obscures the real footprint.
        print("Loading buildings...")
        building_data, _ = load_buildings_with_parsed(scale_pct)
        print(f"  Total buildings: {len(building_data['features'])}")

        print("Computing shadow coverage...")
        coverage = compute_shadow_coverage_raster(shadow_polys, resolution_m=10.0)
        print(f"  Shadow coverage: {coverage:.1f}%")
        return building_data, shadows, coverage

    print("Loading buildings...")
    building_data, parsed = load_buildings_with_parsed(scale_pct)
    building_count = len(building_data["features"])
    print(f"  Total buildings: {building_count}")

    if building_count == 0:
        print("ERROR: No buildings loaded.")
        return None, [], 0.0

    print("Computing shadows...")
    shadows, _, _ = compute_all_shadows(
        parsed, target_time, lat=MAP_CENTER[0], lon=MAP_CENTER[1]
    )
    print(f"  Shadows computed: {len(shadows)}")

    print("Computing shadow coverage...")
    coverage = compute_shadow_coverage(shadows)
    print(f"  Shadow coverage: {coverage:.1f}%")

    return building_data, shadows, coverage


# ---------------------------------------------------------------------------
# Shared layer helpers (eliminate duplication)
# ---------------------------------------------------------------------------
# Render strategies (RENDER_STRATEGIES, add_shadow_layer, etc.) have moved
# to src/render/strategies.py. This module only imports the public API.


def _create_base_map(
    tiles="CartoDB positron", *, prefer_canvas=True,
    lock_zoom=False,
    min_lat=INITIAL_BBOX[0], max_lat=INITIAL_BBOX[2],
    min_lon=INITIAL_BBOX[1], max_lon=INITIAL_BBOX[3],
):
    # prefer_canvas=True switches Leaflet to Canvas rendering instead of
    # SVG. SVG is fine for hundreds of polygons but chokes on the 123K
    # shadow polygons we render at 100% scale, where every feature becomes
    # a DOM node. Canvas draws them into a single <canvas> element, so
    # the browser handles the map smoothly even at the full city scale.
    #
    # Default pan bounds are the active city bbox so day and night
    # renderers share the same frame. A
    # caller can still pass wider bounds if a specific view needs them.
    #
    # lock_zoom=True pins the *minimum* zoom at zoom_start so the user
    # cannot scroll out of the prepared frame while scrubbing the slider,
    # but still allows zooming in (up to 20) for a closer look. Zoom
    # interactions stay enabled in both modes.
    zoom_start = 16
    min_z = zoom_start if lock_zoom else 15
    max_z = 20
    m = folium.Map(
        location=MAP_CENTER, zoom_start=zoom_start, tiles=tiles,
        width="100%", height="100%",
        prefer_canvas=prefer_canvas,
        # min_zoom is 1 level below zoom_start so users can pull back
        # slightly for a wider overview but not so far that the shadow
        # redraw cost explodes. max_zoom allows two closer inspection
        # steps beyond the previous 18 cap.
        min_zoom=min_z, max_zoom=max_z,
        min_lat=min_lat, max_lat=max_lat,
        min_lon=min_lon, max_lon=max_lon,
        max_bounds=True,
    )
    m.get_root().html.add_child(folium.Element(
        "<style>html,body{margin:0;padding:0;height:100%;width:100%}</style>"
    ))
    return m


def _add_building_layer(m, building_data):
    if not building_data.get("features"):
        return
    folium.GeoJson(
        building_data,
        name="Buildings",
        style_function=lambda x: {
            "fillColor": "#64748b",
            "color": "#475569",
            "weight": 0.5,
            "fillOpacity": 0.6,
        },
    ).add_to(m)


def _make_shadow_cmap():
    return cm.LinearColormap(
        colors=SHADOW_CMAP_COLORS, vmin=0, vmax=200,
        caption="Shadow darkness by building height (ft)",
    )


def _add_streetlight_layer(m, coords):
    if coords:
        HeatMap(
            coords, name="Streetlights", radius=12, blur=20,
            gradient=HEATMAP_GRADIENT,
        ).add_to(m)


def _add_food_layer(m, places):
    food_group = folium.FeatureGroup(name="Food Establishments")
    for p in places:
        folium.CircleMarker(
            location=[p["lat"], p["lon"]],
            radius=5,
            color="#fde68a",
            weight=2,
            opacity=0.55,
            fill=True,
            fill_color="#fbbf24",
            fill_opacity=0.9,
        ).add_to(food_group)
    food_group.add_to(m)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _sun_description(altitude):
    if altitude <= 0:
        return "Below horizon"
    if altitude < 15:
        return "Very low sun, long shadows"
    if altitude < 30:
        return "Low sun, moderate shadows"
    if altitude < 50:
        return "Mid-height sun, short shadows"
    return "High sun, minimal shadows"


ONBOARDING_HTML = """
<div id="onboarding" style="
    position:fixed; top:0; left:0; width:100%; height:100%;
    background:rgba(0,0,0,0.6); z-index:9999;
    display:flex; align-items:center; justify-content:center;
    font-family:sans-serif;">
  <div style="
      background:white; border-radius:12px; padding:32px 36px;
      max-width:460px; width:90%; box-shadow:0 8px 32px rgba(0,0,0,0.3);">
    <h2 style="margin:0 0 8px 0; font-size:22px;">LightMap</h2>
    <p style="margin:0 0 16px 0; color:#64748b; font-size:14px;">
      Shade by day. Light by night.</p>
    <p style="margin:0 0 12px 0; font-size:14px; line-height:1.6;">
      This map shows <b>where shade falls</b> during the day and
      <b>where streetlights shine</b> at night across __CITY_DISPLAY__.</p>
    <ul style="font-size:13px; line-height:1.8; padding-left:20px; margin:0 0 16px 0;">
      <li>Toggle <b>layers</b> on/off with the control (top-right)</li>
    </ul>
    <button id="onboarding-btn" style="
        background:#1e293b; color:white; border:none; border-radius:6px;
        padding:10px 28px; font-size:14px; cursor:pointer; width:100%;">
      Explore the map
    </button>
  </div>
</div>
<script>
(function(){
  var el = document.getElementById('onboarding');
  if (localStorage.getItem('lightmap_onboarded')) {
    el.style.display = 'none';
  }
  document.getElementById('onboarding-btn').addEventListener('click', function(){
    localStorage.setItem('lightmap_onboarded', '1');
    el.style.display = 'none';
  });
})();
</script>
"""


def _add_ui_plugins(m, theme="light"):
    Fullscreen(position="topleft").add_to(m)
    MousePosition(position="bottomleft", separator=" | ", prefix="Coords: ").add_to(m)
    tile_layer = "CartoDB positron" if theme == "light" else "CartoDB dark_matter"
    MiniMap(toggle_display=True, minimized=True, tile_layer=tile_layer).add_to(m)
    onboarding = ONBOARDING_HTML.replace("__CITY_DISPLAY__", CITY.display_name)
    m.get_root().html.add_child(folium.Element(onboarding))


def _add_info_panel(m, lines, theme="light", position="left:60px"):
    if theme == "light":
        bg = "rgba(255,255,255,0.95)"
        color = "#1e293b"
        shadow = "rgba(0,0,0,0.15)"
    else:
        bg = "rgba(15,23,42,0.95)"
        color = "#e2e8f0"
        shadow = "rgba(0,0,0,0.3)"
    content = "<br>".join(lines)
    html = (
        f'<div style="position:fixed; top:10px; {position}; z-index:1000;'
        f" background:{bg}; color:{color}; padding:14px 18px;"
        f" border-radius:8px; font-family:sans-serif; font-size:14px;"
        f' box-shadow:0 2px 8px {shadow};">'
        f"{content}</div>"
    )
    m.get_root().html.add_child(folium.Element(html))


# ---------------------------------------------------------------------------
# Map builders
# ---------------------------------------------------------------------------

def build_day_map(target_time, altitude, azimuth, scale_pct,
                  render_strategy=DEFAULT_RENDER_STRATEGY):
    building_data, shadows, coverage = _load_buildings_and_shadows(scale_pct, target_time)
    if building_data is None:
        return None

    cfg = RENDER_STRATEGIES.get(render_strategy, RENDER_STRATEGIES[DEFAULT_RENDER_STRATEGY])

    building_count = len(building_data["features"])
    m = _create_base_map("CartoDB positron", prefer_canvas=cfg["prefer_canvas"])

    # Route the building layer through the render strategy module so
    # async strategies can ship a sidecar instead of inlining 123 K
    # polygons. Inline strategies (r0/r1) fall back to folium's
    # embedded GeoJson via _add_building_layer.
    if cfg["shadow_mode"] == "inline":
        _add_building_layer(m, building_data)
    else:
        add_building_layer(m, building_data, out_dir=OUT_DIR, cfg=cfg)
    cmap = _make_shadow_cmap()
    add_shadow_layer(m, shadows, cmap, strategy=render_strategy, out_dir=OUT_DIR)
    _add_ui_plugins(m, theme="light")
    cmap.add_to(m)

    sun_desc = _sun_description(altitude)
    time_str = target_time.strftime("%b %d, %Y %I:%M %p")
    _add_info_panel(m, [
        "<b>LightMap</b> &mdash; Shadow Map",
        f'<span style="color:#64748b;">{time_str}</span>',
        sun_desc,
        f"{building_count:,} buildings &middot; {len(shadows):,} shadows",
        f"<b>{coverage:.1f}%</b> of area in shadow",
        '<span style="color:#94a3b8; font-size:11px;">'
        f"{CITY.source_notes.get('building_heights', 'Building heights from city data')}</span>",
    ])
    return m


def build_night_map(target_time, altitude, azimuth, scale_pct):
    m = _create_base_map("CartoDB dark_matter")

    print("Loading streetlights...")
    coords = load_streetlights(scale_pct)
    _add_streetlight_layer(m, coords)

    print("Loading food establishments...")
    places = load_food_establishments(scale_pct)
    _add_food_layer(m, places)

    _add_ui_plugins(m, theme="dark")

    brightness_legend = cm.LinearColormap(
        colors=["#78350f", "#d97706", "#fbbf24", "#fde68a", "#ffffff"],
        vmin=0, vmax=1,
        caption="Light intensity (streetlight density)",
    )
    brightness_legend.add_to(m)

    time_str = target_time.strftime("%b %d, %Y %I:%M %p")
    _add_info_panel(m, [
        "<b>LightMap</b> &mdash; Brightness Map",
        f'<span style="color:#94a3b8;">{time_str}</span>',
        f"{len(coords):,} streetlights &middot; {len(places):,} food places",
        '<span style="color:#64748b; font-size:11px;">'
        f"{CITY.source_notes.get('night_sources', 'Source: city data')}</span>",
    ], theme="dark")
    return m


def build_time_map(target_time, scale_pct):
    print("Loading buildings...")
    building_data, parsed = load_buildings_with_parsed(scale_pct)
    building_count = len(building_data["features"])
    print(f"  Total buildings: {building_count}")
    if building_count == 0:
        print("ERROR: No buildings loaded.")
        return None

    all_features = []
    height_cmap = cm.LinearColormap(colors=SHADOW_CMAP_COLORS, vmin=0, vmax=200)
    for hour in TIME_STEPS:
        step_time = datetime(
            target_time.year, target_time.month, target_time.day,
            hour, 0, tzinfo=LOCAL_TZ,
        )
        timestamp = step_time.strftime("%Y-%m-%dT%H:%M:%S")
        alt, az = get_sun_position(step_time, lat=MAP_CENTER[0], lon=MAP_CENTER[1])
        print(f"  {step_time.strftime('%H:%M')}: alt={alt:.1f}, az={az:.1f}")

        if alt <= 0:
            print(f"    Skipped (sun below horizon)")
            continue

        shadows, _, _ = compute_all_shadows(
            parsed, step_time, lat=MAP_CENTER[0], lon=MAP_CENTER[1]
        )
        print(f"    Shadows: {len(shadows)}")

        for feat in shadows:
            h = feat["properties"].get("height_ft", 0)
            color = height_cmap(h)
            all_features.append({
                "type": "Feature",
                "geometry": feat["geometry"],
                "properties": {
                    "times": [timestamp],
                    "style": {
                        "fillColor": color, "color": color,
                        "weight": 0.3, "fillOpacity": 0.45,
                    },
                },
            })

    m = _create_base_map("CartoDB positron")
    _add_building_layer(m, building_data)

    TimestampedGeoJson(
        {"type": "FeatureCollection", "features": all_features},
        period="PT2H", duration="PT2H", transition_time=500,
        auto_play=True, loop=True, loop_button=True,
        speed_slider=True, date_options="HH:mm",
    ).add_to(m)

    _add_ui_plugins(m, theme="light")

    legend = _make_shadow_cmap()
    legend.add_to(m)

    date_str = target_time.strftime("%b %d, %Y")
    _add_info_panel(m, [
        "<b>LightMap</b> &mdash; Shadow Animation",
        f'<span style="color:#64748b;">{date_str}</span>',
        f"{building_count:,} buildings &middot; 7 AM to 5 PM",
        '<span style="color:#64748b; font-size:11px;">'
        "Press play to watch shadows move through the day</span>",
    ])

    folium.LayerControl().add_to(m)
    return m


def build_dual_map(target_time, scale_pct):
    day_time = datetime(
        target_time.year, target_time.month, target_time.day,
        14, 0, tzinfo=LOCAL_TZ,
    )
    night_time = datetime(
        target_time.year, target_time.month, target_time.day,
        22, 0, tzinfo=LOCAL_TZ,
    )
    day_alt, _ = get_sun_position(day_time, lat=MAP_CENTER[0], lon=MAP_CENTER[1])

    print(f"Dual map: day={day_time.strftime('%H:%M')}, night={night_time.strftime('%H:%M')}")

    building_data, shadows, coverage = _load_buildings_and_shadows(scale_pct, day_time)
    if building_data is None:
        return None
    building_count = len(building_data["features"])

    print("Loading streetlights...")
    coords = load_streetlights(scale_pct)
    print("Loading food establishments...")
    places = load_food_establishments(scale_pct)

    dm = DualMap(
        location=MAP_CENTER, zoom_start=16, tiles=None,
        min_zoom=15, max_zoom=18,
        min_lat=INITIAL_BBOX[0], max_lat=INITIAL_BBOX[2],
        min_lon=INITIAL_BBOX[1], max_lon=INITIAL_BBOX[3],
        max_bounds=True,
    )
    folium.TileLayer("CartoDB positron").add_to(dm.m1)
    folium.TileLayer("CartoDB dark_matter").add_to(dm.m2)

    # folium silently drops min_zoom / max_zoom from the Map options
    # when `tiles=None`, so the DualMap sub-maps end up without zoom
    # limits even though we passed them. Patch each sub-map via JS so
    # the zoom behavior matches every other renderer.
    dm.get_root().script.add_child(folium.Element(
        f"""(function() {{
  function patch() {{
    if (typeof {dm.m1.get_name()} === "undefined" ||
        typeof {dm.m2.get_name()} === "undefined") {{
      setTimeout(patch, 50); return;
    }}
    [{dm.m1.get_name()}, {dm.m2.get_name()}].forEach(function(map) {{
      map.setMinZoom(15);
      map.setMaxZoom(18);
    }});
  }}
  patch();
}})();"""
    ))

    # Day side
    _add_building_layer(dm.m1, building_data)
    cmap = cm.LinearColormap(colors=SHADOW_CMAP_COLORS, vmin=0, vmax=200)
    add_shadow_layer(dm.m1, shadows, cmap, out_dir=OUT_DIR)

    # Night side
    _add_streetlight_layer(dm.m2, coords)
    _add_food_layer(dm.m2, places)

    # Info panels
    sun_desc = _sun_description(day_alt)
    _add_info_panel(dm.m1, [
        f"<b>Shadow Map</b> &middot; {day_time.strftime('%I:%M %p')}",
        sun_desc,
        f"{building_count:,} buildings &middot; <b>{coverage:.1f}%</b> in shadow",
        '<span style="color:#94a3b8; font-size:10px;">'
        f"{CITY.source_notes.get('building_heights', 'Heights from city data')}</span>",
    ], position="left:10px")

    _add_info_panel(dm.m1, [
        f"<b>Brightness Map</b> &middot; {night_time.strftime('%I:%M %p')}",
        f"{len(coords):,} streetlights &middot; {len(places):,} food places",
    ], theme="dark", position="right:10px")

    return dm


def build_time_slider_map(target_time, scale_pct):
    """Client-side time slider with free date picker.

    Ships building footprints + SunCalc to the browser. Shadows are
    projected live in JS for any (date, time) the user scrubs to.
    Sunrise/sunset markers on the slider track track the chosen date's
    daylight window. When the sun drops below the horizon, the viewer
    auto-swaps to the nighttime layer (Dark Matter tiles + streetlight
    heatmap + food establishments).
    """
    print("Loading buildings...")
    building_data, _ = load_buildings_with_parsed(scale_pct)
    building_count = len(building_data["features"])
    print(f"  Total buildings: {building_count}")
    if building_count == 0:
        print("ERROR: No buildings loaded.")
        return None

    # Hard data cutoff: restrict every dataset (buildings, streetlights,
    # food) to INITIAL_BBOX = (min_lat, min_lon, max_lat, max_lon). No
    # background tier — anything outside the box is simply excluded.
    # Trims file size and speeds up first paint without sacrificing
    # the configured city frame that the app is actually about.
    bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon = INITIAL_BBOX

    def _in_bbox_latlon(lat, lon):
        return (bbox_min_lat <= lat <= bbox_max_lat
                and bbox_min_lon <= lon <= bbox_max_lon)

    # Extract footprints (outer ring) + height in meters + bbox for the
    # JS shadow engine. Holes are dropped. bbox lets the browser reject
    # out-of-view buildings in O(1) before running convex hull.
    js_buildings = []
    bbox_rejected = 0
    for feat in building_data["features"]:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        rings = geom.get("coordinates") or []
        if not rings or not rings[0]:
            continue
        h_ft = feat.get("properties", {}).get("BLDG_HGT_2010", 0) or 0
        try:
            h_ft = float(h_ft)
        except (TypeError, ValueError):
            h_ft = 0.0
        if h_ft <= 0:
            continue
        # Coord precision 6 -> 5 decimals (~1 m at 42 N). Height 0.01 m
        # -> 1 m. Neither change is visible at zoom 15-18 and the HTML
        # shrinks by ~25%. Shadows redraw faster because fewer digits
        # serialize and fewer chars to parse on load.
        ring = [[round(pt[0], 5), round(pt[1], 5)] for pt in rings[0]]
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        # Intersection with INITIAL_BBOX: bbox = [minLon, minLat, maxLon, maxLat].
        if (bbox[2] < bbox_min_lon or bbox[0] > bbox_max_lon or
                bbox[3] < bbox_min_lat or bbox[1] > bbox_max_lat):
            bbox_rejected += 1
            continue
        js_buildings.append([round(h_ft * 0.3048, 0), ring, bbox])
    print(f"  Inside INITIAL_BBOX: {len(js_buildings)} "
          f"(rejected {bbox_rejected})")

    # Tree canopies render as a static green canopy overlay, NOT as
    # projected shadows. The canopy footprint already represents the
    # shaded area under the tree. Removing per-tick projection cuts
    # ~32% off the per-tick shadow compute (was ~123K buildings + ~59K
    # trees, now ~123K buildings only) and lets the tree layer paint
    # once on day-start instead of every slot. Trade-off: we lose the
    # cast-shadow extension that follows the sun. Acceptable per the
    # speed budget.
    print("Loading tree canopy...")
    trees = load_trees()
    # Additional simplify on top of the 2 m tolerance already applied in
    # download_trees.py. 5 m tolerance at zoom 15-18 is imperceptible but
    # cuts tree vertex count roughly in half, which drops the static
    # canvas paint cost proportionally. Tree COUNT is preserved — we only
    # thin the outlines, never drop an individual tree.
    from shapely.geometry import Polygon as _Poly
    # preserve_topology=True guarantees the polygon never collapses to
    # empty, so we can use a relatively aggressive 6 m tolerance without
    # losing a single canopy. Falls back to raw ring on any shapely
    # error so the no-tree-dropped invariant holds even on edge cases.
    _TREE_SIMPLIFY_TOL = 5.4e-5  # ~6 m in degrees at 42 N
    trees_static = []
    _tree_verts_before = 0
    _tree_verts_after = 0
    _tree_simplified = 0
    _tree_kept_raw = 0
    for t in trees:
        raw = t["ring"]
        if len(raw) < 4:
            continue
        _tree_verts_before += len(raw)
        ring = None
        try:
            p = _Poly(raw).simplify(_TREE_SIMPLIFY_TOL, preserve_topology=True)
            if p.geom_type == "MultiPolygon":
                p = max(p.geoms, key=lambda g: g.area)
            if not p.is_empty and p.exterior:
                ring = [[round(x, 5), round(y, 5)] for x, y in p.exterior.coords]
                _tree_simplified += 1
        except Exception:
            pass
        if ring is None:
            ring = [[round(pt[0], 5), round(pt[1], 5)] for pt in raw]
            _tree_kept_raw += 1
        _tree_verts_after += len(ring)
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        fminx, fmaxx = min(xs), max(xs)
        fminy, fmaxy = min(ys), max(ys)
        if (fmaxx < bbox_min_lon or fminx > bbox_max_lon or
                fmaxy < bbox_min_lat or fminy > bbox_max_lat):
            continue
        trees_static.append(ring)
    print(f"  Tree canopies kept as static overlay: {len(trees_static)}")
    print(f"    simplified: {_tree_simplified}, preserved raw (small): {_tree_kept_raw}")
    if _tree_verts_before:
        print(f"  Tree vertices: {_tree_verts_before} -> {_tree_verts_after} "
              f"({100 * _tree_verts_after / _tree_verts_before:.0f}%)")

    # Bake the tree-canopy rings into a single PNG sidecar. The browser
    # then renders ONE image overlay instead of ~59K canvas polygons —
    # the static-canvas paint cost on pan/zoom drops effectively to zero.
    # Slight pixelation at zoom 18 is acceptable per the user: trees are
    # an auxiliary layer and have soft edges anyway.
    from PIL import Image as _PILImage, ImageDraw as _PILDraw
    _PNG_W, _PNG_H = 4000, 2972  # ~1.85 m/pixel across INITIAL_BBOX
    _bbox_w = bbox_max_lon - bbox_min_lon
    _bbox_h = bbox_max_lat - bbox_min_lat
    _trees_png_relpath = TREES_CANOPY_PNG
    _trees_png_path = os.path.join(OUT_DIR, _trees_png_relpath)
    _img = _PILImage.new("RGBA", (_PNG_W, _PNG_H), (0, 0, 0, 0))
    _draw = _PILDraw.Draw(_img, "RGBA")
    _fill = (21, 128, 61, int(0.32 * 255))  # green-700 at 32% opacity
    for ring in trees_static:
        if len(ring) < 3:
            continue
        _px = []
        for pt in ring:
            x = (pt[0] - bbox_min_lon) / _bbox_w * _PNG_W
            y = (bbox_max_lat - pt[1]) / _bbox_h * _PNG_H
            _px.append((x, y))
        _draw.polygon(_px, fill=_fill)
    os.makedirs(os.path.dirname(_trees_png_path), exist_ok=True)
    _img.save(_trees_png_path, "PNG", optimize=True)
    _png_kb = os.path.getsize(_trees_png_path) / 1024
    print(f"  Tree canopy raster: docs/{_trees_png_relpath} ({_png_kb:.0f} KB)")
    trees_png_bbox = [bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon]

    # Rebuild the building_data for folium's static building layer too,
    # so the base layer matches the shadow-engine footprint set.
    def _feat_in_bbox(f):
        g = f.get("geometry") or {}
        if g.get("type") != "Polygon":
            return False
        rings = g.get("coordinates") or []
        if not rings or not rings[0]:
            return False
        xs = [pt[0] for pt in rings[0]]
        ys = [pt[1] for pt in rings[0]]
        fminx, fmaxx = min(xs), max(xs)
        fminy, fmaxy = min(ys), max(ys)
        return not (fmaxx < bbox_min_lon or fminx > bbox_max_lon
                    or fmaxy < bbox_min_lat or fminy > bbox_max_lat)

    building_data = {
        "type": "FeatureCollection",
        "features": [f for f in building_data["features"] if _feat_in_bbox(f)],
    }
    building_count = len(building_data["features"])
    print(f"  Building layer features: {building_count}")

    print("Loading streetlights...")
    coords = [c for c in load_streetlights(scale_pct)
              if _in_bbox_latlon(c[0], c[1])]
    print(f"  Inside INITIAL_BBOX: {len(coords)} streetlights")
    print("Loading OSM POIs (opening_hours)...")
    osm_pois = [p for p in load_osm_pois()
                if _in_bbox_latlon(p["lat"], p["lon"])]
    print(f"  Inside INITIAL_BBOX: {len(osm_pois)} POIs")

    print("Loading cooling centers (proxy)...")
    cooling = [c for c in load_cooling_centers()
               if _in_bbox_latlon(c["lat"], c["lon"])]
    print(f"  Inside INITIAL_BBOX: {len(cooling)} cooling")

    # ----- Building-coverage mask -----
    # Areas inside INITIAL_BBOX that have no building data are masked
    # out visually AND filtered from every point-based layer. The user
    # should see no venue, cooling, or streetlight info on
    # ground we cannot verify. Trees stay because they are geographic
    # features, not user-activity data.
    print("Building coverage mask:")
    GRID_LAT_STEPS = 25  # ~220 m per cell at 42.36 N
    GRID_LON_STEPS = 36  # ~205 m per cell
    cell_lat_size = (bbox_max_lat - bbox_min_lat) / GRID_LAT_STEPS
    cell_lon_size = (bbox_max_lon - bbox_min_lon) / GRID_LON_STEPS

    def _cell_of(lat, lon):
        cy = int((lat - bbox_min_lat) / cell_lat_size)
        cx = int((lon - bbox_min_lon) / cell_lon_size)
        if cy < 0 or cy >= GRID_LAT_STEPS or cx < 0 or cx >= GRID_LON_STEPS:
            return None
        return (cx, cy)

    covered = set()
    for _b in js_buildings:
        for _pt in _b[1]:
            _c = _cell_of(_pt[1], _pt[0])
            if _c is not None:
                covered.add(_c)
    total_cells = GRID_LAT_STEPS * GRID_LON_STEPS
    print(f"  Covered cells: {len(covered)}/{total_cells}")

    # Only the OUTER empty region gets masked. Interior empty cells
    # (parks, plazas, parking lots fully surrounded by buildings) are
    # treated as covered so the mask traces just the edge of the city
    # data, not every internal hole. Flood-fill from the bbox border
    # through 4-connected empty cells.
    edge_empty = set()
    queue = []
    for _cy in range(GRID_LAT_STEPS):
        for _cx in range(GRID_LON_STEPS):
            on_edge = (_cx == 0 or _cx == GRID_LON_STEPS - 1
                       or _cy == 0 or _cy == GRID_LAT_STEPS - 1)
            if on_edge and (_cx, _cy) not in covered:
                edge_empty.add((_cx, _cy))
                queue.append((_cx, _cy))
    while queue:
        cx, cy = queue.pop()
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ncx, ncy = cx + dx, cy + dy
            if not (0 <= ncx < GRID_LON_STEPS and 0 <= ncy < GRID_LAT_STEPS):
                continue
            if (ncx, ncy) in covered or (ncx, ncy) in edge_empty:
                continue
            edge_empty.add((ncx, ncy))
            queue.append((ncx, ncy))
    interior_holes = (total_cells - len(covered)) - len(edge_empty)
    print(f"  Outer empty cells: {len(edge_empty)}")
    print(f"  Interior empty (treated as covered): {interior_holes}")

    # Visual mask layer was dropped — the translucent fill read poorly.
    # Coverage filtering below still suppresses point data on uncovered
    # ground so we never show data we cannot verify.
    mask_geojson = None

    def _in_cov(lat, lon):
        _c = _cell_of(lat, lon)
        return _c is not None and _c not in edge_empty

    _before = (len(coords), len(osm_pois), len(cooling))
    coords = [c for c in coords if _in_cov(c[0], c[1])]
    osm_pois = [p for p in osm_pois if _in_cov(p["lat"], p["lon"])]
    cooling = [c for c in cooling if _in_cov(c["lat"], c["lon"])]
    print(f"  After mask filter:")
    print(f"    streetlights {_before[0]} -> {len(coords)}")
    print(f"    POIs {_before[1]} -> {len(osm_pois)}")
    print(f"    cooling {_before[2]} -> {len(cooling)}")

    # Coord precision trim (~1 m) for everything that gets embedded.
    # No feature is dropped — only the coordinate string is shorter.
    coords = [[round(lat, 5), round(lon, 5)] for lat, lon in coords]
    for p in osm_pois:
        p["lat"] = round(p["lat"], 5)
        p["lon"] = round(p["lon"], 5)
    for p in cooling:
        p["lat"] = round(p["lat"], 5)
        p["lon"] = round(p["lon"], 5)
    m = _create_base_map("CartoDB positron", lock_zoom=True)
    m.get_root().header.add_child(folium.Element("<title>LightMap</title>"))
    _add_building_layer(m, building_data)

    # Dark Matter tiles overlay, hidden by default. JS toggles on at night.
    # min_zoom / max_zoom must match the map's pinned range. Leaflet
    # recomputes the effective zoom span from the widest tile layer
    # (Math.min of each layer's minZoom, Math.max of each layer's
    # maxZoom) whenever the Map's own options.minZoom / maxZoom are
    # unset, which is folium's default. Without these explicit values
    # the dark tile's defaults (0 and 20) would loosen the zoom clamp
    # the moment it was added to the map.
    dark_tiles = folium.TileLayer(
        "CartoDB dark_matter", name="Night tiles",
        overlay=True, control=False, show=False,
        min_zoom=16, max_zoom=20,
    )
    dark_tiles.add_to(m)

    streetlight_group = folium.FeatureGroup(
        name="Streetlights", show=False, control=False,
    )
    if coords:
        # max=5 raises the density threshold for "bright pixel" so only
        # genuinely dense streetlight clusters glow yellow. Combined
        # with the shifted HEATMAP_GRADIENT (transparent below 0.35
        # density), the heatmap reads as actual streets instead of a
        # citywide haze.
        # max_zoom=14 (<= map's minZoom of 15) makes Leaflet.heat skip
        # the zoom-based intensity scaling, so the heatmap reads the
        # same brightness at every zoom level. Was max_zoom=18 which
        # caused the heatmap to brighten toward zoom 18 (1/2^(18-zoom)
        # divisor) — felt like the whole map lit up on zoom-in.
        HeatMap(
            coords, radius=5, blur=8, max_zoom=14, max=5,
            gradient=HEATMAP_GRADIENT,
        ).add_to(streetlight_group)
    streetlight_group.add_to(m)

    _add_ui_plugins(m, theme="light")
    legend = _make_shadow_cmap()
    legend.add_to(m)

    slider_css = """
<style>
  .leaflet-container.lm-twilight .leaflet-tile-pane {
    filter: brightness(0.86) saturate(0.82) contrast(0.96);
    transition: filter 0.16s ease;
  }
  .leaflet-container.lm-twilight::after {
    content: "";
    position: absolute;
    inset: 0;
    z-index: 450;
    pointer-events: none;
    background-color: rgba(49, 46, 129, 0.12);
    background-image:
      radial-gradient(
        ellipse at 50% 100%,
        rgba(251, 146, 60, 0.13),
        rgba(244, 114, 182, 0.07) 28%,
        transparent 58%
      ),
      radial-gradient(
        ellipse at 50% 8%,
        rgba(99, 102, 241, 0.15),
        transparent 44%
      ),
      linear-gradient(
        180deg,
        rgba(15, 23, 42, 0.13),
        rgba(79, 70, 229, 0.07) 54%,
        rgba(244, 114, 182, 0.05) 78%,
        rgba(251, 146, 60, 0.045)
      );
  }
  #lm-slider-host {
    position: fixed;
    bottom: 28px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 1000;
    background: rgba(255, 255, 255, 0.92);
    border-radius: 14px;
    padding: 14px 22px 18px 22px;
    box-shadow: 0 10px 32px rgba(15, 23, 42, 0.18);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 "Helvetica Neue", Arial, sans-serif;
    color: #1e293b;
    width: 560px;
    max-width: 92vw;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    user-select: none;
  }
  #lm-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
  }
  #lm-time-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 210px;
  }
  #lm-mode-icon {
    font-size: 20px;
    line-height: 1;
    color: #eab308;
    text-shadow: 0 0 8px rgba(234, 179, 8, 0.24);
  }
  #lm-mode-icon[data-phase="dawn"] {
    color: #6366f1;
    text-shadow: 0 0 9px rgba(99, 102, 241, 0.3);
  }
  #lm-mode-icon[data-phase="day"] {
    color: #eab308;
    text-shadow: 0 0 8px rgba(234, 179, 8, 0.24);
  }
  #lm-mode-icon[data-phase="dusk"] {
    color: #a855f7;
    text-shadow: 0 0 9px rgba(168, 85, 247, 0.28);
  }
  #lm-mode-icon[data-phase="night"] {
    color: #cbd5e1;
    text-shadow: 0 0 8px rgba(148, 163, 184, 0.24);
  }
  #lm-time {
    font-size: 28px;
    font-weight: 600;
    letter-spacing: 0.5px;
    color: #1e293b;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }
  #lm-phase {
    align-items: center;
    border: 1px solid rgba(148, 163, 184, 0.6);
    border-radius: 999px;
    display: inline-flex;
    font-size: 12px;
    font-weight: 800;
    height: 24px;
    justify-content: center;
    letter-spacing: 0.04em;
    line-height: 1;
    min-width: 76px;
    padding: 0 10px;
    text-transform: uppercase;
    white-space: nowrap;
  }
  #lm-phase[data-phase="dawn"] {
    background: #e0e7ff;
    border-color: #c7d2fe;
    color: #3730a3;
  }
  #lm-phase[data-phase="day"] {
    background: #fef3c7;
    border-color: #fde68a;
    color: #854d0e;
  }
  #lm-phase[data-phase="dusk"] {
    background: #ede9fe;
    border-color: #ddd6fe;
    color: #5b21b6;
  }
  #lm-phase[data-phase="night"] {
    background: #1e293b;
    border-color: #475569;
    color: #cbd5e1;
  }
  #lm-date {
    background: transparent;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 6px 10px;
    font-family: inherit;
    font-size: 14px;
    color: inherit;
    cursor: pointer;
    outline: none;
    font-variant-numeric: tabular-nums;
    transition: border-color 0.2s;
  }
  #lm-date:hover, #lm-date:focus { border-color: #94a3b8; }
  #lm-date::-webkit-calendar-picker-indicator {
    cursor: pointer; opacity: 0.55;
  }
  #lm-play {
    background: #f1f5f9;
    color: #1e293b;
    border: 1px solid #cbd5e1;
    border-radius: 999px;
    width: 34px;
    height: 34px;
    padding: 0;
    cursor: pointer;
    font-size: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: background 0.2s, border-color 0.2s;
  }
  #lm-play:hover { background: #e2e8f0; border-color: #94a3b8; }

  #lm-range-wrap {
    position: relative;
    padding-top: 18px;
    padding-bottom: 54px;
  }
  #lm-point-window-track {
    position: absolute;
    left: 0;
    right: 0;
    top: 34px;
    height: 24px;
    display: grid;
    grid-template-columns: repeat(25, minmax(0, 1fr));
    gap: 3px;
    padding: 3px;
    border: 2px solid rgba(148, 163, 184, 0.62);
    border-radius: 999px;
    background: rgba(241, 245, 249, 0.92);
    box-shadow: inset 0 1px 3px rgba(15, 23, 42, 0.12),
                0 4px 14px rgba(15, 23, 42, 0.14);
    pointer-events: none;
    z-index: 1;
  }
  .lm-point-window-segment {
    min-width: 0;
    height: 100%;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.62);
    opacity: 0.75;
    transition: transform 0.15s ease, opacity 0.15s ease;
  }
  .lm-point-window-segment.is-sun {
    background: #eab308;
    opacity: 1;
    box-shadow: 0 0 8px rgba(234, 179, 8, 0.55);
  }
  .lm-point-window-segment.is-shade {
    background: #2563eb;
    opacity: 1;
    box-shadow: 0 0 8px rgba(37, 99, 235, 0.5);
  }
  .lm-point-window-segment.is-lit,
  .lm-point-window-segment.is-combined {
    background: #facc15;
    opacity: 1;
    box-shadow: 0 0 9px rgba(250, 204, 21, 0.58);
  }
  .lm-point-window-segment.is-active-night {
    background: #fb923c;
    opacity: 1;
    box-shadow: 0 0 9px rgba(251, 146, 60, 0.5);
  }
  .lm-point-window-segment.is-open {
    background: #38bdf8;
    opacity: 1;
    box-shadow: 0 0 9px rgba(56, 189, 248, 0.48);
  }
  .lm-point-window-segment.is-best {
    outline: 2px solid #0f172a;
    outline-offset: 1px;
    transform: scaleY(1.26);
  }
  .lm-point-window-segment.is-current {
    outline: 2px solid #2563eb;
    outline-offset: 2px;
    transform: scaleY(1.36);
  }
  #lm-point-window-summary {
    position: absolute;
    left: 0;
    right: 0;
    top: 66px;
    display: none;
    align-items: center;
    justify-content: center;
    gap: 8px;
    color: #334155;
    font-size: 13px;
    font-weight: 700;
    line-height: 1.2;
    pointer-events: none;
  }
  #lm-point-window-summary.is-visible {
    display: flex;
  }
  .lm-point-window-dot {
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: #2563eb;
    box-shadow: 0 0 8px rgba(37, 99, 235, 0.5);
  }
  .lm-point-window-dot.is-sun {
    background: #eab308;
    box-shadow: 0 0 8px rgba(234, 179, 8, 0.52);
  }
  .lm-point-window-dot.is-lit,
  .lm-point-window-dot.is-combined {
    background: #facc15;
    box-shadow: 0 0 8px rgba(250, 204, 21, 0.58);
  }
  .lm-point-window-dot.is-active-night {
    background: #fb923c;
    box-shadow: 0 0 8px rgba(251, 146, 60, 0.5);
  }
  .lm-point-window-dot.is-open {
    background: #38bdf8;
    box-shadow: 0 0 8px rgba(56, 189, 248, 0.48);
  }
  .lm-point-window-pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    border: 1px solid rgba(203, 213, 225, 0.92);
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.9);
    padding: 5px 10px;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.12);
  }
  .lm-sun-marker {
    position: absolute;
    top: 16px;
    width: 2px;
    height: 14px;
    transform: translateX(-50%);
    pointer-events: none;
    z-index: 1;
    transition: left 0.4s ease;
  }
  #lm-sunrise-marker { background: #fbbf24; }
  #lm-next-sunrise-marker { background: #fbbf24; }
  #lm-sunset-marker { background: #f97316; }
  .lm-sun-label {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
    font-size: 11px;
    font-weight: 500;
    color: #94a3b8;
    pointer-events: none;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    transition: left 0.4s ease;
  }
  #lm-sunrise-label::before { content: "\u25B2 "; color: #fbbf24; }
  #lm-sunset-label::before { content: "\u25BC "; color: #f97316; }

  #lm-range {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 4px;
    background: #cbd5e1;
    border-radius: 2px;
    outline: none;
    cursor: pointer;
    margin: 0;
    position: relative;
    z-index: 2;
  }
  #lm-range::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 16px;
    height: 16px;
    border-radius: 999px;
    background: #ffffff;
    border: 2px solid #a78bfa;
    cursor: pointer;
    box-shadow: 0 0 0 3px rgba(167, 139, 250, 0.2);
    transition: box-shadow 0.15s;
  }
  #lm-range::-webkit-slider-thumb:hover {
    box-shadow: 0 0 0 6px rgba(167, 139, 250, 0.3);
  }
  #lm-range::-moz-range-thumb {
    width: 16px;
    height: 16px;
    border-radius: 999px;
    background: #ffffff;
    border: 2px solid #a78bfa;
    cursor: pointer;
    box-shadow: 0 0 0 3px rgba(167, 139, 250, 0.2);
    transition: box-shadow 0.15s;
  }

  /* NIGHT theme overrides */
  #lm-slider-host.night {
    background: rgba(15, 23, 42, 0.92);
    color: #e2e8f0;
    box-shadow: 0 10px 32px rgba(0, 0, 0, 0.4);
  }
  #lm-slider-host.night #lm-time { color: #e2e8f0; }
  #lm-slider-host.night #lm-phase[data-phase="dusk"] {
    background: #2e2242;
    border-color: #7c3aed;
    color: #ddd6fe;
  }
  #lm-slider-host.night #lm-phase[data-phase="dawn"] {
    background: #1e2a4a;
    border-color: #6366f1;
    color: #c7d2fe;
  }
  #lm-slider-host.night #lm-date {
    border-color: #334155; color: #e2e8f0;
  }
  #lm-slider-host.night #lm-date:hover,
  #lm-slider-host.night #lm-date:focus {
    border-color: #475569;
  }
  #lm-slider-host.night #lm-date::-webkit-calendar-picker-indicator {
    filter: invert(0.8);
  }
  #lm-slider-host.night #lm-play {
    background: #1e293b; color: #e2e8f0; border-color: #334155;
  }
  #lm-slider-host.night #lm-play:hover {
    background: #334155; border-color: #475569;
  }
  #lm-slider-host.night #lm-range { background: #334155; }
  #lm-slider-host.night #lm-point-window-track {
    border-color: rgba(71, 85, 105, 0.8);
    background: rgba(15, 23, 42, 0.82);
    box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.32),
                0 2px 8px rgba(0, 0, 0, 0.26);
  }
  #lm-slider-host.night .lm-point-window-segment {
    background: rgba(100, 116, 139, 0.62);
  }
  #lm-slider-host.night .lm-point-window-segment.is-sun {
    background: #eab308;
    box-shadow: 0 0 9px rgba(234, 179, 8, 0.56);
  }
  #lm-slider-host.night .lm-point-window-segment.is-shade {
    background: #3b82f6;
    box-shadow: 0 0 9px rgba(59, 130, 246, 0.54);
  }
  #lm-slider-host.night .lm-point-window-segment.is-lit,
  #lm-slider-host.night .lm-point-window-segment.is-combined {
    background: #facc15;
    box-shadow: 0 0 10px rgba(250, 204, 21, 0.62);
  }
  #lm-slider-host.night .lm-point-window-segment.is-active-night {
    background: #fb923c;
    box-shadow: 0 0 10px rgba(251, 146, 60, 0.54);
  }
  #lm-slider-host.night .lm-point-window-segment.is-open {
    background: #38bdf8;
    box-shadow: 0 0 10px rgba(56, 189, 248, 0.5);
  }
  #lm-slider-host.night .lm-point-window-segment.is-best {
    outline-color: #f8fafc;
  }
  #lm-slider-host.night #lm-point-window-summary {
    color: #cbd5e1;
  }
  #lm-slider-host.night .lm-point-window-pill {
    border-color: rgba(51, 65, 85, 0.95);
    background: rgba(15, 23, 42, 0.86);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.28);
  }
  #lm-slider-host.night #lm-range::-webkit-slider-thumb {
    border-color: #fbbf24;
    box-shadow: 0 0 0 3px rgba(251, 191, 36, 0.2);
  }
  #lm-slider-host.night #lm-range::-webkit-slider-thumb:hover {
    box-shadow: 0 0 0 6px rgba(251, 191, 36, 0.3);
  }
  #lm-slider-host.night #lm-range::-moz-range-thumb {
    border-color: #fbbf24;
    box-shadow: 0 0 0 3px rgba(251, 191, 36, 0.2);
  }
  #lm-slider-host.night .lm-sun-label { color: #64748b; }
  #lm-slider-host.twilight {
    background:
      linear-gradient(
        180deg,
        rgba(213, 221, 240, 0.97),
        rgba(224, 215, 234, 0.97)
      );
    color: #1f2937;
    box-shadow:
      0 10px 34px rgba(49, 46, 129, 0.24),
      0 0 28px rgba(244, 114, 182, 0.12),
      inset 0 0 0 1px rgba(79, 70, 229, 0.18);
  }
  #lm-slider-host.twilight #lm-range {
    background: linear-gradient(90deg, #6f7ca4, #9b8cc5, #c58c8f);
  }
  #lm-slider-host.twilight #lm-date,
  #lm-slider-host.twilight #lm-play {
    background: rgba(241, 245, 249, 0.88);
    border-color: #a5aecb;
  }
  #lm-slider-host.twilight #lm-time {
    color: #1f2937;
  }
  #lm-slider-host.twilight .lm-sun-label {
    color: #566079;
  }
  #lm-slider-host.twilight #lm-range::-webkit-slider-thumb {
    border-color: #4f46e5;
    box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.2);
  }
  #lm-slider-host.twilight #lm-range::-moz-range-thumb {
    border-color: #4f46e5;
    box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.2);
  }
  #lm-slider-host[data-phase="dawn"] #lm-range::-webkit-slider-thumb {
    background: #c7d2fe;
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2),
                0 0 12px rgba(99, 102, 241, 0.28);
  }
  #lm-slider-host[data-phase="dawn"] #lm-range::-webkit-slider-thumb:hover {
    box-shadow: 0 0 0 6px rgba(99, 102, 241, 0.28),
                0 0 14px rgba(99, 102, 241, 0.32);
  }
  #lm-slider-host[data-phase="dawn"] #lm-range::-moz-range-thumb {
    background: #c7d2fe;
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2),
                0 0 12px rgba(99, 102, 241, 0.28);
  }
  #lm-slider-host[data-phase="day"] #lm-range::-webkit-slider-thumb {
    background: #fef3c7;
    border-color: #eab308;
    box-shadow: 0 0 0 3px rgba(234, 179, 8, 0.2),
                0 0 12px rgba(234, 179, 8, 0.28);
  }
  #lm-slider-host[data-phase="day"] #lm-range::-webkit-slider-thumb:hover {
    box-shadow: 0 0 0 6px rgba(234, 179, 8, 0.28),
                0 0 14px rgba(234, 179, 8, 0.32);
  }
  #lm-slider-host[data-phase="day"] #lm-range::-moz-range-thumb {
    background: #fef3c7;
    border-color: #eab308;
    box-shadow: 0 0 0 3px rgba(234, 179, 8, 0.2),
                0 0 12px rgba(234, 179, 8, 0.28);
  }
  #lm-slider-host[data-phase="dusk"] #lm-range::-webkit-slider-thumb {
    background: #e9d5ff;
    border-color: #a855f7;
    box-shadow: 0 0 0 3px rgba(168, 85, 247, 0.2),
                0 0 12px rgba(168, 85, 247, 0.28);
  }
  #lm-slider-host[data-phase="dusk"] #lm-range::-webkit-slider-thumb:hover {
    box-shadow: 0 0 0 6px rgba(168, 85, 247, 0.28),
                0 0 14px rgba(168, 85, 247, 0.32);
  }
  #lm-slider-host[data-phase="dusk"] #lm-range::-moz-range-thumb {
    background: #e9d5ff;
    border-color: #a855f7;
    box-shadow: 0 0 0 3px rgba(168, 85, 247, 0.2),
                0 0 12px rgba(168, 85, 247, 0.28);
  }
  #lm-slider-host[data-phase="night"] #lm-range::-webkit-slider-thumb {
    background: #cbd5e1;
    border-color: #64748b;
    box-shadow: 0 0 0 3px rgba(148, 163, 184, 0.22),
                0 0 12px rgba(203, 213, 225, 0.24);
  }
  #lm-slider-host[data-phase="night"] #lm-range::-webkit-slider-thumb:hover {
    box-shadow: 0 0 0 6px rgba(148, 163, 184, 0.3),
                0 0 14px rgba(203, 213, 225, 0.28);
  }
  #lm-slider-host[data-phase="night"] #lm-range::-moz-range-thumb {
    background: #cbd5e1;
    border-color: #64748b;
    box-shadow: 0 0 0 3px rgba(148, 163, 184, 0.22),
                0 0 12px rgba(203, 213, 225, 0.24);
  }

  #lm-agent-host {
    position: fixed;
    right: 18px;
    bottom: 150px;
    z-index: 1000;
    width: 380px;
    max-width: calc(100vw - 36px);
    background: rgba(255, 255, 255, 0.94);
    border-radius: 10px;
    padding: 12px;
    box-shadow: 0 10px 32px rgba(15, 23, 42, 0.18);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 "Helvetica Neue", Arial, sans-serif;
    color: #1e293b;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    transition: box-shadow 0.18s ease, transform 0.18s ease;
  }
  #lm-agent-host.is-busy {
    box-shadow:
      0 12px 34px rgba(15, 23, 42, 0.22),
      0 0 0 1px rgba(37, 99, 235, 0.32),
      0 0 0 4px rgba(37, 99, 235, 0.12);
    transform: translateY(-1px);
  }
  #lm-agent-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
    font-size: 15px;
    font-weight: 700;
  }
  #lm-agent-status {
    color: #64748b;
    font-size: 12px;
    font-weight: 500;
    text-align: right;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
  }
  #lm-agent-host.is-busy #lm-agent-status::before {
    content: "";
    width: 8px;
    height: 8px;
    border: 2px solid #bfdbfe;
    border-top-color: #2563eb;
    border-radius: 50%;
    animation: lmAgentSpin 0.8s linear infinite;
  }
  #lm-agent-actions {
    display: grid;
    grid-template-columns: 1fr;
    gap: 8px;
  }
  .lm-agent-section {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }
  .lm-agent-section-title {
    grid-column: 1 / -1;
    color: #64748b;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .lm-agent-chip {
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    background: #f8fafc;
    color: #334155;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 5px;
    font-family: inherit;
    font-size: 12px;
    font-weight: 800;
    line-height: 1.25;
    min-height: 58px;
    padding: 8px 6px;
    position: relative;
    text-align: center;
    transition: background 0.15s ease, border-color 0.15s ease,
                color 0.15s ease, transform 0.15s ease;
  }
  .lm-agent-chip::before {
    content: "";
    width: 19px;
    height: 19px;
    border-radius: 50%;
    box-shadow: inset 0 0 0 2px rgba(255, 255, 255, 0.75),
                0 1px 5px rgba(15, 23, 42, 0.2);
  }
  .lm-agent-chip[data-action="point-shade"]::before {
    background: #2563eb;
    box-shadow: inset 8px 0 0 rgba(15, 23, 42, 0.26),
                inset 0 0 0 2px rgba(255, 255, 255, 0.75),
                0 1px 5px rgba(37, 99, 235, 0.35);
  }
  .lm-agent-chip[data-action="point-sun"]::before {
    background: #facc15;
    box-shadow: 0 0 10px rgba(250, 204, 21, 0.68),
                inset 0 0 0 2px rgba(255, 255, 255, 0.8);
  }
  .lm-agent-chip[data-action="point-active"]::before {
    background: #fb923c;
    box-shadow: 0 0 12px rgba(251, 146, 60, 0.58),
                inset 0 0 0 2px rgba(255, 255, 255, 0.82);
  }
  .lm-agent-chip[data-action="point-zero"]::before {
    background: #334155;
    box-shadow: inset 0 0 0 2px rgba(148, 163, 184, 0.95),
                0 1px 5px rgba(15, 23, 42, 0.22);
  }
  .lm-agent-chip:hover {
    background: #f1f5f9;
    border-color: #94a3b8;
    transform: translateY(-1px);
  }
  .lm-agent-chip.is-active {
    background: #eff6ff;
    border-color: #2563eb;
    color: #1d4ed8;
  }
  .lm-agent-chip.is-active::after {
    content: "";
    position: absolute;
    right: 9px;
    top: 9px;
    width: 12px;
    height: 12px;
    border: 2px solid #bfdbfe;
    border-top-color: #2563eb;
    border-radius: 50%;
    animation: lmAgentSpin 0.8s linear infinite;
  }
  #lm-agent-answer {
    margin-top: 10px;
    max-height: 190px;
    overflow: auto;
    border-top: 1px solid #e2e8f0;
    padding-top: 8px;
    color: #334155;
    font-size: 14px;
    line-height: 1.45;
    white-space: pre-wrap;
  }
  #lm-agent-answer.is-empty {
    display: none;
  }
  #lm-slider-host.night ~ #lm-agent-host {
    background: rgba(15, 23, 42, 0.94);
    color: #e2e8f0;
    box-shadow: 0 10px 32px rgba(0, 0, 0, 0.4);
  }
  #lm-slider-host.night ~ #lm-agent-host.is-busy {
    box-shadow:
      0 12px 34px rgba(0, 0, 0, 0.46),
      0 0 0 1px rgba(251, 146, 60, 0.28),
      0 0 0 4px rgba(251, 146, 60, 0.1);
  }
  #lm-slider-host.night ~ #lm-agent-host #lm-agent-status,
  #lm-slider-host.night ~ #lm-agent-host #lm-agent-answer {
    color: #94a3b8;
  }
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-section-title {
    color: #94a3b8;
  }
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-chip {
    background: #1e293b;
    color: #e2e8f0;
    border-color: #334155;
  }
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-chip.is-active {
    background: #2f2a16;
    border-color: #eab308;
    color: #fde68a;
  }
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-chip.is-active[data-action="point-shade"] {
    background: #172554;
    border-color: #3b82f6;
    color: #bfdbfe;
  }
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-chip.is-active[data-action="point-sun"] {
    background: #2f2a16;
    border-color: #eab308;
    color: #fde68a;
  }
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-chip.is-active[data-action="point-active"],
  #lm-slider-host.night ~ #lm-agent-host .lm-agent-chip.is-active[data-action="point-zero"] {
    background: #3b1f0b;
    border-color: #fb923c;
    color: #fed7aa;
  }
  #lm-slider-host.night ~ #lm-agent-host #lm-agent-answer {
    border-top-color: #334155;
  }
  .leaflet-overlay-pane path.lm-agent-scan-circle {
    stroke-dasharray: 8 10;
    animation: lmAgentDash 1.1s linear infinite,
               lmAgentPulse 1.4s ease-in-out infinite;
  }
  .leaflet-marker-icon.lm-agent-scan-label {
    pointer-events: none;
  }
  @keyframes lmAgentSpin {
    to { transform: rotate(360deg); }
  }
  @keyframes lmAgentDash {
    to { stroke-dashoffset: -36; }
  }
  @keyframes lmAgentPulse {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }
  @media (prefers-reduced-motion: reduce) {
    #lm-agent-host,
    .lm-agent-chip {
      transition: none;
    }
    #lm-agent-host.is-busy #lm-agent-status::before,
    .lm-agent-chip.is-active::after,
    .leaflet-overlay-pane path.lm-agent-scan-circle {
      animation: none;
    }
  }
  @media (max-width: 760px) {
    #lm-agent-host {
      left: 18px;
      right: 18px;
      bottom: 154px;
      width: auto;
    }
    #lm-agent-answer { max-height: 120px; }
  }
</style>
"""

    slider_html = """
<div id="lm-slider-host" data-phase="day">
  <div id="lm-row">
    <div id="lm-time-wrap">
      <span id="lm-mode-icon" data-phase="day">\u2600</span>
      <span id="lm-time">--:--</span>
      <span id="lm-phase" data-phase="day">Day</span>
    </div>
    <input type="date" id="lm-date" value="__INITIAL_DATE__">
    <button id="lm-play" aria-label="Play or pause the time slider">
      <span id="lm-play-icon">\u25B6</span>
    </button>
  </div>
  <div id="lm-range-wrap">
    <div class="lm-sun-label" id="lm-sunrise-label">--:--</div>
    <div class="lm-sun-label" id="lm-sunset-label">--:--</div>
    <div class="lm-sun-label" id="lm-next-sunrise-label">--:-- +1</div>
    <div class="lm-sun-marker" id="lm-sunrise-marker"></div>
    <div class="lm-sun-marker" id="lm-sunset-marker"></div>
    <div class="lm-sun-marker" id="lm-next-sunrise-marker"></div>
    <div id="lm-point-window-track" aria-hidden="true"></div>
    <div id="lm-point-window-summary" aria-live="polite"></div>
    <input type="range" id="lm-range" min="4" max="28" step="1" value="14">
  </div>
</div>
<div id="lm-agent-host" aria-live="polite">
  <div id="lm-agent-title">
    <span>LightTime Agent</span>
    <span id="lm-agent-status">local agent</span>
  </div>
  <div id="lm-agent-actions">
    <div class="lm-agent-section">
      <div class="lm-agent-section-title">Daytime</div>
      <button class="lm-agent-chip" data-action="point-shade" type="button">
        Shade Time
      </button>
      <button class="lm-agent-chip" data-action="point-sun" type="button">
        Sunny Time
      </button>
    </div>
    <div class="lm-agent-section">
      <div class="lm-agent-section-title">Nighttime</div>
      <button class="lm-agent-chip" data-action="point-active" type="button">
        Active Time
      </button>
      <button class="lm-agent-chip" data-action="point-zero" type="button">
        Inactive Time
      </button>
    </div>
  </div>
  <div id="lm-agent-answer" class="is-empty" aria-live="polite">
  </div>
</div>
"""

    # Shadow projection runs client-side. SunCalc (CDN) gives sun
    # altitude/azimuth for any (date, location). Projection math
    # mirrors src/shadow/compute.py:compute_shadow — translate the
    # footprint opposite the sun by h / tan(altitude), then take the
    # convex hull of original + translated. MAX_SHADOW_LENGTH caps
    # absurd shadows near sunrise/sunset (altitude near 0).
    slider_js_template = """
<script src="https://cdn.jsdelivr.net/npm/suncalc@1.9.0/suncalc.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/i18next@23.15.2/i18next.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/opening_hours@3.8.0/build/opening_hours.min.js"></script>
<script>
(function() {
  var BUILDINGS = __BUILDINGS__;
  var POIS = __POIS__;
  var LIGHT_POINTS = __LIGHT_POINTS__;
  var TREES_PNG_URL = "__TREES_PNG_URL__";
  var TREES_BBOX = __TREES_BBOX__;
  var COOLING = __COOLING__;
  var HEAT_TMAX_F = __HEAT_TMAX_F__;
  var HEAT_APPARENT_F = __HEAT_APPARENT_F__;
  var HEAT_UV = __HEAT_UV__;
  var STATIC_COUNTS = __STATIC_COUNTS__;
  var LAT_CENTER = __CENTER_LAT__;
  var LON_CENTER = __CENTER_LON__;
  var CITY_NAME = __CITY_NAME__;
  var CITY_TIMEZONE = __CITY_TIMEZONE__;
  var INITIAL_DATE = "__INITIAL_DATE__";
  var M_PER_DEG_LAT = 111320;
  var M_PER_DEG_LON = 111320 * Math.cos(LAT_CENTER * Math.PI / 180);
  var MAX_SHADOW_LENGTH = 500;
  // Building shadows use a higher threshold than the visual day/night
  // theme because low sun projects 200 m+ shadows that merge into a
  // city-wide wall. The theme itself follows SunCalc sunrise/sunset.
  var DAY_THRESHOLD = 15;
  var POINT_CHECK_RADIUS_M = 17;
  var POINT_SCAN_RADIUS_M = 20;
  var POINT_SHADE_MIN_SAMPLES = 2;
  var POINT_FULL_SHADE_MIN_SAMPLES = 11;
  var TIMELINE_START_SLOT = 4;
  var TIMELINE_END_SLOT = 28;
  var NIGHT_ACTIVITY_RADIUS_M = 100;
  var ACTIVITY_MAX_SCORE = 50;
  var NIGHT_ACTIVE_SCORE = 45;
  var NIGHT_QUIET_SCORE = 25;
  var STREETLIGHT_GLOW_BASE_ZOOM = 16;
  var STREETLIGHT_GLOW_BASE_RADIUS = 5;
  var STREETLIGHT_GLOW_BASE_BLUR = 8;
  var STREETLIGHT_GLOW_MAX_RADIUS = 80;
  var STREETLIGHT_GLOW_MAX_BLUR = 128;

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function d2r(d) { return d * Math.PI / 180; }
  function slotHour(slot) {
    return ((slot % 24) + 24) % 24;
  }

  function timeZoneParts(date, timeZone) {
    var parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23"
    }).formatToParts(date);
    var values = {};
    for (var i = 0; i < parts.length; i++) {
      values[parts[i].type] = parts[i].value;
    }
    var hour = parseInt(values.hour || "0", 10);
    if (hour === 24) hour = 0;
    return {
      year: parseInt(values.year, 10),
      month: parseInt(values.month, 10),
      day: parseInt(values.day, 10),
      hour: hour,
      minute: parseInt(values.minute || "0", 10),
      second: parseInt(values.second || "0", 10)
    };
  }

  function cityUtcOffsetMinutes(date) {
    var p = timeZoneParts(date, CITY_TIMEZONE);
    var asUTC = Date.UTC(
      p.year, p.month - 1, p.day, p.hour, p.minute, p.second
    );
    return Math.round((asUTC - date.getTime()) / 60000);
  }

  function cityDate(year, monthIdx, day, hour, min) {
    var base = Date.UTC(year, monthIdx, day, hour, min);
    var offset = cityUtcOffsetMinutes(new Date(base));
    var zoned = new Date(base - offset * 60000);
    var secondOffset = cityUtcOffsetMinutes(zoned);
    if (secondOffset !== offset) {
      zoned = new Date(base - secondOffset * 60000);
    }
    return zoned;
  }

  // Andrew's monotone chain convex hull in 2D. Vertices: [[lon, lat]...]
  function convexHull(points) {
    if (points.length < 3) return points.slice();
    var pts = points.slice().sort(function(a, b) {
      return a[0] - b[0] || a[1] - b[1];
    });
    function cross(o, a, b) {
      return (a[0] - o[0]) * (b[1] - o[1]) -
             (a[1] - o[1]) * (b[0] - o[0]);
    }
    var lower = [];
    for (var i = 0; i < pts.length; i++) {
      while (lower.length >= 2 &&
        cross(lower[lower.length - 2], lower[lower.length - 1], pts[i]) <= 0) {
        lower.pop();
      }
      lower.push(pts[i]);
    }
    var upper = [];
    for (var j = pts.length - 1; j >= 0; j--) {
      while (upper.length >= 2 &&
        cross(upper[upper.length - 2], upper[upper.length - 1], pts[j]) <= 0) {
        upper.pop();
      }
      upper.push(pts[j]);
    }
    lower.pop(); upper.pop();
    return lower.concat(upper);
  }

  function setup() {
    if (typeof __MAP_NAME__ === "undefined" || !window.L || !window.SunCalc
        || !window.opening_hours) {
      setTimeout(setup, 100); return;
    }
    var map = __MAP_NAME__;
    var dark = __DARK_NAME__;
    var lights = __STREET_NAME__;
    var host = document.getElementById("lm-slider-host");
    var rangeEl = document.getElementById("lm-range");
    var dateEl = document.getElementById("lm-date");
    var timeEl = document.getElementById("lm-time");
    var iconEl = document.getElementById("lm-mode-icon");
    var phaseEl = document.getElementById("lm-phase");
    var playEl = document.getElementById("lm-play");
    var playIconEl = document.getElementById("lm-play-icon");
    var srMarker = document.getElementById("lm-sunrise-marker");
    var ssMarker = document.getElementById("lm-sunset-marker");
    var nsMarker = document.getElementById("lm-next-sunrise-marker");
    var srLabel = document.getElementById("lm-sunrise-label");
    var ssLabel = document.getElementById("lm-sunset-label");
    var nsLabel = document.getElementById("lm-next-sunrise-label");
    var pointWindowEl = document.getElementById("lm-point-window-track");
    var pointWindowSummaryEl = document.getElementById("lm-point-window-summary");
    var agentHostEl = document.getElementById("lm-agent-host");
    var agentAnswerEl = document.getElementById("lm-agent-answer");
    var agentStatusEl = document.getElementById("lm-agent-status");
    if (!host || !rangeEl || !dateEl) { setTimeout(setup, 100); return; }

    var state = {
      dateStr: INITIAL_DATE, slot: parseInt(rangeEl.value, 10),
      playing: false, playTimer: null, shadowLayer: null,
      hadShadows: false,
      lastSun: null, nightMix: 0, heatOn: false,
      userLocation: null, userMarker: null,
      currentShadows: [], openVenuePoints: [],
      agentHighlight: null, agentHighlightLabel: null,
      agentScan: null, agentScanLabel: null, agentRunId: 0,
      pointWindowKind: null, pointWindowAnalysis: null,
      pointWindowBest: null
    };
    var streetlightHeatLayer = null;

    function parseDateStr(s) {
      var parts = s.split("-").map(Number);
      return { y: parts[0], m: parts[1] - 1, d: parts[2] };
    }

    function currentCityClock() {
      try {
        var values = timeZoneParts(new Date(), CITY_TIMEZONE);
        var hour = parseInt(values.hour || "0", 10);
        if (hour === 24) hour = 0;
        var dateStr = values.year + "-"
          + pad(values.month) + "-"
          + pad(values.day);
        var slot = hour;
        if (hour < TIMELINE_START_SLOT) {
          var serviceDate = new Date(Date.UTC(
            parseInt(values.year, 10),
            parseInt(values.month, 10) - 1,
            parseInt(values.day, 10) - 1
          ));
          dateStr = serviceDate.getUTCFullYear() + "-"
            + pad(serviceDate.getUTCMonth() + 1) + "-"
            + pad(serviceDate.getUTCDate());
          slot = hour + 24;
        }
        return {
          dateStr: dateStr,
          slot: Math.max(TIMELINE_START_SLOT, Math.min(TIMELINE_END_SLOT, slot))
        };
      } catch (e) {
        var now = new Date();
        var fallbackDate = new Date(
          now.getFullYear(), now.getMonth(), now.getDate()
        );
        var fallbackSlot = now.getHours();
        if (fallbackSlot < TIMELINE_START_SLOT) {
          fallbackDate.setDate(fallbackDate.getDate() - 1);
          fallbackSlot += 24;
        }
        return {
          dateStr: fallbackDate.getFullYear() + "-"
            + pad(fallbackDate.getMonth() + 1)
            + "-" + pad(fallbackDate.getDate()),
          slot: Math.max(
            TIMELINE_START_SLOT,
            Math.min(TIMELINE_END_SLOT, fallbackSlot)
          )
        };
      }
    }

    function loadCurrentCityTime() {
      var now = currentCityClock();
      state.dateStr = now.dateStr;
      state.slot = now.slot;
      dateEl.value = now.dateStr;
      rangeEl.value = now.slot;
    }

    function sunAt(dateStr, slot) {
      var p = parseDateStr(dateStr);
      var hour = slot;
      var min = 0;
      var d = cityDate(p.y, p.m, p.d, hour, min);
      var pos = SunCalc.getPosition(d, LAT_CENTER, LON_CENTER);
      // SunCalc: azimuth measured from south, west-positive.
      // Convert to compass bearing (from north, clockwise) to match
      // pvlib / compute.py convention.
      return {
        alt: pos.altitude * 180 / Math.PI,
        az: (pos.azimuth * 180 / Math.PI + 180 + 360) % 360,
        hour: slotHour(slot), min: min,
        slot: slot
      };
    }

    function localHourFraction(dt) {
      var p = timeZoneParts(dt, CITY_TIMEZONE);
      return p.hour + p.minute / 60;
    }

    function sunWindowForDate(dateStr) {
      var p = parseDateStr(dateStr);
      var noon = cityDate(p.y, p.m, p.d, 12, 0);
      var times = SunCalc.getTimes(noon, LAT_CENTER, LON_CENTER);
      if (!times.sunrise || !times.sunset ||
          isNaN(times.sunrise.getTime()) ||
          isNaN(times.sunset.getTime())) {
        return null;
      }
      return {
        sunrise: localHourFraction(times.sunrise),
        sunset: localHourFraction(times.sunset)
      };
    }

    function addDaysToDateStr(dateStr, days) {
      var p = parseDateStr(dateStr);
      var d = new Date(Date.UTC(p.y, p.m, p.d + days));
      return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1)
        + "-" + pad(d.getUTCDate());
    }

    function findStreetlightHeatLayer() {
      if (streetlightHeatLayer) return streetlightHeatLayer;
      if (!lights || !lights.eachLayer) return null;
      lights.eachLayer(function(layer) {
        if (!streetlightHeatLayer && layer && layer._heat) {
          streetlightHeatLayer = layer;
        }
      });
      return streetlightHeatLayer;
    }

    function streetlightGlowForZoom(zoom) {
      var scale = Math.pow(2, Math.max(0, zoom - STREETLIGHT_GLOW_BASE_ZOOM));
      return {
        radius: Math.round(Math.min(
          STREETLIGHT_GLOW_MAX_RADIUS,
          STREETLIGHT_GLOW_BASE_RADIUS * scale
        )),
        blur: Math.round(Math.min(
          STREETLIGHT_GLOW_MAX_BLUR,
          STREETLIGHT_GLOW_BASE_BLUR * scale
        ))
      };
    }

    function applyStreetlightGlowZoom() {
      var layer = findStreetlightHeatLayer();
      if (!layer) return;
      var zoom = map.getZoom();
      var glow = streetlightGlowForZoom(zoom);
      if (layer.setOptions) {
        layer.setOptions({ radius: glow.radius, blur: glow.blur });
      } else {
        layer.options.radius = glow.radius;
        layer.options.blur = glow.blur;
        if (layer._heat && layer._heat.radius) {
          layer._heat.radius(glow.radius, glow.blur);
        }
        if (layer.redraw) layer.redraw();
      }
      window.__lightmapStreetlightGlow = {
        zoom: zoom,
        radius: glow.radius,
        blur: glow.blur
      };
    }

    function projectShadow(ring, hm, altDeg, azDeg) {
      if (altDeg <= 0) return null;
      var lenM = Math.min(hm / Math.tan(d2r(altDeg)), MAX_SHADOW_LENGTH);
      var oppRad = d2r((azDeg + 180) % 360);
      var dx = lenM * Math.sin(oppRad);
      var dy = lenM * Math.cos(oppRad);
      var dLon = dx / M_PER_DEG_LON;
      var dLat = dy / M_PER_DEG_LAT;
      var pts = [];
      for (var i = 0; i < ring.length; i++) pts.push(ring[i]);
      for (var j = 0; j < ring.length; j++) {
        pts.push([ring[j][0] + dLon, ring[j][1] + dLat]);
      }
      return convexHull(pts);
    }

    // Expand the current map viewport by MAX_SHADOW_LENGTH so we don't
    // drop buildings whose shadow reaches into the visible area even
    // though the footprint itself is just off-screen.
    var SHADOW_LAT_MARGIN = MAX_SHADOW_LENGTH / M_PER_DEG_LAT;
    var SHADOW_LON_MARGIN = MAX_SHADOW_LENGTH / M_PER_DEG_LON;

    function cullBounds() {
      var b = map.getBounds();
      return [
        b.getWest() - SHADOW_LON_MARGIN,
        b.getSouth() - SHADOW_LAT_MARGIN,
        b.getEast() + SHADOW_LON_MARGIN,
        b.getNorth() + SHADOW_LAT_MARGIN
      ];
    }

    // Direct canvas shadow layer. The older path rebuilt a GeoJSON
    // layer on every tick, which forced Leaflet to allocate thousands
    // of L.Polygon children. Here the shadow engine returns simple
    // [height, ring] tuples and this layer fills them directly.
    var ShadowCanvasLayer = L.Layer.extend({
      initialize: function() {
        this._shadows = [];
      },
      onAdd: function(map_) {
        this._map = map_;
        this._canvas = L.DomUtil.create(
          "canvas", "leaflet-layer lm-shadow-canvas"
        );
        this._canvas.style.pointerEvents = "none";
        this._ctx = this._canvas.getContext("2d");
        map_.getPanes().overlayPane.appendChild(this._canvas);
        map_.on("resize zoomend moveend", this._reset, this);
        this._reset();
      },
      onRemove: function(map_) {
        map_.off("resize zoomend moveend", this._reset, this);
        if (this._canvas && this._canvas.parentNode) {
          this._canvas.parentNode.removeChild(this._canvas);
        }
        this._canvas = null;
        this._ctx = null;
      },
      clear: function() {
        this._shadows = [];
        return this._draw();
      },
      setShadows: function(shadows) {
        this._shadows = shadows || [];
        return this._draw();
      },
      _reset: function() {
        if (!this._map || !this._canvas) return;
        var size = this._map.getSize();
        var topLeft = this._map.containerPointToLayerPoint([0, 0]);
        L.DomUtil.setPosition(this._canvas, topLeft);
        this._canvas.width = size.x;
        this._canvas.height = size.y;
        this._draw();
      },
      _fillForHeight: function(h) {
        var op = Math.min(0.18 + (h / 60) * 0.22, 0.45);
        return "rgba(15,23,42," + op.toFixed(3) + ")";
      },
      _draw: function() {
        if (!this._ctx || !this._map) return 0;
        var t0 = performance.now();
        var ctx = this._ctx;
        var size = this._map.getSize();
        ctx.clearRect(0, 0, size.x, size.y);
        for (var i = 0; i < this._shadows.length; i++) {
          var item = this._shadows[i];
          var ring = item[1];
          if (!ring || ring.length < 3) continue;
          ctx.beginPath();
          for (var j = 0; j < ring.length; j++) {
            var p = this._map.latLngToContainerPoint([ring[j][1], ring[j][0]]);
            if (j === 0) ctx.moveTo(p.x, p.y);
            else ctx.lineTo(p.x, p.y);
          }
          ctx.closePath();
          ctx.fillStyle = this._fillForHeight(item[0]);
          ctx.fill();
        }
        return performance.now() - t0;
      }
    });
    state.shadowLayer = new ShadowCanvasLayer().addTo(map);

    // Tree canopy as a baked PNG overlay. One image draw on pan/zoom
    // versus ~59K polygon paints. Mild pixelation at zoom 18 is OK —
    // tree canopy is auxiliary and has soft edges by nature.
    state.treeLayer = L.imageOverlay(
      TREES_PNG_URL,
      [[TREES_BBOX[0], TREES_BBOX[1]], [TREES_BBOX[2], TREES_BBOX[3]]],
      { interactive: false, opacity: 1, pane: 'overlayPane' }
    );
    // tree layer is added/removed by renderShadows based on altDeg

    // OSM POIs with opening_hours. Each entry gets its hours string
    // parsed once by opening_hours.js. At render time the parsed
    // object answers open/closed for the slider's (date, time). We
    // build a canvas-backed GeoJSON layer and refill it on each tick
    // the same way the shadow layer works.
    var poiParsed = [];
    for (var pi = 0; pi < POIS.length; pi++) {
      var p = POIS[pi];
      var oh = null;
      try { oh = new opening_hours(p.hours, null, 2); }
      catch (e) { oh = null; }
      if (oh) {
        poiParsed.push({
          lon: p.lon, lat: p.lat,
          name: p.name, amenity: p.amenity,
          hours: p.hours, oh: oh
        });
      }
    }

    var poiLayer = L.geoJson(null, {
      interactive: false,
      pointToLayer: function(feat, latlng) {
        // L.marker with DivIcon lands in markerPane (z-index 600),
        // above both the streetlight heatmap (overlayPane) and the
        // shadow canvas, so the yellow dot stays legible at night.
        return L.marker(latlng, {
          interactive: false,
          icon: L.divIcon({
            className: 'lm-poi',
            iconSize: [20, 20],
            iconAnchor: [10, 10],
            html: (
              '<div style="width:14px;height:14px;border-radius:50%;'
              + 'background:#facc15;border:1.5px solid #422006;'
              + 'box-shadow:0 0 7px rgba(250,204,21,0.95);"></div>'
            )
          })
        });
      }
    }).addTo(map);

    // Construct a Date whose local fields match the slider slot, so
    // opening_hours.js reads the configured city wall-clock values.
    function ohDate(dateStr, slot) {
      var p = parseDateStr(dateStr);
      return new Date(p.y, p.m, p.d, slot, 0, 0);
    }

    // Cache the feature list per (date, slot) so repeat scrubs skip
    // the getState loop. Invalidated on date change (different sun).
    var poiCache = new Map();

    function renderPois() {
      // POI visibility mirrors the hard day/night switch so the venue
      // markers never sit over the daytime basemap.
      poiLayer.clearLayers();
      if (!isNightSlot(state.slot)) {
        state.openVenuePoints = [];
        return;
      }
      var key = state.dateStr + ":" + state.slot;
      var feats;
      if (poiCache.has(key)) {
        feats = poiCache.get(key);
        poiCache.delete(key); poiCache.set(key, feats);
      } else {
        var d = ohDate(state.dateStr, state.slot);
        feats = [];
        for (var i = 0; i < poiParsed.length; i++) {
          var p = poiParsed[i];
          try {
            if (!p.oh.getState(d)) continue;
          } catch (e) { continue; }
          feats.push({
            type: "Feature",
            geometry: {
              type: "Point", coordinates: [p.lon, p.lat]
            },
            properties: {
              name: p.name, amenity: p.amenity, hours: p.hours
            }
          });
        }
      if (poiCache.size >= TIMELINE_END_SLOT + 2) {
        poiCache.delete(poiCache.keys().next().value);
      }
        poiCache.set(key, feats);
      }
      poiLayer.addData({
        type: "FeatureCollection", features: feats
      });
      state.openVenuePoints = feats.map(function(f) {
        return f.geometry.coordinates;
      });
    }

    // Per-slot shadow cache. Key = "YYYY-MM-DD:slot". Populated lazily
    // on first visit to a slot; subsequent visits skip the convex-hull
    // loop entirely. Viewport changes invalidate the whole cache at
    // moveend because the culled building set differs.
    var shadowCache = new Map();
    var CACHE_LIMIT = 24;

    function computeShadowRings(altDeg, azDeg) {
      var cb = cullBounds();
      var cullW = cb[0], cullS = cb[1], cullE = cb[2], cullN = cb[3];
      var shadows = [];
      for (var i = 0; i < BUILDINGS.length; i++) {
        var b = BUILDINGS[i];
        var bb = b[2];
        if (bb[2] < cullW || bb[0] > cullE ||
            bb[3] < cullS || bb[1] > cullN) continue;
        var hull = projectShadow(b[1], b[0], altDeg, azDeg);
        if (!hull || hull.length < 3) continue;
        shadows.push([b[0], hull]);
      }
      return shadows;
    }

    function pointInRing(lat, lon, ring) {
      var inside = false;
      for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        var xi = ring[i][0], yi = ring[i][1];
        var xj = ring[j][0], yj = ring[j][1];
        var crosses = ((yi > lat) !== (yj > lat)) &&
          (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi);
        if (crosses) inside = !inside;
      }
      return inside;
    }

    function buildingCanReachPoint(b, lat, lon) {
      var bb = b[2];
      return !(lon < bb[0] - SHADOW_LON_MARGIN ||
               lon > bb[2] + SHADOW_LON_MARGIN ||
               lat < bb[1] - SHADOW_LAT_MARGIN ||
               lat > bb[3] + SHADOW_LAT_MARGIN);
    }

    function buildingCanReachCircle(b, lat, lon, radiusM) {
      var bb = b[2];
      var dLat = radiusM / M_PER_DEG_LAT;
      var dLon = radiusM / M_PER_DEG_LON;
      return !(lon + dLon < bb[0] - SHADOW_LON_MARGIN ||
               lon - dLon > bb[2] + SHADOW_LON_MARGIN ||
               lat + dLat < bb[1] - SHADOW_LAT_MARGIN ||
               lat - dLat > bb[3] + SHADOW_LAT_MARGIN);
    }

    function circleSamplePoints(lat, lon, radiusM) {
      var r = radiusM;
      var s = r * 0.55;
      var o = r * 0.85;
      var raw = [
        [0, 0],
        [s, 0], [-s, 0], [0, s], [0, -s],
        [s * 0.7, s * 0.7], [s * 0.7, -s * 0.7],
        [-s * 0.7, s * 0.7], [-s * 0.7, -s * 0.7],
        [o, 0], [-o, 0], [0, o], [0, -o]
      ];
      var out = [];
      for (var i = 0; i < raw.length; i++) {
        out.push({
          lat: lat + raw[i][1] / M_PER_DEG_LAT,
          lon: lon + raw[i][0] / M_PER_DEG_LON
        });
      }
      return out;
    }

    function pointShadeAtSlot(lat, lon, slot) {
      var sun = sunAt(state.dateStr, slot);
      var samples = circleSamplePoints(lat, lon, POINT_CHECK_RADIUS_M);
      var shadedSamples = new Array(samples.length).fill(false);
      var result = {
        slot: slot,
        alt: sun.alt,
        shaded: false,
        bright: false,
        fullShade: false,
        partialShade: false,
        coverage: 0,
        brightCoverage: 0,
        sampleCount: samples.length,
        shadedSampleCount: 0,
        score: 0,
        count: 0,
        maxHeight: 0
      };
      if (sun.alt < DAY_THRESHOLD) {
        return result;
      }
      for (var i = 0; i < BUILDINGS.length; i++) {
        var b = BUILDINGS[i];
        if (!buildingCanReachCircle(b, lat, lon, POINT_CHECK_RADIUS_M)) continue;
        var hull = projectShadow(b[1], b[0], sun.alt, sun.az);
        if (!hull || hull.length < 3) continue;
        var coveredByBuilding = 0;
        for (var si = 0; si < samples.length; si++) {
          if (!pointInRing(samples[si].lat, samples[si].lon, hull)) continue;
          coveredByBuilding += 1;
          if (!shadedSamples[si]) {
            shadedSamples[si] = true;
            result.shadedSampleCount += 1;
          }
        }
        if (!coveredByBuilding) continue;
        result.count += 1;
        result.score += (coveredByBuilding / samples.length)
          * (1 + Math.min(b[0] / 60, 2));
        if (b[0] > result.maxHeight) result.maxHeight = b[0];
      }
      result.coverage = result.shadedSampleCount / samples.length;
      result.brightCoverage = 1 - result.coverage;
      result.shaded = result.shadedSampleCount >= POINT_SHADE_MIN_SAMPLES;
      result.fullShade = result.shadedSampleCount >= POINT_FULL_SHADE_MIN_SAMPLES;
      result.partialShade = result.shaded && !result.fullShade;
      result.bright = !result.shaded;
      result.score += result.coverage * 4;
      return result;
    }

    function analyzePointSun(lat, lon) {
      var slots = [];
      var bestShade = null;
      var bestBright = null;
      for (
        var slot = TIMELINE_START_SLOT;
        slot <= TIMELINE_END_SLOT;
        slot++
      ) {
        var item = pointShadeAtSlot(lat, lon, slot);
        slots.push(item);
        if (item.shaded &&
            (!bestShade || item.score > bestShade.score)) {
          bestShade = item;
        }
        if (item.bright &&
            (!bestBright ||
             (item.brightCoverage * 20 + item.alt) >
             (bestBright.brightCoverage * 20 + bestBright.alt))) {
          bestBright = item;
        }
      }
      return {
        lat: lat,
        lon: lon,
        slots: slots,
        shadeGroups: pointSignalGroups(slots, "shaded"),
        brightGroups: pointSignalGroups(slots, "bright"),
        bestShade: bestShade,
        bestBright: bestBright
      };
    }

    function nearbyLightStats(lat, lon, radiusM) {
      var stats = {
        count: 0,
        score: 0,
        nearestM: null,
        radiusM: radiusM
      };
      for (var i = 0; i < LIGHT_POINTS.length; i++) {
        var p = LIGHT_POINTS[i];
        var d = distanceM(lat, lon, p[0], p[1]);
        if (d > radiusM) continue;
        stats.count += 1;
        stats.score += Math.max(0.1, 1 - d / radiusM);
        if (stats.nearestM === null || d < stats.nearestM) {
          stats.nearestM = d;
        }
      }
      if (stats.nearestM !== null) {
        stats.nearestM = Math.round(stats.nearestM);
      }
      return stats;
    }

    function nearbyOpenVenuesAtSlot(lat, lon, slot, radiusM) {
      var d = ohDate(state.dateStr, slot);
      var places = [];
      for (var i = 0; i < poiParsed.length; i++) {
        var p = poiParsed[i];
        var dist = distanceM(lat, lon, p.lat, p.lon);
        if (dist > radiusM) continue;
        try {
          if (!p.oh.getState(d)) continue;
        } catch (e) { continue; }
        places.push({
          name: p.name,
          amenity: p.amenity,
          distanceM: Math.round(dist)
        });
      }
      places.sort(function(a, b) { return a.distanceM - b.distanceM; });
      return {
        count: places.length,
        radiusM: radiusM,
        places: places.slice(0, 4)
      };
    }

    function isNightSlot(slot) {
      var dayOffset = slot >= 24 ? 1 : 0;
      var hour = slot >= 24 ? slot - 24 : slot;
      var dateStr = dayOffset ? addDaysToDateStr(state.dateStr, 1)
        : state.dateStr;
      var sunWindow = sunWindowForDate(dateStr);
      if (!sunWindow) {
        return sunAt(state.dateStr, slot).alt < 0;
      }
      return hour < sunWindow.sunrise || hour >= sunWindow.sunset;
    }

    function phaseForSlot(slot) {
      var hour = slotHour(slot);
      var sun = state.lastSun || sunAt(state.dateStr, slot);
      if (isNightSlot(slot)) {
        return { key: "night", label: "Night" };
      }
      if (sun.alt < DAY_THRESHOLD && hour < 12) {
        return { key: "dawn", label: "Dawn" };
      }
      if (sun.alt < DAY_THRESHOLD && hour >= 12) {
        return { key: "dusk", label: "Dusk" };
      }
      return { key: "day", label: "Day" };
    }

    function activityLevel(score) {
      if (score >= ACTIVITY_MAX_SCORE) return "high";
      if (score >= NIGHT_ACTIVE_SCORE) return "active";
      if (score > NIGHT_QUIET_SCORE) return "low";
      return "quiet";
    }

    function activityScoreIntensity(score) {
      return Math.max(
        0,
        Math.min(ACTIVITY_MAX_SCORE, score || 0)
      ) / ACTIVITY_MAX_SCORE;
    }

    function activityHighlightOpacity(coverage) {
      var intensity = Math.max(0, Math.min(1, coverage || 0));
      return 0.06 + Math.pow(intensity, 1.45) * 0.48;
    }

    function nightActivityAt(lat, lon, slot, lightStats) {
      var openStats = nearbyOpenVenuesAtSlot(
        lat, lon, slot, NIGHT_ACTIVITY_RADIUS_M
      );
      var night = isNightSlot(slot);
      var hasOpenPlaces = openStats.count > 0;
      var openCountComponent = hasOpenPlaces
        ? Math.min(ACTIVITY_MAX_SCORE, openStats.count * 10)
        : 0;
      var activityScore = night && hasOpenPlaces
        ? Math.max(
          0,
          Math.min(
            ACTIVITY_MAX_SCORE,
            openCountComponent
          )
        )
        : 0;
      activityScore = Math.round(activityScore);
      return {
        slot: slot,
        night: night,
        active: night && activityScore >= NIGHT_ACTIVE_SCORE,
        quiet: night && activityScore <= NIGHT_QUIET_SCORE,
        zero: night && activityScore === 0,
        openSignal: night && openStats.count > 0,
        activityScore: activityScore,
        level: activityLevel(activityScore),
        lightCount: lightStats.count,
        lightNearestM: lightStats.nearestM,
        openCount: openStats.count,
        places: openStats.places
      };
    }

    function firstMatchingNightSlot(slots, prop, startSlot) {
      if (!slots || !slots.length) return null;
      var fallback = null;
      for (var i = 0; i < slots.length; i++) {
        var slot = slots[i];
        if (!slot.night || !slot[prop]) continue;
        if (!fallback) fallback = slot;
        if (slot.slot >= startSlot) return slot;
      }
      return fallback;
    }

    function analyzePointNight(lat, lon, kind) {
      var lightStats = nearbyLightStats(lat, lon, NIGHT_ACTIVITY_RADIUS_M);
      var slots = [];
      var best = null;
      var bestActive = null;
      var bestOpen = null;
      var lowestNight = null;
      for (
        var slot = TIMELINE_START_SLOT;
        slot <= TIMELINE_END_SLOT;
        slot++
      ) {
        var item = nightActivityAt(lat, lon, slot, lightStats);
        slots.push(item);
        if (item.night &&
            (!best || item.activityScore > best.activityScore)) {
          best = item;
        }
        if (item.night &&
            (!lowestNight ||
             item.activityScore < lowestNight.activityScore)) {
          lowestNight = item;
        }
        if (item.active &&
            (!bestActive ||
             item.activityScore > bestActive.activityScore)) {
          bestActive = item;
        }
        if (item.openSignal &&
            (!bestOpen ||
             item.openCount > bestOpen.openCount ||
             (item.openCount === bestOpen.openCount &&
              item.activityScore > bestOpen.activityScore))) {
          bestOpen = item;
        }
      }
      var quietStart = null;
      if (bestActive || best) {
        var anchor = bestActive || best;
        var anchorIndex = slots.indexOf(anchor);
        for (var step = 1; step < slots.length - anchorIndex; step++) {
          var candidate = slots[anchorIndex + step];
          if (candidate.night && candidate.quiet) {
            quietStart = candidate;
            break;
          }
        }
      }
      if (!quietStart) {
        for (var qi = 0; qi < slots.length; qi++) {
          if (slots[qi].night && slots[qi].quiet) {
            quietStart = slots[qi];
            break;
          }
        }
      }
      return {
        lat: lat,
        lon: lon,
        kind: kind,
        slots: slots,
        lightStats: lightStats,
        activityRadiusM: NIGHT_ACTIVITY_RADIUS_M,
        activeGroups: pointSignalGroups(slots, "active"),
        openGroups: pointSignalGroups(slots, "openSignal"),
        zeroGroups: pointSignalGroups(slots, "zero"),
        quietGroups: pointSignalGroups(slots, "quiet"),
        best: bestActive || best,
        bestOpen: bestOpen,
        bestAny: best,
        quietStart: quietStart,
        lowestNight: lowestNight
      };
    }

    function pointSignalGroups(slots, prop) {
      var groups = [];
      var start = null;
      for (var i = 0; i < slots.length; i++) {
        if (slots[i][prop]) {
          if (start === null) start = slots[i].slot;
        } else if (start !== null) {
          groups.push({ start: start, end: slots[i].slot });
          start = null;
        }
      }
      if (start !== null) {
        groups.push({ start: start, end: slots[slots.length - 1].slot + 1 });
      }
      return groups;
    }

    function clearPointWindowTrack() {
      if (pointWindowEl) pointWindowEl.innerHTML = "";
      if (pointWindowSummaryEl) {
        pointWindowSummaryEl.innerHTML = "";
        pointWindowSummaryEl.classList.remove("is-visible");
      }
      state.pointWindowKind = null;
      state.pointWindowAnalysis = null;
      state.pointWindowBest = null;
    }

    function currentAnalysisSlot(analysis) {
      if (!analysis || !analysis.slots) return null;
      for (var i = 0; i < analysis.slots.length; i++) {
        if (analysis.slots[i].slot === state.slot) return analysis.slots[i];
      }
      return null;
    }

    function isNightActivityKind(kind) {
      return kind === "active" || kind === "quiet" ||
        kind === "zero" || kind === "nearby-active";
    }

    function currentPointWindowLabel(analysis, kind) {
      var slot = currentAnalysisSlot(analysis);
      if (!slot) return "";
      if (kind === "shade") {
        return "Shadow here: " + formatPct(slot.coverage)
          + " at " + formatHour(slot.slot);
      }
      if (kind === "sun") {
        return "Sun here: " + formatPct(slot.brightCoverage)
          + " at " + formatHour(slot.slot);
      }
      if (isNightActivityKind(kind)) {
        if (kind === "active" || kind === "zero") {
          return "Activity here: " + Math.round(slot.activityScore || 0)
            + " points at " + formatHour(slot.slot);
        }
        var prefix = kind === "nearby-active"
          ? "Active area: "
          : "Activity here: ";
        return prefix + Math.round(slot.activityScore || 0)
          + " points at " + formatHour(slot.slot);
      }
      return "";
    }

    function renderPointWindowSummary(analysis, kind) {
      if (!pointWindowSummaryEl) return;
      pointWindowSummaryEl.innerHTML = "";
      if (kind === "shade" || kind === "sun" ||
          isNightActivityKind(kind)) {
        var currentText = currentPointWindowLabel(analysis, kind);
        if (!currentText) return;
        var currentPill = document.createElement("span");
        currentPill.className = "lm-point-window-pill";
        var currentDot = document.createElement("span");
        currentDot.className = "lm-point-window-dot"
          + (kind === "sun" ? " is-sun" : "")
          + (isNightActivityKind(kind) ? " is-active-night" : "");
        var currentLabel = document.createElement("span");
        currentLabel.textContent = currentText;
        currentPill.appendChild(currentDot);
        currentPill.appendChild(currentLabel);
        pointWindowSummaryEl.appendChild(currentPill);
        pointWindowSummaryEl.classList.add("is-visible");
        return;
      }
      var groups = kind === "sun" ? analysis.brightGroups
        : kind === "shade" ? analysis.shadeGroups
        : analysis.activeGroups;
      var pill = document.createElement("span");
      pill.className = "lm-point-window-pill";
      var dot = document.createElement("span");
      var dotClass = kind === "sun" ? " is-sun"
        : isNightActivityKind(kind) ? " is-active-night"
        : "";
      dot.className = "lm-point-window-dot" + dotClass;
      var label = document.createElement("span");
      if (groups && groups.length) {
        var prefix = isNightActivityKind(kind) ? "Active window: " : "";
        label.textContent = prefix + formatGroups(groups);
      } else {
        label.textContent = kind === "sun" ? "No day sun here today"
          : kind === "shade" ? "No building shade here today"
          : "No active nighttime window here today";
      }
      pill.appendChild(dot);
      pill.appendChild(label);
      pointWindowSummaryEl.appendChild(pill);
      pointWindowSummaryEl.classList.add("is-visible");
    }

    function renderPointWindowTrack(analysis, kind, best) {
      clearPointWindowTrack();
      if (!pointWindowEl || !analysis || !analysis.slots) return;
      state.pointWindowKind = kind;
      state.pointWindowAnalysis = analysis;
      state.pointWindowBest = best || null;
      for (var i = 0; i < analysis.slots.length; i++) {
        var slot = analysis.slots[i];
        var el = document.createElement("div");
        var active = kind === "sun" ? slot.bright
          : kind === "shade" ? slot.shaded
          : kind === "active" ? slot.openSignal
          : kind === "zero" ? slot.openSignal
          : kind === "nearby-active" ? slot.openSignal
          : isNightActivityKind(kind) ? slot.night
          : false;
        el.className = "lm-point-window-segment"
          + (kind === "sun" && active ? " is-sun" : "")
          + (kind === "shade" && active ? " is-shade" : "")
          + (isNightActivityKind(kind) && active
            ? " is-active-night" : "")
          + (best && best.slot === slot.slot ? " is-best" : "");
        el.setAttribute("data-slot", String(slot.slot));
        if (kind === "shade" && active) {
          el.style.opacity = String(0.3 + slot.coverage * 0.7);
        } else if (kind === "sun" && active) {
          el.style.opacity = String(0.3 + slot.brightCoverage * 0.7);
        } else if (isNightActivityKind(kind) && active) {
          var activityIntensity = activityScoreIntensity(slot.activityScore);
          el.style.opacity = String(
            0.16 + Math.pow(activityIntensity, 1.35) * 0.84
          );
          if (slot.activityScore >= ACTIVITY_MAX_SCORE) {
            el.style.backgroundColor = "#fed7aa";
            el.style.boxShadow = (
              "0 0 18px rgba(251, 146, 60, 0.78), "
              + "0 0 5px rgba(255, 237, 213, 0.72)"
            );
          } else if (slot.activityScore >= 40) {
            el.style.backgroundColor = "#fdba74";
            el.style.boxShadow = "0 0 14px rgba(251, 146, 60, 0.62)";
          } else if (slot.activityScore >= 30) {
            el.style.backgroundColor = "#fb923c";
            el.style.boxShadow = "0 0 11px rgba(251, 146, 60, 0.54)";
          } else if (slot.activityScore >= 20) {
            el.style.backgroundColor = "#f97316";
            el.style.boxShadow = "0 0 8px rgba(249, 115, 22, 0.46)";
          } else {
            el.style.backgroundColor = "#c2410c";
            el.style.boxShadow = "0 0 5px rgba(194, 65, 12, 0.32)";
          }
        }
        el.title = formatHour(slot.slot) + " "
          + (kind === "sun" || kind === "shade"
            ? (slot.shaded
              ? (slot.fullShade ? "full building shade "
                : "partial building shade ") + formatPct(slot.coverage)
              : slot.bright ? "day sun " + formatPct(slot.brightCoverage)
              : "low sun or night")
            : isNightActivityKind(kind)
              ? (slot.night
                ? (slot.openCount || 0) + " open place"
                  + (slot.openCount === 1 ? "" : "s")
                  + ", " + slot.lightCount + " light signal"
                  + (slot.lightCount === 1 ? "" : "s")
                : "daytime")
              : "daytime");
        pointWindowEl.appendChild(el);
      }
      syncPointWindowToSlot();
    }

    function syncPointWindowToSlot() {
      if (!state.pointWindowAnalysis || !state.pointWindowKind) return;
      renderPointWindowSummary(
        state.pointWindowAnalysis, state.pointWindowKind
      );
      if (pointWindowEl) {
        var segments = pointWindowEl.querySelectorAll(".lm-point-window-segment");
        for (var i = 0; i < segments.length; i++) {
          segments[i].classList.toggle(
            "is-current",
            segments[i].getAttribute("data-slot") === String(state.slot)
          );
        }
      }
      syncAgentHighlightToSlot();
    }

    function formatHour(slot) {
      return pad(slotHour(slot)) + ":00" + (slot >= 24 ? " +1" : "");
    }

    function formatPct(value) {
      return Math.round((value || 0) * 100) + "%";
    }

    function formatGroups(groups) {
      if (!groups || !groups.length) return "";
      var out = [];
      for (var i = 0; i < groups.length; i++) {
        out.push(formatHour(groups[i].start) + "-" + formatHour(groups[i].end));
      }
      return out.join(", ");
    }

    function renderShadows(altDeg, azDeg) {
      // Keep shadows off until the sun is DAY_THRESHOLD above the
      // horizon. Below that angle shadow_length = height / tan(alt)
      // explodes and every building hits the 500 m cap, flooding the
      // view with a uniform shadow wall. Static tree canopy follows
      // the same gate so the night map stays dark and uncluttered.
      if (altDeg < DAY_THRESHOLD) {
        if (state.hadShadows) {
          state.shadowLayer.clear();
          state.hadShadows = false;
        }
        state.currentShadows = [];
        state.shadowCount = 0;
        if (state.treeLayer && map.hasLayer(state.treeLayer)) {
          map.removeLayer(state.treeLayer);
        }
        return;
      }
      if (state.treeLayer && !map.hasLayer(state.treeLayer)) {
        state.treeLayer.addTo(map);
      }
      state.hadShadows = true;
      var key = state.dateStr + ":" + state.slot;
      var shadows;
      var t0 = performance.now();
      var cacheHit = false;
      if (shadowCache.has(key)) {
        shadows = shadowCache.get(key);
        cacheHit = true;
        // LRU bump.
        shadowCache.delete(key);
        shadowCache.set(key, shadows);
      } else {
        shadows = computeShadowRings(altDeg, azDeg);
        if (shadowCache.size >= CACHE_LIMIT) {
          var firstKey = shadowCache.keys().next().value;
          shadowCache.delete(firstKey);
        }
        shadowCache.set(key, shadows);
      }
      var t1 = performance.now();
      var drawMs = state.shadowLayer.setShadows(shadows);
      var t2 = performance.now();
      state.shadowCount = shadows.length;
      state.currentShadows = shadows;
      window.__lightmapTimeSlider = {
        slot: state.slot,
        date: state.dateStr,
        shadowCount: shadows.length,
        shadowCacheHit: cacheHit,
        shadowComputeMs: t1 - t0,
        shadowDrawMs: drawMs,
        shadowRenderMs: t2 - t0
      };
    }

    function renderTheme() {
      // Keep the basemap binary. A blended opacity transition between
      // day and night reads as a muddy gray state.
      var nightMix = isNightSlot(state.slot) ? 1 : 0;
      state.nightMix = nightMix;
      var twilight = nightMix <= 0.5 && state.lastSun &&
        state.lastSun.alt < DAY_THRESHOLD;
      var phase = phaseForSlot(state.slot);
      if (phaseEl) {
        phaseEl.textContent = phase.label;
        phaseEl.setAttribute("data-phase", phase.key);
      }
      host.setAttribute("data-phase", phase.key);
      if (iconEl) {
        iconEl.setAttribute("data-phase", phase.key);
      }

      if (nightMix > 0) {
        if (!map.hasLayer(dark)) dark.addTo(map);
        dark.setOpacity(nightMix);
        if (!map.hasLayer(lights)) lights.addTo(map);
        applyStreetlightGlowZoom();
      } else {
        if (map.hasLayer(dark)) map.removeLayer(dark);
        if (map.hasLayer(lights)) map.removeLayer(lights);
      }

      map.getContainer().classList.toggle("lm-twilight", !!twilight);
      if (nightMix > 0.5) {
        host.classList.add("night");
        host.classList.remove("twilight");
        iconEl.textContent = "\u263E";
      } else {
        host.classList.remove("night");
        host.classList.toggle("twilight", !!twilight);
        iconEl.textContent = "\u2600";
      }
    }

    function updateSunMarkers() {
      var p = parseDateStr(state.dateStr);
      var noon = cityDate(p.y, p.m, p.d, 12, 0);
      var nextBase = new Date(Date.UTC(p.y, p.m, p.d + 1, 12, 0));
      var nextNoon = cityDate(
        nextBase.getUTCFullYear(),
        nextBase.getUTCMonth(),
        nextBase.getUTCDate(),
        12,
        0
      );
      var t = SunCalc.getTimes(noon, LAT_CENTER, LON_CENTER);
      var nextT = SunCalc.getTimes(nextNoon, LAT_CENTER, LON_CENTER);
      if (!t.sunrise || isNaN(t.sunrise.getTime())) return;
      function localHm(dt) {
        var p = timeZoneParts(dt, CITY_TIMEZONE);
        return [p.hour, p.minute];
      }
      var sr = localHm(t.sunrise);
      var ss = localHm(t.sunset);
      var srFrac = sr[0] + sr[1] / 60;
      var ssFrac = ss[0] + ss[1] / 60;
      function placeMarker(marker, label, slotFrac, text) {
        if (!marker || !label) return;
        if (slotFrac < TIMELINE_START_SLOT || slotFrac > TIMELINE_END_SLOT) {
          marker.style.display = "none";
          label.style.display = "none";
          return;
        }
        var pct = (
          (slotFrac - TIMELINE_START_SLOT) /
          (TIMELINE_END_SLOT - TIMELINE_START_SLOT)
        ) * 100;
        marker.style.display = "";
        label.style.display = "";
        marker.style.left = pct + "%";
        label.style.left = pct + "%";
        label.textContent = text;
      }
      placeMarker(
        srMarker, srLabel, srFrac, pad(sr[0]) + ":" + pad(sr[1])
      );
      placeMarker(
        ssMarker, ssLabel, ssFrac, pad(ss[0]) + ":" + pad(ss[1])
      );
      if (nextT.sunrise && !isNaN(nextT.sunrise.getTime()) &&
          nsMarker && nsLabel) {
        var ns = localHm(nextT.sunrise);
        var nsFrac = 24 + ns[0] + ns[1] / 60;
        placeMarker(
          nsMarker, nsLabel, nsFrac,
          pad(ns[0]) + ":" + pad(ns[1]) + " +1"
        );
      }
    }

    // Render pipeline: cheap light update runs synchronously on every
    // input tick (time label, slider value). The heavy shadow render
    // + theme swap is coalesced to at most one call per animation
    // frame. During a slider drag this collapses dozens of input
    // events into one render and keeps the UI thread unblocked.
    var pendingRender = null;
    function scheduleRender() {
      if (pendingRender !== null) return;
      pendingRender = requestAnimationFrame(function() {
        pendingRender = null;
      var s = sunAt(state.dateStr, state.slot);
      state.lastSun = s;
      renderTheme();
      renderShadows(s.alt, s.az);
      renderPois();
      });
    }

    function updateScene() {
      var s = sunAt(state.dateStr, state.slot);
      timeEl.textContent = formatHour(state.slot);
      rangeEl.value = state.slot;
      scheduleRender();
      syncPointWindowToSlot();
    }

    rangeEl.addEventListener("input", function() {
      state.slot = parseInt(rangeEl.value, 10);
      updateScene();
    });

    dateEl.addEventListener("change", function() {
      if (!dateEl.value) return;
      state.dateStr = dateEl.value;
      // Same slot on a different date has a different sun angle, so
      // cached features are invalid.
      shadowCache.clear();
      // POI open/closed state also depends on day of week and date
      // (weekday vs weekend, PH, specific date overrides).
      poiCache.clear();
      clearPointWindowTrack();
      updateSunMarkers();
      updateScene();
      fetchWeather(state.dateStr);
    });

    // Weather (Open-Meteo). Free, no auth, covers forecast
    // (near-future + today) and archive (historical). Writes
    // temp range and max UV into #lm-weather. Cached per date.
    var weatherEl = document.getElementById("lm-weather");
    var weatherCache = new Map();
    // WMO weather codes mapped to BMP-only icons so everything
    // renders without emoji-font support and without surrogate-pair
    // escape gymnastics. Good-enough fidelity: sun / partial / cloud /
    // fog / umbrella for rain / snowflake / thundercloud.
    var weatherCodeIcon = {
      0: "\u2600",                             // clear
      1: "\u26C5", 2: "\u26C5", 3: "\u2601",   // few clouds to overcast
      45: "\u2601", 48: "\u2601",              // fog -> cloud
      51: "\u2602", 53: "\u2602", 55: "\u2602",
      56: "\u2602", 57: "\u2602",
      61: "\u2602", 63: "\u2602", 65: "\u2602",
      66: "\u2602", 67: "\u2602",
      71: "\u2744", 73: "\u2744", 75: "\u2744", 77: "\u2744",
      80: "\u2602", 81: "\u2602", 82: "\u2602",
      85: "\u2744", 86: "\u2744",
      95: "\u26C8", 96: "\u26C8", 99: "\u26C8"
    };
    function weatherIcon(code) {
      return weatherCodeIcon[code] || "\u2601";
    }

    function fetchWeather(dateStr) {
      if (!weatherEl) return;
      if (weatherCache.has(dateStr)) {
        renderWeather(weatherCache.get(dateStr));
        return;
      }
      // Pick archive vs forecast. Forecast serves today + 16 days out
      // and has a short ingest lag for very recent past. Archive
      // serves reliable history back to 1940 but is a day or two
      // behind the present.
      var today = new Date();
      today.setHours(0, 0, 0, 0);
      var picked = new Date(dateStr + "T12:00:00");
      picked.setHours(0, 0, 0, 0);
      var daysFromToday = (picked - today) / 86400000;
      var useArchive = daysFromToday < -7;
      var base = useArchive
        ? "https://archive-api.open-meteo.com/v1/archive"
        : "https://api.open-meteo.com/v1/forecast";
      var url = base
        + "?latitude=" + LAT_CENTER
        + "&longitude=" + LON_CENTER
        + "&daily=temperature_2m_max,temperature_2m_min,"
        + "apparent_temperature_max,"
        + "uv_index_max,weather_code"
        + "&temperature_unit=fahrenheit"
        + "&timezone=" + encodeURIComponent(CITY_TIMEZONE)
        + "&start_date=" + dateStr + "&end_date=" + dateStr;
      weatherEl.textContent = "Loading weather...";
      fetch(url)
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(j) {
          if (!j || !j.daily) {
            weatherEl.textContent = "Weather unavailable";
            return;
          }
          var d = j.daily;
          var info = {
            tmax: d.temperature_2m_max && d.temperature_2m_max[0],
            tmin: d.temperature_2m_min && d.temperature_2m_min[0],
            apparent: d.apparent_temperature_max && d.apparent_temperature_max[0],
            uv:   d.uv_index_max && d.uv_index_max[0],
            code: d.weather_code && d.weather_code[0]
          };
          weatherCache.set(dateStr, info);
          renderWeather(info);
        })
        .catch(function() {
          weatherEl.textContent = "Weather unavailable";
        });
    }

    function renderWeather(w) {
      if (!weatherEl) return;
      if (w.tmax == null) {
        weatherEl.textContent = "Weather unavailable";
        applyHeatState(false);
        return;
      }
      var icon = weatherIcon(w.code);
      var txt = icon + "  "
        + Math.round(w.tmin) + "\u2013"
        + Math.round(w.tmax) + "\u00B0F  \u00B7  UV "
        + (w.uv == null ? "?" : Math.round(w.uv));
      weatherEl.textContent = txt;
      var heat = (w.tmax != null && w.tmax >= HEAT_TMAX_F)
              || (w.apparent != null && w.apparent >= HEAT_APPARENT_F)
              || (w.uv != null && w.uv >= HEAT_UV);
      applyHeatState(heat);
    }

    // --- Heat-response overlay ---
    // Cooling markers appear only when weather fetch crosses HEAT_*
    // thresholds. Emergency-room markers are intentionally omitted.
    var coolingLayer = L.layerGroup();
    for (var ci = 0; ci < COOLING.length; ci++) {
      var cl = COOLING[ci];
      var cIcon = L.divIcon({
        className: 'lm-cooling',
        iconSize: [12, 12], iconAnchor: [6, 6],
        html: '<div style="background:#0891b2;border:2px solid #fff;'
            + 'border-radius:50%;width:12px;height:12px;'
            + 'box-shadow:0 1px 3px rgba(0,0,0,0.3);"></div>'
      });
      var cM = L.marker([cl.lat, cl.lon], { icon: cIcon });
      cM.addTo(coolingLayer);
    }

    var heatOn = false;
    function applyHeatState(on) {
      if (on === heatOn) return;
      heatOn = !!on;
      state.heatOn = heatOn;
      if (heatOn) {
        if (!map.hasLayer(coolingLayer)) coolingLayer.addTo(map);
      } else {
        if (map.hasLayer(coolingLayer)) map.removeLayer(coolingLayer);
      }
      var badge = document.getElementById('lm-heat-badge');
      if (badge) {
        badge.style.display = heatOn ? 'inline-block' : 'none';
      }
    }

    function agentMode() {
      if (state.nightMix > 0.5) return "night";
      if (state.lastSun && state.lastSun.alt < DAY_THRESHOLD) return "twilight";
      return "day";
    }

    function distanceM(aLat, aLon, bLat, bLon) {
      var dx = (aLon - bLon) * M_PER_DEG_LON;
      var dy = (aLat - bLat) * M_PER_DEG_LAT;
      return Math.sqrt(dx * dx + dy * dy);
    }

    function activeReferencePoint() {
      if (state.userLocation) {
        return { lat: state.userLocation.lat, lon: state.userLocation.lon };
      }
      var c = map.getCenter();
      return { lat: c.lat, lon: c.lng };
    }

    function ensureReferenceLocation(done) {
      if (state.userLocation) {
        done();
        return;
      }
      if (!navigator.geolocation) {
        var c = map.getCenter();
        setAgentLocation(c.lat, c.lng, null, "map center");
        done();
        return;
      }
      setAgentStatus("locating...");
      navigator.geolocation.getCurrentPosition(function(pos) {
        setAgentLocation(
          pos.coords.latitude, pos.coords.longitude,
          pos.coords.accuracy, "browser"
        );
        done();
      }, function() {
        var c = map.getCenter();
        setAgentLocation(c.lat, c.lng, null, "map center");
        done();
      }, {
        enableHighAccuracy: true, timeout: 5000, maximumAge: 60000
      });
    }

    function makeCandidateGrid(kind) {
      var b = map.getBounds();
      var west = b.getWest(), east = b.getEast();
      var south = b.getSouth(), north = b.getNorth();
      var cols = 5, rows = 5;
      var ref = activeReferencePoint();
      var cells = [];
      for (var y = 0; y < rows; y++) {
        for (var x = 0; x < cols; x++) {
          var lat = south + (north - south) * (y + 0.5) / rows;
          var lon = west + (east - west) * (x + 0.5) / cols;
          var cellSpan = Math.min(
            (north - south) * M_PER_DEG_LAT / rows,
            (east - west) * M_PER_DEG_LON / cols
          );
          var radius = Math.max(
            kind === "shade" ? 45 : 65,
            Math.min(kind === "shade" ? 95 : 170, cellSpan * 0.28)
          );
          cells.push({
            id: kind + "-" + x + "-" + y,
            kind: kind,
            lat: lat,
            lon: lon,
            radiusM: Math.round(radius),
            rawScore: 0,
            distanceM: distanceM(lat, lon, ref.lat, ref.lon)
          });
        }
      }

      function cellFor(lat, lon) {
        if (lat < south || lat > north || lon < west || lon > east) return null;
        var cx = Math.min(cols - 1, Math.max(0, Math.floor((lon - west) / (east - west) * cols)));
        var cy = Math.min(rows - 1, Math.max(0, Math.floor((lat - south) / (north - south) * rows)));
        return cells[cy * cols + cx];
      }

      if (kind === "shade") {
        for (var si = 0; si < state.currentShadows.length; si++) {
          var item = state.currentShadows[si];
          var ring = item[1];
          if (!ring || ring.length < 3) continue;
          var minLon = ring[0][0], maxLon = ring[0][0];
          var minLat = ring[0][1], maxLat = ring[0][1];
          for (var r = 1; r < ring.length; r++) {
            var lon = ring[r][0], lat = ring[r][1];
            if (lon < minLon) minLon = lon; else if (lon > maxLon) maxLon = lon;
            if (lat < minLat) minLat = lat; else if (lat > maxLat) maxLat = lat;
          }
          var cell = cellFor((minLat + maxLat) / 2, (minLon + maxLon) / 2);
          if (cell) cell.rawScore += 1 + Math.min(item[0] / 60, 2);
        }
      } else if (kind === "bright") {
        for (var li = 0; li < LIGHT_POINTS.length; li++) {
          var lp = LIGHT_POINTS[li];
          var lc = cellFor(lp[0], lp[1]);
          if (lc) lc.rawScore += 1;
        }
        for (var vi = 0; vi < state.openVenuePoints.length; vi++) {
          var vp = state.openVenuePoints[vi];
          var vc = cellFor(vp[1], vp[0]);
          if (vc) vc.rawScore += 5;
        }
      } else {
        var centerCell = cellFor(ref.lat, ref.lon) || cells[Math.floor(cells.length / 2)];
        centerCell.rawScore = 1;
      }

      for (var i = 0; i < cells.length; i++) {
        var distPenalty = 1 + (cells[i].distanceM / 550);
        cells[i].score = cells[i].rawScore / distPenalty;
        cells[i].label = kind === "shade" ? "Recommended shade area"
          : kind === "bright" ? "Recommended bright area"
          : "Current view area";
        cells[i].evidence = kind === "shade"
          ? Math.round(cells[i].rawScore) + " nearby shadow signals"
          : kind === "bright"
            ? Math.round(cells[i].rawScore) + " nearby light and venue signals"
            : "current map center";
      }
      cells.sort(function(a, b) { return b.score - a.score; });
      return cells.slice(0, 8);
    }

    function makeNearbyActiveCandidates() {
      var b = map.getBounds();
      var west = b.getWest(), east = b.getEast();
      var south = b.getSouth(), north = b.getNorth();
      var cols = 5, rows = 5;
      var ref = activeReferencePoint();
      var candidates = [];
      for (var y = 0; y < rows; y++) {
        for (var x = 0; x < cols; x++) {
          var lat = south + (north - south) * (y + 0.5) / rows;
          var lon = west + (east - west) * (x + 0.5) / cols;
          var analysis = analyzePointNight(lat, lon, "nearby-active");
          var best = analysis.best;
          var dist = distanceM(lat, lon, ref.lat, ref.lon);
          var rawScore = best ? best.activityScore : 0;
          var nearbyFactor = dist < 55 ? 0.45 : 1;
          candidates.push({
            id: "nearby-active-" + x + "-" + y,
            kind: "nearby-active",
            lat: lat,
            lon: lon,
            radiusM: NIGHT_ACTIVITY_RADIUS_M,
            analysis: analysis,
            best: best,
            rawScore: rawScore,
            distanceM: dist,
            score: rawScore * nearbyFactor / (1 + dist / 700)
          });
        }
      }
      candidates.sort(function(a, b) { return b.score - a.score; });
      return candidates.slice(0, 8);
    }

    function actionQuestion(action) {
      if (action === "shade") return "Find the best nearby shade area.";
      if (action === "bright") return "Find the brightest nearby area at night.";
      if (action === "point-active") return "Active Time";
      if (action === "point-zero") return "Inactive Time";
      return "Summarize the selected map evidence.";
    }

    function collectAgentContext(action) {
      var b = map.getBounds();
      var c = map.getCenter();
      var weatherText = weatherEl ? weatherEl.textContent : "";
      return {
        app: "LightMap " + CITY_NAME,
        action: action || "view",
        date: state.dateStr,
        hour: state.slot,
        mode: agentMode(),
        sun: state.lastSun,
        heat: { active: state.heatOn, weather: weatherText },
        location: state.userLocation,
        viewport: {
          center: { lat: c.lat, lon: c.lng },
          zoom: map.getZoom(),
          bounds: {
            west: b.getWest(), south: b.getSouth(),
            east: b.getEast(), north: b.getNorth()
          }
        },
        candidates: makeCandidateGrid(action || "view"),
        visible: {
          shadowCount: state.shadowCount || 0,
          treesShown: !!(state.treeLayer && map.hasLayer(state.treeLayer)),
          streetlightsShown: map.hasLayer(lights),
          openVenuesShown: poiLayer.getLayers().length,
          coolingShown: map.hasLayer(coolingLayer)
        },
        counts: STATIC_COUNTS,
        caveats: [
          "Tree canopy is a static shade overlay.",
          "Streetlights and venues are public-data visibility context."
        ]
      };
    }

    function setAgentAnswer(text) {
      if (!agentAnswerEl) return;
      agentAnswerEl.textContent = text || "";
      agentAnswerEl.classList.toggle("is-empty", !text);
    }

    function setAgentStatus(text) {
      if (agentStatusEl) agentStatusEl.textContent = text;
    }

    function setAgentBusy(action, busy) {
      if (agentHostEl) {
        agentHostEl.classList.toggle("is-busy", !!busy);
        agentHostEl.setAttribute("aria-busy", busy ? "true" : "false");
      }
      var chips = document.querySelectorAll(".lm-agent-chip");
      for (var i = 0; i < chips.length; i++) {
        var chipAction = chips[i].getAttribute("data-action") || "view";
        chips[i].classList.toggle("is-active", !!busy && chipAction === action);
      }
    }

    function finishAgentRun(runId, startedAt, done) {
      var delay = Math.max(0, 520 - (Date.now() - startedAt));
      setTimeout(function() {
        if (runId !== state.agentRunId) return;
        setAgentBusy(null, false);
        clearAgentScan();
        done();
      }, delay);
    }

    function agentActionColor(kind) {
      return (kind === "bright" || kind === "sun") ? "#eab308"
        : isNightActivityKind(kind) ? "#fb923c"
        : kind === "shade" ? "#2563eb"
        : "#2563eb";
    }

    function escapeHtml(value) {
      var map = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      };
      return String(value).replace(/[&<>"']/g, function(ch) {
        return map[ch];
      });
    }

    function setAgentLocation(lat, lon, accuracy, source) {
      state.userLocation = {
        lat: lat, lon: lon,
        accuracyM: accuracy || null,
        source: source || "browser"
      };
      if (!state.userMarker) {
        state.userMarker = L.marker([lat, lon], {
          title: "Agent location",
          icon: L.divIcon({
            className: "lm-agent-location",
            iconSize: [18, 18],
            iconAnchor: [9, 9],
            html: (
              '<div style="width:14px;height:14px;border-radius:50%;'
              + 'background:#2563eb;border:2px solid #fff;'
              + 'box-shadow:0 1px 5px rgba(15,23,42,0.4);"></div>'
            )
          })
        }).addTo(map);
      } else {
        state.userMarker.setLatLng([lat, lon]);
      }
      clearPointWindowTrack();
      setAgentStatus("location set");
    }

    function clearAgentHighlight() {
      if (state.agentHighlight) {
        map.removeLayer(state.agentHighlight);
        state.agentHighlight = null;
      }
      if (state.agentHighlightLabel) {
        map.removeLayer(state.agentHighlightLabel);
        state.agentHighlightLabel = null;
      }
    }

    function clearAgentScan() {
      if (state.agentScan) {
        map.removeLayer(state.agentScan);
        state.agentScan = null;
      }
      if (state.agentScanLabel) {
        map.removeLayer(state.agentScanLabel);
        state.agentScanLabel = null;
      }
    }

    function drawAgentScan(highlight, action) {
      clearAgentScan();
      clearAgentHighlight();
      if (!highlight || typeof highlight.lat !== "number" ||
          typeof highlight.lon !== "number") {
        return;
      }
      var kind = highlight.kind || action || "view";
      var color = agentActionColor(kind);
      var pointAction = action && action.indexOf("point-") === 0;
      var minRadius = pointAction ? POINT_SCAN_RADIUS_M : 90;
      state.agentScan = L.circle([highlight.lat, highlight.lon], {
        radius: Math.max(minRadius, highlight.radiusM || minRadius),
        color: color,
        weight: 3,
        opacity: 0.72,
        fillColor: color,
        fillOpacity: 0.08,
        interactive: false,
        className: "lm-agent-scan-circle"
      }).addTo(map);
      var scanLabel = action === "point-sun" ? "Checking sun windows..."
        : action === "point-shade" ? "Checking shade windows..."
        : action === "point-active" ? "Checking active time..."
        : action === "point-zero" ? "Checking inactive time..."
        : kind === "bright" ? "Checking light signals..."
        : kind === "shade" ? "Checking shade signals..."
        : "Reading this view...";
      state.agentScanLabel = L.marker([highlight.lat, highlight.lon], {
        interactive: false,
        icon: L.divIcon({
          className: "lm-agent-scan-label",
          iconSize: [1, 1],
          iconAnchor: [0, 28],
          html: (
            '<div style="display:inline-block;background:rgba(255,255,255,0.94);'
            + 'color:#1e293b;border:1px solid rgba(148,163,184,0.45);'
            + 'border-radius:6px;padding:5px 9px;font-size:12px;'
            + 'font-weight:700;white-space:nowrap;transform:translateX(-50%);'
            + 'box-shadow:0 2px 8px rgba(0,0,0,0.18);">'
            + scanLabel + '</div>'
          )
        })
      }).addTo(map);
      map.panTo([highlight.lat, highlight.lon], { animate: true });
    }

    function agentHighlightLabelIcon(label) {
      return L.divIcon({
        className: "lm-agent-highlight-label",
        iconSize: [1, 1],
        iconAnchor: [0, 28],
        html: (
          '<div style="display:inline-block;background:rgba(15,23,42,0.88);'
          + 'color:#fff;border-radius:6px;padding:5px 9px;font-size:12px;'
          + 'font-weight:700;white-space:nowrap;transform:translateX(-50%);'
          + 'box-shadow:0 2px 8px rgba(0,0,0,0.24);">'
          + escapeHtml(label || "Selected area") + '</div>'
        )
      });
    }

    function syncAgentHighlightToSlot() {
      var kind = state.pointWindowKind;
      if (kind !== "shade" && kind !== "sun" &&
          !isNightActivityKind(kind)) {
        return;
      }
      if (!state.pointWindowAnalysis) return;
      var slot = currentAnalysisSlot(state.pointWindowAnalysis);
      if (!slot) return;
      var coverage = isNightActivityKind(kind)
        ? Math.max(
          0,
          Math.min(1, (slot.activityScore || 0) / ACTIVITY_MAX_SCORE)
        )
        : kind === "sun" ? slot.brightCoverage : slot.coverage;
      var label = kind === "active"
        ? "Activity " + Math.round(slot.activityScore || 0) + " points at "
          + formatHour(slot.slot)
        : isNightActivityKind(kind)
        ? (kind === "nearby-active" ? "Active nearby " : "Activity ")
          + Math.round(slot.activityScore || 0) + " points at "
          + formatHour(slot.slot)
        : kind === "sun"
          ? "Sun " + formatPct(coverage) + " at " + formatHour(slot.slot)
          : "Shadow " + formatPct(coverage) + " at "
            + formatHour(slot.slot);
      if (state.agentHighlight) {
        state.agentHighlight.setStyle({
          fillOpacity: isNightActivityKind(kind)
            ? activityHighlightOpacity(coverage)
            : 0.08 + Math.max(0, Math.min(1, coverage)) * 0.28
        });
      }
      if (state.agentHighlightLabel) {
        state.agentHighlightLabel.setIcon(agentHighlightLabelIcon(label));
      }
    }

    function drawAgentHighlight(highlight, action) {
      if (!highlight || typeof highlight.lat !== "number" ||
          typeof highlight.lon !== "number") {
        return;
      }
      clearAgentScan();
      clearAgentHighlight();
      var kind = highlight.kind || action || "view";
      var color = agentActionColor(kind);
      var intensity = typeof highlight.coverage === "number"
        ? Math.max(0, Math.min(1, highlight.coverage))
        : null;
      var fillOpacity = intensity === null ? 0.18
        : isNightActivityKind(kind)
          ? activityHighlightOpacity(intensity)
          : 0.08 + intensity * 0.28;
      state.agentHighlight = L.circle([highlight.lat, highlight.lon], {
        radius: highlight.radiusM || 180,
        color: color,
        weight: 3,
        opacity: 0.95,
        fillColor: color,
        fillOpacity: fillOpacity,
        interactive: false
      }).addTo(map);
      var label = highlight.label || "Recommended area";
      state.agentHighlightLabel = L.marker([highlight.lat, highlight.lon], {
        interactive: false,
        icon: agentHighlightLabelIcon(label)
      }).addTo(map);
      map.panTo([highlight.lat, highlight.lon], { animate: true });
    }

    function runAgentAction(action) {
      var question = actionQuestion(action);
      var context = collectAgentContext(action);
      var preview = context.candidates && context.candidates.length
        ? context.candidates[0] : null;
      var runId = state.agentRunId + 1;
      state.agentRunId = runId;
      var startedAt = Date.now();
      setAgentBusy(action, true);
      setAgentStatus("working...");
      setAgentAnswer("Scanning the current map evidence...");
      drawAgentScan(preview, action);
      fetch("/api/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: question,
          context: context
        })
      })
        .then(function(resp) {
          return resp.json().then(function(data) {
            return { ok: resp.ok, status: resp.status, data: data };
          });
        })
        .then(function(result) {
          finishAgentRun(runId, startedAt, function() {
            var source = result.data && result.data.source;
            setAgentStatus(source === "openai" ? "AI agent" : "local agent");
            if (!result.ok) {
              if (preview) drawAgentHighlight(preview, action);
              setAgentAnswer(result.data.error || "Agent request failed.");
              return;
            }
            var highlight = result.data.highlight || preview;
            if (highlight) {
              drawAgentHighlight(highlight, action);
            }
            setAgentAnswer(
              result.data.answer || "The agent returned no answer."
            );
          });
        })
        .catch(function() {
          finishAgentRun(runId, startedAt, function() {
            setAgentStatus("local agent");
            if (preview) drawAgentHighlight(preview, action);
            setAgentAnswer(
              "Highlighted a local candidate. Start scripts/serve_agent.py from localhost for the full agent answer."
            );
          });
        });
    }

    function runPointSunAction(action) {
      if (!state.userLocation) {
        var center = map.getCenter();
        setAgentLocation(center.lat, center.lng, null, "map center");
      }
      var loc = state.userLocation;
      var kind = action === "point-sun" ? "sun" : "shade";
      var runId = state.agentRunId + 1;
      state.agentRunId = runId;
      var startedAt = Date.now();
      var preview = {
        lat: loc.lat,
        lon: loc.lon,
        radiusM: POINT_CHECK_RADIUS_M,
        label: kind === "sun" ? "Checking sun" : "Checking shade",
        kind: kind
      };
      setAgentBusy(action, true);
      setAgentStatus("working...");
      setAgentAnswer("");
      drawAgentScan(preview, action);
      requestAnimationFrame(function() {
        var analysis = analyzePointSun(loc.lat, loc.lon);
        var best = kind === "sun"
          ? analysis.bestBright : analysis.bestShade;
        finishAgentRun(runId, startedAt, function() {
          setAgentStatus("computed");
          if (best) {
            state.slot = best.slot;
            rangeEl.value = best.slot;
            updateScene();
          }
          drawAgentHighlight({
            lat: loc.lat,
            lon: loc.lon,
            radiusM: POINT_CHECK_RADIUS_M,
            label: best
              ? (kind === "sun"
                  ? "Sun " + formatPct(best.brightCoverage)
                    + " at " + formatHour(best.slot)
                  : "Shadow " + formatPct(best.coverage)
                    + " at " + formatHour(best.slot))
              : (kind === "sun" ? "No day sun" : "No building shade"),
            kind: kind,
            coverage: best
              ? (kind === "sun" ? best.brightCoverage : best.coverage)
              : 0
          }, kind);
          renderPointWindowTrack(analysis, kind, best);
          setAgentAnswer("");
        });
      });
    }

    function runPointNightAction(action) {
      if (!state.userLocation) {
        var center = map.getCenter();
        setAgentLocation(center.lat, center.lng, null, "map center");
      }
      var loc = state.userLocation;
      var kind = action === "point-zero" ? "zero" : "active";
      var runId = state.agentRunId + 1;
      state.agentRunId = runId;
      var startedAt = Date.now();
      var preview = {
        lat: loc.lat,
        lon: loc.lon,
        radiusM: NIGHT_ACTIVITY_RADIUS_M,
        label: kind === "zero" ? "Checking inactive time" : "Checking active time",
        kind: kind
      };
      setAgentBusy(action, true);
      setAgentStatus("working...");
      setAgentAnswer("");
      drawAgentScan(preview, action);
      requestAnimationFrame(function() {
        var analysis = analyzePointNight(loc.lat, loc.lon, kind);
        var best = kind === "zero"
          ? firstMatchingNightSlot(analysis.slots, "zero", state.slot)
          : analysis.bestOpen || analysis.best;
        finishAgentRun(runId, startedAt, function() {
          setAgentStatus("computed");
          renderPointWindowTrack(analysis, kind, best);
          if (best) {
            state.slot = best.slot;
            rangeEl.value = best.slot;
            updateScene();
          }
          drawAgentHighlight({
            lat: loc.lat,
            lon: loc.lon,
            radiusM: NIGHT_ACTIVITY_RADIUS_M,
            label: best
              ? "Activity " + Math.round(best.activityScore || 0)
                + " points at "
                + formatHour(best.slot)
              : (kind === "zero" ? "No zero window" : "Activity 0 points"),
            kind: kind,
            coverage: best ? best.activityScore / ACTIVITY_MAX_SCORE : 0
          }, kind);
          setAgentAnswer("");
        });
      });
    }

    function runNearbyActiveAction(action) {
      if (!state.userLocation) {
        var center = map.getCenter();
        setAgentLocation(center.lat, center.lng, null, "map center");
      }
      var ref = activeReferencePoint();
      var runId = state.agentRunId + 1;
      state.agentRunId = runId;
      var startedAt = Date.now();
      var preview = {
        lat: ref.lat,
        lon: ref.lon,
        radiusM: NIGHT_ACTIVITY_RADIUS_M,
        label: "Checking nearby activity",
        kind: "nearby-active"
      };
      setAgentBusy(action, true);
      setAgentStatus("working...");
      setAgentAnswer("");
      drawAgentScan(preview, action);
      requestAnimationFrame(function() {
        var candidates = makeNearbyActiveCandidates();
        var top = candidates && candidates.length ? candidates[0] : null;
        finishAgentRun(runId, startedAt, function() {
          setAgentStatus("computed");
          if (!top || !top.best ||
              top.best.activityScore < NIGHT_ACTIVE_SCORE) {
            setAgentAnswer("");
            return;
          }
          top.analysis.nearbyDistanceM = Math.round(top.distanceM);
          state.slot = top.best.slot;
          rangeEl.value = top.best.slot;
          updateScene();
          renderPointWindowTrack(top.analysis, "nearby-active", top.best);
          drawAgentHighlight({
            lat: top.lat,
            lon: top.lon,
            radiusM: top.radiusM,
            label: "Activity " + Math.round(top.best.activityScore)
              + " points at " + formatHour(top.best.slot),
            kind: "nearby-active",
            coverage: top.best.activityScore / ACTIVITY_MAX_SCORE
          }, "nearby-active");
          setAgentAnswer("");
        });
      });
    }

    var agentChips = document.querySelectorAll(".lm-agent-chip");
    for (var ac = 0; ac < agentChips.length; ac++) {
      agentChips[ac].addEventListener("click", function() {
        var action = this.getAttribute("data-action") || "view";
        if (action === "point-shade" || action === "point-sun") {
          runPointSunAction(action);
        } else if (action === "point-active" || action === "point-zero") {
          runPointNightAction(action);
        } else if (action === "shade" || action === "bright") {
          ensureReferenceLocation(function() { runAgentAction(action); });
        } else {
          runAgentAction(action);
        }
      });
    }
    map.on("click", function(e) {
      setAgentLocation(e.latlng.lat, e.latlng.lng, null, "map pin");
      setAgentAnswer("");
    });

    // Pan or zoom changes the viewport, so the culled shadow set must
    // be recomputed. moveend fires once at the end of a gesture.
    map.on("moveend", function() {
      shadowCache.clear();
      scheduleRender();
    });
    map.on("zoomend", function() {
      applyStreetlightGlowZoom();
    });

    playEl.addEventListener("click", function() {
      if (state.playing) {
        clearInterval(state.playTimer);
        state.playTimer = null;
        state.playing = false;
        playIconEl.textContent = "\u25B6";
      } else {
        if (state.slot >= TIMELINE_END_SLOT) return;
        // Fixed 0.5 s cadence regardless of per-slot render cost.
        state.playTimer = setInterval(function() {
          if (state.slot >= TIMELINE_END_SLOT) {
            clearInterval(state.playTimer);
            state.playTimer = null;
            state.playing = false;
            playIconEl.textContent = "\u25B6";
            return;
          }
          state.slot += 1;
          updateScene();
        }, 500);
        state.playing = true;
        playIconEl.textContent = "\u23F8";
      }
    });

    loadCurrentCityTime();
    updateSunMarkers();
    updateScene();
    fetchWeather(state.dateStr);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
</script>
"""
    slider_js = (slider_js_template
        .replace("__BUILDINGS__",
                 json.dumps(js_buildings, separators=(",", ":")))
        .replace("__POIS__",
                 json.dumps(osm_pois, separators=(",", ":")))
        .replace("__LIGHT_POINTS__",
                 json.dumps(coords, separators=(",", ":")))
        .replace("__TREES_PNG_URL__", _trees_png_relpath)
        .replace("__TREES_BBOX__",
                 json.dumps(trees_png_bbox, separators=(",", ":")))
        .replace("__COOLING__",
                 json.dumps(cooling, separators=(",", ":")))
        .replace("__HEAT_TMAX_F__", str(HEAT_TMAX_F))
        .replace("__HEAT_APPARENT_F__", str(HEAT_APPARENT_F))
        .replace("__HEAT_UV__", str(HEAT_UV))
        .replace("__STATIC_COUNTS__", json.dumps({
            "buildings": building_count,
            "trees": len(trees_static),
            "streetlights": len(coords),
            "venues": len(osm_pois),
            "coolingOptions": len(cooling),
        }, separators=(",", ":")))
        .replace("__MAP_NAME__", m.get_name())
        .replace("__DARK_NAME__", dark_tiles.get_name())
        .replace("__STREET_NAME__", streetlight_group.get_name())
        .replace("__INITIAL_DATE__", target_time.strftime("%Y-%m-%d"))
        .replace("__CENTER_LAT__", str(MAP_CENTER[0]))
        .replace("__CENTER_LON__", str(MAP_CENTER[1]))
        .replace("__CITY_NAME__", json.dumps(CITY.display_name))
        .replace("__CITY_TIMEZONE__", json.dumps(CITY.timezone)))
    slider_html = slider_html.replace(
        "__INITIAL_DATE__", target_time.strftime("%Y-%m-%d")
    )

    m.get_root().html.add_child(
        folium.Element(slider_css + slider_html + slider_js)
    )

    _add_info_panel(m, [
        "<b>LightMap</b> &mdash; Shade by day. Light by night."
        '<span id="lm-weather" style="display:none;"></span>',
    ])

    return m


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def build_map(target_time, scale_pct=1, dual=False, time_compare=False,
              time_slider=False,
              render_strategy=DEFAULT_RENDER_STRATEGY, out_filename=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    # Each strategy writes to its own HTML file so benchmark runs can
    # generate the full matrix without clobbering each other. The
    # time-slider writes the public LightMap artifact by default.
    if out_filename is None:
        if time_slider:
            out_filename = "LightMap.html"
        elif render_strategy == DEFAULT_RENDER_STRATEGY:
            out_filename = "prototype.html"
        else:
            out_filename = f"prototype_{render_strategy}.html"
    out_path = os.path.join(OUT_DIR, out_filename)

    if time_slider:
        m = build_time_slider_map(target_time, scale_pct)
    elif time_compare:
        m = build_time_map(target_time, scale_pct)
    elif dual:
        m = build_dual_map(target_time, scale_pct)
    else:
        altitude, azimuth = get_sun_position(
            target_time, lat=MAP_CENTER[0], lon=MAP_CENTER[1]
        )
        is_day = altitude > 0
        mode = "Day" if is_day else "Night"
        print(f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"Sun: altitude={altitude:.1f}, azimuth={azimuth:.1f}")
        print(f"Mode: {mode} | Scale: {scale_pct}%")
        print(f"Render strategy: {render_strategy}")

        if is_day:
            m = build_day_map(target_time, altitude, azimuth, scale_pct,
                              render_strategy=render_strategy)
        else:
            m = build_night_map(target_time, altitude, azimuth, scale_pct)

        if m is not None:
            folium.LayerControl().add_to(m)

    if m is None:
        return None

    m.save(out_path)
    print(f"Saved: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="LightMap")
    parser.add_argument(
        "--time", type=str, default="2026-04-20 14:00",
        help="Target time (YYYY-MM-DD HH:MM)",
    )
    parser.add_argument(
        "--city", default=DEFAULT_CITY_ID,
        help="City profile id under cities/. Default: boston-cambridge.",
    )
    parser.add_argument(
        "--night", action="store_true",
        help="Force night mode (2026-04-20 22:00)",
    )
    parser.add_argument(
        "--scale", type=int, default=1, choices=[0, 1, 10, 50, 100],
        help="Percent of data to use (0=1 each, 1, 10, 50, 100)",
    )
    parser.add_argument(
        "--dual", action="store_true",
        help="Dual map: day (left) + night (right) side by side",
    )
    parser.add_argument(
        "--time-compare", action="store_true",
        help="Shadow animation across 6 time steps (7 AM - 5 PM)",
    )
    parser.add_argument(
        "--time-slider", action="store_true",
        help="24-hour slider. Shadows by day, streetlights by night, "
             "auto day/night swap on sunrise/sunset",
    )
    parser.add_argument(
        "--render-strategy", default=DEFAULT_RENDER_STRATEGY,
        choices=list(RENDER_STRATEGIES.keys()),
        help="Browser-side rendering strategy. See RENDER_STRATEGIES. "
             f"Default: {DEFAULT_RENDER_STRATEGY}.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output filename under docs/. Defaults to LightMap.html for "
             "the time-slider.",
    )
    args = parser.parse_args()

    set_active_city(load_city_profile(args.city))

    if args.night:
        target_time = datetime(2026, 4, 20, 22, 0, tzinfo=LOCAL_TZ)
    else:
        target_time = datetime.strptime(args.time, "%Y-%m-%d %H:%M")
        target_time = target_time.replace(tzinfo=LOCAL_TZ)

    build_map(target_time, args.scale, dual=args.dual,
              time_compare=args.time_compare,
              time_slider=args.time_slider,
              render_strategy=args.render_strategy,
              out_filename=args.out)


if __name__ == "__main__":
    main()
