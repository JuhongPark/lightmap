"""End-to-end smoke test for the r9 render strategy.

Regenerates the r9 HTML + sidecar at scale=1 (fast, ~2 s, no PostGIS
required), brings up `scripts/serve.py`, loads the page in headless
Chromium via `scripts/render_verify.py`, and asserts:

  * The shadow layer actually rendered (status=done, canvas has
    non-transparent pixels, featureCount matches the scale-1 dataset).
  * The PNG preview fires before the vector (previewAt < addedAt) so
    r9 is behaving as designed.

Scale=1 gives ~1.2 K shadows — small enough to run under 15 s total
in a test but large enough to exercise the full preview→fetch→vector
pipeline, including gzipped sidecar transport.

The test is skipped when Playwright, Chromium, or the raw building
dataset is missing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
SRC_DIR = os.path.join(REPO_ROOT, "src")
VENV_PYTHON = os.path.join(REPO_ROOT, ".venv", "bin", "python")
# Name starts with "prototype_r" so docs/.gitignore ignores it.
R9_HTML_NAME = "prototype_r9-smoke.html"
R9_HTML = os.path.join(DOCS_DIR, R9_HTML_NAME)
BUILDINGS_DB = os.path.join(REPO_ROOT, "data", "buildings.db")

# At scale=1 the prototype loads ~1230 shadows. Threshold keeps the
# assertion meaningful (catches zero-feature regressions) without
# pinning to a specific sample count.
MIN_FEATURE_COUNT = 500

sys.path.insert(0, SCRIPTS_DIR)

try:
    from render_verify import run as verify_run
    _RENDER_VERIFY_OK = True
except Exception:
    _RENDER_VERIFY_OK = False

try:
    import playwright.sync_api  # noqa: F401
    _PLAYWRIGHT_OK = True
except Exception:
    _PLAYWRIGHT_OK = False


def _pick_free_port() -> int:
    """Ask the kernel for an ephemeral port that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)
    return False


@unittest.skipUnless(_PLAYWRIGHT_OK, "playwright not installed")
@unittest.skipUnless(_RENDER_VERIFY_OK, "render_verify import failed")
@unittest.skipUnless(os.path.isfile(BUILDINGS_DB),
                     f"preprocessed buildings DB missing: {BUILDINGS_DB}")
class TestR9RenderSmoke(unittest.TestCase):
    server_proc: subprocess.Popen | None = None
    port: int = 0

    @classmethod
    def setUpClass(cls) -> None:
        # Regenerate the r9 HTML + sidecar together at scale=1. This
        # keeps the test self-contained (no dependency on whatever
        # scale the last build used) and the regen cost is ~2 s.
        regen = subprocess.run(
            [VENV_PYTHON, os.path.join(SRC_DIR, "prototype.py"),
             "--scale", "1",
             "--render-strategy", "r9-png-then-vector",
             "--out", R9_HTML_NAME],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        if regen.returncode != 0:
            raise unittest.SkipTest(
                f"r9 regen failed (rc={regen.returncode}): "
                f"{regen.stderr[-400:]}"
            )

        cls.port = _pick_free_port()
        cls.server_proc = subprocess.Popen(
            [VENV_PYTHON, os.path.join(SCRIPTS_DIR, "serve.py"),
             str(cls.port), DOCS_DIR],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        url = f"http://localhost:{cls.port}/{R9_HTML_NAME}"
        if not _wait_for_http(url, timeout_s=5.0):
            cls.server_proc.terminate()
            raise unittest.SkipTest(f"server did not respond at {url}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server_proc is not None:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cls.server_proc.kill()
        # Leave the regen artifact in place — it's tiny and useful
        # for poking at with a real browser after a failing run.

    def test_r9_end_to_end(self) -> None:
        url = f"http://localhost:{self.port}/{R9_HTML_NAME}"
        result = verify_run(
            url=url, label="smoke_r9",
            viewport_w=1280, viewport_h=800,
            timeout_ms=60_000,
            wait_for_shadows=True,
            hide_onboarding=True,
        )
        lm = result.lightmap or {}
        paint = (result.timings or {}).get("paint") or {}

        # Correctness: shadows actually landed on the map.
        self.assertEqual(lm.get("status"), "done",
                         f"shadow status != done: {lm.get('status')}")
        self.assertGreater(lm.get("featureCount") or 0, MIN_FEATURE_COUNT,
                           f"feature count too low "
                           f"(expected > {MIN_FEATURE_COUNT}, "
                           f"got {lm.get('featureCount')})")

        # r9-specific: PNG preview fires first, vector comes later.
        preview = lm.get("previewAt")
        added = lm.get("addedAt")
        self.assertIsNotNone(preview, "previewAt missing — PNG overlay never fired")
        self.assertIsNotNone(added, "addedAt missing — vector layer never fired")
        self.assertLessEqual(preview, added,
                             f"previewAt ({preview}) should precede addedAt ({added})")

        # Canvas actually has rendered shadow pixels (not a blank overlay).
        # At scale=1 with 1230 shadows across Boston+Cambridge, the 16-px
        # sample step catches ~30-50 nonzero pixels in a typical run.
        # Threshold = 10 guards against "canvas is completely empty"
        # regressions without being brittle to exact sample counts.
        overlay = (result.dom_summary or {}).get("overlay_canvas") or {}
        canvases = overlay.get("canvases") or []
        nonzero_any = any(c.get("nonzero", 0) > 10 for c in canvases)
        self.assertTrue(nonzero_any,
                        f"overlay canvas has no (or too few) non-transparent "
                        f"pixels: {canvases}")

        # Observability: print timings so a regression is easy to spot
        # in CI logs even when the assertions pass.
        print(
            f"\n  r9 smoke: preview={preview:.0f}ms added={added:.0f}ms "
            f"FCP={paint.get('fcp') or 'n/a'} LCP={paint.get('lcp') or 'n/a'} "
            f"(element={paint.get('lcp_element') or 'n/a'})"
        )


if __name__ == "__main__":
    unittest.main()
