"""Clip tree canopy polygons against the water mask.

Reads `data/trees/trees.geojson` (merged canopy patches) and
`data/water/water.geojson`, subtracts the water union from every
canopy polygon, and rewrites trees.geojson in place.

Fixes the visual artifact where canopy patches overlap the Charles
River and other water bodies: the `_merge_canopy` buffer-union step
in `download_trees.py` can push merged canopy polygons slightly
past a shoreline, and occasional source crowns already fall inside
water. A direct `difference(water_union)` cleans both.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from shapely.geometry import shape as _shape, mapping as _mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from city_config import DEFAULT_CITY_ID, load_city_profile, profile_data_path

TREES_PATH = os.path.join(REPO_ROOT, "data", "trees", "trees.geojson")
WATER_PATH = os.path.join(REPO_ROOT, "data", "water", "water.geojson")

# Small positive buffer applied to the water union so the clip pulls
# canopies back a meter or two from the actual shoreline. Keeps the
# visual free of hairline canopy lips over the edge of the river.
SHORELINE_PAD_DEG = 1e-5  # ~1.1 m at lat 42


def _load_fc(path):
    with open(path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not write trees.geojson back.")
    parser.add_argument(
        "--city", default=DEFAULT_CITY_ID,
        help="City profile id under cities/. Default: boston-cambridge.",
    )
    args = parser.parse_args()
    city = load_city_profile(args.city)
    trees_path = profile_data_path(city, "trees", "trees", "trees.geojson")
    water_path = profile_data_path(city, "water", "water", "water.geojson")

    if not os.path.exists(trees_path):
        print(f"missing {trees_path}", file=sys.stderr)
        return 2
    if not os.path.exists(water_path):
        print(f"missing {water_path}. Run scripts/download_water.py first.",
              file=sys.stderr)
        return 2

    trees_fc = _load_fc(trees_path)
    water_fc = _load_fc(water_path)

    water_geoms = []
    invalid = 0
    for f in water_fc.get("features", []):
        try:
            g = _shape(f["geometry"])
        except Exception:
            continue
        if g.is_empty:
            continue
        # OSM multipolygon stitching is approximate in download_water.py,
        # so some polygons come in with self-touch / bowtie topology.
        # make_valid repairs these into a valid (Multi)Polygon before
        # unary_union, which errors otherwise.
        if not g.is_valid:
            invalid += 1
            g = make_valid(g)
            if g.is_empty:
                continue
        water_geoms.append(g)
    if invalid:
        print(f"  repaired {invalid} invalid water polygons")
    print(f"water: {len(water_geoms)} polygons")
    water_union = unary_union(water_geoms)
    if SHORELINE_PAD_DEG:
        water_union = water_union.buffer(SHORELINE_PAD_DEG, resolution=2)
    print(f"water union (padded {SHORELINE_PAD_DEG} deg): "
          f"{water_union.geom_type}, area~{water_union.area:.5f} deg^2")

    cleaned = []
    trimmed = 0
    dropped = 0
    out_polys = 0
    for f in trees_fc.get("features", []):
        try:
            g = _shape(f["geometry"])
        except Exception:
            continue
        if g.is_empty:
            continue
        if not g.intersects(water_union):
            cleaned.append(f)
            out_polys += 1
            continue
        clipped = g.difference(water_union)
        if clipped.is_empty:
            dropped += 1
            continue
        trimmed += 1
        props = f.get("properties") or {}
        # Split MultiPolygon back into individual Polygon features so
        # downstream code (which expects Polygon only) stays happy.
        if clipped.geom_type == "Polygon":
            geoms = [clipped]
        elif clipped.geom_type == "MultiPolygon":
            geoms = list(clipped.geoms)
        else:
            # GeometryCollection edge case
            geoms = [g2 for g2 in getattr(clipped, "geoms", [])
                     if g2.geom_type in ("Polygon", "MultiPolygon")]
            geoms = sum(([x] if x.geom_type == "Polygon" else list(x.geoms)
                         for x in geoms), [])

        for g2 in geoms:
            if g2.is_empty or g2.area < 1e-10:
                continue
            # Round coord precision to 6 decimals to match the source file.
            m = _mapping(g2)
            if m["type"] == "Polygon":
                coords = [[[round(x, 6), round(y, 6)] for x, y in ring]
                          for ring in m["coordinates"]]
            else:
                coords = [
                    [[[round(x, 6), round(y, 6)] for x, y in ring]
                     for ring in poly]
                    for poly in m["coordinates"]
                ]
            cleaned.append({
                "type": "Feature",
                "geometry": {"type": m["type"], "coordinates": coords},
                "properties": dict(props),
            })
            out_polys += 1

    print(f"trees: {len(trees_fc['features'])} in, "
          f"{out_polys} out, {trimmed} trimmed, {dropped} dropped entirely")

    if args.dry_run:
        print("[dry-run] not writing.")
        return 0

    fc = {"type": "FeatureCollection", "features": cleaned}
    with open(trees_path, "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    size_kb = os.path.getsize(trees_path) / 1024
    print(f"Saved {trees_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
