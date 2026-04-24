"""Download cooling-center candidates inside INITIAL_BBOX.

Boston does not publish a stable machine-readable "cooling centers"
dataset. This script uses an OSM proxy — libraries, community centres,
and town halls — which the City of Boston opens during heat emergencies
for walk-in cooling. When a stable official dataset is located, swap
the source here and keep the output schema the same.

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
DATA_DIR = os.path.join(REPO_ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "cooling", "cooling.geojson")

# Must match src/render/strategies.py INITIAL_BBOX.
INITIAL_BBOX = (42.335, -71.130, 42.385, -71.040)

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
    args = parser.parse_args()

    if os.path.exists(OUT_PATH) and not args.force:
        size_kb = os.path.getsize(OUT_PATH) / 1024
        print(f"[skip] {OUT_PATH} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    print("Cooling proxy (library + community_centre + townhall):")
    query = build_query(INITIAL_BBOX, PROXY_AMENITIES)
    js = fetch(query, OVERPASS_ENDPOINTS)
    elements = js.get("elements", [])
    print(f"  Raw elements: {len(elements)}")

    gj = to_geojson(elements)
    kept = len(gj["features"])
    print(f"  Kept: {kept}")

    with open(OUT_PATH, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"  Saved {OUT_PATH} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
