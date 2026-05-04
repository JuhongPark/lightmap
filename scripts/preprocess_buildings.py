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

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shapely.geometry import shape
from shapely.wkb import dumps

from city_config import (
    DEFAULT_CITY_ID,
    height_from_properties_ft,
    load_city_profile,
    profile_data_path,
)
from shadow.compute import _extract_polygon

def create_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
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


def insert_features(conn, features, source):
    c = conn.cursor()
    rows = []
    source_id = source.get("id") or source.get("label") or "source"
    for feat in features:
        props = feat.get("properties", {})
        height_ft = height_from_properties_ft(props, source)
        if height_ft is None:
            continue
        try:
            geom = shape(feat["geometry"])
            poly = _extract_polygon(geom)
            if poly is None or poly.is_empty:
                continue
            minx, miny, maxx, maxy = poly.bounds
            wkb = dumps(poly)
            rows.append((source_id, height_ft, minx, miny, maxx, maxy, wkb))
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
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--city", default=DEFAULT_CITY_ID,
        help="City profile id under cities/. Default: boston-cambridge.",
    )
    args = parser.parse_args()

    city = load_city_profile(args.city)
    db_path = profile_data_path(city, "buildings_db", "buildings.db")

    print(f"City: {city.display_name}")
    print(f"Output: {db_path}")
    t0 = time.perf_counter()
    conn = create_db(db_path)

    total = 0
    for source in city.building_sources:
        label = source.get("label") or source.get("id") or "buildings"
        path = source.get("path")
        if not path or not os.path.exists(path):
            print(f"Skipping {label}: missing {path}")
            continue
        print(f"Parsing {label}: {path}")
        with open(path) as f:
            data = json.load(f)
        n = insert_features(conn, data.get("features", []), source)
        total += n
        print(f"  {n:,} {label} inserted")

    conn.execute("ANALYZE")
    conn.commit()
    conn.close()

    elapsed = time.perf_counter() - t0
    size_mb = os.path.getsize(db_path) / 1024 / 1024
    print(f"\nDone in {elapsed:.1f}s")
    print(f"DB size: {size_mb:.1f} MB")
    print(f"Total buildings: {total:,}")


if __name__ == "__main__":
    main()
