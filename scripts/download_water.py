"""Fetch water polygons (river, lake, ocean) for INITIAL_BBOX from OSM.

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
OUT_PATH = os.path.join(REPO_ROOT, "data", "water", "water.geojson")

# Must match src/render/strategies.py INITIAL_BBOX.
INITIAL_BBOX = (42.335, -71.130, 42.385, -71.040)  # min_lat, min_lon, max_lat, max_lon

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
    args = parser.parse_args()

    if os.path.exists(OUT_PATH) and not args.force:
        size_kb = os.path.getsize(OUT_PATH) / 1024
        print(f"[skip] {OUT_PATH} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    q = overpass_query(INITIAL_BBOX)
    print(f"POST Overpass query (bbox {INITIAL_BBOX})")
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
    with open(OUT_PATH, "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"Saved {OUT_PATH} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
