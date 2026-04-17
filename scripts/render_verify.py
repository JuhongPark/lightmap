"""Headless render verification for LightMap prototype.html.

Loads a URL (default: http://localhost:8765/prototype.html) in headless
Chromium via Playwright and records:

  1. Navigation + load timings (DOMContentLoaded, load, first paint).
  2. Shadow sidecar fetch + parse + add-to-map timestamps, surfaced by
     the `window.__lightmap` harness that prototype.py injects.
  3. Every console message and every failed network request.
  4. A viewport screenshot before and after shadows load, and a full
     page screenshot at the end.
  5. DOM shape: number of <canvas> elements, leaflet overlay pane info.

Output goes to benchmarks/render/<timestamp>_<label>/ so consecutive
runs do not overwrite each other and you can diff them later.

Usage
-----
  .venv/bin/python scripts/render_verify.py
  .venv/bin/python scripts/render_verify.py \
      --url http://localhost:8765/prototype.html --label scale100-async

Requirements
------------
  Playwright and the Chromium headless shell must be installed:
    uv pip install playwright
    .venv/bin/python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESULTS_ROOT = os.path.join(REPO_ROOT, "benchmarks", "render")


@dataclass
class RenderResult:
    label: str
    url: str
    timestamp: str
    viewport: dict
    timings: dict = field(default_factory=dict)
    lightmap: dict = field(default_factory=dict)
    console: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    requests_failed: list = field(default_factory=list)
    dom_summary: dict = field(default_factory=dict)
    screenshots: dict = field(default_factory=dict)
    ok: bool = False


def _now_ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def run(url: str, label: str, viewport_w: int, viewport_h: int,
        timeout_ms: int, wait_for_shadows: bool,
        hide_onboarding: bool) -> RenderResult:
    # Local import so the module import does not require playwright if the
    # caller only wants to touch the dataclass types.
    from playwright.sync_api import sync_playwright

    ts = _now_ts()
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    out_dir = os.path.join(RESULTS_ROOT, f"{ts}_{safe_label}")
    os.makedirs(out_dir, exist_ok=True)

    result = RenderResult(
        label=label,
        url=url,
        timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        viewport={"width": viewport_w, "height": viewport_h},
    )

    console: list = []
    errors: list = []
    requests_failed: list = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": viewport_w, "height": viewport_h},
            device_scale_factor=1,
        )
        # Install PerformanceObserver before any page script runs so FCP
        # and LCP entries are not missed (buffered:true also catches
        # entries that fired before the observer was registered).
        #
        # For LCP we capture more than tagName — at 123K shadows, the
        # raw "tag=IMG" doesn't distinguish between a shadow preview
        # PNG (what we want to measure) and a CARTO basemap tile
        # (often the LCP for vector-only strategies). `lcp_kind` is
        # derived from className: "shadows" if the leaflet image layer
        # loaded shadows.png, "tile" if a basemap tile, else "other".
        context.add_init_script("""
            (() => {
              window.__lightmap_perf = {
                fcp: null,
                lcp: null,
                lcp_element: null,
                lcp_size: null,
                lcp_src: null,
                lcp_class: null,
                lcp_kind: null,
              };
              function classify(el, src) {
                if (!el) return null;
                var cn = el.className || '';
                if (typeof cn !== 'string') cn = cn.baseVal || '';
                if (cn.indexOf('leaflet-image-layer') >= 0) {
                  return (src || '').indexOf('shadows') >= 0
                    ? 'shadows' : 'image-layer';
                }
                if (cn.indexOf('leaflet-tile') >= 0) return 'tile';
                if (cn.indexOf('leaflet-overlay-pane') >= 0) return 'overlay';
                return 'other';
              }
              try {
                new PerformanceObserver((list) => {
                  for (const e of list.getEntries()) {
                    var el = e.element;
                    var src = (el && (el.currentSrc || el.src)) || null;
                    window.__lightmap_perf.lcp = e.startTime;
                    window.__lightmap_perf.lcp_size = e.size;
                    window.__lightmap_perf.lcp_element = el && el.tagName;
                    window.__lightmap_perf.lcp_src = src;
                    window.__lightmap_perf.lcp_class = el && (
                      typeof el.className === 'string'
                        ? el.className : (el.className && el.className.baseVal)
                    );
                    window.__lightmap_perf.lcp_kind = classify(el, src);
                  }
                }).observe({type: 'largest-contentful-paint', buffered: true});
              } catch (e) {}
              try {
                new PerformanceObserver((list) => {
                  for (const e of list.getEntries()) {
                    if (e.name === 'first-contentful-paint') {
                      window.__lightmap_perf.fcp = e.startTime;
                    }
                  }
                }).observe({type: 'paint', buffered: true});
              } catch (e) {}
            })();
        """)
        page = context.new_page()

        def on_console(msg):
            console.append({
                "type": msg.type,
                "text": msg.text,
                # location may not exist for every message
                "location": getattr(msg, "location", None) or {},
            })

        def on_pageerror(err):
            errors.append({"message": str(err)})

        def on_requestfailed(req):
            requests_failed.append({
                "url": req.url,
                "failure": req.failure or "",
                "method": req.method,
            })

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("requestfailed", on_requestfailed)

        print(f"[verify] navigating to {url}")
        nav_response = page.goto(url, wait_until="load", timeout=timeout_ms)
        if nav_response is None:
            print("[verify] no navigation response")
            result.errors.append("no navigation response")
            result.ok = False
            context.close()
            browser.close()
            result.console = console
            return result
        print(f"[verify] got HTTP {nav_response.status}")

        # Pull basic Navigation Timing fields the browser already has.
        nav_timing = page.evaluate("""
            () => {
              const t = performance.timing || {};
              const n = performance.getEntriesByType('navigation')[0] || {};
              return {
                responseEnd: n.responseEnd,
                domContentLoaded: n.domContentLoadedEventEnd,
                load: n.loadEventEnd,
                duration: n.duration,
              };
            }
        """)
        result.timings["navigation"] = nav_timing

        # Screenshot right after load (before shadows may have appeared).
        shot_pre = os.path.join(out_dir, "pre_shadows.png")
        page.screenshot(path=shot_pre)
        result.screenshots["pre_shadows"] = os.path.relpath(shot_pre, REPO_ROOT)
        print(f"[verify] screenshot (pre shadows) -> {shot_pre}")

        if hide_onboarding:
            # Click the explore button or drop the overlay so it does not
            # cover the canvas in the follow-up screenshot.
            page.evaluate("""
                () => {
                  const el = document.getElementById('onboarding');
                  if (el) el.style.display = 'none';
                }
            """)

        if wait_for_shadows:
            print("[verify] waiting for window.__lightmap.addedAt")
            try:
                page.wait_for_function(
                    "window.__lightmap && window.__lightmap.status === 'done'",
                    timeout=timeout_ms,
                )
                print("[verify] shadows reported as added")
            except Exception as e:
                print(f"[verify] timed out waiting for shadows: {e}")
                errors.append(f"wait_for_shadows timeout: {e}")

        # Pull the lightmap harness state in full.
        lightmap = page.evaluate("window.__lightmap || null")
        result.lightmap = lightmap or {}

        # Paint timings from the PerformanceObserver we installed before
        # navigation. LCP reflects the latest largest-contentful element
        # at the time of read; for shadow strategies this is typically
        # the canvas or the PNG overlay once shadows land.
        paint = page.evaluate("window.__lightmap_perf || null")
        result.timings["paint"] = paint or {}

        # DOM summary: canvas elements, leaflet layers.
        dom_summary = page.evaluate("""
            () => {
              const canvases = Array.from(document.querySelectorAll('canvas'));
              return {
                canvas_count: canvases.length,
                canvas_sizes: canvases.map(c => ({w: c.width, h: c.height})),
                leaflet_panes: Array.from(
                    document.querySelectorAll('.leaflet-pane')
                ).map(p => p.className),
                body_html_len: document.body.innerHTML.length,
                tile_layers: document.querySelectorAll(
                    '.leaflet-tile-loaded'
                ).length,
                svg_paths: document.querySelectorAll('path').length,
              };
            }
        """)
        result.dom_summary = dom_summary

        # Canvas pixel sanity: are any non-transparent pixels present in
        # the overlay canvas? Indicates shadow rendering actually happened.
        canvas_check = page.evaluate("""
            () => {
              const cs = Array.from(document.querySelectorAll(
                  '.leaflet-overlay-pane canvas'
              ));
              if (cs.length === 0) return {found: 0};
              const out = [];
              for (const c of cs) {
                try {
                  const ctx = c.getContext('2d');
                  const step = 16;
                  const w = c.width, h = c.height;
                  let nonzero = 0, sampled = 0;
                  for (let y = 0; y < h; y += step) {
                    for (let x = 0; x < w; x += step) {
                      const d = ctx.getImageData(x, y, 1, 1).data;
                      sampled += 1;
                      if (d[3] !== 0) nonzero += 1;
                    }
                  }
                  out.push({w, h, sampled, nonzero});
                } catch (e) {
                  out.push({error: String(e)});
                }
              }
              return {found: cs.length, canvases: out};
            }
        """)
        result.dom_summary["overlay_canvas"] = canvas_check

        # Image overlay sanity: some strategies (r8 PNG overlay) render
        # shadows as a single <img> in the overlayPane instead of a
        # canvas. Capture image presence and loaded state so the
        # summary can show "shadows are visible" for those too.
        image_check = page.evaluate("""
            () => {
              const imgs = Array.from(document.querySelectorAll(
                  '.leaflet-overlay-pane img.leaflet-image-layer'
              ));
              return imgs.map(i => ({
                src: i.currentSrc || i.src,
                naturalWidth: i.naturalWidth,
                naturalHeight: i.naturalHeight,
                complete: i.complete,
              }));
            }
        """)
        result.dom_summary["overlay_images"] = image_check

        # Second screenshot, after shadows (if any) and overlay removal.
        shot_post = os.path.join(out_dir, "post_shadows.png")
        page.screenshot(path=shot_post)
        result.screenshots["post_shadows"] = os.path.relpath(shot_post, REPO_ROOT)
        print(f"[verify] screenshot (post shadows) -> {shot_post}")

        shot_full = os.path.join(out_dir, "full_page.png")
        page.screenshot(path=shot_full, full_page=True)
        result.screenshots["full_page"] = os.path.relpath(shot_full, REPO_ROOT)

        context.close()
        browser.close()

    result.console = console
    result.errors = errors
    result.requests_failed = requests_failed
    # Decide OK status: no page errors AND (shadows added OR shadows weren't expected)
    lm = result.lightmap or {}
    if wait_for_shadows:
        result.ok = (
            not errors
            and lm.get("status") == "done"
            and isinstance(lm.get("addedAt"), (int, float))
        )
    else:
        result.ok = not errors

    # Save JSON
    json_path = os.path.join(out_dir, "result.json")
    with open(json_path, "w") as f:
        json.dump(asdict(result), f, indent=2, default=str)
    print(f"[verify] saved {json_path}")

    return result


def print_summary(r: RenderResult) -> None:
    print()
    print("=" * 64)
    print(f"RENDER {r.label}   ok={r.ok}")
    print("=" * 64)
    nt = r.timings.get("navigation", {}) or {}
    paint = r.timings.get("paint", {}) or {}
    lm = r.lightmap or {}
    print(f"  viewport:           {r.viewport['width']}x{r.viewport['height']}")
    print(f"  HTTP load:          {nt.get('load', 'n/a')} ms")
    print(f"  DOMContentLoaded:   {nt.get('domContentLoaded', 'n/a')} ms")
    fcp = paint.get("fcp")
    lcp = paint.get("lcp")
    lcp_el = paint.get("lcp_element")
    if fcp is not None:
        print(f"  FCP:                {fcp:.0f} ms")
    if lcp is not None:
        kind = paint.get("lcp_kind") or "?"
        src = (paint.get("lcp_src") or "").split("/")[-1] or "-"
        print(f"  LCP:                {lcp:.0f} ms "
              f"(kind={kind}, tag={lcp_el}, src={src})")
    if lm:
        fs = lm.get("fetchStart")
        fe = lm.get("fetchEnd")
        added = lm.get("addedAt")
        status = lm.get("status", "n/a")
        fc = lm.get("featureCount", "n/a")
        print(f"  shadow status:      {status}  features={fc}")
        if fs is not None:
            print(f"  fetchStart:         {fs:.0f} ms (since navigationStart)")
        if fs is not None and fe is not None:
            print(f"  fetch duration:     {fe - fs:.0f} ms")
        if fe is not None and added is not None:
            print(f"  parse+add:          {added - fe:.0f} ms")
        if added is not None:
            print(f"  shadows addedAt:    {added:.0f} ms (total from nav start)")
    dom = r.dom_summary or {}
    if dom:
        print(f"  canvases:           {dom.get('canvas_count', 'n/a')}")
        oc = dom.get("overlay_canvas", {}) or {}
        if oc.get("found"):
            for i, c in enumerate(oc.get("canvases", [])):
                if "error" in c:
                    print(f"    overlay[{i}]:      error {c['error']}")
                else:
                    total = c.get("sampled", 1) or 1
                    pct = c.get("nonzero", 0) / total * 100.0
                    print(f"    overlay[{i}]:      "
                          f"{c['w']}x{c['h']}  "
                          f"{c['nonzero']}/{total} nonzero ({pct:.1f}%)")
        img_overlays = dom.get("overlay_images", []) or []
        if img_overlays:
            for i, im in enumerate(img_overlays):
                src = (im.get("src") or "").split("/")[-1]
                print(f"    img_overlay[{i}]:  "
                      f"{im.get('naturalWidth', 0)}x{im.get('naturalHeight', 0)}  "
                      f"{src}  complete={im.get('complete')}")
        print(f"  tile_layers loaded: {dom.get('tile_layers', 'n/a')}")
        print(f"  svg path count:     {dom.get('svg_paths', 'n/a')}")
    if r.errors:
        print(f"  page errors ({len(r.errors)}):")
        for e in r.errors[:5]:
            print(f"    - {e}")
    if r.requests_failed:
        print(f"  failed requests ({len(r.requests_failed)}):")
        for f in r.requests_failed[:5]:
            print(f"    - {f.get('method')} {f.get('url')}  {f.get('failure')}")
    if r.console:
        errs = [c for c in r.console if c.get("type") == "error"]
        warns = [c for c in r.console if c.get("type") == "warning"]
        print(f"  console:            {len(r.console)} msgs "
              f"({len(errs)} errors, {len(warns)} warnings)")
        for c in errs[:5]:
            print(f"    [error] {c.get('text', '')[:120]}")
    print()
    for name, path in (r.screenshots or {}).items():
        print(f"  {name:14s} -> {path}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8765/prototype.html")
    parser.add_argument("--label", default="run")
    parser.add_argument("--viewport-width", type=int, default=1280)
    parser.add_argument("--viewport-height", type=int, default=800)
    parser.add_argument("--timeout", type=int, default=60000,
                        help="Timeout in milliseconds for navigation and wait.")
    parser.add_argument("--no-wait-shadows", action="store_true",
                        help="Do not wait for __lightmap.addedAt. "
                             "Use for prototypes that render everything inline.")
    parser.add_argument("--keep-onboarding", action="store_true",
                        help="Do not hide the onboarding overlay "
                             "(kept visible for debugging).")
    args = parser.parse_args()

    result = run(
        url=args.url,
        label=args.label,
        viewport_w=args.viewport_width,
        viewport_h=args.viewport_height,
        timeout_ms=args.timeout,
        wait_for_shadows=not args.no_wait_shadows,
        hide_onboarding=not args.keep_onboarding,
    )
    print_summary(result)
    return 0 if result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
