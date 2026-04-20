"""Build the merged tree canopy dataset for the time-slider.

Combines two sources into a single `data/trees/trees.geojson`:

1. Cambridge tree canopy (2018) from the Cambridge GIS GitHub mirror.
   Delivered as TopoJSON in WGS84. Converted to GeoJSON in pure
   Python (topology arc decoding).

2. Boston tree canopy change assessment (2019-2024) from data.boston.gov.
   Delivered as a ~1 GB ZIP containing `TreeTops2024.geojson` (~2.4 GB
   uncompressed) among several other layers. To avoid loading the
   giant file into memory we:
     a) stream the download to disk in 1 MiB chunks (no buffering),
     b) open the inner GeoJSON via GDAL's `/vsizip/` virtual FS and
        let `ogr2ogr` apply `-spat INITIAL_BBOX` so only features
        inside the viewport leave the GDAL process (~280 MB clipped),
     c) simplify + unit-convert (feet to meters) in Python with a
        RLIMIT_AS cap so a runaway allocation dies cleanly instead
        of taking the machine with it.
   System `ogr2ogr` (GDAL) is required. It is already present at
   `/usr/bin/ogr2ogr` on most Linux dev machines.

Both sources are filtered to INITIAL_BBOX (same clamp the rest of the
time-slider uses) and simplified with a ~2 m tolerance before merge.

Usage
-----
    .venv/bin/python scripts/download_trees.py
    .venv/bin/python scripts/download_trees.py --force
    .venv/bin/python scripts/download_trees.py --skip-boston
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import shutil
import subprocess
import sys

import httpx
from shapely.geometry import shape
from shapely.ops import unary_union

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(REPO_ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "trees", "trees.geojson")

CAMBRIDGE_TREES_URL = (
    "https://raw.githubusercontent.com/cambridgegis/"
    "cambridgegis_data_environmental/main/Tree_Canopy_2018/"
    "ENVIRONMENTAL_TreeCanopy2018.topojson"
)

# Boston tree canopy change assessment 2019-2024 (BPDA). GeoJSON
# inside a ZIP in WGS84, so no CRS reprojection is needed at clip
# time (we still pass -t_srs EPSG:4326 defensively).
BOSTON_TREES_URL = (
    "https://data.boston.gov/dataset/b619811b-c52e-417c-a9d0-e82c19f89ca3"
    "/resource/7645f9fd-c8d8-4f08-9b6d-6cf35ff895a0"
    "/download/2019-2024-data.zip"
)
# Name of the GeoJSON entry inside the Boston ZIP that holds per-tree
# crown polygons. Alternatives in the same archive: ForestPatches2024
# (fewer, coarser polygons) and TreeCentroids2024 (points only).
BOSTON_ZIP_MEMBER = "2019-2024Data/TreeTops2024.geojson"
# Cache the downloaded ZIP and the ogr2ogr-clipped GeoJSON in /tmp so
# repeat runs of this script skip the ~2 min network + GDAL passes.
BOSTON_ZIP_CACHE = "/tmp/lightmap_boston_trees.zip"
BOSTON_CLIP_CACHE = "/tmp/lightmap_boston_clipped.geojson"
# Safety cap for the Python simplify step. The clipped file is ~280 MB
# on disk and peaks at ~1.3 GB of RSS when json.load parses it into
# dicts. 3 GB gives comfortable headroom while still OOM-killing any
# runaway allocation before it can swap the machine.
BOSTON_RLIMIT_BYTES = 3 * 1024 ** 3
# Feet -> meters. Boston TreeTops Height is in feet.
FT_TO_M = 0.3048
# Tree height sanity window (meters). LiDAR crown detection occasionally
# misclassifies stacks, light poles, and towers, which show up as
# 100+ m "trees". Cap at 40 m to drop those; 1.5 m floor keeps small
# ornamentals without letting ground clutter through.
BOSTON_MIN_H_M = 1.5
BOSTON_MAX_H_M = 40.0

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


def fetch_bytes(url, retries=3):
    """Fetch URL with httpx (follows redirects correctly for S3
    signed URLs where urllib's redirect handler mangles the
    signature) and retry transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=300, follow_redirects=True,
                              headers={"User-Agent": "lightmap/0.1"}) as client:
                resp = client.get(url)
                if resp.status_code >= 400:
                    body = resp.text[:400]
                    raise RuntimeError(
                        f"HTTP {resp.status_code} for {url}\n  {body}"
                    )
                return resp.content
        except httpx.HTTPError as e:
            last_err = e
            print(f"  transient network error on attempt {attempt + 1}: {e}")
        except RuntimeError:
            raise
    raise RuntimeError(
        f"download failed after {retries} attempts: {last_err}"
    )


def _process_features(features, label):
    """Filter features to INITIAL_BBOX, simplify, round coords, and
    attach the default tree height. Returns the cleaned feature list."""
    print(f"  Filtering {label} to INITIAL_BBOX...")
    in_bbox = []
    for f in features:
        fb = feature_bbox(f)
        if fb is None:
            continue
        if not bbox_intersects(fb, INITIAL_BBOX):
            continue
        in_bbox.append(f)
    print(f"    {len(in_bbox)} inside bbox "
          f"(dropped {len(features) - len(in_bbox)})")

    simplified = []
    before, after = 0, 0
    for f in in_bbox:
        for ring in f["geometry"]["coordinates"]:
            before += len(ring)
        f2 = simplify_feature(f, SIMPLIFY_TOL_DEG)
        if f2 is None:
            continue
        for ring in f2["geometry"]["coordinates"]:
            after += len(ring)
        rounded = []
        for ring in f2["geometry"]["coordinates"]:
            rounded.append(
                [[round(pt[0], 6), round(pt[1], 6)] for pt in ring]
            )
        f2["geometry"]["coordinates"] = rounded
        f2["properties"] = {"height_m": DEFAULT_TREE_HEIGHT_M}
        simplified.append(f2)
    print(f"    {len(simplified)} features after simplify. "
          f"Vertices {before} -> {after} "
          f"({100 * after / max(1, before):.0f}%)")
    return simplified


def cambridge_features():
    print("Cambridge tree canopy (TopoJSON):")
    print(f"  Fetching {CAMBRIDGE_TREES_URL}")
    raw = fetch_bytes(CAMBRIDGE_TREES_URL)
    print(f"  Downloaded {len(raw)/1024/1024:.1f} MB")
    topo = json.loads(raw)
    arcs = decode_topojson_arcs(topo)
    obj_name = next(iter(topo.get("objects", {})))
    feats = topojson_object_to_features(topo, obj_name, arcs)
    print(f"  Decoded {len(arcs)} arcs, {len(feats)} polygons")
    return _process_features(feats, "Cambridge")


def _stream_download(url, dest, min_mb=10):
    """Download `url` to `dest` with chunked writes so the 1 GB Boston
    ZIP never sits in Python memory. If `dest` already exists and is
    at least `min_mb` megabytes, treat it as a valid cache and skip.
    """
    if os.path.exists(dest) and os.path.getsize(dest) >= min_mb * 1024 * 1024:
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"  cached ZIP at {dest} ({size_mb:.1f} MB), skipping download")
        return
    print(f"  streaming {url}")
    total = 0
    tmp_dest = dest + ".part"
    try:
        with httpx.Client(timeout=600, follow_redirects=True,
                          headers={"User-Agent": "lightmap/0.1"}) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    body = resp.read()[:400].decode(errors="replace")
                    raise RuntimeError(
                        f"HTTP {resp.status_code} for {url}\n  {body}")
                expected = int(resp.headers.get("content-length", 0))
                chunk_mb = 1 << 20
                last_tick = 0
                with open(tmp_dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_mb):
                        f.write(chunk)
                        total += len(chunk)
                        if total // (50 * chunk_mb) != last_tick:
                            last_tick = total // (50 * chunk_mb)
                            pct = 100 * total / expected if expected else 0
                            print(f"    ... {total/1024/1024:.0f} MB "
                                  f"({pct:.0f}%)")
        os.replace(tmp_dest, dest)
    finally:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)
    print(f"  downloaded {total/1024/1024:.1f} MB -> {dest}")


def _gdal_clip_boston(zip_path, dst):
    """Use ogr2ogr to open the GeoJSON inside the ZIP via /vsizip/ and
    write only features inside INITIAL_BBOX. Output is ~280 MB instead
    of the 2.4 GB original. Pre-filters Height in feet so noise
    (negative, absurd values) never reach Python."""
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        print(f"  cached clip at {dst} "
              f"({os.path.getsize(dst)/1024/1024:.1f} MB), skipping ogr2ogr")
        return
    min_lat, min_lon, max_lat, max_lon = INITIAL_BBOX
    vsizip_src = f"/vsizip/{zip_path}/{BOSTON_ZIP_MEMBER}"
    # Height check uses feet because the source column is feet. The
    # bracket matches BOSTON_MIN/MAX_H_M converted back to feet with
    # generous margin: GDAL's -where runs before Python's filter.
    cmd = [
        "ogr2ogr", "-f", "GeoJSON",
        "-spat", str(min_lon), str(min_lat), str(max_lon), str(max_lat),
        "-spat_srs", "EPSG:4326", "-t_srs", "EPSG:4326",
        "-where", "CAST(Height AS float) > 3 AND CAST(Height AS float) < 150",
        dst, vsizip_src,
    ]
    print(f"  ogr2ogr clip: {vsizip_src}")
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"  clipped -> {dst} "
          f"({os.path.getsize(dst)/1024/1024:.1f} MB)")


def boston_features():
    """Fetch + clip + simplify Boston TreeTops to INITIAL_BBOX.

    Memory strategy:
      - Download streams chunked into /tmp, never in memory.
      - ogr2ogr opens the inner GeoJSON inside the ZIP via /vsizip/
        and writes only INITIAL_BBOX features (bypass the 2.4 GB full
        scan on the Python side).
      - Python loads the 280 MB clipped file inside a RLIMIT_AS cap
        so any accidental runaway allocation dies cleanly.
    """
    if shutil.which("ogr2ogr") is None:
        print("Boston tree canopy: ogr2ogr not found on PATH, skipping.")
        print("  Install GDAL (system) or rerun with --skip-boston.")
        return []

    print("Boston tree canopy (ZIP + /vsizip/ + ogr2ogr):")
    _stream_download(BOSTON_TREES_URL, BOSTON_ZIP_CACHE, min_mb=500)

    try:
        _gdal_clip_boston(BOSTON_ZIP_CACHE, BOSTON_CLIP_CACHE)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="replace")[:400]
        print(f"  ogr2ogr failed: {stderr}")
        return []

    # Cap virtual address space for the (only) heavy step in this
    # script. Other steps use < 50 MB so the cap is harmless to them.
    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (BOSTON_RLIMIT_BYTES, BOSTON_RLIMIT_BYTES),
        )
    except (ValueError, resource.error) as e:
        print(f"  warn: could not set RLIMIT_AS: {e}")

    print(f"  loading clipped GeoJSON...")
    with open(BOSTON_CLIP_CACHE) as f:
        gj = json.load(f)
    feats_in = gj.get("features", [])
    # Drop the wrapper dict so we release the large parse tree ASAP.
    del gj
    print(f"  {len(feats_in)} features loaded")

    out = []
    before_pts = 0
    after_pts = 0
    dropped_h = 0
    dropped_small = 0
    for f in feats_in:
        props = f.get("properties") or {}
        try:
            h_ft = float(props.get("Height"))
        except (TypeError, ValueError):
            dropped_h += 1
            continue
        h_m = h_ft * FT_TO_M
        if h_m < BOSTON_MIN_H_M or h_m > BOSTON_MAX_H_M:
            dropped_h += 1
            continue
        coords = f.get("geometry", {}).get("coordinates") or []
        if not coords:
            continue
        for ring in coords:
            before_pts += len(ring)
        f2 = simplify_feature(f, SIMPLIFY_TOL_DEG)
        if f2 is None:
            dropped_small += 1
            continue
        for ring in f2["geometry"]["coordinates"]:
            after_pts += len(ring)
        rounded = [
            [[round(pt[0], 6), round(pt[1], 6)] for pt in ring]
            for ring in f2["geometry"]["coordinates"]
        ]
        out.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": rounded},
            "properties": {"height_m": round(h_m, 2)},
        })

    print(f"  kept {len(out)}, dropped {dropped_h} (height), "
          f"{dropped_small} (too-small)")
    if before_pts:
        print(f"  vertex {before_pts} -> {after_pts} "
              f"({100 * after_pts / before_pts:.0f}%)")
    return out


def _merge_canopy(features, bridge_deg=5e-5, simplify_deg=3e-5):
    """Union every crown polygon into a coarser "shade area" layer.

    The time-slider only needs to know *where* canopy shade falls,
    not the individual tree it came from. Unioning 60K crown polygons
    into a few thousand merged patches drops the rendered polygon
    count by ~20x and the embedded JSON size by similar. The
    `bridge_deg` buffer (~5.5 m at 42 deg N) glues adjacent crowns
    along a tree-lined street into a continuous shade strip. We
    un-buffer by the same amount so the outer footprint stays close
    to the original canopy reach; the net effect is "same shade
    area, one polygon per shade zone instead of per tree".
    """
    polys = []
    for f in features:
        g = f.get("geometry") or {}
        if g.get("type") == "Polygon":
            try:
                polys.append(shape(g))
            except Exception:
                continue
    if not polys:
        return []
    print(f"  Union input: {len(polys)} crown polygons")
    buffered = [p.buffer(bridge_deg, resolution=2) for p in polys]
    merged = unary_union(buffered)
    if not merged.is_empty:
        merged = merged.buffer(-bridge_deg, resolution=2)
    merged = merged.simplify(simplify_deg, preserve_topology=True)

    if merged.is_empty:
        return []
    if merged.geom_type == "Polygon":
        geoms = [merged]
    elif merged.geom_type == "MultiPolygon":
        geoms = list(merged.geoms)
    else:
        return []

    out_feats = []
    for g in geoms:
        if g.is_empty:
            continue
        rings = [list(g.exterior.coords)]
        for r in g.interiors:
            rings.append(list(r.coords))
        rings = [[[round(x, 6), round(y, 6)] for x, y in r] for r in rings]
        out_feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": rings},
            "properties": {"height_m": DEFAULT_TREE_HEIGHT_M},
        })
    total_pts = sum(sum(len(r) for r in f["geometry"]["coordinates"])
                    for f in out_feats)
    print(f"  Union output: {len(out_feats)} patches, {total_pts} vertices")
    return out_feats


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-boston", action="store_true",
                        help="Skip the 2019-2024 Boston download.")
    parser.add_argument("--no-merge", action="store_true",
                        help="Keep per-crown polygons instead of unioning "
                             "into shade-area patches. Produces a much "
                             "larger output file.")
    args = parser.parse_args()

    if os.path.exists(OUT_PATH) and not args.force:
        size_kb = os.path.getsize(OUT_PATH) / 1024
        print(f"[skip] {OUT_PATH} already exists ({size_kb:.1f} KB). "
              f"Use --force to redownload.")
        return 0

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    all_feats = cambridge_features()
    if not args.skip_boston:
        try:
            all_feats += boston_features()
        except Exception as e:
            print(f"Boston tree canopy failed: {e}")
            print("  Continuing with Cambridge only.")

    if not args.no_merge:
        print("\nMerging crowns into shade-area patches:")
        all_feats = _merge_canopy(all_feats)

    out = {"type": "FeatureCollection", "features": all_feats}
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"\nSaved {OUT_PATH} ({size_kb:.1f} KB, "
          f"{len(all_feats)} total features)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
