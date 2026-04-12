"""LightMap performance benchmark.

Measures each stage of the data pipeline at 100% scale.
Each stage is measured in isolation to avoid double-counting.
Each stage runs 3 times, best-of-3 reported.

To reduce run-to-run system noise, warm up the DB connection and call
gc.collect() between stages.

Run from project root:
    .venv/bin/python scripts/benchmark.py
"""

import gc

import os
import resource
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime
from zoneinfo import ZoneInfo

from shadow.compute import (
    _parsed_buildings,
    compute_all_shadows,
    compute_shadow,
    compute_shadow_coverage,
    compute_shadow_coverage_disjoint,
    compute_shadow_coverage_from_polys,
    compute_shadow_coverage_raster,
    get_sun_position,
    parse_building_features,
)
from prototype import (
    _postgis_enabled,
    load_buildings,
    load_buildings_with_parsed,
    load_food_establishments,
    load_streetlights,
)

USE_POSTGIS = _postgis_enabled()
if USE_POSTGIS:
    from shadow.postgis_compute import (
        compute_all_shadows_postgis,
        get_connection as get_postgis_connection,
        load_buildings_postgis,
    )

BOSTON_TZ = ZoneInfo("US/Eastern")
DAY_TIME = datetime(2025, 7, 15, 14, 0, tzinfo=BOSTON_TZ)
NIGHT_TIME = datetime(2025, 7, 15, 22, 0, tzinfo=BOSTON_TZ)
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RUNS = 5


def measure(fn):
    times = []
    result = None
    for _ in range(RUNS):
        gc.collect()
        start = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - start)
    return min(times), times, result


def get_mem_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main():
    print(f"Benchmark config: {RUNS} runs per stage, best-of-{RUNS} reported")
    print(f"PostGIS: {'enabled' if USE_POSTGIS else 'disabled'}")
    print(f"Day:   {DAY_TIME.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Night: {NIGHT_TIME.strftime('%Y-%m-%d %H:%M %Z')}")

    mem_start = get_mem_mb()
    results = []

    # --- Stage 1: Load buildings ---
    if USE_POSTGIS:
        def load_from_postgis():
            conn = get_postgis_connection()
            try:
                return load_buildings_postgis(conn)
            finally:
                conn.close()
        best, all_t, loaded = measure(load_from_postgis)
    else:
        best, all_t, loaded = measure(lambda: load_buildings_with_parsed(100))
    mem_after_load = get_mem_mb()
    building_data, parsed_buildings = loaded
    results.append(("1. load_buildings", best, all_t))

    # --- Stage 2: Shadow projection ---
    alt, az = get_sun_position(DAY_TIME)
    from shapely.geometry import mapping

    shadow_polys = None

    if USE_POSTGIS:
        def shadow_only():
            nonlocal shadow_polys
            conn = get_postgis_connection()
            try:
                features, polys, _, _ = compute_all_shadows_postgis(
                    conn, DAY_TIME, return_polygons=True,
                )
                shadow_polys = polys
                return features
            finally:
                conn.close()
    else:
        def shadow_only():
            nonlocal shadow_polys
            features = []
            polys = []
            for poly, height_ft in parsed_buildings:
                shadow, shadow_len = compute_shadow(poly, height_ft, alt, az)
                if shadow is None:
                    continue
                polys.append(shadow)
                features.append({
                    "type": "Feature",
                    "properties": {
                        "height_ft": height_ft,
                        "shadow_len_ft": round(shadow_len / 0.3048, 1),
                        "type": "shadow",
                    },
                    "geometry": mapping(shadow),
                })
            shadow_polys = polys
            return features

    best, all_t, shadows = measure(shadow_only)
    mem_after_shadow = get_mem_mb()
    results.append(("2. shadow_projection", best, all_t))

    # --- Stage 3a: Coverage using polygons directly (v7a path, STRtree batched) ---
    best, all_t, coverage = measure(
        lambda: compute_shadow_coverage_from_polys(shadow_polys)
    )
    results.append(("3a. coverage_strtree", best, all_t))

    # --- Stage 3b: Coverage via shapely disjoint_subset_union_all (v7d path) ---
    # Skip by default at 100% scale — measured ~17s single-run, and running it
    # 5 times in a benchmark loop triggered a 14+ min hang on the first attempt.
    # Set BENCH_INCLUDE_DISJOINT=1 to include it.
    if os.environ.get("BENCH_INCLUDE_DISJOINT"):
        best_d, all_d, cov_d = measure(
            lambda: compute_shadow_coverage_disjoint(shadow_polys)
        )
    else:
        best_d, all_d = float("nan"), [float("nan")]
    results.append(("3b. coverage_disjoint", best_d, all_d))

    # --- Stage 3c: Coverage via dict re-parse (v6 baseline path) ---
    best_v6, all_v6, cov_v6 = measure(lambda: compute_shadow_coverage(shadows))
    results.append(("3c. coverage_from_dicts", best_v6, all_v6))

    # --- Stage 3d: Coverage via rasterio (v7e path) ---
    best_r, all_r, cov_r = measure(
        lambda: compute_shadow_coverage_raster(shadow_polys, resolution_m=10.0)
    )
    results.append(("3d. coverage_raster10m", best_r, all_r))

    # --- Stage 5: Folium rendering (day) ---
    import folium
    from prototype import (
        _add_building_layer, _add_info_panel, _add_shadow_layer,
        _add_ui_plugins, _create_base_map, _make_shadow_cmap,
    )

    def render_day():
        m = _create_base_map("CartoDB positron")
        _add_building_layer(m, building_data)
        cmap = _make_shadow_cmap()
        _add_shadow_layer(m, shadows, cmap)
        _add_ui_plugins(m, theme="light")
        cmap.add_to(m)
        _add_info_panel(m, ["test"])
        folium.LayerControl().add_to(m)
        return m

    best, all_t, _ = measure(render_day)
    results.append(("4. folium_render_day", best, all_t))

    # --- Stage 5: Load streetlights ---
    best, all_t, coords = measure(lambda: load_streetlights(100))
    results.append(("5. load_streetlights", best, all_t))

    # --- Stage 6: Load food ---
    best, all_t, places = measure(lambda: load_food_establishments(100))
    results.append(("6. load_food", best, all_t))

    # --- Stage 8: Folium rendering (night) ---
    from prototype import _add_food_layer, _add_streetlight_layer

    def render_night():
        m = _create_base_map("CartoDB dark_matter")
        _add_streetlight_layer(m, coords)
        _add_food_layer(m, places)
        _add_ui_plugins(m, theme="dark")
        _add_info_panel(m, ["test"], theme="dark")
        folium.LayerControl().add_to(m)
        return m

    best, all_t, _ = measure(render_night)
    results.append(("7. folium_render_night", best, all_t))

    # --- Report ---
    print("\n" + "=" * 60)
    print(f"BENCHMARK RESULTS (100% scale, best of {RUNS})")
    print("=" * 60)

    for name, best, all_t in results:
        runs_str = ", ".join(f"{t:.2f}" for t in all_t)
        print(f"  {name:25s}  {best:8.2f}s  ({runs_str})")

    # Day pipeline: load + shadow + best_coverage + render.
    import math
    load_t = results[0][1]
    shadow_t = results[1][1]
    cov_strtree = results[2][1]
    cov_disjoint = results[3][1]
    cov_v6 = results[4][1]
    cov_raster = results[5][1]
    best_cov = min(c for c in [cov_strtree, cov_disjoint, cov_raster]
                   if not math.isnan(c))
    render_t = results[6][1]
    day_pipeline = load_t + shadow_t + best_cov + render_t
    night_pipeline = sum(r[1] for r in results[7:])

    print(f"\n  {'DAY PIPELINE':25s}  {day_pipeline:8.2f}s  "
          "(load + parse + shadow + coverage + render)")
    print(f"  {'NIGHT PIPELINE':25s}  {night_pipeline:8.2f}s  "
          "(streetlights + food + render)")

    print(f"\n  Data counts:")
    print(f"    Buildings:    {len(building_data['features']):>8,}")
    print(f"    Shadows:      {len(shadows):>8,}")
    print(f"    Streetlights: {len(coords):>8,}")
    print(f"    Food:         {len(places):>8,}")
    print(f"    Coverage:     {coverage:>7.2f}%")

    print(f"\n  Memory (peak RSS):")
    print(f"    Start:          {mem_start:>7.0f} MB")
    print(f"    After load:     {mem_after_load:>7.0f} MB")
    print(f"    After shadows:  {mem_after_shadow:>7.0f} MB")


if __name__ == "__main__":
    main()
