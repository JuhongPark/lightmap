import csv
import json
import os
import sys

import httpx

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

CKAN_BASE = "https://data.boston.gov/api/3/action/datastore_search"

CAMBRIDGE_BUILDINGS_URL = (
    "https://raw.githubusercontent.com/cambridgegis/cambridgegis_data"
    "/main/Basemap/Buildings/BASEMAP_Buildings.geojson"
)
CAMBRIDGE_BUILDINGS_PATH = os.path.join(
    DATA_DIR, "cambridge", "buildings", "buildings.geojson"
)

CAMBRIDGE_STREETLIGHTS_URL = (
    "https://raw.githubusercontent.com/cambridgegis/cambridgegis_data_infra"
    "/main/Street_Lights/INFRA_StreetLights.geojson"
)
CAMBRIDGE_STREETLIGHTS_PATH = os.path.join(
    DATA_DIR, "cambridge", "streetlights", "streetlights.geojson"
)

BOSTON_STREETLIGHTS_ID = "c2fcc1e3-c38f-44ad-a0cf-e5ea2a6585b5"
BOSTON_STREETLIGHTS_PATH = os.path.join(
    DATA_DIR, "streetlights", "streetlights.csv"
)

BOSTON_FOOD_ID = "f1e13724-284d-478c-b8bc-ef042aa5b70b"
BOSTON_FOOD_PATH = os.path.join(
    DATA_DIR, "safety", "food_establishments.csv"
)


def download_file(url, dest, description):
    if os.path.exists(dest):
        size_mb = os.path.getsize(dest) / (1024 * 1024)
        print(f"  [skip] {description} already exists ({size_mb:.1f} MB)")
        return True

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  Downloading {description}...")

    for attempt in range(3):
        try:
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = downloaded / total * 100
                                print(
                                    f"\r  {downloaded/1024/1024:.1f}"
                                    f"/{total/1024/1024:.1f} MB"
                                    f" ({pct:.0f}%)",
                                    end="",
                                    flush=True,
                                )
                    print()
            size_mb = os.path.getsize(dest) / (1024 * 1024)
            print(f"  Done: {size_mb:.1f} MB")
            return True
        except (httpx.HTTPError, OSError) as e:
            print(f"\n  Attempt {attempt + 1}/3 failed: {e}")
            if os.path.exists(dest):
                os.remove(dest)
            if attempt < 2:
                import time
                time.sleep(2)

    print(f"  FAILED: could not download {description}")
    return False


def download_ckan(resource_id, dest, columns, description):
    if os.path.exists(dest):
        with open(dest) as f:
            count = sum(1 for _ in f) - 1
        print(f"  [skip] {description} already exists ({count} records)")
        return True

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  Downloading {description} from CKAN...")

    all_records = []
    offset = 0
    limit = 32000

    with httpx.Client(timeout=120) as client:
        while True:
            url = f"{CKAN_BASE}?resource_id={resource_id}&limit={limit}&offset={offset}"
            resp = client.get(url)
            resp.raise_for_status()
            result = resp.json()["result"]
            records = result["records"]
            total = result["total"]
            all_records.extend(records)
            print(f"\r  {len(all_records)}/{total} records", end="", flush=True)
            if len(all_records) >= total:
                break
            offset += limit

    print()

    with open(dest, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for rec in all_records:
            writer.writerow(rec)

    print(f"  Done: {len(all_records)} records")
    return True


def download_cambridge_buildings():
    print("Cambridge buildings (GeoJSON):")
    return download_file(
        CAMBRIDGE_BUILDINGS_URL,
        CAMBRIDGE_BUILDINGS_PATH,
        "Cambridge buildings",
    )


def download_cambridge_streetlights():
    print("Cambridge streetlights (GeoJSON):")
    return download_file(
        CAMBRIDGE_STREETLIGHTS_URL,
        CAMBRIDGE_STREETLIGHTS_PATH,
        "Cambridge streetlights",
    )


def download_boston_streetlights():
    print("Boston streetlights (CKAN):")
    return download_ckan(
        BOSTON_STREETLIGHTS_ID,
        BOSTON_STREETLIGHTS_PATH,
        ["Lat", "Long"],
        "Boston streetlights",
    )


def download_boston_food():
    print("Boston food establishments (CKAN):")
    return download_ckan(
        BOSTON_FOOD_ID,
        BOSTON_FOOD_PATH,
        ["businessname", "latitude", "longitude"],
        "Boston food establishments",
    )


def main():
    print("=== LightMap Data Download ===\n")
    results = [
        download_cambridge_buildings(),
        download_cambridge_streetlights(),
        download_boston_streetlights(),
        download_boston_food(),
    ]
    if all(results):
        print("\nAll downloads complete.")
    else:
        print("\nSome downloads failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
