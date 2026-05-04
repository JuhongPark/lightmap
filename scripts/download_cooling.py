"""Download cooling-center candidates inside a LightMap city bbox.

This script uses an OSM proxy of libraries, community centres, and town halls.
When a stable official city dataset is available, swap the source and keep the
output schema the same.

Output
------
data/cooling/cooling.geojson -- a FeatureCollection. Properties:
source, amenity, name, operator, opening_hours, addr_street, addr_city.

Usage
-----
    .venv/bin/python scripts/download_cooling.py
    .venv/bin/python scripts/download_cooling.py --force
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

PROXY_AMENITIES = ("library", "community_centre", "townhall")

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def build_query(bbox, amenities):
    min_lat, min_lon, max_lat, max_lon = bbox
    box = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    union = "|".join(amenities)
    return (
        "[out:json][timeout:120];\n"
        "(\n"
        f'  node["amenity"~"^({union})$"]({box});\n'
        f'  way["amenity"~"^({union})$"]({box});\n'
        ");\n"
        "out center;"
    )


def fetch(query, endpoints):
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for url in endpoints:
        print(f"  Trying {url} ...")
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "lightmap/0.1 (cooling proxy download)"},
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
                "source": "osm-proxy",
                "amenity": tags.get("amenity") or "",
                "name": tags.get("name") or "",
                "operator": tags.get("operator") or "",
                "opening_hours": tags.get("opening_hours") or "",
                "addr_street": tags.get("addr:street") or "",
                "addr_city": tags.get("addr:city") or "",
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
    out_path = profile_data_path(city, "cooling", "cooling", "cooling.geojson")

    if os.path.exists(out_path) and not args.force:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"[skip] {out_path} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print("Cooling proxy (library + community_centre + townhall):")
    print(f"  City: {city.display_name}")
    print(f"  BBox: {city.bbox}")
    query = build_query(city.bbox, PROXY_AMENITIES)
    js = fetch(query, OVERPASS_ENDPOINTS)
    elements = js.get("elements", [])
    print(f"  Raw elements: {len(elements)}")

    gj = to_geojson(elements)
    kept = len(gj["features"])
    print(f"  Kept: {kept}")

    with open(out_path, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  Saved {out_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
