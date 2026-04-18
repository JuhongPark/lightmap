"""Render strategy benchmark driver.

Regenerates prototype_<strategy>.html for every entry in
`RENDER_STRATEGIES`, runs the headless Playwright verifier against each,
and writes a side-by-side comparison table + per-strategy JSON results
to `benchmarks/render/<ts>_suite/`. Every run in a single suite shares
the same timestamp directory so the results cluster together.

The strategies live in `src/prototype.py`. This driver only dispatches.

Usage
-----
    .venv/bin/python scripts/render_bench.py                 # all strategies
    .venv/bin/python scripts/render_bench.py --only r3-preload r4-fade
    .venv/bin/python scripts/render_bench.py --scale 100 --runs 2
    .venv/bin/python scripts/render_bench.py --server-url http://localhost:8765

The server must already be running. Start it with:

    .venv/bin/python scripts/serve.py 8765

(serve.py is gzip-aware, which r5-gzip needs; plain python -m http.server
also works for r0..r4 but r5-gzip will regress to the plain sidecar.)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import asdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESULTS_ROOT = os.path.join(REPO_ROOT, "benchmarks", "render")

sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
from prototype import RENDER_STRATEGIES  # noqa: E402

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
from render_verify import run as verify_run, RenderResult  # noqa: E402


_DOCS_DIR = os.path.join(REPO_ROOT, "docs")
_SIDECAR_JSON = os.path.join(_DOCS_DIR, "shadows.geojson")
_SIDECAR_GZ = os.path.join(_DOCS_DIR, "shadows.geojson.gz")
# Ratio of expected feature count to scale percent. At scale=100 we see
# ~123K features; scale=1 gives ~1230. Used to flag sidecar/scale mismatch.
_FEATURES_PER_PCT = 1230


def _sidecar_feature_count() -> int | None:
    """Count features in docs/shadows.geojson without loading the full JSON.

    Returns None if the file is missing. This is a cheap approximation
    (linear scan) that avoids parsing a 28 MB JSON just to sanity-check.
    """
    if not os.path.isfile(_SIDECAR_JSON):
        return None
    try:
        with open(_SIDECAR_JSON, "rb") as f:
            # `"type":"Feature"` appears once per feature in our compact
            # output. Good enough for a mismatch warning.
            return f.read().count(b'"type":"Feature"')
    except OSError:
        return None


def _sidecar_sanity_check(strategy: str, scale: int) -> None:
    """Warn loudly if the post-regen sidecar does not match the scale.

    Silent when the strategy doesn't use the sidecar (inline, png-overlay).
    """
    from prototype import RENDER_STRATEGIES  # lazy to avoid import-time cost
    cfg = RENDER_STRATEGIES.get(strategy) or {}
    mode = cfg.get("shadow_mode")
    if mode in ("inline", "png-overlay"):
        return  # no sidecar involved

    actual = _sidecar_feature_count()
    if actual is None:
        print(f"[bench] WARNING: {strategy} expected a sidecar but "
              f"{_SIDECAR_JSON} is missing")
        return
    expected = scale * _FEATURES_PER_PCT
    lo, hi = int(expected * 0.5), int(expected * 2.0)
    if not (lo <= actual <= hi):
        print(f"[bench] WARNING: sidecar featureCount={actual} for "
              f"{strategy} at scale={scale} (expected ~{expected}). "
              f"Stale cache or wrong scale? Investigate before trusting "
              f"these numbers.")
    else:
        print(f"[bench] sidecar ok: featureCount={actual} for {strategy} "
              f"at scale={scale}")

    # Also catch the stale-.gz case (plain is fresh, gz is older). The
    # server now handles this correctly, but flag it so the user can fix
    # the root cause (e.g. delete stale .gz) instead of relying on the
    # server's mtime fallback forever.
    if cfg.get("gzip_sidecar") and os.path.isfile(_SIDECAR_GZ):
        try:
            if os.path.getmtime(_SIDECAR_GZ) < os.path.getmtime(_SIDECAR_JSON):
                print(f"[bench] WARNING: {_SIDECAR_GZ} is older than "
                      f"the plain sidecar. Server will fall back to "
                      f"plain; delete the .gz if it is stale.")
        except OSError:
            pass


def regen(strategy: str, scale: int) -> str:
    """Invoke prototype.py to emit the strategy-specific HTML file.

    Returns the relative URL path (e.g. 'prototype_r2-async.html').
    """
    print(f"\n[bench] regen {strategy} at scale {scale}")
    out_filename = f"prototype_{strategy}.html"
    cmd = [
        os.path.join(REPO_ROOT, ".venv", "bin", "python"),
        os.path.join(REPO_ROOT, "src", "prototype.py"),
        "--scale", str(scale),
        "--render-strategy", strategy,
        "--out", out_filename,
    ]
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    _sidecar_sanity_check(strategy, scale)
    return out_filename


def _fmt_ms(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:8.0f}"
    return "       -"


def _fmt_ms_short(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:6.0f}"
    return "     -"


_REGRESSION_THRESHOLD = 1.50  # 50% over expected trips the regression flag


def _regression_tag(actual, expected) -> str:
    """Mark actual vs expected. 'ok' if within threshold, 'REGR +X%' otherwise.

    Returns '' when expected is None (no budget configured).
    """
    if expected is None or actual is None:
        return ""
    if expected <= 0:
        return ""
    ratio = actual / expected
    if ratio > _REGRESSION_THRESHOLD:
        return f"REGR {ratio * 100 - 100:+.0f}%"
    if ratio < 0.70:
        return f"FAST {ratio * 100 - 100:+.0f}%"
    return "ok"


def print_suite_summary(results: list[dict]) -> None:
    print()
    print("=" * 120)
    print("RENDER BENCH SUITE SUMMARY")
    print("=" * 120)
    hdr = (
        f"  {'strategy':<18s}  {'ok':>3s}  "
        f"{'fetch':>8s}  {'parse+add':>10s}  {'preview':>8s}  "
        f"{'total':>8s}  {'vs exp':>8s}  {'FCP':>6s}  {'LCP':>6s}  "
        f"{'LCPkind':>8s}  "
        f"{'shadow':>14s}  {'err':>4s}  notes"
    )
    print(hdr)
    print("  " + "-" * 140)
    for r in results:
        lm = r.get("lightmap") or {}
        fs = lm.get("fetchStart")
        fe = lm.get("fetchEnd")
        added = lm.get("addedAt")
        preview_at = lm.get("previewAt")
        fetch_dur = (fe - fs) if (fs is not None and fe is not None) else None
        parse_add = (added - fe) if (fe is not None and added is not None) else None
        total = added
        paint = (r.get("timings") or {}).get("paint") or {}
        fcp = paint.get("fcp")
        lcp = paint.get("lcp")
        lcp_kind = (paint.get("lcp_kind") or "-")[:8]

        # Visual sanity: either we have a non-empty canvas overlay or
        # a loaded image overlay. Canvas strategies fill nz_pct; image
        # overlay strategies (r8) report img.
        nz_pct = None
        shadow_visual = "-"
        dom = r.get("dom_summary") or {}
        oc = dom.get("overlay_canvas") or {}
        for c in (oc.get("canvases") or []):
            if "error" in c:
                continue
            s = c.get("sampled", 0) or 0
            if s:
                nz_pct = c.get("nonzero", 0) / s * 100
                shadow_visual = f"cv {nz_pct:4.1f}%"
                break
        if nz_pct is None:
            imgs = dom.get("overlay_images") or []
            for im in imgs:
                if im.get("complete") and im.get("naturalWidth", 0) > 0:
                    shadow_visual = (
                        f"img {im.get('naturalWidth')}x{im.get('naturalHeight')}"
                    )
                    break
        err_count = len(r.get("errors") or [])
        ok_str = "yes" if r.get("ok") else "no"

        # Regression vs expected_total_ms (and expected_preview_ms if set).
        cfg = RENDER_STRATEGIES.get(r.get("strategy") or "") or {}
        expected_total = cfg.get("expected_total_ms")
        expected_preview = cfg.get("expected_preview_ms")
        regr_total = _regression_tag(total, expected_total)
        regr_preview = _regression_tag(preview_at, expected_preview)
        regr_cell = regr_total or (
            "no-bud" if expected_total is None else ""
        )

        notes = ""
        if lm.get("status") and lm.get("status") != "done":
            notes = f"status={lm.get('status')}"
        if (r.get("requests_failed") or []):
            notes += (" " if notes else "") + f"failed={len(r['requests_failed'])}"
        if r.get("fatal_error"):
            notes = (notes + " " if notes else "") + "TIMEOUT"
        if regr_preview and regr_preview not in ("ok", ""):
            notes = (notes + " " if notes else "") + f"preview={regr_preview}"
        print(
            f"  {r['label']:<18s}  {ok_str:>3s}  "
            f"{_fmt_ms(fetch_dur)}  {_fmt_ms(parse_add)}  "
            f"{_fmt_ms(preview_at)}  {_fmt_ms(total)}  "
            f"{regr_cell:>8s}  "
            f"{_fmt_ms_short(fcp)}  {_fmt_ms_short(lcp)}  "
            f"{lcp_kind:>8s}  "
            f"{shadow_visual:>14s}  {err_count:>4d}  {notes}"
        )
    print()
    print("  total   = addedAt (ms to fully interactive vector / final render)")
    print("  preview = previewAt (ms to first shadow pixels visible, r8/r9)")
    print("  vs exp  = total vs expected_total_ms in RENDER_STRATEGIES.")
    print("            ok = within +50%/-30%, REGR/FAST = outside, no-bud = None.")
    print("  FCP/LCP = browser PerformanceObserver values at end of run.")
    print("  LCPkind = classification of the LCP element:")
    print("            shadows  = PNG shadow preview (r8/r9 design target)")
    print("            tile     = CARTO basemap tile (most vector strategies)")
    print("            other    = fallback, inspect lcp_src/class for detail")
    print("  shadow  = cv <pct%> canvas nonzero or img <WxH> image overlay")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scale", type=int, default=100, choices=[0, 1, 10, 50, 100])
    parser.add_argument("--runs", type=int, default=1,
                        help="Render_verify runs per strategy (discard worst).")
    parser.add_argument("--only", nargs="+", default=None,
                        help="Limit to a subset of strategies.")
    parser.add_argument("--server-url", default="http://localhost:8765",
                        help="Base URL of the static server that serves docs/.")
    parser.add_argument("--timeout", type=int, default=120000,
                        help="Per-run navigation+wait timeout (ms).")
    parser.add_argument("--skip-regen", action="store_true",
                        help="Reuse the already-built prototype_<strategy>.html files.")
    args = parser.parse_args()

    strategies = list(RENDER_STRATEGIES.keys())
    if args.only:
        strategies = [s for s in strategies if s in args.only]
        if not strategies:
            print("error: no strategies match --only", file=sys.stderr)
            return 2

    suite_ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suite_dir = os.path.join(RESULTS_ROOT, f"{suite_ts}_suite")
    os.makedirs(suite_dir, exist_ok=True)
    print(f"\n[bench] suite dir: {suite_dir}")
    print(f"[bench] server:    {args.server_url}")
    print(f"[bench] scale:     {args.scale}")
    print(f"[bench] strategies:{strategies}")

    results = []
    for strategy in strategies:
        if not args.skip_regen:
            regen(strategy, args.scale)
        url = f"{args.server_url}/prototype_{strategy}.html"

        # Run N times, keep the best (lowest addedAt).
        best_result = None
        for run_idx in range(args.runs):
            label = f"{strategy}_r{run_idx + 1}" if args.runs > 1 else strategy
            print(f"\n[bench] verify {label} -> {url}")
            # Inline strategies (r0/r1) can legitimately hang the browser
            # on this dataset; cap their timeout tighter so a failure is
            # recorded within a reasonable time and the suite moves on.
            strat_timeout = args.timeout
            if RENDER_STRATEGIES[strategy]["shadow_mode"] == "inline":
                strat_timeout = min(args.timeout, 60000)
            try:
                res = verify_run(
                    url=url,
                    label=f"suite_{strategy}",
                    viewport_w=1280,
                    viewport_h=800,
                    timeout_ms=strat_timeout,
                    # Inline strategies embed features; no __lightmap
                    # harness to wait on.
                    wait_for_shadows=(
                        RENDER_STRATEGIES[strategy]["shadow_mode"] != "inline"
                    ),
                    hide_onboarding=True,
                )
                res_dict = asdict(res)
                res_dict["strategy"] = strategy
                res_dict["label"] = strategy
                res_dict["run_idx"] = run_idx
                res_dict["fatal_error"] = None
            except Exception as e:
                # Record the failure, still produce a row in the suite
                # table so "this strategy does not render" is visible.
                print(f"[bench] {strategy} failed: {type(e).__name__}: {e}")
                res_dict = {
                    "strategy": strategy,
                    "label": strategy,
                    "run_idx": run_idx,
                    "ok": False,
                    "url": url,
                    "lightmap": {},
                    "dom_summary": {},
                    "errors": [f"{type(e).__name__}: {e}"],
                    "requests_failed": [],
                    "console": [],
                    "screenshots": {},
                    "viewport": {"width": 1280, "height": 800},
                    "timings": {},
                    "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "fatal_error": f"{type(e).__name__}: {e}",
                }
            lm = res_dict.get("lightmap") or {}
            added = lm.get("addedAt")
            if (best_result is None) or (
                added is not None
                and (best_result.get("lightmap") or {}).get("addedAt", float("inf"))
                > added
            ):
                best_result = res_dict

        # Save best result into the suite dir under a predictable name.
        out_path = os.path.join(suite_dir, f"{strategy}.json")
        with open(out_path, "w") as f:
            json.dump(best_result, f, indent=2, default=str)
        print(f"[bench] saved {out_path}")
        results.append(best_result)

    suite_path = os.path.join(suite_dir, "suite.json")
    with open(suite_path, "w") as f:
        json.dump({
            "timestamp": suite_ts,
            "scale": args.scale,
            "server_url": args.server_url,
            "strategies": strategies,
            "results": results,
        }, f, indent=2, default=str)

    print_suite_summary(results)
    print(f"suite written to {os.path.relpath(suite_dir, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
