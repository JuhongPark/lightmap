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
import statistics
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
    warmup: int = 1
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
# System state + preflight
# ---------------------------------------------------------------------------

# Thresholds below which a run is considered reliable enough to trust the
# absolute numbers. Breaching them does not invalidate the result on its own,
# it just flags the run as noisy so the reader knows to treat absolute deltas
# with caution.
PREFLIGHT_MAX_LOAD_1MIN = 1.0
PREFLIGHT_MIN_FREE_MB = 2048
PREFLIGHT_MAX_SWAP_USED_MB = 200
STAGE_NOISY_COV = 0.20
STAGE_NOISY_MAX_MIN_RATIO = 1.5


def read_proc_meminfo() -> dict:
    """Parse /proc/meminfo into a dict of MB values for the keys we care about."""
    keys = {
        "MemTotal",
        "MemAvailable",
        "MemFree",
        "Buffers",
        "Cached",
        "SwapTotal",
        "SwapFree",
    }
    result: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in keys:
                    kb = int(rest.strip().split()[0])
                    result[name] = kb // 1024
    except Exception:
        pass
    if "SwapTotal" in result and "SwapFree" in result:
        result["SwapUsed"] = result["SwapTotal"] - result["SwapFree"]
    return result


def read_loadavg() -> tuple[float, float, float]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return (float("nan"), float("nan"), float("nan"))


def capture_system_state() -> dict:
    """Snapshot of host resource state at the instant this is called."""
    mem = read_proc_meminfo()
    load1, load5, load15 = read_loadavg()
    return {
        "load_1min": load1,
        "load_5min": load5,
        "load_15min": load15,
        "mem_free_mb": mem.get("MemFree", 0),
        "mem_available_mb": mem.get("MemAvailable", 0),
        "mem_total_mb": mem.get("MemTotal", 0),
        "swap_used_mb": mem.get("SwapUsed", 0),
        "swap_total_mb": mem.get("SwapTotal", 0),
    }


def _docker_container_status(name: str) -> str:
    """Return 'running', 'stopped', 'missing', or 'unknown' for a container."""
    out = _run_text(["docker", "inspect", "--format", "{{.State.Status}}", name])
    if out in ("running", "exited", "created", "paused", "restarting", "dead"):
        return "running" if out == "running" else "stopped"
    if not out:
        return "missing"
    return "unknown"


def _postgis_reachable() -> tuple[bool, str, int]:
    """Check PostGIS connectivity and buildings row count.

    Returns (ok, message, buildings_count). Errors are surfaced in the message.
    """
    if not USE_POSTGIS:
        return False, "LIGHTMAP_NO_POSTGIS is set or psycopg2 missing", 0
    try:
        conn = get_postgis_connection()
    except Exception as e:
        return False, f"connect failed: {e}", 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM buildings")
        n = int(cur.fetchone()[0])
        cur.close()
        return (n > 0), f"buildings rows: {n:,}", n
    except Exception as e:
        return False, f"query failed: {e}", 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_process_samples(top_n: int = 5) -> list[dict]:
    """Return the top N non-idle processes by CPU. Used to identify noisy neighbors."""
    # `ps` is cross-distro and avoids a psutil dep.
    out = _run_text(["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"])
    if not out:
        return []
    lines = out.splitlines()[1 : top_n + 1]
    samples = []
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            samples.append({
                "pid": int(parts[0]),
                "pcpu": float(parts[1]),
                "pmem": float(parts[2]),
                "comm": parts[3],
            })
        except ValueError:
            continue
    return samples


def run_preflight() -> dict:
    """Run the preflight checks and return a structured result.

    The result is always a dict with a top-level `status` of "pass", "warn",
    or "fail", plus per-check details. `fail` means a blocking problem (no
    PostGIS, etc.); `warn` means we can still run but the numbers will be
    noisy; `pass` means the host is in a clean state.
    """
    checks: list[dict] = []

    state = capture_system_state()

    def add(name: str, status: str, value, detail: str):
        checks.append({
            "name": name,
            "status": status,
            "value": value,
            "detail": detail,
        })

    # --- Load average ---
    load1 = state["load_1min"]
    if math.isnan(load1):
        add("load_1min", "warn", load1, "could not read /proc/loadavg")
    elif load1 > PREFLIGHT_MAX_LOAD_1MIN:
        add("load_1min", "warn", load1,
            f"load {load1:.2f} exceeds {PREFLIGHT_MAX_LOAD_1MIN:.2f}; "
            f"other processes are using the CPU")
    else:
        add("load_1min", "pass", load1, f"load {load1:.2f}")

    # --- Free memory ---
    free = state["mem_free_mb"]
    avail = state["mem_available_mb"]
    if avail < PREFLIGHT_MIN_FREE_MB:
        add("mem_available", "warn", avail,
            f"{avail} MB available < {PREFLIGHT_MIN_FREE_MB} MB threshold; "
            f"benchmark may hit swap")
    else:
        add("mem_available", "pass", avail, f"{avail} MB available, {free} MB free")

    # --- Swap usage ---
    swap_used = state["swap_used_mb"]
    if swap_used > PREFLIGHT_MAX_SWAP_USED_MB:
        add("swap_used", "warn", swap_used,
            f"{swap_used} MB swap in use; RAM pressure active. "
            f"`sudo swapoff -a && sudo swapon -a` to reset")
    else:
        add("swap_used", "pass", swap_used, f"{swap_used} MB swap used")

    # --- PostGIS container ---
    container_status = _docker_container_status("lightmap-postgis")
    if container_status == "running":
        add("postgis_container", "pass", "running", "lightmap-postgis is running")
    elif container_status == "missing":
        add("postgis_container", "warn", "missing",
            "lightmap-postgis container not found; falling back to SQLite")
    else:
        add("postgis_container", "warn", container_status,
            f"lightmap-postgis is {container_status}; run `docker start lightmap-postgis`")

    # --- PostGIS reachable + data populated ---
    ok, msg, n_buildings = _postgis_reachable()
    if ok:
        add("postgis_data", "pass", n_buildings, msg)
    elif USE_POSTGIS:
        add("postgis_data", "fail", 0, msg)
    else:
        add("postgis_data", "warn", 0, msg)

    # --- Noisy neighbors (informational) ---
    top_procs = _load_process_samples(top_n=5)
    if top_procs:
        top_summary = ", ".join(
            f"{p['comm']}({p['pcpu']:.0f}%)" for p in top_procs[:3]
        )
        hot = [p for p in top_procs if p["pcpu"] > 20.0]
        if hot:
            add("top_processes", "warn", top_procs,
                f"high-CPU neighbors: {top_summary}")
        else:
            add("top_processes", "pass", top_procs, f"top procs: {top_summary}")

    # --- Aggregate ---
    any_fail = any(c["status"] == "fail" for c in checks)
    any_warn = any(c["status"] == "warn" for c in checks)
    status = "fail" if any_fail else ("warn" if any_warn else "pass")

    return {
        "status": status,
        "checks": checks,
        "state": state,
        "top_processes": top_procs,
    }


def print_preflight(pf: dict) -> None:
    status = pf["status"]
    tag = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[status]
    print(f"\nPREFLIGHT {tag}")
    print("-" * 64)
    for c in pf["checks"]:
        mark = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL"}[c["status"]]
        print(f"  [{mark}] {c['name']:<20s} {c['detail']}")
    print()


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def measure(fn: Callable, runs: int, warmup: int = 1) -> tuple[float, list[float]]:
    """Measure fn() `runs` times after `warmup` discarded iterations.

    The GC is collected before each timed iteration and then disabled for
    the duration of the iteration so a stop-the-world cycle cannot land
    inside the measurement window. It is re-enabled between iterations so
    long-lived objects from previous iterations still get cleaned up.
    """
    for _ in range(max(0, warmup)):
        fn()

    times: list[float] = []
    for _ in range(runs):
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            fn()
            elapsed = time.perf_counter() - start
        finally:
            gc.enable()
        times.append(elapsed)
    return min(times), times


def stage_stats(runs: list[float]) -> dict:
    """Return summary statistics plus a noisy flag for a stage's run list.

    `cov` is coefficient of variation (stdev / mean). `max_min_ratio` catches
    the case where one outlier dominates even though stdev is small.
    """
    valid = [t for t in runs if not math.isnan(t)]
    if not valid:
        return {
            "best": float("nan"),
            "median": float("nan"),
            "mean": float("nan"),
            "stdev": float("nan"),
            "cov": float("nan"),
            "max_min_ratio": float("nan"),
            "noisy": False,
        }
    best = min(valid)
    worst = max(valid)
    median = statistics.median(valid)
    mean = statistics.fmean(valid)
    stdev = statistics.pstdev(valid) if len(valid) > 1 else 0.0
    cov = (stdev / mean) if mean > 0 else 0.0
    mmr = (worst / best) if best > 0 else float("inf")
    noisy = cov > STAGE_NOISY_COV or mmr > STAGE_NOISY_MAX_MIN_RATIO
    return {
        "best": best,
        "median": median,
        "mean": mean,
        "stdev": stdev,
        "cov": cov,
        "max_min_ratio": mmr,
        "noisy": noisy,
    }


def mem_peak_mb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


@dataclass
class StageResult:
    best: float
    runs: list[float]
    median: float = float("nan")
    mean: float = float("nan")
    stdev: float = float("nan")
    cov: float = float("nan")
    max_min_ratio: float = float("nan")
    noisy: bool = False


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
    preflight: dict = field(default_factory=dict)
    system_state: dict = field(default_factory=dict)
    reliability: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------


def _preimport_heavy_modules() -> None:
    """Import every heavy dependency the benchmark will use BEFORE the first
    timed stage, so module-load cost never gets charged to a measurement.

    Import order matters: importing pandas+pvlib inside get_sun_position
    would otherwise land in the first shadow_projection iteration.
    """
    import folium  # noqa: F401
    import numpy  # noqa: F401
    import rasterio  # noqa: F401
    import rasterio.features  # noqa: F401
    import shapely  # noqa: F401
    import shapely.geometry  # noqa: F401
    import shapely.ops  # noqa: F401

    from shadow import compute as _compute  # noqa: F401
    from prototype import (  # noqa: F401
        _add_building_layer,
        _add_food_layer,
        _add_info_panel,
        _add_shadow_layer,
        _add_streetlight_layer,
        _add_ui_plugins,
        _create_base_map,
        _make_shadow_cmap,
    )

    # pvlib / pandas come in lazily via get_sun_position(); calling it once
    # forces that import chain before timing begins.
    try:
        get_sun_position(_dt.datetime(2025, 7, 15, 14, 0, tzinfo=BOSTON_TZ))
    except Exception:
        pass


def _warmup_postgis() -> None:
    """Force a connection and a tiny query so the first timed stage does
    not pay authentication + planner warmup cost."""
    if not USE_POSTGIS:
        return
    try:
        conn = get_postgis_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.execute("SELECT count(*) FROM buildings WHERE height_ft > 0")
        cur.fetchone()
        cur.close()
        conn.close()
    except Exception:
        pass


def run_benchmark(config: BenchConfig, label: str,
                  preflight: Optional[dict] = None) -> BenchResult:
    from shapely.geometry import mapping

    # Everything the measured stages will reach for is imported up front so
    # the first iteration of stage 1 does not silently pay 200-500 ms of
    # pandas/rasterio/folium import cost.
    _preimport_heavy_modules()
    _warmup_postgis()

    # Record the host state immediately before we start timing, so we can
    # audit the run post hoc even if something spikes mid-run.
    system_state_start = capture_system_state()

    stages: dict[str, StageResult] = {}
    mem_start = mem_peak_mb()

    def run_stage(name: str, fn: Callable) -> None:
        best, runs = measure(fn, config.runs, warmup=config.warmup)
        stats = stage_stats(runs)
        stages[name] = StageResult(
            best=best,
            runs=runs,
            median=stats["median"],
            mean=stats["mean"],
            stdev=stats["stdev"],
            cov=stats["cov"],
            max_min_ratio=stats["max_min_ratio"],
            noisy=stats["noisy"],
        )

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

    run_stage("load_buildings", load_stage)
    building_data, parsed_buildings = load_stage()
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

    run_stage("shadow_projection", shadow_stage)
    shadows = shadow_stage()
    mem_after_shadow = mem_peak_mb()

    # --- Stage 3a: Coverage via polygons directly (STRtree batched) ---
    run_stage(
        "coverage_strtree",
        lambda: compute_shadow_coverage_from_polys(shadow_polys),
    )
    coverage = compute_shadow_coverage_from_polys(shadow_polys)

    # --- Stage 3b: Coverage via disjoint_subset_union_all (optional) ---
    if config.include_disjoint:
        run_stage(
            "coverage_disjoint",
            lambda: compute_shadow_coverage_disjoint(shadow_polys),
        )
    else:
        stages["coverage_disjoint"] = StageResult(
            best=float("nan"), runs=[float("nan")],
        )

    # --- Stage 3c: Coverage via dict re-parse (historical v6 path) ---
    run_stage(
        "coverage_from_dicts",
        lambda: compute_shadow_coverage(shadows),
    )

    # --- Stage 3d: Coverage via rasterio ---
    run_stage(
        "coverage_raster",
        lambda: compute_shadow_coverage_raster(
            shadow_polys, resolution_m=config.raster_resolution_m
        ),
    )

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

    run_stage("folium_render_day", render_day)

    # --- Stage 5: Load streetlights ---
    run_stage("load_streetlights", lambda: load_streetlights(config.scale_pct))
    coords = load_streetlights(config.scale_pct)

    # --- Stage 6: Load food ---
    run_stage("load_food", lambda: load_food_establishments(config.scale_pct))
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

    run_stage("folium_render_night", render_night)

    system_state_end = capture_system_state()

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

    # Per-run reliability aggregate so readers can see at a glance which
    # stages are trustworthy and whether the host moved under us mid-run.
    noisy_stages = [name for name, s in stages.items() if s.noisy]
    load_drift = max(
        abs(system_state_end["load_1min"] - system_state_start["load_1min"]),
        0.0,
    ) if not math.isnan(system_state_start["load_1min"]) else float("nan")
    mem_drift_mb = (
        system_state_start["mem_available_mb"] - system_state_end["mem_available_mb"]
    )
    reliability = {
        "noisy_stages": noisy_stages,
        "noisy_stage_count": len(noisy_stages),
        "load_drift_1min": load_drift,
        "mem_available_drift_mb": mem_drift_mb,
        "preflight_status": preflight["status"] if preflight else "skipped",
    }

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
        preflight=preflight or {},
        system_state={
            "start": system_state_start,
            "end": system_state_end,
        },
        reliability=reliability,
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
          f"warmup={cfg.get('warmup', 0)}  "
          f"postgis={env.get('postgis_enabled')}  "
          f"raster={cfg['raster_resolution_m']}m")

    ss = result.system_state or {}
    start = ss.get("start", {}) if isinstance(ss, dict) else {}
    end = ss.get("end", {}) if isinstance(ss, dict) else {}
    if start:
        print(f"  state:  load {start.get('load_1min', 0):.2f} → "
              f"{end.get('load_1min', 0):.2f}  "
              f"avail {start.get('mem_available_mb', 0)} → "
              f"{end.get('mem_available_mb', 0)} MB  "
              f"swap {start.get('swap_used_mb', 0)} → "
              f"{end.get('swap_used_mb', 0)} MB")

    print()
    print(f"  {'stage':<25s}  {'best':>8s}  {'median':>8s}  {'cov':>6s}   runs")
    print("  " + "-" * 72)
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
        if math.isnan(best):
            print(f"  {name:25s}  skipped")
            continue
        runs = st["runs"]
        median = st.get("median", float("nan"))
        cov = st.get("cov", float("nan"))
        noisy = st.get("noisy", False)
        runs_str = ", ".join(f"{t:.2f}" for t in runs)
        tag = "  [NOISY]" if noisy else ""
        median_str = f"{median:8.2f}" if not math.isnan(median) else "      --"
        cov_str = f"{cov*100:5.1f}%" if not math.isnan(cov) else "    --"
        print(f"  {name:25s}  {best:8.2f}  {median_str}  {cov_str}   "
              f"[{runs_str}]{tag}")

    print()
    print(f"  day pipeline    {result.totals['day_pipeline']:8.2f}s")
    print(f"  night pipeline  {result.totals['night_pipeline']:8.2f}s")

    rel = result.reliability or {}
    if rel:
        noisy_n = rel.get("noisy_stage_count", 0)
        noisy_names = rel.get("noisy_stages", [])
        pf_status = rel.get("preflight_status", "skipped")
        load_drift = rel.get("load_drift_1min", float("nan"))
        mem_drift = rel.get("mem_available_drift_mb", 0)
        if noisy_n:
            print(f"  reliability: {noisy_n} noisy stage(s): "
                  f"{', '.join(noisy_names)}")
        else:
            print(f"  reliability: all stages within CoV "
                  f"≤ {STAGE_NOISY_COV:.0%} and max/min ≤ "
                  f"{STAGE_NOISY_MAX_MIN_RATIO:.1f}x")
        drift_bits = []
        if not math.isnan(load_drift):
            drift_bits.append(f"load Δ={load_drift:+.2f}")
        drift_bits.append(f"avail Δ={-mem_drift:+d} MB")
        drift_bits.append(f"preflight={pf_status}")
        print(f"               " + "  ".join(drift_bits))

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
    ra = (a.reliability or {}).get("noisy_stage_count", "?")
    rb = (b.reliability or {}).get("noisy_stage_count", "?")
    print(f"   noisy stages: A={ra}  B={rb}")
    print()
    print(f"  {'stage':<25s}  {'A (s)':>8s}  {'B (s)':>8s}  "
          f"{'delta':>8s}  {'pct':>6s}  flags")
    print("  " + "-" * 72)

    stage_names = list(a.stages.keys())
    for name in stage_names:
        sa_obj = a.stages.get(name, {})
        sb_obj = b.stages.get(name, {})
        sa = sa_obj.get("best", float("nan"))
        sb = sb_obj.get("best", float("nan"))
        if math.isnan(sa) or math.isnan(sb):
            continue
        delta = sb - sa
        pct = (delta / sa * 100) if sa else 0
        flags = []
        if sa_obj.get("noisy"):
            flags.append("A~")
        if sb_obj.get("noisy"):
            flags.append("B~")
        flag_str = " ".join(flags)
        print(f"  {name:<25s}  {sa:8.2f}  {sb:8.2f}  {delta:+8.2f}  "
              f"{pct:+6.1f}%  {flag_str}")

    da = a.totals["day_pipeline"]
    db_ = b.totals["day_pipeline"]
    delta = db_ - da
    pct = (delta / da * 100) if da else 0
    print("  " + "-" * 72)
    print(f"  {'day pipeline':<25s}  {da:8.2f}  {db_:8.2f}  {delta:+8.2f}  "
          f"{pct:+6.1f}%")
    print()
    print("  (A~/B~ marks stages whose CoV or max/min ratio breached the "
          "noisy threshold.")
    print("   Trust deltas on clean stages; treat noisy-stage deltas as "
          "directional only.)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--label", default="run",
                        help="Human-readable label saved with the result.")
    parser.add_argument("--scale", type=int, default=100, choices=[0, 1, 10, 50, 100])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1,
                        help="Discarded iterations per stage before timing starts.")
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
    parser.add_argument("--preflight", action="store_true",
                        help="Run the preflight environment check and exit.")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the preflight check before running the benchmark.")
    parser.add_argument("--strict", action="store_true",
                        help="Abort the benchmark if any preflight check is warn or fail.")
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

    if args.preflight:
        pf = run_preflight()
        print_preflight(pf)
        return 0 if pf["status"] != "fail" else 2

    if args.config:
        prior = load_result(args.config)
        config = BenchConfig.from_dict(prior.config)
    else:
        config = BenchConfig(
            scale_pct=args.scale,
            runs=args.runs,
            warmup=args.warmup,
            raster_resolution_m=args.raster_resolution,
            include_disjoint=args.include_disjoint,
        )

    # Always run preflight unless the user opted out, so every saved result
    # has a reliability stamp attached to it.
    preflight: Optional[dict] = None
    if not args.skip_preflight:
        preflight = run_preflight()
        print_preflight(preflight)
        if preflight["status"] == "fail":
            print("Preflight FAIL. Fix the issues above or pass "
                  "--skip-preflight to override.")
            return 2
        if args.strict and preflight["status"] != "pass":
            print("Preflight is not 'pass' and --strict is set. Aborting.")
            return 2

    print(f"Running benchmark: label={args.label}  "
          f"scale={config.scale_pct}%  runs={config.runs}  "
          f"warmup={config.warmup}  postgis={USE_POSTGIS}")

    result = run_benchmark(config, args.label, preflight=preflight)
    print_result(result)

    if not args.no_save:
        path = save_result(result)
        print(f"\nSaved: {os.path.relpath(path, REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
