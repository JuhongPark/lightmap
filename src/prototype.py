import argparse
import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
from folium.plugins import HeatMap
from shapely.geometry import Polygon, mapping

from shadow.compute import compute_all_shadows, get_sun_position

BOSTON_TZ = ZoneInfo("US/Eastern")
BOSTON_CENTER = [42.355, -71.065]
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")

# Hardcoded Boston test building: Prudential Tower area footprint
# Approximate polygon near 800 Boylston St, height ~228m (749ft)
BOSTON_TEST_BUILDING = {
    "type": "Feature",
    "properties": {"BLDG_HGT_2010": 749.0},
    "geometry": mapping(Polygon([
        (-71.0818, 42.3470),
        (-71.0812, 42.3470),
        (-71.0812, 42.3475),
        (-71.0818, 42.3475),
        (-71.0818, 42.3470),
    ])),
}


def load_cambridge_building(geojson_path):
    with open(geojson_path) as f:
        data = json.load(f)

    for feat in data["features"]:
        props = feat.get("properties", {})
        top_gl = props.get("TOP_GL")
        if top_gl is not None and top_gl > 10:
            height_ft = top_gl * 3.28084
            return {
                "type": "Feature",
                "properties": {"BLDG_HGT_2010": round(height_ft, 1)},
                "geometry": feat["geometry"],
            }
    return None


def make_test_geojson(cambridge_path):
    features = [BOSTON_TEST_BUILDING]
    cam_building = load_cambridge_building(cambridge_path)
    if cam_building:
        features.append(cam_building)
    return {"type": "FeatureCollection", "features": features}


def load_streetlights(max_per_city=1):
    coords = []

    boston_path = os.path.join(DATA_DIR, "streetlights", "streetlights.csv")
    if os.path.exists(boston_path):
        count = 0
        with open(boston_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if count >= max_per_city:
                    break
                try:
                    lat = float(row["Lat"])
                    lon = float(row["Long"])
                    if 42.2 < lat < 42.5 and -71.2 < lon < -70.9:
                        coords.append([lat, lon])
                        count += 1
                except (ValueError, KeyError):
                    continue
        print(f"  Boston streetlights: {count}")

    cam_path = os.path.join(DATA_DIR, "cambridge", "streetlights", "streetlights.geojson")
    cam_count = 0
    if os.path.exists(cam_path):
        with open(cam_path) as f:
            data = json.load(f)
        for feat in data["features"]:
            if cam_count >= max_per_city:
                break
            geom = feat.get("geometry", {})
            if geom.get("type") == "Point":
                lon, lat = geom["coordinates"][:2]
                if 42.2 < lat < 42.5 and -71.2 < lon < -70.9:
                    coords.append([lat, lon])
                    cam_count += 1
        print(f"  Cambridge streetlights: {cam_count}")

    print(f"  Total streetlights: {len(coords)}")
    return coords


def load_food_establishments(max_count=1):
    path = os.path.join(DATA_DIR, "safety", "food_establishments.csv")
    if not os.path.exists(path):
        print("  Food establishments file not found.")
        return []

    places = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if len(places) >= max_count:
                break
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
                name = row.get("businessname", "")
                if 42.2 < lat < 42.5 and -71.2 < lon < -70.9:
                    places.append({"lat": lat, "lon": lon, "name": name})
            except (ValueError, KeyError):
                continue

    print(f"  Food establishments: {len(places)}")
    return places


def build_day_map(target_time, altitude, azimuth):
    cambridge_path = os.path.join(
        DATA_DIR, "cambridge", "buildings", "buildings.geojson"
    )
    if not os.path.exists(cambridge_path):
        print(f"ERROR: {cambridge_path} not found. Run scripts/download_data.py first.")
        return None

    test_data = make_test_geojson(cambridge_path)
    tmp_path = os.path.join(DATA_DIR, "_test_buildings.geojson")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(test_data, f)

    building_count = len(test_data["features"])
    print(f"Buildings: {building_count}")

    shadows, _, _ = compute_all_shadows(tmp_path, target_time)
    print(f"Shadows computed: {len(shadows)}")

    m = folium.Map(
        location=BOSTON_CENTER,
        zoom_start=14,
        tiles="CartoDB positron",
    )

    folium.GeoJson(
        {"type": "FeatureCollection", "features": test_data["features"]},
        name="Buildings",
        style_function=lambda x: {
            "fillColor": "#64748b",
            "color": "#475569",
            "weight": 1,
            "fillOpacity": 0.6,
        },
    ).add_to(m)

    folium.GeoJson(
        {"type": "FeatureCollection", "features": shadows},
        name="Shadows",
        style_function=lambda x: {
            "fillColor": "#1e293b",
            "color": "#1e293b",
            "weight": 0.5,
            "fillOpacity": 0.4,
        },
    ).add_to(m)

    info_html = (
        f'<div style="position:fixed; top:10px; left:60px; z-index:1000;'
        f" background:rgba(255,255,255,0.9); padding:12px 16px;"
        f' border-radius:8px; font-family:sans-serif; font-size:13px;">'
        f"<b>LightMap Prototype - Day</b><br>"
        f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}<br>"
        f"Sun altitude: {altitude:.1f}&deg;<br>"
        f"Sun azimuth: {azimuth:.1f}&deg;<br>"
        f"Buildings: {building_count} | Shadows: {len(shadows)}"
        f"</div>"
    )
    m.get_root().html.add_child(folium.Element(info_html))
    return m


def build_night_map(target_time, altitude, azimuth):
    m = folium.Map(
        location=BOSTON_CENTER,
        zoom_start=13,
        tiles="CartoDB dark_matter",
    )

    print("Loading streetlights...")
    coords = load_streetlights()
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
    places = load_food_establishments()
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
        f"<b>LightMap Prototype - Night</b><br>"
        f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}<br>"
        f"Sun altitude: {altitude:.1f}&deg;<br>"
        f"Streetlights: {len(coords):,} | Food: {len(places):,}"
        f"</div>"
    )
    m.get_root().html.add_child(folium.Element(info_html))
    return m


def build_map(target_time):
    altitude, azimuth = get_sun_position(target_time)
    is_day = altitude > 0
    mode = "Day" if is_day else "Night"

    print(f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Sun: altitude={altitude:.1f}, azimuth={azimuth:.1f}")
    print(f"Mode: {mode}")

    if is_day:
        m = build_day_map(target_time, altitude, azimuth)
    else:
        m = build_night_map(target_time, altitude, azimuth)

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
        default="2026-07-15 14:00",
        help="Target time (YYYY-MM-DD HH:MM)",
    )
    parser.add_argument(
        "--night",
        action="store_true",
        help="Force night mode (2026-07-15 22:00)",
    )
    args = parser.parse_args()

    if args.night:
        target_time = datetime(2026, 7, 15, 22, 0, tzinfo=BOSTON_TZ)
    else:
        target_time = datetime.strptime(args.time, "%Y-%m-%d %H:%M")
        target_time = target_time.replace(tzinfo=BOSTON_TZ)

    build_map(target_time)


if __name__ == "__main__":
    main()
