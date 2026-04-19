"""Download Cambridge tree canopy (2018) from the Cambridge GIS GitHub
mirror, convert TopoJSON to GeoJSON in pure Python, filter to
INITIAL_BBOX, simplify geometry, and write one merged GeoJSON for the
time-slider shadow engine to consume.

Only Cambridge is shipped for now. Boston's BPDA tree canopy is a 1 GB
shapefile ZIP and requires reprojection from EPSG:2249 to WGS84. That
layer is a follow-up if the MIT + Cambridge core alone is not enough.

The TopoJSON format is delta-encoded and uses an arc index table. This
script decodes arcs once, then resolves each polygon ring's arc
references (supporting the negative-index-means-reverse convention) to
build plain coordinate lists that GeoJSON can consume directly.

Usage
-----
    .venv/bin/python scripts/download_trees.py
    .venv/bin/python scripts/download_trees.py --force
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(REPO_ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "trees", "trees.geojson")

CAMBRIDGE_TREES_URL = (
    "https://raw.githubusercontent.com/cambridgegis/"
    "cambridgegis_data_environmental/main/Tree_Canopy_2018/"
    "ENVIRONMENTAL_TreeCanopy2018.topojson"
)

# Must match src/render/strategies.py INITIAL_BBOX.
INITIAL_BBOX = (42.335, -71.130, 42.385, -71.040)  # min_lat, min_lon, max_lat, max_lon

# Douglas-Peucker tolerance in degrees (~2e-5 deg ≈ 2.2 m at 42 deg N).
# A stricter 5e-5 dropped ~70% of small-canopy features by collapsing
# their rings to under 4 points. 2e-5 keeps every meaningful tree
# outline while still trimming ~90% of vertex count.
SIMPLIFY_TOL_DEG = 2e-5

# Default tree height assumed for all trees. Cambridge TopoJSON ships
# empty properties. BPDA has per-tree height, but Cambridge data does
# not, so we pin a reasonable canopy height for the shadow engine.
DEFAULT_TREE_HEIGHT_M = 10.0


def decode_topojson_arcs(topology):
    """Return list of absolute [lon, lat] arcs from a TopoJSON object."""
    transform = topology.get("transform") or {}
    scale = transform.get("scale", [1.0, 1.0])
    translate = transform.get("translate", [0.0, 0.0])
    out = []
    for raw_arc in topology.get("arcs", []):
        x, y = 0, 0
        pts = []
        for dx, dy in raw_arc:
            x += dx
            y += dy
            pts.append([x * scale[0] + translate[0],
                        y * scale[1] + translate[1]])
        out.append(pts)
    return out


def resolve_ring(arc_refs, arcs):
    """Stitch a ring from signed arc references.

    ref >= 0 means use arcs[ref] in order.
    ref <  0 means use arcs[~ref] in reverse.
    Adjacent arcs share the joining endpoint so we drop the leading
    point of every arc after the first.
    """
    coords = []
    for i, ref in enumerate(arc_refs):
        if ref < 0:
            arc = list(reversed(arcs[~ref]))
        else:
            arc = arcs[ref]
        if i == 0:
            coords.extend(arc)
        else:
            coords.extend(arc[1:])
    return coords


def topojson_object_to_features(topology, object_name, arcs):
    obj = topology.get("objects", {}).get(object_name) or {}
    geometries = obj.get("geometries", [])
    features = []
    for g in geometries:
        t = g.get("type")
        if t == "Polygon":
            rings = [resolve_ring(r, arcs) for r in g.get("arcs", [])]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": rings},
                "properties": dict(g.get("properties") or {}),
            })
        elif t == "MultiPolygon":
            for poly in g.get("arcs", []):
                rings = [resolve_ring(r, arcs) for r in poly]
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": rings},
                    "properties": dict(g.get("properties") or {}),
                })
    return features


def feature_bbox(feat):
    coords = feat["geometry"]["coordinates"]
    if not coords or not coords[0]:
        return None
    xs = [pt[0] for pt in coords[0]]
    ys = [pt[1] for pt in coords[0]]
    return [min(xs), min(ys), max(xs), max(ys)]


def bbox_intersects(fb, box):
    """fb = [minx, miny, maxx, maxy]; box = (minlat, minlon, maxlat, maxlon)."""
    minlat, minlon, maxlat, maxlon = box
    return not (fb[2] < minlon or fb[0] > maxlon
                or fb[3] < minlat or fb[1] > maxlat)


def _perp_distance(p, a, b):
    """Perpendicular distance from point p to line segment a-b."""
    if a == b:
        dx = p[0] - a[0]
        dy = p[1] - a[1]
        return math.hypot(dx, dy)
    dx, dy = b[0] - a[0], b[1] - a[1]
    num = abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0])
    den = math.hypot(dx, dy)
    return num / den


def simplify_ring(ring, tol):
    """Douglas-Peucker on a ring. Keeps first and last point."""
    if len(ring) < 4:
        return ring
    stack = [(0, len(ring) - 1)]
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        max_d = 0.0
        idx = -1
        for i in range(first + 1, last):
            d = _perp_distance(ring[i], ring[first], ring[last])
            if d > max_d:
                max_d = d
                idx = i
        if max_d > tol and idx > 0:
            keep[idx] = True
            stack.append((first, idx))
            stack.append((idx, last))
    return [p for p, k in zip(ring, keep) if k]


def simplify_feature(feat, tol):
    coords = feat["geometry"]["coordinates"]
    new_rings = []
    for ring in coords:
        r = simplify_ring(ring, tol)
        # A valid polygon needs at least 4 points (3 unique + close).
        if len(r) >= 4:
            new_rings.append(r)
    if not new_rings:
        return None
    feat = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": new_rings},
        "properties": feat["properties"],
    }
    return feat


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lightmap/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--force", action="store_true",
        help="Rebuild even if data/trees/trees.geojson already exists.",
    )
    args = parser.parse_args()

    if os.path.exists(OUT_PATH) and not args.force:
        size_kb = os.path.getsize(OUT_PATH) / 1024
        print(f"[skip] {OUT_PATH} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    print("Cambridge tree canopy (TopoJSON):")
    print(f"  Fetching {CAMBRIDGE_TREES_URL}")
    raw = fetch_bytes(CAMBRIDGE_TREES_URL)
    print(f"  Downloaded {len(raw)/1024/1024:.1f} MB")
    topo = json.loads(raw)

    print("  Decoding arcs...")
    arcs = decode_topojson_arcs(topo)
    print(f"  {len(arcs)} arcs decoded")

    obj_name = next(iter(topo.get("objects", {})))
    print(f"  Resolving geometries from object '{obj_name}'...")
    features = topojson_object_to_features(topo, obj_name, arcs)
    print(f"  {len(features)} polygon features")

    print(f"  Filtering to INITIAL_BBOX {INITIAL_BBOX}...")
    in_bbox = []
    for f in features:
        fb = feature_bbox(f)
        if fb is None:
            continue
        if not bbox_intersects(fb, INITIAL_BBOX):
            continue
        in_bbox.append(f)
    print(f"  {len(in_bbox)} inside bbox "
          f"(dropped {len(features) - len(in_bbox)})")

    print(f"  Simplifying (tolerance {SIMPLIFY_TOL_DEG} deg)...")
    simplified = []
    total_pts_before = 0
    total_pts_after = 0
    for f in in_bbox:
        for ring in f["geometry"]["coordinates"]:
            total_pts_before += len(ring)
        f2 = simplify_feature(f, SIMPLIFY_TOL_DEG)
        if f2 is None:
            continue
        for ring in f2["geometry"]["coordinates"]:
            total_pts_after += len(ring)
        # Round coordinates to 6 decimals (~11 cm).
        rounded_rings = []
        for ring in f2["geometry"]["coordinates"]:
            rounded_rings.append(
                [[round(pt[0], 6), round(pt[1], 6)] for pt in ring]
            )
        f2["geometry"]["coordinates"] = rounded_rings
        f2["properties"]["height_m"] = DEFAULT_TREE_HEIGHT_M
        simplified.append(f2)
    print(f"  {len(simplified)} features after simplify. "
          f"Vertices {total_pts_before} -> {total_pts_after} "
          f"({100 * total_pts_after / max(1, total_pts_before):.0f}%)")

    out = {"type": "FeatureCollection", "features": simplified}
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"  Saved {OUT_PATH} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
