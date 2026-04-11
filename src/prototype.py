import argparse
import csv
import json
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
import branca.colormap as cm
from folium.features import GeoJsonPopup, GeoJsonTooltip
from folium.plugins import (
    DualMap, Fullscreen, Geocoder, HeatMap, MiniMap, MousePosition,
    TimestampedGeoJson,
)

from shadow.compute import compute_all_shadows, compute_shadow_coverage, get_sun_position

BOSTON_TZ = ZoneInfo("US/Eastern")
MAP_CENTER = [42.36, -71.08]
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")

BOSTON_BUILDINGS_PATH = os.path.join(DATA_DIR, "buildings", "boston_buildings.geojson")
CAMBRIDGE_BUILDINGS_PATH = os.path.join(
    DATA_DIR, "cambridge", "buildings", "buildings.geojson"
)

TOTAL_BOSTON_STREETLIGHTS = 74065
TOTAL_CAMBRIDGE_STREETLIGHTS = 6117
TOTAL_FOOD = 3207


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
            area = geom.area
            score = h * (area ** 0.5)
            if score > best_score:
                best_score = score
                best = feat
        except Exception:
            continue
    return best


def load_buildings(scale_pct):
    random.seed(42)
    features = []

    # Cambridge buildings
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

    # Boston buildings
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


def load_streetlights(scale_pct):
    random.seed(42)
    coords = []

    # Boston
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

    # Cambridge
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


def _add_ui_plugins(m, theme="light"):
    Fullscreen(position="topleft").add_to(m)
    MousePosition(position="bottomleft", separator=" | ", prefix="Coords: ").add_to(m)
    tile_layer = "CartoDB positron" if theme == "light" else "CartoDB dark_matter"
    MiniMap(toggle_display=True, minimized=True, tile_layer=tile_layer).add_to(m)
    Geocoder(collapsed=True, position="topright").add_to(m)


def build_day_map(target_time, altitude, azimuth, scale_pct):
    print("Loading buildings...")
    building_data = load_buildings(scale_pct)
    building_count = len(building_data["features"])
    print(f"  Total buildings: {building_count}")

    if building_count == 0:
        print("ERROR: No buildings loaded.")
        return None

    tmp_path = os.path.join(DATA_DIR, "_scale_buildings.geojson")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(building_data, f)

    print("Computing shadows...")
    shadows, _, _ = compute_all_shadows(tmp_path, target_time)
    print(f"  Shadows computed: {len(shadows)}")

    print("Computing shadow coverage...")
    coverage_pct = compute_shadow_coverage(shadows)
    print(f"  Shadow coverage: {coverage_pct:.1f}%")

    m = folium.Map(
        location=MAP_CENTER, zoom_start=14, tiles="CartoDB positron",
        width="100%", height="100%",
    )
    m.get_root().html.add_child(folium.Element(
        "<style>html,body{margin:0;padding:0;height:100%;width:100%}</style>"
    ))

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

    folium.GeoJson(
        {"type": "FeatureCollection", "features": shadows},
        name="Shadows",
        style_function=lambda x: {
            "fillColor": "#1e293b",
            "color": "#1e293b",
            "weight": 0.3,
            "fillOpacity": 0.4,
        },
        highlight_function=lambda x: {
            "weight": 2,
            "fillOpacity": 0.6,
        },
        popup=GeoJsonPopup(
            fields=["height_ft", "shadow_len_m"],
            aliases=["Building Height (ft):", "Shadow Length (m):"],
            style="font-size:12px;",
        ),
    ).add_to(m)

    _add_ui_plugins(m, theme="light")

    shadow_legend = cm.LinearColormap(
        colors=["#e2e8f0", "#94a3b8", "#475569", "#1e293b"],
        vmin=0, vmax=100,
        caption="Shadow (building height in ft)",
    )
    shadow_legend.add_to(m)

    info_html = (
        f'<div style="position:fixed; top:10px; left:60px; z-index:1000;'
        f" background:rgba(255,255,255,0.9); padding:12px 16px;"
        f' border-radius:8px; font-family:sans-serif; font-size:13px;">'
        f"<b>LightMap Prototype - Day ({scale_pct}%)</b><br>"
        f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}<br>"
        f"Sun altitude: {altitude:.1f}&deg;<br>"
        f"Sun azimuth: {azimuth:.1f}&deg;<br>"
        f"Buildings: {building_count:,} | Shadows: {len(shadows):,}<br>"
        f"Shadow coverage: {coverage_pct:.1f}%"
        f"</div>"
    )
    m.get_root().html.add_child(folium.Element(info_html))
    return m


def build_night_map(target_time, altitude, azimuth, scale_pct):
    m = folium.Map(
        location=MAP_CENTER, zoom_start=14, tiles="CartoDB dark_matter",
        width="100%", height="100%",
    )
    m.get_root().html.add_child(folium.Element(
        "<style>html,body{margin:0;padding:0;height:100%;width:100%}</style>"
    ))

    print("Loading streetlights...")
    coords = load_streetlights(scale_pct)
    if coords:
        HeatMap(
            coords,
            name="Streetlights",
            radius=12,
            blur=20,
            gradient={
                0.2: "#1e3a5f",
                0.4: "#2563eb",
                0.6: "#60a5fa",
                0.8: "#fbbf24",
                1.0: "#ffffff",
            },
        ).add_to(m)

    print("Loading food establishments...")
    places = load_food_establishments(scale_pct)
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

    _add_ui_plugins(m, theme="dark")

    brightness_legend = cm.LinearColormap(
        colors=["#1e3a5f", "#2563eb", "#60a5fa", "#fbbf24", "#ffffff"],
        vmin=0, vmax=1,
        caption="Light intensity (streetlight density)",
    )
    brightness_legend.add_to(m)

    info_html = (
        f'<div style="position:fixed; top:10px; left:60px; z-index:1000;'
        f" background:rgba(15,23,42,0.9); color:#e2e8f0; padding:12px 16px;"
        f' border-radius:8px; font-family:sans-serif; font-size:13px;">'
        f"<b>LightMap Prototype - Night ({scale_pct}%)</b><br>"
        f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}<br>"
        f"Sun altitude: {altitude:.1f}&deg;<br>"
        f"Streetlights: {len(coords):,} | Food: {len(places):,}"
        f"</div>"
    )
    m.get_root().html.add_child(folium.Element(info_html))
    return m


TIME_STEPS = [7, 9, 11, 13, 15, 17]


def build_time_map(target_time, scale_pct):
    print("Loading buildings...")
    building_data = load_buildings(scale_pct)
    building_count = len(building_data["features"])
    print(f"  Total buildings: {building_count}")

    if building_count == 0:
        print("ERROR: No buildings loaded.")
        return None

    tmp_path = os.path.join(DATA_DIR, "_scale_buildings.geojson")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(building_data, f)

    # Compute shadows at each time step
    all_features = []
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

        shadows, _, _ = compute_all_shadows(tmp_path, step_time)
        print(f"    Shadows: {len(shadows)}")

        for feat in shadows:
            all_features.append({
                "type": "Feature",
                "geometry": feat["geometry"],
                "properties": {
                    "times": [timestamp],
                    "style": {
                        "fillColor": "#1e293b",
                        "color": "#1e293b",
                        "weight": 0.3,
                        "fillOpacity": 0.4,
                    },
                },
            })

    m = folium.Map(
        location=MAP_CENTER, zoom_start=14, tiles="CartoDB positron",
        width="100%", height="100%",
    )
    m.get_root().html.add_child(folium.Element(
        "<style>html,body{margin:0;padding:0;height:100%;width:100%}</style>"
    ))

    # Static building layer
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

    # Animated shadow layer
    TimestampedGeoJson(
        {"type": "FeatureCollection", "features": all_features},
        period="PT2H",
        duration="PT2H",
        transition_time=500,
        auto_play=True,
        loop=True,
        loop_button=True,
        speed_slider=True,
        date_options="HH:mm",
    ).add_to(m)

    _add_ui_plugins(m, theme="light")

    shadow_legend = cm.LinearColormap(
        colors=["#e2e8f0", "#94a3b8", "#475569", "#1e293b"],
        vmin=0, vmax=100,
        caption="Shadow (building height in ft)",
    )
    shadow_legend.add_to(m)

    info_html = (
        '<div style="position:fixed; top:10px; left:60px; z-index:1000;'
        " background:rgba(255,255,255,0.9); padding:12px 16px;"
        ' border-radius:8px; font-family:sans-serif; font-size:13px;">'
        f"<b>LightMap - Shadow Animation ({scale_pct}%)</b><br>"
        f"Date: {target_time.strftime('%Y-%m-%d')}<br>"
        f"Buildings: {building_count:,}<br>"
        f"Time steps: {len(TIME_STEPS)} (7 AM - 5 PM)"
        "</div>"
    )
    m.get_root().html.add_child(folium.Element(info_html))

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
    day_alt, day_az = get_sun_position(day_time)
    night_alt, night_az = get_sun_position(night_time)

    print(f"Dual map: day={day_time.strftime('%H:%M')}, night={night_time.strftime('%H:%M')}")
    print(f"  Day sun: alt={day_alt:.1f}, az={day_az:.1f}")
    print(f"  Night sun: alt={night_alt:.1f}, az={night_az:.1f}")

    # Load data
    print("Loading buildings...")
    building_data = load_buildings(scale_pct)
    building_count = len(building_data["features"])
    print(f"  Total buildings: {building_count}")

    if building_count == 0:
        print("ERROR: No buildings loaded.")
        return None

    tmp_path = os.path.join(DATA_DIR, "_scale_buildings.geojson")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(building_data, f)

    print("Computing shadows...")
    shadows, _, _ = compute_all_shadows(tmp_path, day_time)
    print(f"  Shadows computed: {len(shadows)}")

    print("Computing shadow coverage...")
    coverage_pct = compute_shadow_coverage(shadows)
    print(f"  Shadow coverage: {coverage_pct:.1f}%")

    print("Loading streetlights...")
    coords = load_streetlights(scale_pct)

    print("Loading food establishments...")
    places = load_food_establishments(scale_pct)

    # Build dual map
    dm = DualMap(location=MAP_CENTER, zoom_start=14, tiles=None)
    folium.TileLayer("CartoDB positron").add_to(dm.m1)
    folium.TileLayer("CartoDB dark_matter").add_to(dm.m2)

    # Day side (m1): buildings + shadows
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
    ).add_to(dm.m1)

    folium.GeoJson(
        {"type": "FeatureCollection", "features": shadows},
        name="Shadows",
        style_function=lambda x: {
            "fillColor": "#1e293b",
            "color": "#1e293b",
            "weight": 0.3,
            "fillOpacity": 0.4,
        },
        highlight_function=lambda x: {
            "weight": 2,
            "fillOpacity": 0.6,
        },
        popup=GeoJsonPopup(
            fields=["height_ft", "shadow_len_m"],
            aliases=["Building Height (ft):", "Shadow Length (m):"],
            style="font-size:12px;",
        ),
    ).add_to(dm.m1)

    # Night side (m2): streetlights + food
    if coords:
        HeatMap(
            coords,
            name="Streetlights",
            radius=12,
            blur=20,
            gradient={
                0.2: "#1e3a5f",
                0.4: "#2563eb",
                0.6: "#60a5fa",
                0.8: "#fbbf24",
                1.0: "#ffffff",
            },
        ).add_to(dm.m2)

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
    food_group.add_to(dm.m2)

    # Info panels
    day_info = (
        '<div style="position:fixed; top:10px; left:10px; z-index:1000;'
        " background:rgba(255,255,255,0.9); padding:10px 14px;"
        ' border-radius:8px; font-family:sans-serif; font-size:12px;">'
        f"<b>Day ({scale_pct}%)</b><br>"
        f"Time: {day_time.strftime('%H:%M')}<br>"
        f"Sun: {day_alt:.1f}&deg;<br>"
        f"Buildings: {building_count:,}<br>"
        f"Shadows: {len(shadows):,}<br>"
        f"Coverage: {coverage_pct:.1f}%"
        "</div>"
    )
    dm.m1.get_root().html.add_child(folium.Element(day_info))

    night_info = (
        '<div style="position:fixed; top:10px; right:10px; z-index:1000;'
        " background:rgba(15,23,42,0.9); color:#e2e8f0; padding:10px 14px;"
        ' border-radius:8px; font-family:sans-serif; font-size:12px;">'
        f"<b>Night ({scale_pct}%)</b><br>"
        f"Time: {night_time.strftime('%H:%M')}<br>"
        f"Streetlights: {len(coords):,}<br>"
        f"Food: {len(places):,}"
        "</div>"
    )
    dm.m1.get_root().html.add_child(folium.Element(night_info))

    return dm


def build_map(target_time, scale_pct=1, dual=False, time_compare=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "prototype.html")

    if time_compare:
        m = build_time_map(target_time, scale_pct)
        if m is None:
            return None
        m.save(out_path)
        print(f"Saved: {out_path}")
        return out_path

    if dual:
        m = build_dual_map(target_time, scale_pct)
        if m is None:
            return None
        m.save(out_path)
        print(f"Saved: {out_path}")
        return out_path

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

    if m is None:
        return None

    folium.LayerControl().add_to(m)

    m.save(out_path)
    print(f"Saved: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="LightMap prototype")
    parser.add_argument(
        "--time",
        type=str,
        default="2025-07-15 14:00",
        help="Target time (YYYY-MM-DD HH:MM)",
    )
    parser.add_argument(
        "--night",
        action="store_true",
        help="Force night mode (2025-07-15 22:00)",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=1,
        choices=[0, 1, 10, 50, 100],
        help="Percent of data to use (0=1 each, 1, 10, 50, 100)",
    )
    parser.add_argument(
        "--dual",
        action="store_true",
        help="Dual map: day (left) + night (right) side by side",
    )
    parser.add_argument(
        "--time-compare",
        action="store_true",
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
