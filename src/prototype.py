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
from folium.features import GeoJsonPopup, GeoJsonTooltip
from folium.plugins import (
    DualMap, Fullscreen, Geocoder, HeatMap, MiniMap, MousePosition,
    TimestampedGeoJson,
)
from shapely.geometry import mapping
from shapely.wkb import loads as wkb_loads


def wkb_loads_batch(wkb_blobs):
    """Decode many WKB blobs in one shapely ufunc call."""
    return shapely_module.from_wkb([bytes(b) for b in wkb_blobs]).tolist()

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
    INITIAL_BBOX,
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

BOSTON_TZ = ZoneInfo("US/Eastern")
MAP_CENTER = [42.3601, -71.0942]  # MIT Kresge Oval, inside INITIAL_BBOX
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")

BOSTON_BUILDINGS_PATH = os.path.join(DATA_DIR, "buildings", "boston_buildings.geojson")
CAMBRIDGE_BUILDINGS_PATH = os.path.join(
    DATA_DIR, "cambridge", "buildings", "buildings.geojson"
)
BUILDINGS_DB_PATH = os.path.join(DATA_DIR, "buildings.db")
OSM_POIS_PATH = os.path.join(DATA_DIR, "osm", "pois.geojson")
MEDICAL_PATH = os.path.join(DATA_DIR, "osm", "medical.geojson")
COOLING_PATH = os.path.join(DATA_DIR, "cooling", "cooling.geojson")
TREES_PATH = os.path.join(DATA_DIR, "trees", "trees.geojson")
CRIME_PATH = os.path.join(DATA_DIR, "safety", "crime.geojson")
CRASH_PATH = os.path.join(DATA_DIR, "safety", "crashes.geojson")

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
    for city in ("cambridge", "boston"):
        rows = c.execute(
            "SELECT height_ft, geom FROM buildings WHERE city = ?", (city,)
        ).fetchall()

        if scale_pct == 0:
            # Still support "1 each" mode -- need JSON path for tallest-near-center
            # Fall back to JSON for scale=0 since it's a rarely used debug mode
            conn.close()
            return None

        n = _sample_count(len(rows), scale_pct)
        sampled = random.sample(rows, min(n, len(rows)))
        print(f"  {city.capitalize()} buildings: {len(sampled)}/{len(rows)}")
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
                "properties": {"BLDG_HGT_2010": round(float(height_ft), 1)},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords],
                },
            })

    conn.close()
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

    if os.path.exists(CAMBRIDGE_BUILDINGS_PATH):
        with open(CAMBRIDGE_BUILDINGS_PATH) as f:
            data = json.load(f)
        valid = []
        for feat in data["features"]:
            props = feat.get("properties", {})
            top_gl = props.get("TOP_GL")
            if top_gl is not None and top_gl > 0:
                new_feat = {
                    "type": "Feature",
                    "properties": {"BLDG_HGT_2010": round(top_gl * 3.28084, 1)},
                    "geometry": feat["geometry"],
                }
                valid.append(new_feat)
        if scale_pct == 0:
            sampled = [_pick_tallest_near_center(valid)]
        else:
            n = _sample_count(len(valid), scale_pct)
            sampled = random.sample(valid, min(n, len(valid)))
        features.extend(sampled)
        print(f"  Cambridge buildings: {len(sampled)}/{len(valid)}")

    if os.path.exists(BOSTON_BUILDINGS_PATH):
        with open(BOSTON_BUILDINGS_PATH) as f:
            data = json.load(f)
        valid = []
        for feat in data["features"]:
            props = feat.get("properties", {})
            h = props.get("BLDG_HGT_2010")
            if h is not None and h > 0:
                valid.append(feat)
        if scale_pct == 0:
            sampled = [_pick_tallest_near_center(valid)]
        else:
            n = _sample_count(len(valid), scale_pct)
            sampled = random.sample(valid, min(n, len(valid)))
        features.extend(sampled)
        print(f"  Boston buildings: {len(sampled)}/{len(valid)}")
    else:
        print("  Boston buildings: not downloaded")

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

    boston_path = os.path.join(DATA_DIR, "streetlights", "streetlights.csv")
    if os.path.exists(boston_path):
        all_boston = []
        with open(boston_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    lat = float(row["Lat"])
                    lon = float(row["Long"])
                    if 42.2 < lat < 42.5 and -71.2 < lon < -70.9:
                        all_boston.append([lat, lon])
                except (ValueError, KeyError):
                    continue
        n = _sample_count(len(all_boston), scale_pct)
        sampled = random.sample(all_boston, min(n, len(all_boston)))
        coords.extend(sampled)
        print(f"  Boston streetlights: {len(sampled)}/{len(all_boston)}")

    cam_path = os.path.join(DATA_DIR, "cambridge", "streetlights", "streetlights.geojson")
    if os.path.exists(cam_path):
        all_cam = []
        with open(cam_path) as f:
            data = json.load(f)
        for feat in data["features"]:
            geom = feat.get("geometry", {})
            if geom.get("type") == "Point":
                lon, lat = geom["coordinates"][:2]
                if 42.2 < lat < 42.5 and -71.2 < lon < -70.9:
                    all_cam.append([lat, lon])
        n = _sample_count(len(all_cam), scale_pct)
        sampled = random.sample(all_cam, min(n, len(all_cam)))
        coords.extend(sampled)
        print(f"  Cambridge streetlights: {len(sampled)}/{len(all_cam)}")

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
    """Load Boston crash records (Vision Zero) as list of dicts with
    lat/lon/mode."""
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
    """Load tree canopy polygons from the Cambridge GIS snapshot.

    Each feature keeps its outer ring + a `height_m` property. The
    time-slider shadow engine treats these as short buildings and
    projects shadows from them the same way it does for real
    buildings, so the shade layer includes tree shade automatically.
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
    path = os.path.join(DATA_DIR, "safety", "food_establishments.csv")
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
                if 42.2 < lat < 42.5 and -71.2 < lon < -70.9:
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
    shadows, _, _ = compute_all_shadows(parsed, target_time)
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
    # Default pan bounds are INITIAL_BBOX (MIT + central Cambridge + the
    # Boston core) so day and night renderers share the same frame. A
    # caller can still pass wider bounds if a specific view needs them.
    #
    # lock_zoom=True pins the *minimum* zoom at zoom_start so the user
    # cannot scroll out of the prepared frame while scrubbing the slider,
    # but still allows zooming in (up to 18) for a closer look. Zoom
    # interactions stay enabled in both modes.
    zoom_start = 16
    min_z = zoom_start if lock_zoom else 15
    max_z = 18
    m = folium.Map(
        location=MAP_CENTER, zoom_start=zoom_start, tiles=tiles,
        width="100%", height="100%",
        prefer_canvas=prefer_canvas,
        # min_zoom is 1 level below zoom_start so users can pull back
        # slightly for a wider overview but not so far that the shadow
        # redraw cost explodes. max_zoom matches what CartoDB Positron
        # and Dark Matter serve natively (18).
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
        tooltip=GeoJsonTooltip(
            fields=["BLDG_HGT_2010"],
            aliases=["Height (ft):"],
            style="font-size:12px;",
        ),
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
        popup_html = (
            '<div style="font-family:sans-serif; font-size:12px; min-width:120px;">'
            f'<b>{p["name"]}</b></div>'
        )
        folium.CircleMarker(
            location=[p["lat"], p["lon"]],
            radius=5,
            color="#fde68a",
            weight=2,
            opacity=0.55,
            fill=True,
            fill_color="#fbbf24",
            fill_opacity=0.9,
            popup=folium.Popup(popup_html, max_width=200),
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
      <b>where streetlights shine</b> at night across Boston and Cambridge.</p>
    <ul style="font-size:13px; line-height:1.8; padding-left:20px; margin:0 0 16px 0;">
      <li><b>Hover</b> on a building to see its height</li>
      <li><b>Click</b> a shadow to see building and shadow details</li>
      <li>Use the <b>search box</b> (top-right) to find an address</li>
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
    Geocoder(collapsed=True, position="topright").add_to(m)
    m.get_root().html.add_child(folium.Element(ONBOARDING_HTML))


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
        f" border-radius:8px; font-family:sans-serif; font-size:13px;"
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
        "Building heights: Boston 2010, Cambridge 2018</span>",
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
        "Source: data.boston.gov, Cambridge GIS</span>",
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
            hour, 0, tzinfo=BOSTON_TZ,
        )
        timestamp = step_time.strftime("%Y-%m-%dT%H:%M:%S")
        alt, az = get_sun_position(step_time)
        print(f"  {step_time.strftime('%H:%M')}: alt={alt:.1f}, az={az:.1f}")

        if alt <= 0:
            print(f"    Skipped (sun below horizon)")
            continue

        shadows, _, _ = compute_all_shadows(parsed, step_time)
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
        14, 0, tzinfo=BOSTON_TZ,
    )
    night_time = datetime(
        target_time.year, target_time.month, target_time.day,
        22, 0, tzinfo=BOSTON_TZ,
    )
    day_alt, _ = get_sun_position(day_time)

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
        '<span style="color:#94a3b8; font-size:10px;">Heights: Boston 2010, Cambridge 2018</span>',
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
    # the Boston/Cambridge core that the app is actually about.
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
    _trees_png_relpath = "trees_canopy.png"
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

    print("Loading emergency rooms...")
    medical = [m for m in load_medical()
               if _in_bbox_latlon(m["lat"], m["lon"])]
    print(f"  Inside INITIAL_BBOX: {len(medical)} ERs")

    print("Loading cooling centers (proxy)...")
    cooling = [c for c in load_cooling_centers()
               if _in_bbox_latlon(c["lat"], c["lon"])]
    print(f"  Inside INITIAL_BBOX: {len(cooling)} cooling")

    print("Loading safety data...")
    crime_points = [c for c in load_safety_crime()
                    if _in_bbox_latlon(c[0], c[1])]
    print(f"  Inside INITIAL_BBOX: {len(crime_points)} crime points")
    violent_crime = [c for c in load_violent_crime()
                     if _in_bbox_latlon(c["lat"], c["lon"])]
    print(f"  Inside INITIAL_BBOX: {len(violent_crime)} violent crime incidents")

    # ----- Building-coverage mask -----
    # Areas inside INITIAL_BBOX that have no building data are masked
    # out visually AND filtered from every point-based layer. The user
    # should see no crime, ER, venue, cooling, or streetlight info on
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

    _before = (len(coords), len(osm_pois), len(medical), len(cooling),
               len(crime_points), len(violent_crime))
    coords = [c for c in coords if _in_cov(c[0], c[1])]
    osm_pois = [p for p in osm_pois if _in_cov(p["lat"], p["lon"])]
    medical = [m_ for m_ in medical if _in_cov(m_["lat"], m_["lon"])]
    cooling = [c for c in cooling if _in_cov(c["lat"], c["lon"])]
    crime_points = [c for c in crime_points if _in_cov(c[0], c[1])]
    violent_crime = [c for c in violent_crime if _in_cov(c["lat"], c["lon"])]
    print(f"  After mask filter:")
    print(f"    streetlights {_before[0]} -> {len(coords)}")
    print(f"    POIs {_before[1]} -> {len(osm_pois)}")
    print(f"    medical {_before[2]} -> {len(medical)}")
    print(f"    cooling {_before[3]} -> {len(cooling)}")
    print(f"    crime points {_before[4]} -> {len(crime_points)}")
    print(f"    violent crime {_before[5]} -> {len(violent_crime)}")

    # Coord precision trim (~1 m) for everything that gets embedded.
    # No feature is dropped — only the coordinate string is shorter.
    coords = [[round(lat, 5), round(lon, 5)] for lat, lon in coords]
    crime_points = [[round(lat, 5), round(lon, 5)] for lat, lon in crime_points]
    for p in osm_pois:
        p["lat"] = round(p["lat"], 5)
        p["lon"] = round(p["lon"], 5)
    for p in medical:
        p["lat"] = round(p["lat"], 5)
        p["lon"] = round(p["lon"], 5)
    for p in cooling:
        p["lat"] = round(p["lat"], 5)
        p["lon"] = round(p["lon"], 5)
    for v in violent_crime:
        v["lat"] = round(v["lat"], 5)
        v["lon"] = round(v["lon"], 5)

    m = _create_base_map("CartoDB positron", lock_zoom=True)
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
        min_zoom=16, max_zoom=18,
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

    # Safety context: nighttime crime heatmap + recent crash pins.
    # Historic aggregates covering the last two years. Visible only
    # when nightMix > 0.5 (same gate as OSM venue markers) so they do
    # not clutter the day shadow view.
    crime_group = folium.FeatureGroup(
        name="Crime (2yr, night hrs)", show=False, control=False,
    )
    if crime_points:
        # Tight radius + blur so each incident reads as a discrete
        # spot. The earlier 14 / 22 combination flooded the streets
        # with overlapping red; 6 / 8 keeps the hotspots tight and
        # lets street geometry show through. min_opacity prevents a
        # pale-red wash from painting low-density areas.
        HeatMap(
            crime_points, radius=6, blur=8, max_zoom=14,
            min_opacity=0.25,
            gradient={
                0.35: "#450a0a", 0.6: "#991b1b",
                0.85: "#dc2626", 1.0: "#fca5a5",
            },
        ).add_to(crime_group)
    crime_group.add_to(m)

    # Reuse the crash_group variable + JS name because the slider's
    # day→night toggle already wires that identifier. The layer now
    # holds violent-crime markers instead of traffic crashes; the name
    # in the UI control is the only thing the user sees.
    crash_group = folium.FeatureGroup(
        name="Violent crime (2yr)", show=False, control=False,
    )
    for c in violent_crime:
        popup_html = (
            '<div style="font-family:sans-serif; font-size:12px;">'
            f'<b>{c["type"]}</b></div>'
        )
        folium.Marker(
            location=[c["lat"], c["lon"]],
            icon=folium.DivIcon(
                icon_size=(10, 10),
                icon_anchor=(5, 5),
                html=(
                    '<div style="width:6px;height:6px;'
                    'transform:rotate(45deg);'
                    'background:#dc2626;'
                    'box-shadow:0 0 3px rgba(0,0,0,0.7);"></div>'
                ),
            ),
            popup=folium.Popup(popup_html, max_width=200),
        ).add_to(crash_group)
    crash_group.add_to(m)

    _add_ui_plugins(m, theme="light")
    legend = _make_shadow_cmap()
    legend.add_to(m)

    slider_css = """
<style>
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
    transition: background 0.4s ease, color 0.4s ease,
                box-shadow 0.4s ease;
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
    min-width: 110px;
  }
  #lm-mode-icon {
    font-size: 18px;
    line-height: 1;
    color: #a78bfa;
    transition: color 0.4s ease;
  }
  #lm-time {
    font-size: 24px;
    font-weight: 600;
    letter-spacing: 0.5px;
    color: #1e293b;
    font-variant-numeric: tabular-nums;
    line-height: 1;
    transition: color 0.4s ease;
  }
  #lm-date {
    background: transparent;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 6px 10px;
    font-family: inherit;
    font-size: 13px;
    color: inherit;
    cursor: pointer;
    outline: none;
    font-variant-numeric: tabular-nums;
    transition: border-color 0.2s, color 0.4s ease;
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
    font-size: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: background 0.2s, color 0.4s ease,
                border-color 0.2s;
  }
  #lm-play:hover { background: #e2e8f0; border-color: #94a3b8; }

  #lm-range-wrap {
    position: relative;
    padding-top: 18px;
    padding-bottom: 2px;
  }
  .lm-sun-marker {
    position: absolute;
    top: 16px;
    width: 2px;
    height: 14px;
    transform: translateX(-50%);
    pointer-events: none;
    z-index: 1;
    transition: left 0.4s ease, background 0.4s ease;
  }
  #lm-sunrise-marker { background: #fbbf24; }
  #lm-sunset-marker { background: #f97316; }
  .lm-sun-label {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
    font-size: 10px;
    font-weight: 500;
    color: #94a3b8;
    pointer-events: none;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    transition: left 0.4s ease, color 0.4s ease;
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
    transition: background 0.4s ease;
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
    transition: box-shadow 0.15s, border-color 0.3s ease;
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
    transition: box-shadow 0.15s, border-color 0.3s ease;
  }

  /* NIGHT theme overrides */
  #lm-slider-host.night {
    background: rgba(15, 23, 42, 0.92);
    color: #e2e8f0;
    box-shadow: 0 10px 32px rgba(0, 0, 0, 0.4);
  }
  #lm-slider-host.night #lm-time { color: #e2e8f0; }
  #lm-slider-host.night #lm-mode-icon { color: #fbbf24; }
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
</style>
"""

    slider_html = """
<div id="lm-slider-host">
  <div id="lm-row">
    <div id="lm-time-wrap">
      <span id="lm-mode-icon">\u2600</span>
      <span id="lm-time">--:--</span>
    </div>
    <input type="date" id="lm-date" value="__INITIAL_DATE__">
    <button id="lm-play" aria-label="Play or pause the time slider">
      <span id="lm-play-icon">\u25B6</span>
    </button>
  </div>
  <div id="lm-range-wrap">
    <div class="lm-sun-label" id="lm-sunrise-label">--:--</div>
    <div class="lm-sun-label" id="lm-sunset-label">--:--</div>
    <div class="lm-sun-marker" id="lm-sunrise-marker"></div>
    <div class="lm-sun-marker" id="lm-sunset-marker"></div>
    <input type="range" id="lm-range" min="0" max="23" step="1" value="14">
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
  var TREES_PNG_URL = "__TREES_PNG_URL__";
  var TREES_BBOX = __TREES_BBOX__;
  var MEDICAL = __MEDICAL__;
  var COOLING = __COOLING__;
  var HEAT_TMAX_F = __HEAT_TMAX_F__;
  var HEAT_APPARENT_F = __HEAT_APPARENT_F__;
  var HEAT_UV = __HEAT_UV__;
  var LAT_CENTER = __CENTER_LAT__;
  var LON_CENTER = __CENTER_LON__;
  var INITIAL_DATE = "__INITIAL_DATE__";
  var M_PER_DEG_LAT = 111320;
  var M_PER_DEG_LON = 111320 * Math.cos(LAT_CENTER * Math.PI / 180);
  var MAX_SHADOW_LENGTH = 500;
  // Dawn/dusk transition.
  //   altitude <= TWILIGHT_START : full dark (night theme locked in)
  //   TWILIGHT_START .. DAY_THRESHOLD : dark basemap fades out linearly
  //   altitude >= DAY_THRESHOLD : full day. Shadows appear from here.
  // Below DAY_THRESHOLD every building would project 200 m+ shadows
  // (cap is 500 m) and they merge into a city-wide "shadow wall".
  // Pinning the shadow gate at 15 deg altitude keeps the evening
  // transition smooth — around 18:00 in Boston summer the sun drops
  // below 15 deg, shadows switch off, and the scene gradually darkens
  // into night. Sunrise/sunset slider markers still track the true
  // altitude-0 moments.
  var TWILIGHT_START = 0;
  var DAY_THRESHOLD = 15;

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function d2r(d) { return d * Math.PI / 180; }

  // US DST rough check (2nd Sun of March to 1st Sun of November).
  // Used so Boston-local wall-clock hour on the slider maps to the
  // correct UTC moment regardless of the viewer's timezone.
  function bostonUtcOffset(year, monthIdx, day) {
    var march = new Date(Date.UTC(year, 2, 1));
    var dstStart = new Date(Date.UTC(
      year, 2, 1 + ((7 - march.getUTCDay()) % 7) + 7
    ));
    var november = new Date(Date.UTC(year, 10, 1));
    var dstEnd = new Date(Date.UTC(
      year, 10, 1 + ((7 - november.getUTCDay()) % 7)
    ));
    var today = new Date(Date.UTC(year, monthIdx, day));
    return (today >= dstStart && today < dstEnd) ? -4 : -5;
  }

  // Returns a Date whose UTC moment equals Boston wall-clock
  // {year, month, day, hour, min}.
  function bostonDate(year, monthIdx, day, hour, min) {
    var off = bostonUtcOffset(year, monthIdx, day);
    return new Date(Date.UTC(year, monthIdx, day, hour - off, min));
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
    var crime = __CRIME_NAME__;
    var crash = __CRASH_NAME__;
    var host = document.getElementById("lm-slider-host");
    var rangeEl = document.getElementById("lm-range");
    var dateEl = document.getElementById("lm-date");
    var timeEl = document.getElementById("lm-time");
    var iconEl = document.getElementById("lm-mode-icon");
    var playEl = document.getElementById("lm-play");
    var playIconEl = document.getElementById("lm-play-icon");
    var srMarker = document.getElementById("lm-sunrise-marker");
    var ssMarker = document.getElementById("lm-sunset-marker");
    var srLabel = document.getElementById("lm-sunrise-label");
    var ssLabel = document.getElementById("lm-sunset-label");
    if (!host || !rangeEl || !dateEl) { setTimeout(setup, 100); return; }

    var state = {
      dateStr: INITIAL_DATE, slot: parseInt(rangeEl.value, 10),
      playing: false, playTimer: null, shadowLayer: null,
      hadShadows: false
    };

    function parseDateStr(s) {
      var parts = s.split("-").map(Number);
      return { y: parts[0], m: parts[1] - 1, d: parts[2] };
    }

    function sunAt(dateStr, slot) {
      var p = parseDateStr(dateStr);
      var hour = slot;
      var min = 0;
      var d = bostonDate(p.y, p.m, p.d, hour, min);
      var pos = SunCalc.getPosition(d, LAT_CENTER, LON_CENTER);
      // SunCalc: azimuth measured from south, west-positive.
      // Convert to compass bearing (from north, clockwise) to match
      // pvlib / compute.py convention.
      return {
        alt: pos.altitude * 180 / Math.PI,
        az: (pos.azimuth * 180 / Math.PI + 180 + 360) % 360,
        hour: hour, min: min
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

    // Single canvas-backed shadow layer, constructed once. Each tick
    // we clear + refill it via addData, which avoids the per-tick
    // Leaflet bookkeeping of removing and re-registering a fresh
    // L.GeoJSON from scratch. The explicit canvas renderer keeps the
    // render path off SVG even when the map's preferCanvas default is
    // shadowed by plugin layers.
    var shadowCanvas = L.canvas({ padding: 0.5 });
    var shadowStyle = function(f) {
      // Tree shadows get a muted dark green so they read as foliage
      // shade rather than a building casting — the tint mimics the
      // way light filtered through leaves comes out slightly green.
      // Opacity stays fixed at a softer level than buildings so tree
      // shade layers under building shade rather than competing.
      if (f.properties.kind === "t") {
        return {
          fillColor: "#1e3a2a", color: "#1e3a2a",
          weight: 0, fillOpacity: 0.32, opacity: 0.32
        };
      }
      // Building shadows: dark slate, opacity scales with height so
      // tall towers cast visually darker shade than short rows.
      var h = f.properties.h;
      var op = Math.min(0.18 + (h / 60) * 0.22, 0.45);
      return {
        fillColor: "#0f172a", color: "#0f172a",
        weight: 0.2, fillOpacity: op, opacity: op
      };
    };
    state.shadowLayer = L.geoJson(null, {
      interactive: false,
      renderer: shadowCanvas,
      style: shadowStyle
    }).addTo(map);

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
      interactive: true,
      pointToLayer: function(feat, latlng) {
        // L.marker with DivIcon lands in markerPane (z-index 600),
        // above both the streetlight heatmap (overlayPane) and the
        // shadow canvas, so the green dot stays legible at night.
        return L.marker(latlng, {
          icon: L.divIcon({
            className: 'lm-poi',
            iconSize: [16, 16],
            iconAnchor: [8, 8],
            html: (
              '<div style="width:11px;height:11px;border-radius:50%;'
              + 'background:#facc15;border:1.5px solid #422006;'
              + 'box-shadow:0 0 7px rgba(250,204,21,0.95);"></div>'
            )
          })
        });
      },
      onEachFeature: function(feat, layer) {
        var props = feat.properties;
        var html = '<div style="font-family:sans-serif;font-size:12px;'
                 + 'min-width:140px;"><b>' + (props.name || "(unnamed)")
                 + '</b><br><span style="color:#64748b;">'
                 + props.amenity + '</span><br>'
                 + '<span style="color:#475569;font-size:11px;">'
                 + props.hours + '</span></div>';
        layer.bindPopup(html, { maxWidth: 220 });
      }
    }).addTo(map);

    // Construct a Date whose LOCAL hour/minute match the slider slot,
    // so opening_hours.js (which uses local getHours/getDay) reads the
    // Boston wall-clock values regardless of the viewer's own timezone.
    function ohDate(dateStr, slot) {
      var p = parseDateStr(dateStr);
      return new Date(p.y, p.m, p.d, slot, 0, 0);
    }

    // Cache the feature list per (date, slot) so repeat scrubs skip
    // the getState loop. Invalidated on date change (different sun).
    var poiCache = new Map();

    function renderPois(altDeg) {
      // POI visibility mirrors the slider's day/night chrome flip so
      // the moment the slider icon turns to a sun, the yellow markers
      // vanish. That crossover is at nightMix = 0.5, i.e. altitude
      // (TWILIGHT_START + DAY_THRESHOLD) / 2. Keeping the markers on
      // past that point looked like a glitch against the daytime
      // basemap.
      poiLayer.clearLayers();
      var nightMix;
      if (altDeg <= TWILIGHT_START) nightMix = 1;
      else if (altDeg >= DAY_THRESHOLD) nightMix = 0;
      else nightMix = 1 - (altDeg - TWILIGHT_START) /
                          (DAY_THRESHOLD - TWILIGHT_START);
      if (nightMix <= 0.5) return;
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
        if (poiCache.size >= 24) {
          poiCache.delete(poiCache.keys().next().value);
        }
        poiCache.set(key, feats);
      }
      poiLayer.addData({
        type: "FeatureCollection", features: feats
      });
    }

    // Per-slot feature cache. Key = "YYYY-MM-DD:slot". Populated lazily
    // on first visit to a slot; subsequent visits skip the convex-hull
    // loop entirely. Viewport changes invalidate the whole cache at
    // moveend because the culled building set differs.
    var shadowCache = new Map();
    var CACHE_LIMIT = 24;

    function computeShadowFeats(altDeg, azDeg) {
      var cb = cullBounds();
      var cullW = cb[0], cullS = cb[1], cullE = cb[2], cullN = cb[3];
      var feats = [];
      for (var i = 0; i < BUILDINGS.length; i++) {
        var b = BUILDINGS[i];
        var bb = b[2];
        if (bb[2] < cullW || bb[0] > cullE ||
            bb[3] < cullS || bb[1] > cullN) continue;
        // Same sun-projection path for buildings and tree patches —
        // trees only differ in height (uniform 10 m) and kind, so
        // their shadows come out shorter and cast in the same sun
        // direction as buildings. This is what makes canopy shade
        // read as a shadow rather than a flat green overlay.
        var hull = projectShadow(b[1], b[0], altDeg, azDeg);
        if (!hull || hull.length < 3) continue;
        var ring = hull.slice();
        ring.push(hull[0]);
        feats.push({
          type: "Feature",
          geometry: { type: "Polygon", coordinates: [ring] },
          properties: { h: b[0], kind: b[3] === "t" ? "t" : "b" }
        });
      }
      return feats;
    }

    function renderShadows(altDeg, azDeg) {
      // Keep shadows off until the sun is DAY_THRESHOLD above the
      // horizon. Below that angle shadow_length = height / tan(alt)
      // explodes and every building hits the 500 m cap, flooding the
      // view with a uniform shadow wall. Static tree canopy follows
      // the same gate so the night map stays dark and uncluttered.
      if (altDeg < DAY_THRESHOLD) {
        if (state.hadShadows) {
          state.shadowLayer.clearLayers();
          state.hadShadows = false;
        }
        if (state.treeLayer && map.hasLayer(state.treeLayer)) {
          map.removeLayer(state.treeLayer);
        }
        return;
      }
      if (state.treeLayer && !map.hasLayer(state.treeLayer)) {
        state.treeLayer.addTo(map);
      }
      state.shadowLayer.clearLayers();
      state.hadShadows = true;
      var key = state.dateStr + ":" + state.slot;
      var feats;
      if (shadowCache.has(key)) {
        feats = shadowCache.get(key);
        // LRU bump.
        shadowCache.delete(key);
        shadowCache.set(key, feats);
      } else {
        feats = computeShadowFeats(altDeg, azDeg);
        if (shadowCache.size >= CACHE_LIMIT) {
          var firstKey = shadowCache.keys().next().value;
          shadowCache.delete(firstKey);
        }
        shadowCache.set(key, feats);
      }
      state.shadowLayer.addData({
        type: "FeatureCollection", features: feats
      });
    }

    function renderTheme(altDeg) {
      // nightMix goes 1 -> 0 smoothly as the sun climbs from
      // TWILIGHT_START to DAY_THRESHOLD. Below TWILIGHT_START the sky
      // is solid night; above DAY_THRESHOLD the day basemap stands on
      // its own. The dark tile fades by opacity so the scene brightens
      // gradually. Streetlights + food stay on through the transition
      // and clear out at DAY_THRESHOLD.
      var nightMix;
      if (altDeg <= TWILIGHT_START) nightMix = 1;
      else if (altDeg >= DAY_THRESHOLD) nightMix = 0;
      else nightMix = 1 - (altDeg - TWILIGHT_START) /
                          (DAY_THRESHOLD - TWILIGHT_START);

      if (nightMix > 0) {
        if (!map.hasLayer(dark)) dark.addTo(map);
        dark.setOpacity(nightMix);
        if (!map.hasLayer(lights)) lights.addTo(map);
      } else {
        if (map.hasLayer(dark)) map.removeLayer(dark);
        if (map.hasLayer(lights)) map.removeLayer(lights);
      }

      // Slider chrome flips on dominant mix. The 0.5 crossover lines
      // up with altitude -0.5 (roughly true sunrise/sunset) so the
      // slider color aligns with the marker position.
      //
      // Same gate drives the night-only "safety context" overlays
      // (crime heatmap + recent crash pins) so they appear and
      // disappear together with the slider's moon icon.
      if (nightMix > 0.5) {
        host.classList.add("night");
        iconEl.textContent = "\u263E";
        if (!map.hasLayer(crime)) crime.addTo(map);
        if (!map.hasLayer(crash)) crash.addTo(map);
      } else {
        host.classList.remove("night");
        iconEl.textContent = "\u2600";
        if (map.hasLayer(crime)) map.removeLayer(crime);
        if (map.hasLayer(crash)) map.removeLayer(crash);
      }
    }

    function updateSunMarkers() {
      var p = parseDateStr(state.dateStr);
      // Noon Boston time is a safe reference for sunrise/sunset.
      var noon = bostonDate(p.y, p.m, p.d, 12, 0);
      var t = SunCalc.getTimes(noon, LAT_CENTER, LON_CENTER);
      if (!t.sunrise || isNaN(t.sunrise.getTime())) return;
      var off = bostonUtcOffset(p.y, p.m, p.d);
      // Convert UTC to Boston local hour/min so the slider mapping
      // (slot = local hour) is consistent.
      function localHm(dt) {
        var utcH = dt.getUTCHours() + (dt.getUTCMinutes() / 60);
        var localH = (utcH + off + 24) % 24;
        var hh = Math.floor(localH);
        var mm = Math.round((localH - hh) * 60);
        if (mm === 60) { hh = (hh + 1) % 24; mm = 0; }
        return [hh, mm];
      }
      var sr = localHm(t.sunrise);
      var ss = localHm(t.sunset);
      var srFrac = sr[0] + sr[1] / 60;
      var ssFrac = ss[0] + ss[1] / 60;
      // Slot 0..23 maps to hours 0..23. The slider thumb travels
      // across the full track, so position 0% = hour 0, 100% = hour 23.
      var pctSr = (srFrac / 23) * 100;
      var pctSs = (ssFrac / 23) * 100;
      srMarker.style.left = pctSr + "%";
      ssMarker.style.left = pctSs + "%";
      srLabel.style.left = pctSr + "%";
      ssLabel.style.left = pctSs + "%";
      srLabel.textContent = pad(sr[0]) + ":" + pad(sr[1]);
      ssLabel.textContent = pad(ss[0]) + ":" + pad(ss[1]);
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
        renderTheme(s.alt);
        renderShadows(s.alt, s.az);
        renderPois(s.alt);
      });
    }

    function updateScene() {
      var s = sunAt(state.dateStr, state.slot);
      timeEl.textContent = pad(s.hour) + ":" + pad(s.min);
      rangeEl.value = state.slot;
      scheduleRender();
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
        + "&timezone=America%2FNew_York"
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
    // 3-step fallback: shade -> cooling -> ER. ER markers always
    // visible (24h). Cooling markers appear only when weather fetch
    // crosses HEAT_* thresholds. Info panel badge flags the state.
    var erLayer = L.layerGroup();
    for (var ei = 0; ei < MEDICAL.length; ei++) {
      var er = MEDICAL[ei];
      // Only 24-hour ERs render. Non-emergency hospitals are kept in
      // the data but hidden — out of scope for the heat-response
      // 3-step fallback (shade -> cooling -> ER).
      if (!er.is_er) continue;
      var erIcon = L.divIcon({
        className: 'lm-er',
        iconSize: [14, 14], iconAnchor: [7, 7],
        html: '<div style="background:#dc2626;color:#fff;'
            + 'border:1.5px solid #fff;border-radius:3px;'
            + 'width:14px;height:14px;display:flex;'
            + 'align-items:center;justify-content:center;'
            + 'font-weight:900;font-size:11px;'
            + 'box-shadow:0 1px 3px rgba(0,0,0,0.35);">+</div>'
      });
      var erM = L.marker([er.lat, er.lon], { icon: erIcon });
      var erPop = '<div style="font-family:sans-serif;font-size:12px;'
                + 'min-width:160px;"><b>' + (er.name || "Emergency Room")
                + '</b><br><span style="color:#dc2626;font-weight:700;">'
                + '24-hour ER</span>';
      if (er.addr) erPop += '<br><span style="color:#64748b;">'
                          + er.addr + '</span>';
      if (er.phone) erPop += '<br><span style="color:#64748b;">'
                           + er.phone + '</span>';
      erPop += '</div>';
      erM.bindPopup(erPop, { maxWidth: 220 });
      erM.addTo(erLayer);
    }
    erLayer.addTo(map);

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
      var cPop = '<div style="font-family:sans-serif;font-size:12px;'
               + 'min-width:140px;"><b>' + (cl.name || "Cooling")
               + '</b><br><span style="color:#0891b2;font-weight:700;">'
               + 'Cooling option</span>'
               + '<br><span style="color:#64748b;font-size:11px;">'
               + cl.amenity + ' (OSM proxy)</span></div>';
      cM.bindPopup(cPop, { maxWidth: 220 });
      cM.addTo(coolingLayer);
    }

    var heatOn = false;
    function applyHeatState(on) {
      if (on === heatOn) return;
      heatOn = !!on;
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

    fetchWeather(state.dateStr);

    // Pan or zoom changes the viewport, so the culled shadow set must
    // be recomputed. moveend fires once at the end of a gesture.
    map.on("moveend", function() {
      shadowCache.clear();
      scheduleRender();
    });

    playEl.addEventListener("click", function() {
      if (state.playing) {
        clearInterval(state.playTimer);
        state.playTimer = null;
        state.playing = false;
        playIconEl.textContent = "\u25B6";
      } else {
        // Fixed 1 s cadence regardless of per-slot render cost.
        state.playTimer = setInterval(function() {
          state.slot = (state.slot + 1) % 24;
          updateScene();
        }, 1000);
        state.playing = true;
        playIconEl.textContent = "\u23F8";
      }
    });

    updateSunMarkers();
    updateScene();
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
        .replace("__TREES_PNG_URL__", _trees_png_relpath)
        .replace("__TREES_BBOX__",
                 json.dumps(trees_png_bbox, separators=(",", ":")))
        .replace("__MEDICAL__",
                 json.dumps(medical, separators=(",", ":")))
        .replace("__COOLING__",
                 json.dumps(cooling, separators=(",", ":")))
        .replace("__HEAT_TMAX_F__", str(HEAT_TMAX_F))
        .replace("__HEAT_APPARENT_F__", str(HEAT_APPARENT_F))
        .replace("__HEAT_UV__", str(HEAT_UV))
        .replace("__MAP_NAME__", m.get_name())
        .replace("__DARK_NAME__", dark_tiles.get_name())
        .replace("__STREET_NAME__", streetlight_group.get_name())
        .replace("__CRIME_NAME__", crime_group.get_name())
        .replace("__CRASH_NAME__", crash_group.get_name())
        .replace("__INITIAL_DATE__", target_time.strftime("%Y-%m-%d"))
        .replace("__CENTER_LAT__", str(MAP_CENTER[0]))
        .replace("__CENTER_LON__", str(MAP_CENTER[1])))
    slider_html = slider_html.replace(
        "__INITIAL_DATE__", target_time.strftime("%Y-%m-%d")
    )

    m.get_root().html.add_child(
        folium.Element(slider_css + slider_html + slider_js)
    )

    date_str = target_time.strftime("%b %d, %Y")
    _add_info_panel(m, [
        "<b>LightMap</b> &mdash; Time Slider",
        f'<span style="color:#64748b;">{date_str}</span>',
        f"{building_count:,} buildings &middot; {len(trees_static):,} "
        "trees (static) &middot; {:,} venues".format(len(osm_pois)),
        f'<span style="color:#94a3b8; font-size:11px;">Night safety: '
        f'{len(crime_points):,} incidents + {len(violent_crime):,} '
        f'violent-crime pins (2yr)</span>',
        '<span id="lm-weather" style="color:#64748b; font-size:11px;">'
        "Loading weather...</span>"
        '<span id="lm-heat-badge" style="display:none; margin-left:8px; '
        'background:#dc2626; color:#fff; padding:2px 8px; border-radius:10px; '
        'font-size:10px; font-weight:700; letter-spacing:0.4px;">HEAT</span>',
        '<span style="color:#94a3b8; font-size:11px;">Heat-response: '
        f'{sum(1 for m in medical if m.get("is_er")):,} 24h ER + '
        f'{len(cooling):,} cooling (proxy)</span>',
        '<span style="color:#64748b; font-size:11px;">'
        "Drag time or pick a date. Venues toggle on/off by their "
        "real opening hours.</span>",
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
    # default (no --render-strategy) still writes prototype.html to
    # preserve the historical convention.
    if out_filename is None:
        if render_strategy == DEFAULT_RENDER_STRATEGY:
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
        altitude, azimuth = get_sun_position(target_time)
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
    parser = argparse.ArgumentParser(description="LightMap prototype")
    parser.add_argument(
        "--time", type=str, default="2026-04-20 14:00",
        help="Target time (YYYY-MM-DD HH:MM)",
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
        help="Output filename under docs/. Defaults to prototype.html for "
             "the default strategy and prototype_<key>.html otherwise.",
    )
    args = parser.parse_args()

    if args.night:
        target_time = datetime(2026, 4, 20, 22, 0, tzinfo=BOSTON_TZ)
    else:
        target_time = datetime.strptime(args.time, "%Y-%m-%d %H:%M")
        target_time = target_time.replace(tzinfo=BOSTON_TZ)

    build_map(target_time, args.scale, dual=args.dual,
              time_compare=args.time_compare,
              time_slider=args.time_slider,
              render_strategy=args.render_strategy,
              out_filename=args.out)


if __name__ == "__main__":
    main()
