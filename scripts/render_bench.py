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
    return out_filename


def _fmt_ms(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:8.0f}"
    return "       -"


def print_suite_summary(results: list[dict]) -> None:
    print()
    print("=" * 96)
    print("RENDER BENCH SUITE SUMMARY")
    print("=" * 96)
    hdr = (
        f"  {'strategy':<18s}  {'ok':>3s}  "
        f"{'fetch':>8s}  {'parse+add':>10s}  {'preview':>8s}  "
        f"{'total':>8s}  {'shadow':>14s}  {'err':>4s}  notes"
    )
    print(hdr)
    print("  " + "-" * 106)
    for r in results:
        lm = r.get("lightmap") or {}
        fs = lm.get("fetchStart")
        fe = lm.get("fetchEnd")
        added = lm.get("addedAt")
        preview_at = lm.get("previewAt")
        fetch_dur = (fe - fs) if (fs is not None and fe is not None) else None
        parse_add = (added - fe) if (fe is not None and added is not None) else None
        total = added

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
        notes = ""
        if lm.get("status") and lm.get("status") != "done":
            notes = f"status={lm.get('status')}"
        if (r.get("requests_failed") or []):
            notes += (" " if notes else "") + f"failed={len(r['requests_failed'])}"
        if r.get("fatal_error"):
            notes = (notes + " " if notes else "") + "TIMEOUT"
        print(
            f"  {r['label']:<18s}  {ok_str:>3s}  "
            f"{_fmt_ms(fetch_dur)}  {_fmt_ms(parse_add)}  "
            f"{_fmt_ms(preview_at)}  {_fmt_ms(total)}  "
            f"{shadow_visual:>14s}  {err_count:>4d}  {notes}"
        )
    print()
    print("  total   = addedAt (ms to fully interactive vector / final render)")
    print("  preview = previewAt (ms to first shadow pixels visible, r8/r9)")
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
