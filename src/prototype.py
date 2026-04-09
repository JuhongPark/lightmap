import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
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


def build_map(target_time):
    altitude, azimuth = get_sun_position(target_time)
    print(f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Sun: altitude={altitude:.1f}, azimuth={azimuth:.1f}")

    if altitude <= 0:
        print("Sun is below horizon. No shadows to render.")
        return None

    cambridge_path = os.path.join(
        DATA_DIR, "cambridge", "buildings", "buildings.geojson"
    )
    if not os.path.exists(cambridge_path):
        print(f"ERROR: {cambridge_path} not found. Run scripts/download_data.py first.")
        return None

    # Write temp test GeoJSON for compute_all_shadows
    test_data = make_test_geojson(cambridge_path)
    tmp_path = os.path.join(DATA_DIR, "_test_buildings.geojson")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(test_data, f)

    building_count = len(test_data["features"])
    print(f"Buildings: {building_count}")

    shadows, alt, az = compute_all_shadows(tmp_path, target_time)
    print(f"Shadows computed: {len(shadows)}")

    m = folium.Map(
        location=BOSTON_CENTER,
        zoom_start=14,
        tiles="CartoDB positron",
    )

    building_geojson = {
        "type": "FeatureCollection",
        "features": test_data["features"],
    }
    folium.GeoJson(
        building_geojson,
        name="Buildings",
        style_function=lambda x: {
            "fillColor": "#64748b",
            "color": "#475569",
            "weight": 1,
            "fillOpacity": 0.6,
        },
    ).add_to(m)

    shadow_geojson = {"type": "FeatureCollection", "features": shadows}
    folium.GeoJson(
        shadow_geojson,
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
        f"<b>LightMap Prototype</b><br>"
        f"Time: {target_time.strftime('%Y-%m-%d %H:%M %Z')}<br>"
        f"Sun altitude: {altitude:.1f}&deg;<br>"
        f"Sun azimuth: {azimuth:.1f}&deg;<br>"
        f"Buildings: {building_count}<br>"
        f"Shadows: {len(shadows)}"
        f"</div>"
    )
    m.get_root().html.add_child(folium.Element(info_html))

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
    args = parser.parse_args()

    target_time = datetime.strptime(args.time, "%Y-%m-%d %H:%M")
    target_time = target_time.replace(tzinfo=BOSTON_TZ)

    build_map(target_time)


if __name__ == "__main__":
    main()
