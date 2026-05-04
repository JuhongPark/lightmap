"""Download OSM hospitals inside a LightMap city bbox via Overpass.

Fetches nodes, ways, and relations tagged `amenity=hospital`, plus any
`amenity=clinic` with `emergency=yes`. The `emergency` tag is preserved
on each feature so the time-slider can filter to 24h ERs at render
time (emergency=yes) while keeping the full hospital list available
for future non-emergency overlays.

Output
------
data/osm/medical.geojson -- a FeatureCollection. Properties: name,
amenity, emergency, addr_street, addr_city, phone, opening_hours,
healthcare, wheelchair.

Usage
-----
    .venv/bin/python scripts/download_medical.py
    .venv/bin/python scripts/download_medical.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from city_config import DEFAULT_CITY_ID, load_city_profile, profile_data_path

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def build_query(bbox):
    min_lat, min_lon, max_lat, max_lon = bbox
    box = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    clauses = [
        f'  node["amenity"="hospital"]({box});',
        f'  way["amenity"="hospital"]({box});',
        f'  relation["amenity"="hospital"]({box});',
        f'  node["amenity"="clinic"]["emergency"="yes"]({box});',
        f'  way["amenity"="clinic"]["emergency"="yes"]({box});',
    ]
    return "[out:json][timeout:120];\n(\n" + "\n".join(clauses) + "\n);\nout center;"


def fetch(query, endpoints):
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for url in endpoints:
        print(f"  Trying {url} ...")
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "lightmap/0.1 (OSM medical download)"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
            return json.loads(body)
        except Exception as e:
            last_err = e
            print(f"    failed: {e}")
    raise RuntimeError(f"All Overpass endpoints failed. Last error: {last_err}")


def _coords(element):
    if element["type"] == "node":
        return element.get("lon"), element.get("lat")
    center = element.get("center") or {}
    return center.get("lon"), center.get("lat")


def to_geojson(elements):
    features = []
    for e in elements:
        tags = e.get("tags", {}) or {}
        lon, lat = _coords(e)
        if lon is None or lat is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, 6), round(lat, 6)],
            },
            "properties": {
                "name": tags.get("name") or "",
                "amenity": tags.get("amenity") or "",
                "emergency": tags.get("emergency") or "",
                "addr_street": tags.get("addr:street") or "",
                "addr_city": tags.get("addr:city") or "",
                "phone": tags.get("phone") or "",
                "opening_hours": tags.get("opening_hours") or "",
                "healthcare": tags.get("healthcare") or "",
                "wheelchair": tags.get("wheelchair") or "",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true",
                        help="Redownload even if output already exists.")
    parser.add_argument(
        "--city", default=DEFAULT_CITY_ID,
        help="City profile id under cities/. Default: boston-cambridge.",
    )
    args = parser.parse_args()
    city = load_city_profile(args.city)
    out_path = profile_data_path(city, "medical", "osm", "medical.geojson")

    if os.path.exists(out_path) and not args.force:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"[skip] {out_path} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print("OSM hospitals + emergency clinics (Overpass):")
    print(f"  City: {city.display_name}")
    print(f"  BBox: {city.bbox}")
    query = build_query(city.bbox)
    js = fetch(query, OVERPASS_ENDPOINTS)
    elements = js.get("elements", [])
    print(f"  Raw elements: {len(elements)}")

    gj = to_geojson(elements)
    kept = len(gj["features"])
    emergency = sum(
        1 for f in gj["features"]
        if f["properties"].get("emergency") == "yes"
    )
    print(f"  Kept: {kept} features ({emergency} with emergency=yes)")

    with open(out_path, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  Saved {out_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
