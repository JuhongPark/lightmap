"""Pre-process building GeoJSON into a fast-loading SQLite + WKB database.

This is the v5 optimization: instead of parsing 160MB of GeoJSON text every run,
we parse once and store the pre-cleaned (polygon_wkb, height_ft, bbox) rows in
a SQLite database. Subsequent loads read the binary WKB which Shapely decodes
an order of magnitude faster than JSON.

Schema:
    CREATE TABLE buildings (
        id INTEGER PRIMARY KEY,
        city TEXT,
        height_ft REAL,
        min_x REAL, min_y REAL, max_x REAL, max_y REAL,
        geom BLOB        -- shapely WKB of largest polygon
    );
    CREATE INDEX idx_bbox ON buildings (min_x, min_y, max_x, max_y);

Usage:
    .venv/bin/python scripts/preprocess_buildings.py
"""

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shapely.geometry import shape
from shapely.wkb import dumps

from shadow.compute import _extract_polygon

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
BOSTON_PATH = os.path.join(DATA_DIR, "buildings", "boston_buildings.geojson")
CAMBRIDGE_PATH = os.path.join(DATA_DIR, "cambridge", "buildings", "buildings.geojson")
DB_PATH = os.path.join(DATA_DIR, "buildings.db")


def create_db(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE buildings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            height_ft REAL NOT NULL,
            min_x REAL NOT NULL,
            min_y REAL NOT NULL,
            max_x REAL NOT NULL,
            max_y REAL NOT NULL,
            geom BLOB NOT NULL
        )
    """)
    c.execute("CREATE INDEX idx_bbox ON buildings (min_x, min_y, max_x, max_y)")
    c.execute("CREATE INDEX idx_city ON buildings (city)")
    conn.commit()
    return conn


def insert_features(conn, features, city, height_conversion=1.0):
    c = conn.cursor()
    rows = []
    for feat in features:
        props = feat.get("properties", {})
        if city == "cambridge":
            h = props.get("TOP_GL")
            if h is None or h <= 0:
                # Cambridge TOP_GL missing: assume ~2-story residential.
                # Source data drops these silently, which leaves ~18 %
                # of Boston buildings with no footprint AND no shadow.
                # A 20 ft default casts a ~10 ft (3 m) shadow at 2 pm,
                # visible on the 2 m-per-px shadow PNG. Better an honest
                # default than a missing building.
                h = 6.1  # 20 ft in meters
            height_ft = round(h * 3.28084, 1)
        else:
            h = props.get("BLDG_HGT_2010")
            if h is None or h <= 0:
                # Boston: 23 487 of 128 608 buildings (18.3 %) have
                # BLDG_HGT_2010 NULL or 0 in the 2010 vintage. Default
                # to 20 ft for the same reason as above.
                h = 20.0
            height_ft = float(h)
        try:
            geom = shape(feat["geometry"])
            poly = _extract_polygon(geom)
            if poly is None or poly.is_empty:
                continue
            minx, miny, maxx, maxy = poly.bounds
            wkb = dumps(poly)
            rows.append((city, height_ft, minx, miny, maxx, maxy, wkb))
        except Exception:
            continue
    c.executemany(
        "INSERT INTO buildings (city, height_ft, min_x, min_y, max_x, max_y, geom) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def main():
    print(f"Output: {DB_PATH}")
    t0 = time.perf_counter()
    conn = create_db(DB_PATH)

    # Cambridge
    print(f"Parsing Cambridge: {CAMBRIDGE_PATH}")
    with open(CAMBRIDGE_PATH) as f:
        cam_data = json.load(f)
    n_cam = insert_features(conn, cam_data["features"], "cambridge")
    print(f"  {n_cam:,} Cambridge buildings inserted")

    # Boston
    print(f"Parsing Boston: {BOSTON_PATH}")
    with open(BOSTON_PATH) as f:
        bos_data = json.load(f)
    n_bos = insert_features(conn, bos_data["features"], "boston")
    print(f"  {n_bos:,} Boston buildings inserted")

    conn.execute("ANALYZE")
    conn.commit()
    conn.close()

    elapsed = time.perf_counter() - t0
    size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"\nDone in {elapsed:.1f}s")
    print(f"DB size: {size_mb:.1f} MB")
    print(f"Total buildings: {n_cam + n_bos:,}")


if __name__ == "__main__":
    main()
