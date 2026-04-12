"""LightMap performance benchmark.

Measures each stage of the data pipeline at 100% scale. Each stage runs in
isolation, best-of-N is reported, and every run saves a structured JSON
result so you can compare across sessions and hardware.

Basic usage:
    .venv/bin/python scripts/benchmark.py
    .venv/bin/python scripts/benchmark.py --label clean-env
    .venv/bin/python scripts/benchmark.py --list
    .venv/bin/python scripts/benchmark.py --compare <id_a> [<id_b>]
    .venv/bin/python scripts/benchmark.py --no-save

Results are written to benchmarks/YYYYMMDD_HHMMSS_<label>.json at the repo
root. The full environment, config, and timing are captured so every run is
reproducible later on different hardware or in a cleaner environment.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gc
import json
import math
import os
import platform
import resource
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zoneinfo import ZoneInfo

from shadow.compute import (
    compute_shadow,
    compute_shadow_coverage,
    compute_shadow_coverage_disjoint,
    compute_shadow_coverage_from_polys,
    compute_shadow_coverage_raster,
    get_sun_position,
)
from prototype import (
    _postgis_enabled,
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOSTON_TZ = ZoneInfo("US/Eastern")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESULTS_DIR = os.path.join(REPO_ROOT, "benchmarks")


@dataclass
class BenchConfig:
    """Knobs that define what the benchmark measures.

    Saved in each result JSON so a later re-run can use the exact same
    settings. Load with BenchConfig.from_dict(...).
    """

    scale_pct: int = 100
    runs: int = 5
    day_time_iso: str = "2025-07-15T14:00:00-04:00"
    night_time_iso: str = "2025-07-15T22:00:00-04:00"
    include_disjoint: bool = False
    raster_resolution_m: float = 10.0
    coverage_grid_cells: int = 50

    @property
    def day_time(self) -> _dt.datetime:
        return _dt.datetime.fromisoformat(self.day_time_iso).astimezone(BOSTON_TZ)

    @property
    def night_time(self) -> _dt.datetime:
        return _dt.datetime.fromisoformat(self.night_time_iso).astimezone(BOSTON_TZ)

    @classmethod
    def from_dict(cls, data: dict) -> "BenchConfig":
        fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        return cls(**{k: v for k, v in data.items() if k in fields})


# ---------------------------------------------------------------------------
# Environment capture
# ---------------------------------------------------------------------------


def _run_text(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def _pkg_version(mod_name: str) -> str:
    try:
        mod = __import__(mod_name)
        return getattr(mod, "__version__", "unknown")
    except Exception:
        return "missing"


def _postgis_version() -> str:
    if not USE_POSTGIS:
        return "disabled"
    try:
        conn = get_postgis_connection()
        cur = conn.cursor()
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else "unknown"
    except Exception:
        return "error"


def capture_environment() -> dict:
    cpu_info = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_info = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass

    mem_total_mb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_mb = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass

    os_pretty = ""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_pretty = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass

    return {
        "cpu": cpu_info,
        "cores": os.cpu_count() or 0,
        "ram_mb": mem_total_mb,
        "os": os_pretty,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "shapely": _pkg_version("shapely"),
        "numpy": _pkg_version("numpy"),
        "folium": _pkg_version("folium"),
        "rasterio": _pkg_version("rasterio"),
        "psycopg2": _pkg_version("psycopg2"),
        "postgis": _postgis_version(),
        "postgis_enabled": USE_POSTGIS,
    }


def capture_git_info() -> dict:
    commit = _run_text(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"])
    branch = _run_text(["git", "-C", REPO_ROOT, "rev-parse", "--abbrev-ref", "HEAD"])
    status = _run_text(["git", "-C", REPO_ROOT, "status", "--porcelain"])
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(status),
    }


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def measure(fn: Callable, runs: int) -> tuple[float, list[float]]:
    times: list[float] = []
    for _ in range(runs):
        gc.collect()
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times), times


def mem_peak_mb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


@dataclass
class StageResult:
    best: float
    runs: list[float]


@dataclass
class BenchResult:
    label: str
    timestamp: str
    config: dict
    environment: dict
    git: dict
    stages: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    memory_mb: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------


def run_benchmark(config: BenchConfig, label: str) -> BenchResult:
    from shapely.geometry import mapping

    stages: dict[str, StageResult] = {}
    mem_start = mem_peak_mb()

    # --- Stage 1: Load buildings ---
    shadow_polys: list = []

    if USE_POSTGIS and config.scale_pct == 100:
        def load_stage():
            conn = get_postgis_connection()
            try:
                return load_buildings_postgis(conn)
            finally:
                conn.close()
    else:
        def load_stage():
            return load_buildings_with_parsed(config.scale_pct)

    best, runs = measure(load_stage, config.runs)
    building_data, parsed_buildings = load_stage()
    stages["load_buildings"] = StageResult(best, runs)
    mem_after_load = mem_peak_mb()

    # --- Stage 2: Shadow projection ---
    alt, az = get_sun_position(config.day_time)

    if USE_POSTGIS and config.scale_pct == 100:
        def shadow_stage():
            nonlocal shadow_polys
            conn = get_postgis_connection()
            try:
                features, polys, _, _ = compute_all_shadows_postgis(
                    conn, config.day_time, return_polygons=True,
                )
                shadow_polys = polys
                return features
            finally:
                conn.close()
    else:
        def shadow_stage():
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
                    },
                    "geometry": mapping(shadow),
                })
            shadow_polys = polys
            return features

    best, runs = measure(shadow_stage, config.runs)
    shadows = shadow_stage()
    stages["shadow_projection"] = StageResult(best, runs)
    mem_after_shadow = mem_peak_mb()

    # --- Stage 3a: Coverage via polygons directly (STRtree batched) ---
    best, runs = measure(
        lambda: compute_shadow_coverage_from_polys(shadow_polys), config.runs
    )
    stages["coverage_strtree"] = StageResult(best, runs)
    coverage = compute_shadow_coverage_from_polys(shadow_polys)

    # --- Stage 3b: Coverage via disjoint_subset_union_all (optional) ---
    if config.include_disjoint:
        best, runs = measure(
            lambda: compute_shadow_coverage_disjoint(shadow_polys), config.runs
        )
        stages["coverage_disjoint"] = StageResult(best, runs)
    else:
        stages["coverage_disjoint"] = StageResult(float("nan"), [float("nan")])

    # --- Stage 3c: Coverage via dict re-parse (historical v6 path) ---
    best, runs = measure(
        lambda: compute_shadow_coverage(shadows), config.runs
    )
    stages["coverage_from_dicts"] = StageResult(best, runs)

    # --- Stage 3d: Coverage via rasterio ---
    best, runs = measure(
        lambda: compute_shadow_coverage_raster(
            shadow_polys, resolution_m=config.raster_resolution_m
        ),
        config.runs,
    )
    stages["coverage_raster"] = StageResult(best, runs)

    # --- Stage 4: Folium render day ---
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

    best, runs = measure(render_day, config.runs)
    stages["folium_render_day"] = StageResult(best, runs)

    # --- Stage 5: Load streetlights ---
    best, runs = measure(lambda: load_streetlights(config.scale_pct), config.runs)
    stages["load_streetlights"] = StageResult(best, runs)
    coords = load_streetlights(config.scale_pct)

    # --- Stage 6: Load food ---
    best, runs = measure(lambda: load_food_establishments(config.scale_pct), config.runs)
    stages["load_food"] = StageResult(best, runs)
    places = load_food_establishments(config.scale_pct)

    # --- Stage 7: Folium render night ---
    from prototype import _add_food_layer, _add_streetlight_layer

    def render_night():
        m = _create_base_map("CartoDB dark_matter")
        _add_streetlight_layer(m, coords)
        _add_food_layer(m, places)
        _add_ui_plugins(m, theme="dark")
        _add_info_panel(m, ["test"], theme="dark")
        folium.LayerControl().add_to(m)
        return m

    best, runs = measure(render_night, config.runs)
    stages["folium_render_night"] = StageResult(best, runs)

    # --- Totals ---
    load_t = stages["load_buildings"].best
    shadow_t = stages["shadow_projection"].best
    cov_values = [
        stages["coverage_strtree"].best,
        stages["coverage_raster"].best,
    ]
    if not math.isnan(stages["coverage_disjoint"].best):
        cov_values.append(stages["coverage_disjoint"].best)
    best_cov = min(cov_values)
    render_day_t = stages["folium_render_day"].best
    day_pipeline = load_t + shadow_t + best_cov + render_day_t

    night_pipeline = (
        stages["load_streetlights"].best
        + stages["load_food"].best
        + stages["folium_render_night"].best
    )

    return BenchResult(
        label=label,
        timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        config=asdict(config),
        environment=capture_environment(),
        git=capture_git_info(),
        stages={name: asdict(s) for name, s in stages.items()},
        counts={
            "buildings": len(building_data["features"]) or len(shadows),
            "shadows": len(shadows),
            "streetlights": len(coords),
            "food": len(places),
            "coverage_pct": round(coverage, 3),
        },
        memory_mb={
            "start": mem_start,
            "after_load": mem_after_load,
            "after_shadows": mem_after_shadow,
        },
        totals={
            "day_pipeline": round(day_pipeline, 2),
            "night_pipeline": round(night_pipeline, 2),
        },
    )


# ---------------------------------------------------------------------------
# Report / save / load
# ---------------------------------------------------------------------------


def print_result(result: BenchResult) -> None:
    print("\n" + "=" * 64)
    print(f"BENCHMARK {result.label}   {result.timestamp}")
    print("=" * 64)
    print(f"  commit: {result.git.get('commit')}  branch: {result.git.get('branch')}"
          f"  dirty: {result.git.get('dirty')}")
    env = result.environment
    print(f"  env:    {env.get('cpu')} / {env.get('cores')} cores / {env.get('ram_mb')} MB")
    print(f"          {env.get('os')} / Python {env.get('python')} / "
          f"shapely {env.get('shapely')} / PostGIS {env.get('postgis')}")
    cfg = result.config
    print(f"  config: scale={cfg['scale_pct']}%  runs={cfg['runs']}  "
          f"postgis={env.get('postgis_enabled')}  "
          f"raster={cfg['raster_resolution_m']}m")

    print()
    stage_order = [
        "load_buildings", "shadow_projection",
        "coverage_strtree", "coverage_disjoint",
        "coverage_from_dicts", "coverage_raster",
        "folium_render_day",
        "load_streetlights", "load_food",
        "folium_render_night",
    ]
    for name in stage_order:
        st = result.stages.get(name)
        if not st:
            continue
        best = st["best"]
        runs = st["runs"]
        if math.isnan(best):
            print(f"  {name:25s}  skipped")
            continue
        runs_str = ", ".join(f"{t:.2f}" for t in runs)
        print(f"  {name:25s}  {best:8.2f}s  ({runs_str})")

    print()
    print(f"  day pipeline    {result.totals['day_pipeline']:8.2f}s")
    print(f"  night pipeline  {result.totals['night_pipeline']:8.2f}s")

    c = result.counts
    print()
    print(f"  buildings={c['buildings']:,}  shadows={c['shadows']:,}  "
          f"streetlights={c['streetlights']:,}  food={c['food']:,}  "
          f"coverage={c['coverage_pct']}%")

    m = result.memory_mb
    print(f"  memory start={m['start']} MB  after_load={m['after_load']} MB  "
          f"after_shadows={m['after_shadows']} MB")


def save_result(result: BenchResult) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in result.label)
    path = os.path.join(RESULTS_DIR, f"{ts}_{safe_label}.json")
    with open(path, "w") as f:
        json.dump(asdict(result), f, indent=2, default=str)
    return path


def load_result(identifier: str) -> BenchResult:
    """Load a saved result by path, filename, or label.

    Matching order:
      1. Exact file path on disk.
      2. Filename stem matches identifier exactly (e.g. "20260412_162609_smoke"
         or just "smoke" → picks the file whose label field equals "smoke").
      3. Substring match as a last resort.
    """
    if os.path.exists(identifier):
        path = identifier
    else:
        files = sorted(
            f for f in os.listdir(RESULTS_DIR) if f.endswith(".json")
        )
        exact_label = [
            f for f in files
            if f.rsplit("_", 1)[-1].removesuffix(".json") == identifier
        ]
        exact_stem = [
            f for f in files if f.removesuffix(".json") == identifier
        ]
        substring = [f for f in files if identifier in f]
        if exact_stem:
            chosen = exact_stem[-1]
        elif exact_label:
            chosen = exact_label[-1]
        elif substring:
            chosen = substring[-1]
        else:
            raise FileNotFoundError(f"No saved result matches '{identifier}'")
        path = os.path.join(RESULTS_DIR, chosen)
    with open(path) as f:
        data = json.load(f)
    return BenchResult(**data)


def list_saved_results() -> None:
    if not os.path.isdir(RESULTS_DIR):
        print(f"No results directory at {RESULTS_DIR}")
        return
    files = sorted(f for f in os.listdir(RESULTS_DIR) if f.endswith(".json"))
    if not files:
        print("No saved benchmark results yet.")
        return
    print(f"{'filename':<42s}  {'day':>8s}  {'shadows':>8s}  {'cov':>6s}  commit")
    print("-" * 80)
    for fname in files:
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                data = json.load(f)
            day = data["totals"]["day_pipeline"]
            shadows = data["counts"]["shadows"]
            cov = data["counts"]["coverage_pct"]
            commit = data.get("git", {}).get("commit", "")
            print(f"{fname:<42s}  {day:8.2f}  {shadows:>8,}  {cov:>5.2f}%  {commit}")
        except Exception as e:
            print(f"{fname:<42s}  ERROR: {e}")


def compare_results(id_a: str, id_b: Optional[str]) -> None:
    a = load_result(id_a)
    b = load_result(id_b) if id_b else None
    if b is None:
        # Compare a against the most recent different result
        files = sorted(f for f in os.listdir(RESULTS_DIR) if f.endswith(".json"))
        if len(files) < 2:
            print("Need at least 2 saved results to auto-compare.")
            return
        # pick the newest file that isn't the same as a
        for fname in reversed(files):
            if fname != id_a and id_a not in fname:
                b = load_result(os.path.join(RESULTS_DIR, fname))
                break
        if b is None:
            print("No second result to compare against.")
            return

    print(f"\nA: {a.label}  {a.timestamp}  commit={a.git.get('commit')}")
    print(f"B: {b.label}  {b.timestamp}  commit={b.git.get('commit')}")
    print()
    print(f"  {'stage':<25s}  {'A (s)':>8s}  {'B (s)':>8s}  {'delta':>8s}  {'pct':>6s}")
    print("  " + "-" * 62)

    stage_names = list(a.stages.keys())
    for name in stage_names:
        sa = a.stages.get(name, {}).get("best", float("nan"))
        sb = b.stages.get(name, {}).get("best", float("nan"))
        if math.isnan(sa) or math.isnan(sb):
            continue
        delta = sb - sa
        pct = (delta / sa * 100) if sa else 0
        print(f"  {name:<25s}  {sa:8.2f}  {sb:8.2f}  {delta:+8.2f}  {pct:+6.1f}%")

    da = a.totals["day_pipeline"]
    db_ = b.totals["day_pipeline"]
    delta = db_ - da
    pct = (delta / da * 100) if da else 0
    print("  " + "-" * 62)
    print(f"  {'day pipeline':<25s}  {da:8.2f}  {db_:8.2f}  {delta:+8.2f}  {pct:+6.1f}%")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--label", default="run",
                        help="Human-readable label saved with the result.")
    parser.add_argument("--scale", type=int, default=100, choices=[0, 1, 10, 50, 100])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--raster-resolution", type=float, default=10.0,
                        help="Resolution in meters for the rasterio coverage path.")
    parser.add_argument("--include-disjoint", action="store_true",
                        help="Include the disjoint_subset_union_all coverage path (slow at 100%%).")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not write a result JSON to benchmarks/.")
    parser.add_argument("--list", action="store_true",
                        help="List saved benchmark results and exit.")
    parser.add_argument("--compare", nargs="+", metavar="ID",
                        help="Compare saved results. Accepts 1 or 2 ids.")
    parser.add_argument("--config", metavar="PATH",
                        help="Load a previously saved result JSON and replay its config.")
    args = parser.parse_args()

    if args.list:
        list_saved_results()
        return 0

    if args.compare:
        ids = args.compare
        if len(ids) == 1:
            compare_results(ids[0], None)
        elif len(ids) == 2:
            compare_results(ids[0], ids[1])
        else:
            print("--compare takes 1 or 2 ids")
            return 2
        return 0

    if args.config:
        prior = load_result(args.config)
        config = BenchConfig.from_dict(prior.config)
    else:
        config = BenchConfig(
            scale_pct=args.scale,
            runs=args.runs,
            raster_resolution_m=args.raster_resolution,
            include_disjoint=args.include_disjoint,
        )

    print(f"Running benchmark: label={args.label}  "
          f"scale={config.scale_pct}%  runs={config.runs}  "
          f"postgis={USE_POSTGIS}")

    result = run_benchmark(config, args.label)
    print_result(result)

    if not args.no_save:
        path = save_result(result)
        print(f"\nSaved: {os.path.relpath(path, REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
