import os
import sys

import httpx

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

CAMBRIDGE_BUILDINGS_URL = (
    "https://raw.githubusercontent.com/cambridgegis/cambridgegis_data"
    "/main/Basemap/Buildings/BASEMAP_Buildings.geojson"
)
CAMBRIDGE_BUILDINGS_PATH = os.path.join(
    DATA_DIR, "cambridge", "buildings", "buildings.geojson"
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


def download_cambridge_buildings():
    print("Cambridge buildings (GeoJSON):")
    return download_file(
        CAMBRIDGE_BUILDINGS_URL,
        CAMBRIDGE_BUILDINGS_PATH,
        "Cambridge buildings",
    )


def main():
    print("=== LightMap Data Download ===\n")
    ok = download_cambridge_buildings()
    if ok:
        print("\nAll downloads complete.")
    else:
        print("\nSome downloads failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
