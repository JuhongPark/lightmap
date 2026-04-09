import argparse
import csv
import json
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
from folium.plugins import HeatMap

from shadow.compute import compute_all_shadows, get_sun_position

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
    ).add_to(m)

    info_html = (
        f'<div style="position:fixed; top:10px; left:60px; z-index:1000;'
        f" background:rgba(255,255,255,0.9); padding:12px 16px;"
        f' border-radius:8px; font-family:sans-serif; font-size:13px;">'
        f"<b>LightMap Prototype - Day ({scale_pct}%)</b><br>"
        f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}<br>"
        f"Sun altitude: {altitude:.1f}&deg;<br>"
        f"Sun azimuth: {azimuth:.1f}&deg;<br>"
        f"Buildings: {building_count:,} | Shadows: {len(shadows):,}"
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
        folium.CircleMarker(
            location=[p["lat"], p["lon"]],
            radius=3,
            color="#fbbf24",
            fill=True,
            fill_opacity=0.8,
            popup=p["name"],
        ).add_to(food_group)
    food_group.add_to(m)

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


def build_map(target_time, scale_pct=1):
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

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "prototype.html")
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
    args = parser.parse_args()

    if args.night:
        target_time = datetime(2025, 7, 15, 22, 0, tzinfo=BOSTON_TZ)
    else:
        target_time = datetime.strptime(args.time, "%Y-%m-%d %H:%M")
        target_time = target_time.replace(tzinfo=BOSTON_TZ)

    build_map(target_time, args.scale)


if __name__ == "__main__":
    main()
