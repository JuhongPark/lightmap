"""Download Boston crime + crash records, filter to INITIAL_BBOX and to
a recent two-year window, and emit GeoJSON sidecars for the time-slider
safety overlay.

The crime dataset is large (~258K rows total on the CKAN endpoint). We
only keep night-hours (18-05 local) and the last two years so the
shipped payload stays small. Crashes are kept across all hours but
still bounded to the same two-year window.

Both sidecars land in `data/safety/` and are gitignored like every
other raw dataset. The prototype.py build step embeds them into the
time-slider HTML at render time.

Usage
-----
    .venv/bin/python scripts/download_safety.py
    .venv/bin/python scripts/download_safety.py --force
    .venv/bin/python scripts/download_safety.py --years 1
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(REPO_ROOT, "data")
CRIME_PATH = os.path.join(DATA_DIR, "safety", "crime.geojson")
CRASH_PATH = os.path.join(DATA_DIR, "safety", "crashes.geojson")

CKAN_BASE = "https://data.boston.gov/api/3/action/datastore_search_sql"

CRIME_ID = "b973d8cb-eeb2-4e7e-99da-c92938efc9c0"
CRASH_ID = "e4bfe397-6bfc-49c5-9367-c879fac7401d"

INITIAL_BBOX = (42.335, -71.130, 42.385, -71.040)  # (minlat, minlon, maxlat, maxlon)


def _run_sql(query, retries=3):
    """Execute a CKAN datastore_search_sql query and return records.

    Retries on transient network errors. On HTTP 4xx (authorization,
    bad query) we surface the CKAN error body so the user can tell
    whether to adjust the query vs retry later.
    """
    params = urllib.parse.urlencode({"sql": query})
    url = f"{CKAN_BASE}?{params}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "lightmap/0.1"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                js = json.load(resp)
            if not js.get("success"):
                raise RuntimeError(f"CKAN error: {js.get('error')}")
            return js["result"]["records"]
        except urllib.error.HTTPError as e:
            # CKAN puts the useful detail in the response body.
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"CKAN HTTP {e.code} {e.reason}: {body}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            print(f"  transient error on attempt {attempt + 1}: {e}")
    raise RuntimeError(
        f"CKAN request failed after {retries} attempts: {last_err}"
    )


def _write_geojson(path, features, label):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    gj = {"type": "FeatureCollection", "features": features}
    with open(path, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    kb = os.path.getsize(path) / 1024
    print(f"  {label}: {len(features)} features -> {path} ({kb:.1f} KB)")


def fetch_crime(years, bbox):
    """Crime incidents: filter to night hours and the given window."""
    minlat, minlon, maxlat, maxlon = bbox
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=365 * years)).date().isoformat()
    # The CKAN table field names use lowercase mirrors of the Socrata
    # columns. OCCURRED_ON_DATE / HOUR / Lat / Long are the four we
    # need. `HOUR` is 0-23 as a string; cast to integer.
    # All geometry columns are stored as text, so explicit CAST is
    # required. CKAN's datastore_search_sql whitelists a limited set
    # of SQL functions — TRIM is not allowed — so we rely on "HOUR"
    # already being a clean integer-looking string. Rows where the
    # cast fails are dropped at the CKAN side.
    query = f'''
        SELECT "OCCURRED_ON_DATE", "HOUR",
               "Lat" AS lat, "Long" AS lon,
               "OFFENSE_DESCRIPTION" AS descript,
               "DISTRICT" AS district
        FROM "{CRIME_ID}"
        WHERE CAST("Lat" AS float) BETWEEN {minlat} AND {maxlat}
          AND CAST("Long" AS float) BETWEEN {minlon} AND {maxlon}
          AND "OCCURRED_ON_DATE" >= '{cutoff}'
          AND (CAST("HOUR" AS int) >= 18
               OR CAST("HOUR" AS int) <= 5)
        LIMIT 50000
    '''
    print(f"  Crime (night hours, since {cutoff})...")
    records = _run_sql(query)
    feats = []
    for r in records:
        try:
            lat = float(r.get("lat"))
            lon = float(r.get("lon"))
        except (TypeError, ValueError):
            continue
        feats.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, 5), round(lat, 5)],
            },
            "properties": {
                "hour": r.get("HOUR"),
                "descript": r.get("descript"),
                "district": r.get("district"),
            },
        })
    print(f"    records fetched: {len(records)}, kept: {len(feats)}")
    return feats


def fetch_crashes(years, bbox):
    """Crash records (Vision Zero)."""
    minlat, minlon, maxlat, maxlon = bbox
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=365 * years)).date().isoformat()
    query = f'''
        SELECT "dispatch_ts", "lat", "long",
               "mode_type" AS mode
        FROM "{CRASH_ID}"
        WHERE CAST("lat" AS float) BETWEEN {minlat} AND {maxlat}
          AND CAST("long" AS float) BETWEEN {minlon} AND {maxlon}
          AND "dispatch_ts" >= '{cutoff}'
        LIMIT 20000
    '''
    print(f"  Crashes (all hours, since {cutoff})...")
    records = _run_sql(query)
    feats = []
    for r in records:
        try:
            lat = float(r.get("lat"))
            lon = float(r.get("long"))
        except (TypeError, ValueError):
            continue
        feats.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, 5), round(lat, 5)],
            },
            "properties": {
                "ts": r.get("dispatch_ts"),
                "mode": r.get("mode"),
            },
        })
    print(f"    records fetched: {len(records)}, kept: {len(feats)}")
    return feats


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--years", type=int, default=2,
        help="How many recent years of data to keep (default 2).",
    )
    args = parser.parse_args()

    if (os.path.exists(CRIME_PATH) and os.path.exists(CRASH_PATH)
            and not args.force):
        cr_kb = os.path.getsize(CRIME_PATH) / 1024
        ca_kb = os.path.getsize(CRASH_PATH) / 1024
        print(f"[skip] crime.geojson ({cr_kb:.1f} KB) "
              f"and crashes.geojson ({ca_kb:.1f} KB) already exist. "
              f"Use --force to redownload.")
        return 0

    print("Boston safety datasets (CKAN SQL):")
    print(f"  Window: last {args.years} year(s)")
    print(f"  BBox: {INITIAL_BBOX}")
    crime = fetch_crime(args.years, INITIAL_BBOX)
    crash = fetch_crashes(args.years, INITIAL_BBOX)
    _write_geojson(CRIME_PATH, crime, "Crime")
    _write_geojson(CRASH_PATH, crash, "Crashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
