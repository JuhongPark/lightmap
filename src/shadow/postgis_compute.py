"""PostGIS-backed shadow computation.

Delegates geometry projection to PostGIS using ST_Translate + ST_ConvexHull.
The sun position still comes from pvlib in Python.

Key idea: PostGIS computes all 123K shadow polygons in a single SQL query
using the GiST-indexed buildings table, then streams the results back as WKB.
"""

import math

import numpy as np
import psycopg2
import shapely
from shapely.wkb import loads as wkb_loads

from shadow.compute import (
    LAT_CENTER,
    M_PER_DEG_LAT,
    M_PER_DEG_LON,
    MAX_SHADOW_LENGTH,
    STUDY_AREA,
    get_sun_position,
)

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "lightmap",
    "user": "lightmap",
    "password": "lightmap",
}


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def compute_all_shadows_postgis(conn, dt, cities=None, sample_pct=100,
                                 return_polygons=False):
    """Compute shadow polygons for all (or sampled) buildings via PostGIS.

    Returns a list of dicts shaped like the Python compute_all_shadows output,
    along with altitude and azimuth from pvlib.

    If return_polygons is True, also returns the list of shapely polygon
    objects alongside the features (skipping re-parsing later).

    sample_pct: 100 = all buildings, 10 = ~10% random sample (using TABLESAMPLE).
    """
    altitude, azimuth = get_sun_position(dt)
    if altitude <= 0:
        if return_polygons:
            return [], [], altitude, azimuth
        return [], altitude, azimuth

    # Shadow direction in degrees (radians inside SQL below)
    height_m_per_ft = 0.3048
    shadow_dir_rad = math.radians(azimuth + 180)
    # Use a fixed shadow length factor = 1 / tan(altitude), capped inside SQL.
    cotangent = 1.0 / math.tan(math.radians(altitude))

    dx_per_m = math.sin(shadow_dir_rad)
    dy_per_m = math.cos(shadow_dir_rad)

    # Precompute scalar per-foot displacement (in degrees)
    # shadow_m = height_ft * 0.3048 * cotangent (capped at MAX_SHADOW_LENGTH)
    # dx_deg = shadow_m * dx_per_m / M_PER_DEG_LON
    # dy_deg = shadow_m * dy_per_m / M_PER_DEG_LAT
    c = conn.cursor()

    # v7b: request max parallelism for this session/transaction.
    # Shadow projection is embarrassingly parallel (per-row translate+hull),
    # but the default max_parallel_workers_per_gather=2 leaves cores idle.
    c.execute("SET LOCAL max_parallel_workers_per_gather = 8")
    c.execute("SET LOCAL parallel_tuple_cost = 0.001")
    c.execute("SET LOCAL parallel_setup_cost = 10")
    c.execute("SET LOCAL min_parallel_table_scan_size = '1MB'")

    # Use a CTE to:
    #  1. Sample buildings (optional)
    #  2. Compute shadow length per building (capped)
    #  3. Translate the polygon by (dx, dy)
    #  4. Convex hull of original + translated
    where_clause = ""
    params = [
        height_m_per_ft, cotangent, MAX_SHADOW_LENGTH,
        dx_per_m / M_PER_DEG_LON,
        dy_per_m / M_PER_DEG_LAT,
    ]

    if sample_pct < 100:
        where_clause = "WHERE random() < %s"
        params.append(sample_pct / 100.0)

    sql = f"""
        WITH shadow_calc AS (
            SELECT
                b.height_ft,
                LEAST(b.height_ft * %s * %s, %s) AS shadow_m,
                b.geom
            FROM buildings b
            {where_clause}
        ),
        translated AS (
            SELECT
                height_ft,
                shadow_m,
                geom,
                ST_Translate(geom, shadow_m * %s, shadow_m * %s) AS shifted
            FROM shadow_calc
            WHERE shadow_m > 0
        )
        SELECT
            height_ft,
            shadow_m,
            ST_AsBinary(ST_ConvexHull(ST_Union(geom, shifted))) AS shadow_wkb
        FROM translated
    """

    c.execute(sql, params)
    rows = c.fetchall()
    c.close()

    if not rows:
        if return_polygons:
            return [], [], altitude, azimuth
        return [], altitude, azimuth

    # v7c: batch WKB decode via shapely.from_wkb (C-side, GIL released)
    # Filter out null WKB up front so every downstream array stays aligned
    # with the metadata list.
    valid = [(r[0], r[1], r[2]) for r in rows if r[2] is not None]
    wkb_list = [bytes(r[2]) for r in valid]
    polygons_arr = shapely.from_wkb(wkb_list)

    # Simplify shadow geometries to cut vertex count. Shadows are convex
    # hulls of building + translated building, and that hull can carry
    # 10-20+ vertices on complex buildings. Simplifying at 5e-5 deg (~5.5m)
    # cuts the per-polygon work downstream (coverage stages + HTML size).
    # preserve_topology=False roughly halves the simplify cost and is safe
    # here because the input is a convex hull (no interior topology to
    # preserve). A handful of very small shadows collapse to empty under
    # this mode, so for those we fall back to the raw hull rather than
    # dropping the row and losing the visual from the rendered map.
    simplified_arr = shapely.simplify(
        polygons_arr, 5e-5, preserve_topology=False
    )
    empty_mask = shapely.is_empty(simplified_arr)
    display_arr = np.where(empty_mask, polygons_arr, simplified_arr)

    # valid_kept stays aligned 1:1 with display_arr; no rows dropped.
    valid_kept = [(m[0], m[1]) for m in valid]

    # Batch coord extraction: one C call into GEOS produces a flat (N, 2)
    # ndarray of every vertex across every polygon, and a parallel int
    # array of per-polygon vertex counts lets us slice it without a Python
    # loop over vertices.
    flat_coords = shapely.get_coordinates(display_arr)
    np.round(flat_coords, 6, out=flat_coords)
    vertex_counts = shapely.get_num_coordinates(display_arr)
    split_points = np.cumsum(vertex_counts)[:-1]
    per_poly_coords = np.split(flat_coords, split_points)

    # Build features. The only per-row Python work left is wrapping the
    # numpy slice with a GeoJSON-shaped dict, which is unavoidable because
    # folium and consumers expect this shape.
    features = [
        {
            "type": "Feature",
            "properties": {
                "height_ft": round(float(height_ft), 1),
                "shadow_len_ft": round(float(shadow_m) / 0.3048, 1),
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords.tolist()],
            },
        }
        for (height_ft, shadow_m), coords in zip(valid_kept, per_poly_coords)
    ]

    if return_polygons:
        # Coverage uses the raw (unsimplified) hull polygons to preserve
        # area accuracy. Any simplification here would erode small shadows
        # and shift the coverage number (see v4 correctness fix).
        return features, polygons_arr.tolist(), altitude, azimuth
    return features, altitude, azimuth



def compute_shadow_coverage_postgis(conn, dt):
    """Compute shadow coverage directly in PostGIS using ST_Area and ST_Intersection.

    Uses the same ST_Translate + ST_ConvexHull shadow logic as
    compute_all_shadows_postgis, but aggregates the union and intersection
    entirely on the database side so we never materialize 123K polygons in
    Python.
    """
    altitude, azimuth = get_sun_position(dt)
    if altitude <= 0:
        return 0.0

    shadow_dir_rad = math.radians(azimuth + 180)
    cotangent = 1.0 / math.tan(math.radians(altitude))
    dx_per_m = math.sin(shadow_dir_rad)
    dy_per_m = math.cos(shadow_dir_rad)

    sa_minx, sa_miny, sa_maxx, sa_maxy = STUDY_AREA.bounds

    c = conn.cursor()
    sql = """
        WITH shadow_calc AS (
            SELECT
                LEAST(height_ft * %s * %s, %s) AS shadow_m,
                geom
            FROM buildings
        ),
        shadows AS (
            SELECT
                ST_ConvexHull(
                    ST_Union(
                        geom,
                        ST_Translate(geom, shadow_m * %s, shadow_m * %s)
                    )
                ) AS shadow_geom
            FROM shadow_calc
            WHERE shadow_m > 0
        ),
        study_area AS (
            SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS geom
        )
        SELECT
            ST_Area(ST_Intersection(ST_Union(s.shadow_geom), sa.geom)) /
            ST_Area(sa.geom) * 100 AS coverage_pct
        FROM shadows s, study_area sa
        GROUP BY sa.geom
    """
    c.execute(sql, [
        0.3048, cotangent, MAX_SHADOW_LENGTH,
        dx_per_m / M_PER_DEG_LON,
        dy_per_m / M_PER_DEG_LAT,
        sa_minx, sa_miny, sa_maxx, sa_maxy,
    ])
    row = c.fetchone()
    c.close()
    return float(row[0]) if row and row[0] is not None else 0.0


def load_buildings_postgis(conn):
    """Load buildings from PostGIS, returning (geojson_dict, parsed_tuples).

    Drop-in replacement for load_buildings_with_parsed at 100% scale.
    Coordinates in the feature dict are rounded to 6 decimal places to keep
    the rendered folium HTML at a reasonable size.
    """
    c = conn.cursor()
    c.execute("SELECT height_ft, ST_AsBinary(geom) FROM buildings")
    rows = c.fetchall()
    c.close()

    wkb_list = [bytes(r[1]) for r in rows]
    polygons_arr = shapely.from_wkb(wkb_list)

    # Also simplify the building geometries used for the rendered folium
    # layer. Shadows are computed from the unsimplified polygons above to
    # preserve accuracy.
    simplified_arr = shapely.simplify(polygons_arr, 5e-5, preserve_topology=True)

    # Pick simplified, but fall back to the original for polygons that
    # simplify degenerates to empty. This preserves v5 behaviour.
    empty_mask = shapely.is_empty(simplified_arr)
    display_arr = np.where(empty_mask, polygons_arr, simplified_arr)

    # Batch coord extraction: one C call into GEOS produces a flat (N, 2)
    # ndarray of every vertex across every polygon, and a parallel int
    # array of per-polygon vertex counts lets us slice it without a Python
    # loop over vertices.
    flat_coords = shapely.get_coordinates(display_arr)
    np.round(flat_coords, 6, out=flat_coords)
    vertex_counts = shapely.get_num_coordinates(display_arr)
    split_points = np.cumsum(vertex_counts)[:-1]
    per_poly_coords = np.split(flat_coords, split_points)

    heights = [float(r[0]) for r in rows]
    polygons = polygons_arr.tolist()
    parsed = list(zip(polygons, heights))

    features = [
        {
            "type": "Feature",
            "properties": {"BLDG_HGT_2010": round(h, 1)},
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords.tolist()],
            },
        }
        for h, coords in zip(heights, per_poly_coords)
    ]
    return {"type": "FeatureCollection", "features": features}, parsed
