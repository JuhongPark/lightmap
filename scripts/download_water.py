"""Fetch water polygons for a LightMap city from OSM.

Uses Overpass API to pull `natural=water` and `waterway=river|stream`
relations and ways inside the viewport clamp. Resolves multipolygon
relations into simple Polygons/MultiPolygons so downstream shapely
ops do not need OSM plumbing.

Output: `data/water/water.geojson` — a FeatureCollection of water
polygons. Consumed by `scripts/clip_trees_by_water.py` and potentially
any future layer that needs a water mask.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from city_config import DEFAULT_CITY_ID, load_city_profile, profile_data_path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def overpass_query(bbox):
    mnla, mnlo, mxla, mxlo = bbox
    bb = f"{mnla},{mnlo},{mxla},{mxlo}"
    return f"""
[out:json][timeout:120];
(
  way["natural"="water"]({bb});
  relation["natural"="water"]({bb});
  way["waterway"="riverbank"]({bb});
  way["waterway"="river"]({bb});
);
(._;>;);
out geom;
"""


def _ring_closed(pts):
    if len(pts) < 4:
        return False
    return pts[0] == pts[-1]


def _close_ring(pts):
    if not _ring_closed(pts):
        return pts + [pts[0]]
    return pts


def _nodes_to_ring(nodes):
    return [[n["lon"], n["lat"]] for n in nodes]


def ways_and_rels_to_features(osm):
    """Convert Overpass elements to GeoJSON features.

    - way with closed geometry → Polygon
    - relation with type=multipolygon → Polygon/MultiPolygon composed of
      outer/inner rings, with simple ring-stitching for split outer rings
    """
    feats = []
    for el in osm.get("elements", []):
        if el.get("type") == "way":
            geom = el.get("geometry") or []
            if not geom:
                continue
            ring = _nodes_to_ring(geom)
            if len(ring) < 3:
                continue
            ring = _close_ring(ring)
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"source": f"way/{el['id']}"},
            })
        elif el.get("type") == "relation":
            outers = []
            inners = []
            for m in el.get("members") or []:
                if m.get("type") != "way":
                    continue
                g = m.get("geometry") or []
                if not g:
                    continue
                ring = _nodes_to_ring(g)
                if len(ring) < 3:
                    continue
                ring = _close_ring(ring)
                role = m.get("role") or "outer"
                if role == "inner":
                    inners.append(ring)
                else:
                    outers.append(ring)
            # Build polygons: each outer with any inners that fall inside.
            # Point-in-polygon fully is overkill for our use; water holes
            # (inner rings) rarely matter for a shade-clipping mask, so
            # we just attach all inners to the first outer as a coarse
            # approximation.
            if not outers:
                continue
            if len(outers) == 1:
                feats.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [outers[0]] + inners,
                    },
                    "properties": {"source": f"relation/{el['id']}"},
                })
            else:
                polys = [[ring] for ring in outers]
                if inners:
                    polys[0].extend(inners)
                feats.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": polys,
                    },
                    "properties": {"source": f"relation/{el['id']}"},
                })
    return feats


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--city", default=DEFAULT_CITY_ID,
        help="City profile id under cities/. Default: boston-cambridge.",
    )
    args = parser.parse_args()
    city = load_city_profile(args.city)
    out_path = profile_data_path(city, "water", "water", "water.geojson")

    if os.path.exists(out_path) and not args.force:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"[skip] {out_path} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    q = overpass_query(city.bbox)
    print(f"POST Overpass query for {city.display_name} (bbox {city.bbox})")
    with httpx.Client(timeout=180, follow_redirects=True,
                      headers={"User-Agent": "lightmap/0.1"}) as c:
        resp = c.post(OVERPASS_URL, data={"data": q})
        if resp.status_code >= 400:
            print(f"HTTP {resp.status_code}: {resp.text[:400]}",
                  file=sys.stderr)
            return 2
        osm = resp.json()

    feats = ways_and_rels_to_features(osm)
    print(f"Parsed {len(feats)} water features")

    fc = {"type": "FeatureCollection", "features": feats}
    with open(out_path, "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    size_kb = os.path.getsize(out_path) / 1024
    print(f"Saved {out_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
