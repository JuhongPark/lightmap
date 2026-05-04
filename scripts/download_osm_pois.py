"""Download OpenStreetMap POIs with opening_hours for a LightMap city.

Pulls nodes and ways tagged as `amenity` in (restaurant, bar, cafe,
fast_food, pub, nightclub) inside the city profile bbox via the free Overpass
API. Only POIs carrying an `opening_hours` tag are kept -- those are
the ones the time-slider can actually show as open/closed per (date,
time).

OSM provides the community-curated opening_hours in a standardized
format (e.g. "Mo-Fr 09:00-17:00; Sa 10:00-14:00") which the browser
parses with the opening_hours.js library at render time.

Output
------
data/osm/pois.geojson -- a FeatureCollection with one feature per POI.
Properties: name, amenity, opening_hours.

Usage
-----
    .venv/bin/python scripts/download_osm_pois.py
    .venv/bin/python scripts/download_osm_pois.py --force
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

AMENITIES = ("restaurant", "bar", "cafe", "fast_food", "pub", "nightclub")

# Public Overpass mirrors. Tried in order until one responds.
# All are community-operated and require no auth.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def build_query(bbox, amenities):
    min_lat, min_lon, max_lat, max_lon = bbox
    clauses = []
    for a in amenities:
        clauses.append(
            f'  node["amenity"="{a}"]({min_lat},{min_lon},{max_lat},{max_lon});'
        )
        clauses.append(
            f'  way["amenity"="{a}"]({min_lat},{min_lon},{max_lat},{max_lon});'
        )
    return "[out:json][timeout:120];\n(\n" + "\n".join(clauses) + "\n);\nout center;"


def fetch(query, endpoints):
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for url in endpoints:
        print(f"  Trying {url} ...")
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "lightmap/0.1 (OSM POI download)"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
            js = json.loads(body)
            return js
        except Exception as e:
            last_err = e
            print(f"    failed: {e}")
    raise RuntimeError(f"All Overpass endpoints failed. Last error: {last_err}")


def _coords(element):
    """Return (lon, lat) for either a node or a way (uses `center`)."""
    if element["type"] == "node":
        return element.get("lon"), element.get("lat")
    center = element.get("center") or {}
    return center.get("lon"), center.get("lat")


def to_geojson(elements):
    features = []
    for e in elements:
        tags = e.get("tags", {})
        hours = tags.get("opening_hours")
        if not hours:
            continue
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
                "opening_hours": hours,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--force", action="store_true",
        help="Redownload even if output already exists.",
    )
    parser.add_argument(
        "--city", default=DEFAULT_CITY_ID,
        help="City profile id under cities/. Default: boston-cambridge.",
    )
    args = parser.parse_args()
    city = load_city_profile(args.city)
    out_path = profile_data_path(city, "osm_pois", "osm", "pois.geojson")

    if os.path.exists(out_path) and not args.force:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"[skip] {out_path} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print("OSM POIs with opening_hours (Overpass):")
    print(f"  City: {city.display_name}")
    print(f"  BBox: {city.bbox}")
    query = build_query(city.bbox, AMENITIES)
    js = fetch(query, OVERPASS_ENDPOINTS)
    elements = js.get("elements", [])
    print(f"  Fetched {len(elements)} raw elements")

    gj = to_geojson(elements)
    kept = len(gj["features"])
    print(f"  Kept {kept} POIs with opening_hours tag")

    with open(out_path, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  Saved {out_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
