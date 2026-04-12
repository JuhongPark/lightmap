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
MAP_CENTER = [42.36, -71.08]
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")

BOSTON_BUILDINGS_PATH = os.path.join(DATA_DIR, "buildings", "boston_buildings.geojson")
CAMBRIDGE_BUILDINGS_PATH = os.path.join(
    DATA_DIR, "cambridge", "buildings", "buildings.geojson"
)
BUILDINGS_DB_PATH = os.path.join(DATA_DIR, "buildings.db")

SHADOW_CMAP_COLORS = ["#cbd5e1", "#64748b", "#334155", "#0f172a"]
HEATMAP_GRADIENT = {
    0.2: "#1e3a5f", 0.4: "#2563eb", 0.6: "#60a5fa",
    0.8: "#fbbf24", 1.0: "#ffffff",
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

        # At 100% scale we skip the separate building layer entirely. Each
        # shadow polygon is the ConvexHull of the building plus its
        # translation, so it visually contains the building footprint. A
        # separate building layer would double the rendered geometry for no
        # visual benefit, and at 123K polygons that doubling costs ~50 MB
        # in the output HTML.
        building_data = {"type": "FeatureCollection", "features": []}

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

    # Skip the building layer when there are enough shadows to cover every
    # building visually. Each shadow is ConvexHull of building+translated, so
    # it already contains the building footprint. Drawing a second layer on
    # top is redundant above ~10K features and doubles the rendered HTML.
    if building_count > 10000:
        print("  Building layer skipped for rendering (shadows cover it)")
        render_building_data = {"type": "FeatureCollection", "features": []}
    else:
        render_building_data = building_data

    print("Computing shadows...")
    shadows, _, _ = compute_all_shadows(parsed, target_time)
    print(f"  Shadows computed: {len(shadows)}")

    print("Computing shadow coverage...")
    coverage = compute_shadow_coverage(shadows)
    print(f"  Shadow coverage: {coverage:.1f}%")

    return render_building_data, shadows, coverage


# ---------------------------------------------------------------------------
# Shared layer helpers (eliminate duplication)
# ---------------------------------------------------------------------------

def _create_base_map(tiles="CartoDB positron"):
    m = folium.Map(
        location=MAP_CENTER, zoom_start=14, tiles=tiles,
        width="100%", height="100%",
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


def _add_shadow_layer(m, shadows, cmap):
    folium.GeoJson(
        {"type": "FeatureCollection", "features": shadows},
        name="Shadows",
        style_function=lambda x: {
            "fillColor": cmap(x["properties"].get("height_ft", 0)),
            "color": cmap(x["properties"].get("height_ft", 0)),
            "weight": 0.3,
            "fillOpacity": 0.45,
        },
        highlight_function=lambda x: {
            "weight": 2,
            "fillOpacity": 0.65,
        },
        popup=GeoJsonPopup(
            fields=["height_ft", "shadow_len_ft"],
            aliases=["Building Height (ft):", "Shadow Length (ft):"],
            style="font-size:12px;",
        ),
    ).add_to(m)


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
            radius=3,
            color="#fbbf24",
            fill=True,
            fill_opacity=0.8,
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

def build_day_map(target_time, altitude, azimuth, scale_pct):
    building_data, shadows, coverage = _load_buildings_and_shadows(scale_pct, target_time)
    if building_data is None:
        return None

    # building_count falls back to shadow count when the building layer was
    # dropped for size reasons at 100% scale. Each shadow corresponds to one
    # building anyway.
    building_count = len(building_data["features"]) or len(shadows)
    m = _create_base_map("CartoDB positron")

    _add_building_layer(m, building_data)
    cmap = _make_shadow_cmap()
    _add_shadow_layer(m, shadows, cmap)
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
        colors=["#1e3a5f", "#2563eb", "#60a5fa", "#fbbf24", "#ffffff"],
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

    dm = DualMap(location=MAP_CENTER, zoom_start=14, tiles=None)
    folium.TileLayer("CartoDB positron").add_to(dm.m1)
    folium.TileLayer("CartoDB dark_matter").add_to(dm.m2)

    # Day side
    _add_building_layer(dm.m1, building_data)
    cmap = cm.LinearColormap(colors=SHADOW_CMAP_COLORS, vmin=0, vmax=200)
    _add_shadow_layer(dm.m1, shadows, cmap)

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


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def build_map(target_time, scale_pct=1, dual=False, time_compare=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "prototype.html")

    if time_compare:
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

        if is_day:
            m = build_day_map(target_time, altitude, azimuth, scale_pct)
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
        "--time", type=str, default="2025-07-15 14:00",
        help="Target time (YYYY-MM-DD HH:MM)",
    )
    parser.add_argument(
        "--night", action="store_true",
        help="Force night mode (2025-07-15 22:00)",
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
    args = parser.parse_args()

    if args.night:
        target_time = datetime(2025, 7, 15, 22, 0, tzinfo=BOSTON_TZ)
    else:
        target_time = datetime.strptime(args.time, "%Y-%m-%d %H:%M")
        target_time = target_time.replace(tzinfo=BOSTON_TZ)

    build_map(target_time, args.scale, dual=args.dual,
              time_compare=args.time_compare)


if __name__ == "__main__":
    main()
