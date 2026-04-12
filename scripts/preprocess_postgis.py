"""Import buildings into PostGIS with spatial indexing.

Creates the schema, loads 123K Boston + Cambridge buildings, and builds
GiST spatial indexes for fast bbox and geometry queries.

Usage:
    .venv/bin/python scripts/preprocess_postgis.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import psycopg2
from psycopg2 import Binary
from psycopg2.extras import execute_batch
from shapely.geometry import shape
from shapely.wkb import dumps as wkb_dumps

from shadow.compute import _extract_polygon

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
BOSTON_PATH = os.path.join(DATA_DIR, "buildings", "boston_buildings.geojson")
CAMBRIDGE_PATH = os.path.join(DATA_DIR, "cambridge", "buildings", "buildings.geojson")

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "lightmap",
    "user": "lightmap",
    "password": "lightmap",
}


def create_schema(conn):
    c = conn.cursor()
    c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    c.execute("DROP TABLE IF EXISTS buildings CASCADE")
    c.execute("""
        CREATE TABLE buildings (
            id SERIAL PRIMARY KEY,
            city TEXT NOT NULL,
            height_ft REAL NOT NULL,
            geom GEOMETRY(Polygon, 4326) NOT NULL
        )
    """)
    conn.commit()


def insert_features(conn, features, city):
    c = conn.cursor()
    rows = []
    for feat in features:
        props = feat.get("properties", {})
        if city == "cambridge":
            h = props.get("TOP_GL")
            if h is None or h <= 0:
                continue
            height_ft = round(h * 3.28084, 1)
        else:
            h = props.get("BLDG_HGT_2010")
            if h is None or h <= 0:
                continue
            height_ft = float(h)
        try:
            geom = shape(feat["geometry"])
            poly = _extract_polygon(geom)
            if poly is None or poly.is_empty:
                continue
            wkb = wkb_dumps(poly)
            rows.append((city, height_ft, Binary(wkb)))
        except Exception:
            continue

    execute_batch(
        c,
        "INSERT INTO buildings (city, height_ft, geom) VALUES (%s, %s, ST_GeomFromWKB(%s, 4326))",
        rows,
        page_size=1000,
    )
    conn.commit()
    return len(rows)


def create_indexes(conn):
    c = conn.cursor()
    c.execute("CREATE INDEX idx_buildings_geom ON buildings USING GIST (geom)")
    c.execute("CREATE INDEX idx_buildings_city ON buildings (city)")
    # v7b: tell the planner to use up to 8 parallel workers when scanning this
    # table. Without this the planner's default log2-of-size formula picks
    # only 2 workers for a 50 MB table, leaving 75% of the cores idle.
    c.execute("ALTER TABLE buildings SET (parallel_workers = 8)")
    c.execute("VACUUM ANALYZE buildings")
    conn.commit()


def main():
    t0 = time.perf_counter()
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    print("Creating schema...")
    create_schema(conn)

    print(f"Parsing Cambridge: {CAMBRIDGE_PATH}")
    with open(CAMBRIDGE_PATH) as f:
        cam_data = json.load(f)
    n_cam = insert_features(conn, cam_data["features"], "cambridge")
    print(f"  {n_cam:,} Cambridge buildings inserted")

    print(f"Parsing Boston: {BOSTON_PATH}")
    with open(BOSTON_PATH) as f:
        bos_data = json.load(f)
    n_bos = insert_features(conn, bos_data["features"], "boston")
    print(f"  {n_bos:,} Boston buildings inserted")

    # VACUUM cannot run inside a transaction with psycopg2 default
    conn.autocommit = True
    print("Building GiST spatial index...")
    create_indexes(conn)
    conn.close()

    elapsed = time.perf_counter() - t0
    print(f"\nDone in {elapsed:.1f}s")
    print(f"Total buildings: {n_cam + n_bos:,}")


if __name__ == "__main__":
    main()
