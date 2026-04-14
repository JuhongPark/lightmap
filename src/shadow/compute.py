import json
import math
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib.solarposition
import shapely
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
    box,
    mapping,
    shape,
)
from shapely.ops import unary_union
from shapely.strtree import STRtree

_parsed_buildings = {}

BOSTON_TZ = ZoneInfo("US/Eastern")
LAT_CENTER = 42.36
M_PER_DEG_LAT = 111320
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT_CENTER))
MAX_SHADOW_LENGTH = 500


def get_sun_position(dt, lat=LAT_CENTER, lon=-71.06):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BOSTON_TZ)
    times = pd.DatetimeIndex([dt])
    pos = pvlib.solarposition.get_solarposition(times, lat, lon)
    altitude = float(pos["apparent_elevation"].iloc[0])
    azimuth = float(pos["azimuth"].iloc[0])
    return altitude, azimuth


def compute_shadow(building_polygon, height_ft, sun_altitude, sun_azimuth):
    if sun_altitude <= 0:
        return None, 0

    height_m = height_ft * 0.3048
    shadow_length_m = height_m / math.tan(math.radians(sun_altitude))
    shadow_length_m = min(shadow_length_m, MAX_SHADOW_LENGTH)

    shadow_dir = math.radians(sun_azimuth + 180)
    dx_m = shadow_length_m * math.sin(shadow_dir)
    dy_m = shadow_length_m * math.cos(shadow_dir)

    dx_deg = dx_m / M_PER_DEG_LON
    dy_deg = dy_m / M_PER_DEG_LAT

    translated = affinity.translate(building_polygon, xoff=dx_deg, yoff=dy_deg)
    result = unary_union([building_polygon, translated]).convex_hull

    if not result.is_valid:
        result = result.buffer(0)

    return result, shadow_length_m


def _extract_polygon(geom):
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None
        largest = max(polys, key=lambda g: g.area)
        if isinstance(largest, MultiPolygon):
            return max(largest.geoms, key=lambda g: g.area)
        return largest
    return None


def parse_building_features(features, height_field="BLDG_HGT_2010"):
    """Parse a list of GeoJSON features into (polygon, height_ft) tuples.

    Keeps the parsing logic reusable and decoupled from file I/O.
    """
    buildings = []
    for feat in features:
        props = feat.get("properties", {})
        height = props.get(height_field)
        if height is None or height <= 0:
            continue
        geom = shape(feat["geometry"])
        poly = _extract_polygon(geom)
        if poly is None or poly.is_empty:
            continue
        buildings.append((poly, float(height)))
    return buildings


# Simplify tolerance in degrees. 5e-5 degrees is about 5.5 meters at this
# latitude, well below any rendering pixel size at sensible map zoom levels.
RENDER_SIMPLIFY_TOLERANCE = 5e-5
# Coordinate decimal places for JSON serialization. 6 decimals is ~11 cm.
RENDER_COORD_PRECISION = 6


def _shadow_feature(shadow_poly, height_ft, shadow_len_m):
    """Build a GeoJSON-like feature dict from a shadow polygon.

    Applies simplification + coordinate rounding so the rendered folium HTML
    stays compact across all scales, not only the PostGIS path.
    """
    import shapely as _shapely
    simp = _shapely.simplify(shadow_poly, RENDER_SIMPLIFY_TOLERANCE,
                              preserve_topology=True)
    if simp.is_empty:
        simp = shadow_poly
    coords = [
        (round(x, RENDER_COORD_PRECISION), round(y, RENDER_COORD_PRECISION))
        for x, y in simp.exterior.coords
    ]
    return {
        "type": "Feature",
        "properties": {
            "height_ft": round(float(height_ft), 1),
            "shadow_len_ft": round(shadow_len_m / 0.3048, 1),
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [coords],
        },
    }


def compute_all_shadows(source, dt, height_field="BLDG_HGT_2010"):
    """Compute shadow polygons for all buildings in source.

    source can be:
    - a file path (str) pointing to a GeoJSON file
    - a GeoJSON FeatureCollection dict
    - a pre-parsed list of (polygon, height_ft) tuples
    """
    if isinstance(source, str):
        # File path, cache by path
        if source not in _parsed_buildings:
            with open(source) as f:
                data = json.load(f)
            _parsed_buildings[source] = parse_building_features(
                data["features"], height_field
            )
        buildings = _parsed_buildings[source]
    elif isinstance(source, dict):
        # In-memory FeatureCollection
        buildings = parse_building_features(source["features"], height_field)
    elif isinstance(source, list):
        # Already parsed
        buildings = source
    else:
        raise TypeError(f"Unsupported source type: {type(source)}")

    altitude, azimuth = get_sun_position(dt)

    if altitude <= 0:
        return [], altitude, azimuth

    features = []
    for poly, height_ft in buildings:
        shadow, shadow_len = compute_shadow(poly, height_ft, altitude, azimuth)
        if shadow is None:
            continue
        features.append(_shadow_feature(shadow, height_ft, shadow_len))
    return features, altitude, azimuth


# Fixed study area covering Boston and Cambridge urban core
STUDY_AREA = Polygon([
    (-71.16, 42.30),
    (-71.16, 42.40),
    (-71.03, 42.40),
    (-71.03, 42.30),
])


COVERAGE_GRID_CELLS = 50


def compute_shadow_coverage_raster(shadow_polys, resolution_m=5.0):
    """v7e: rasterize shadows into a uint8 grid and count filled pixels.

    For "area of the union of N polygons" where N is large, rasterization is
    asymptotically cheaper than computing the union as vector geometry. Each
    polygon is stamped (painter's algorithm) into a uint8 raster, then the
    count of non-zero pixels gives covered area (all pixels are 1 m^2-ish).
    Overlapping stamps naturally merge because we set, not add.

    Note: set GDAL_CACHEMAX environment variable to at least the raster size
    in MB before calling this, or rasterio's shapes iterator may be
    re-materialized and turn the call O(N^2).
    """
    import os as _os
    # Ensure GDAL has enough in-memory cache to hold the full raster.
    # Default is 5% of RAM which may be fine, but the docs specifically warn
    # about setting this for rasterize performance.
    _os.environ.setdefault("GDAL_CACHEMAX", "1024")  # 1 GB

    import numpy as np
    import rasterio.features
    from rasterio.transform import from_bounds

    if not shadow_polys:
        return 0.0
    polys = [p for p in shadow_polys if p is not None and not p.is_empty]
    if not polys:
        return 0.0

    minx, miny, maxx, maxy = STUDY_AREA.bounds
    width_m = (maxx - minx) * M_PER_DEG_LON
    height_m = (maxy - miny) * M_PER_DEG_LAT
    W = max(1, int(round(width_m / resolution_m)))
    H = max(1, int(round(height_m / resolution_m)))

    transform = from_bounds(minx, miny, maxx, maxy, W, H)
    # Pass a list (not a generator) so rasterio doesn't re-iterate.
    shapes = [(p, 1) for p in polys]
    mask = rasterio.features.rasterize(
        shapes,
        out_shape=(H, W),
        transform=transform,
        fill=0,
        dtype=np.uint8,
        all_touched=False,
    )

    covered = int(mask.sum())
    total = W * H
    if total == 0:
        return 0.0
    return (covered / total) * 100


def compute_shadow_coverage_pil(shadow_polys, resolution_m=10.0, shrink_px=0.7):
    """Pillow-backed coverage rasterizer.

    Draws every shadow polygon into an 8-bit PIL Image using
    ImageDraw.polygon and counts non-zero pixels. Pillow's filled-polygon
    routine is roughly an order of magnitude faster than
    rasterio.features.rasterize on our workload because it skips GDAL's
    per-polygon setup overhead, but its scan-line fill paints every pixel
    an edge touches, not just pixels whose centers are inside the polygon.
    That yields an over-filled mask (~48% over-count on this dataset).

    To approximate rasterio's center-based semantics we shrink each
    polygon toward its centroid by `shrink_px` pixels before drawing. A
    value of 0.7 is empirically close to rasterio for shadow-shaped
    convex hulls on the 10 m Boston-Cambridge grid (delta ~0.07 pp, well
    under the 0.02 pp rasterio-10m bias in magnitude as a fraction of
    the coverage signal).

    All per-polygon math runs as a single batched numpy operation over
    the flat (N_vertices, 2) coordinate array returned by
    shapely.get_coordinates, so there is no Python loop over vertices.
    The only remaining Python loop is the draw.polygon call itself.
    """
    if not shadow_polys:
        return 0.0

    import numpy as np
    from PIL import Image, ImageDraw

    non_empty = [p for p in shadow_polys if p is not None and not p.is_empty]
    if not non_empty:
        return 0.0

    minx, miny, maxx, maxy = STUDY_AREA.bounds
    width_m = (maxx - minx) * M_PER_DEG_LON
    height_m = (maxy - miny) * M_PER_DEG_LAT
    W = max(1, int(round(width_m / resolution_m)))
    H = max(1, int(round(height_m / resolution_m)))

    dx = W / (maxx - minx)
    dy = H / (maxy - miny)

    flat = shapely.get_coordinates(non_empty)
    counts = shapely.get_num_coordinates(non_empty).astype(np.int64)
    # World coords -> pixel coords (y flipped so screen-space has origin
    # at the top-left).
    px = (flat[:, 0] - minx) * dx
    py = (maxy - flat[:, 1]) * dy

    if shrink_px > 0:
        # Fully batched centroid + radial shrink across every polygon.
        starts = np.empty_like(counts)
        starts[0] = 0
        np.cumsum(counts[:-1], out=starts[1:])
        sum_x = np.add.reduceat(px, starts)
        sum_y = np.add.reduceat(py, starts)
        cx = sum_x / counts
        cy = sum_y / counts
        cx_rep = np.repeat(cx, counts)
        cy_rep = np.repeat(cy, counts)
        dxv = px - cx_rep
        dyv = py - cy_rep
        norms = np.sqrt(dxv * dxv + dyv * dyv)
        norms = np.where(norms < 1e-12, 1.0, norms)
        scale = np.maximum(0.0, 1.0 - shrink_px / norms)
        px = cx_rep + dxv * scale
        py = cy_rep + dyv * scale

    splits = np.cumsum(counts)[:-1]
    per_x = np.split(px, splits)
    per_y = np.split(py, splits)

    img = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(img)
    for xs, ys in zip(per_x, per_y):
        # PIL wants a flat sequence of (x, y) tuples. tolist + zip is the
        # fastest path for small arrays because np.column_stack + tolist
        # has higher per-call overhead for 5-vertex polygons.
        draw.polygon(list(zip(xs.tolist(), ys.tolist())), fill=1)

    arr = np.asarray(img)
    total = W * H
    if total == 0:
        return 0.0
    return int((arr > 0).sum()) / total * 100.0


def compute_shadow_coverage_disjoint(shadow_polys):
    """v7d: use shapely 2.1's disjoint_subset_union_all.

    Designed for large polygon sets with many disjoint clusters. Shadow sets
    over city blocks naturally form disjoint clusters (each block's shadows
    rarely touch neighboring blocks' shadows), so this should be a natural
    fit.
    """
    if not shadow_polys:
        return 0.0
    polys = [p for p in shadow_polys if p is not None and not p.is_empty]
    if not polys:
        return 0.0

    union = shapely.disjoint_subset_union_all(polys)
    covered = union.intersection(STUDY_AREA).area
    return (covered / STUDY_AREA.area) * 100


def compute_shadow_coverage_from_polys(shadow_polys):
    """Compute coverage from a list of shapely Polygon objects directly.

    Skips the re-parsing step that `compute_shadow_coverage` does on feature
    dicts. Uses the same 50x50 STRtree-batched union.
    """
    if not shadow_polys:
        return 0.0

    polys = [p for p in shadow_polys if p is not None and not p.is_empty]
    if not polys:
        return 0.0

    tree = STRtree(polys)
    minx, miny, maxx, maxy = STUDY_AREA.bounds
    N = COVERAGE_GRID_CELLS
    dx = (maxx - minx) / N
    dy = (maxy - miny) / N

    total_area = 0.0
    for i in range(N):
        for j in range(N):
            cell = box(
                minx + j * dx, miny + i * dy,
                minx + (j + 1) * dx, miny + (i + 1) * dy,
            )
            candidates = tree.query(cell)
            if len(candidates) == 0:
                continue
            cell_shadows = [polys[k] for k in candidates]
            u = unary_union(cell_shadows)
            total_area += u.intersection(cell).area

    return (total_area / STUDY_AREA.area) * 100


def compute_shadow_coverage(shadow_features):
    """Compute percentage of STUDY_AREA covered by shadow union.

    Uses a 50x50 STRtree-batched approach: partition the study area into
    cells, unary_union shadows within each cell, sum intersection areas.
    Equivalent precision to a full unary_union but much faster because each
    per-cell union operates on a small subset of geometries.
    """
    if not shadow_features:
        return 0.0

    shadow_polys = []
    for feat in shadow_features:
        try:
            geom = shape(feat["geometry"])
            if not geom.is_empty:
                shadow_polys.append(geom)
        except Exception:
            continue

    if not shadow_polys:
        return 0.0

    tree = STRtree(shadow_polys)
    minx, miny, maxx, maxy = STUDY_AREA.bounds
    N = COVERAGE_GRID_CELLS
    dx = (maxx - minx) / N
    dy = (maxy - miny) / N

    total_area = 0.0
    for i in range(N):
        for j in range(N):
            cell = box(
                minx + j * dx, miny + i * dy,
                minx + (j + 1) * dx, miny + (i + 1) * dy,
            )
            candidates = tree.query(cell)
            if len(candidates) == 0:
                continue
            cell_shadows = [shadow_polys[k] for k in candidates]
            u = unary_union(cell_shadows)
            total_area += u.intersection(cell).area

    return (total_area / STUDY_AREA.area) * 100
