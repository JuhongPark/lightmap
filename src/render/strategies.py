"""Browser-side shadow render strategies.

Each entry in RENDER_STRATEGIES is one version of the browser-side shadow
rendering path we have tried. They coexist so `scripts/render_bench.py`
can regenerate every variant, run a headless Chromium load against each,
and compare timings. Keys never move or disappear — history is the point.

Public API
----------
    RENDER_STRATEGIES       dict of strategy key → config
    DEFAULT_RENDER_STRATEGY currently best default
    add_shadow_layer(m, shadows, cmap, *, strategy, out_dir)
        Dispatches to the right private implementation.
    shadow_color_stops_for_js(cmap)
        Helper exposed for tests.

`expected_total_ms` / `expected_preview_ms` in each entry are
last-observed single-run times at scale=100 (suite 20260414_001829,
WSL2 with VSCode load). `render_bench` prints a regression marker when
a run exceeds these by more than 50%. None = no budget (known to fail
or not applicable).
"""

from __future__ import annotations

import gzip as _gzip
import json
import os

import folium
from folium.features import GeoJsonPopup

from shadow.compute import render_shadows_png


# Three darker buckets. The earlier 4-bucket palette started at slate-300
# (#cbd5e1) which at fillOpacity 0.45 tinted the basemap so faintly that
# short-building shadows looked indistinguishable from sunny ground. That
# produced a visual "sudden brightening" where a short building's shadow
# met a taller building's clearly-gray shadow. Dropping the super-light
# bucket keeps every shadow visibly darker than basemap.
SHADOW_CMAP_COLORS = ["#64748b", "#334155", "#0f172a"]

# Maximum-zoom-out viewport: MIT + Boston Financial District + Fenway,
# which also defines the pannable area enforced by the map's maxBounds.
# Features inside this box are pre-filtered into a small sidecar so the
# first paint shows interactive shadows + buildings across every area
# the user can see, without waiting for the full 123 K-feature sidecar.
# Box covers MIT + Cambridge core + Back Bay + Boston downtown + Financial
# District. Sized so users can pan a useful distance before the full
# fetch resolves. (south, west, north, east)
INITIAL_BBOX = (42.335, -71.130, 42.385, -71.040)


def _feature_bbox(feature):
    """Return (s, w, n, e) for a GeoJSON feature, or None if unusable."""
    geom = feature.get("geometry") or {}
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        ring = coords[0] if coords else []
    elif t == "MultiPolygon":
        ring = coords[0][0] if (coords and coords[0]) else []
    else:
        return None
    if not ring:
        return None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(ys), min(xs), max(ys), max(xs))


def _bbox_intersects(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _filter_features_by_bbox(features, bbox):
    out = []
    for f in features:
        fb = _feature_bbox(f)
        if fb is not None and _bbox_intersects(fb, bbox):
            out.append(f)
    return out


def _simplify_features(features, tol_deg):
    """Simplify each feature's polygon to reduce vertex count.

    tol_deg is in WGS84 degrees. 0.00003 ≈ 3 m at Boston latitude.
    Polygons that go empty after simplification are dropped.
    """
    from shapely.geometry import shape as _shape, mapping as _mapping
    out = []
    for f in features:
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            out.append(f)
            continue
        try:
            g = _shape(geom).simplify(tol_deg, preserve_topology=True)
        except Exception:
            out.append(f)
            continue
        if g.is_empty:
            continue
        out.append({
            "type": "Feature",
            "properties": f.get("properties") or {},
            "geometry": _mapping(g),
        })
    return out


# Wire-format coordinate precision. 5 decimals is ~1.1 m at lat 42,
# below one pixel at max map zoom. Applied only at serialize time so
# in-memory shadows stay topologically clean for compute steps.
WIRE_COORD_PRECISION = 5


def _merge_shadows_by_bucket(features, stops):
    """Union shadows inside each color bucket into one geometry each.

    Without this, each building produces one L.polygon ring. Where
    neighboring same-bucket shadows abut, Leaflet's canvas anti-
    aliasing draws each ring's edge with partial alpha, which shows
    up as a 1-2 px bright seam along the shared boundary. Merging
    into one geometry per bucket eliminates the shared edge entirely.

    Returns a list of features, each with the bucket's threshold as
    `height_ft` so the client-side `colorFor` maps to the intended
    color. Splits MultiPolygon results back into individual Polygon
    features so the on-wire format stays homogeneous (every feature
    is a Polygon).
    """
    from shapely.geometry import shape as _shape, mapping as _mapping
    from shapely.ops import unary_union as _unary_union

    if not features or not stops:
        return features

    thresholds = [t for t, _ in stops]
    def _bucket(h):
        bi = 0
        for i, t in enumerate(thresholds):
            if h >= t:
                bi = i
        return bi

    buckets = {i: [] for i in range(len(stops))}
    for f in features:
        props = f.get("properties") or {}
        h = props.get("height_ft")
        if h is None:
            h = 0
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            buckets[_bucket(float(h))].append(_shape(geom))
        except Exception:
            continue

    out = []
    for bi in range(len(stops)):
        polys = buckets[bi]
        if not polys:
            continue
        merged = _unary_union(polys)
        if merged.is_empty:
            continue
        bucket_threshold = thresholds[bi]
        geoms = []
        if merged.geom_type == "Polygon":
            geoms.append(merged)
        elif merged.geom_type == "MultiPolygon":
            geoms.extend(merged.geoms)
        else:
            # GeometryCollection edge case. Skip non-polygon parts.
            for g in getattr(merged, "geoms", []):
                if g.geom_type in ("Polygon", "MultiPolygon"):
                    geoms.extend([g] if g.geom_type == "Polygon" else list(g.geoms))
        for g in geoms:
            if g.is_empty:
                continue
            out.append({
                "type": "Feature",
                "properties": {"height_ft": bucket_threshold},
                "geometry": _mapping(g),
            })
    return out


def _round_coords(obj, n):
    """Recursively round GeoJSON coordinate arrays to n decimal places."""
    if isinstance(obj, (list, tuple)):
        if len(obj) == 2 and all(isinstance(v, (int, float)) for v in obj):
            return [round(obj[0], n), round(obj[1], n)]
        return [_round_coords(x, n) for x in obj]
    return obj


def _ring_is_ccw(ring):
    """Shoelace. Returns True when ring wraps counterclockwise in
    GeoJSON coords (lon=x, lat=y). Needed because canvas nonzero fill
    rule cancels overlaps between mixed-winding rings, which shows up
    as transparent holes where adjacent buildings touch.
    """
    if not ring or len(ring) < 3:
        return True
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        area += (x2 - x1) * (y2 + y1)
    return area < 0


def _normalize_polygon_winding(rings):
    """Ensure exterior ring is CCW and interior rings are CW, so that
    canvas-nonzero fill treats every polygon identically. The source
    building geometries arrived with ~15 percent CW exterior rings,
    which punched holes through their neighbors when batched into a
    single L.polygon.
    """
    out = []
    for i, ring in enumerate(rings):
        want_ccw = (i == 0)
        if _ring_is_ccw(ring) == want_ccw:
            out.append(ring)
        else:
            out.append(list(reversed(ring)))
    return out


def _write_geojson(path, features, *, gzip_sidecar=False, simplify_tol_deg=None,
                   coord_precision=WIRE_COORD_PRECISION):
    if simplify_tol_deg:
        features = _simplify_features(features, simplify_tol_deg)
    cleaned = []
    for feat in features:
        geom = feat["geometry"]
        coords = geom["coordinates"]
        if coord_precision is not None:
            coords = _round_coords(coords, coord_precision)
        if geom["type"] == "Polygon":
            coords = _normalize_polygon_winding(coords)
        elif geom["type"] == "MultiPolygon":
            coords = [_normalize_polygon_winding(p) for p in coords]
        cleaned.append({
            "type": "Feature",
            "properties": feat.get("properties") or {},
            "geometry": {"type": geom["type"], "coordinates": coords},
        })
    with open(path, "w") as f:
        json.dump(
            {"type": "FeatureCollection", "features": cleaned},
            f, separators=(",", ":"),
        )
    if gzip_sidecar:
        with open(path, "rb") as src, _gzip.open(path + ".gz", "wb", 6) as dst:
            dst.writelines(src)
    return len(cleaned)


RENDER_STRATEGIES = {
    "r0-inline-svg": {
        "label": "baseline: inline features + SVG renderer",
        "prefer_canvas": False,
        "shadow_mode": "inline",      # folium GeoJson with data embedded
        "preload": False,
        "fade_in": False,
        "gzip_sidecar": False,
        "expected_total_ms": None,     # times out at scale=100
        "expected_preview_ms": None,
    },
    "r1-inline-canvas": {
        "label": "inline features + canvas renderer",
        "prefer_canvas": True,
        "shadow_mode": "inline",
        "preload": False,
        "fade_in": False,
        "gzip_sidecar": False,
        "expected_total_ms": None,     # times out at scale=100
        "expected_preview_ms": None,
    },
    "r2-async": {
        "label": "async sidecar fetch after load + canvas",
        "prefer_canvas": True,
        "shadow_mode": "async",       # fetch on window.load
        "preload": False,
        "fade_in": False,
        "gzip_sidecar": False,
        "expected_total_ms": 4000,
        "expected_preview_ms": None,
    },
    "r3-preload": {
        "label": "r2 + <link rel=preload> + fetch during HTML parse",
        "prefer_canvas": True,
        "shadow_mode": "async-preload",
        "preload": True,
        "fade_in": False,
        "gzip_sidecar": False,
        "expected_total_ms": 2350,
        "expected_preview_ms": None,
    },
    "r4-fade": {
        "label": "r3 + canvas opacity fade-in transition",
        "prefer_canvas": True,
        "shadow_mode": "async-preload",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": False,
        "expected_total_ms": 2425,
        "expected_preview_ms": None,
    },
    "r5-gzip": {
        "label": "r4 + gzipped sidecar (requires scripts/serve.py)",
        "prefer_canvas": True,
        "shadow_mode": "async-preload",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "expected_total_ms": 1775,
        "expected_preview_ms": None,
    },
    "r6-chunked": {
        "label": "r5 + requestAnimationFrame chunked addData (progressive)",
        "prefer_canvas": True,
        "shadow_mode": "async-preload",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "chunked_add": True,
        "expected_total_ms": 3550,
        "expected_preview_ms": None,
    },
    "r7-canvas-direct": {
        "label": "custom CanvasLayer (naive latLng-per-vertex -- known slow)",
        "prefer_canvas": True,
        "shadow_mode": "async-preload",
        "preload": True,
        "fade_in": False,
        "gzip_sidecar": True,
        "canvas_direct": True,
        "expected_total_ms": None,     # known broken, ~97s at scale=100
        "expected_preview_ms": None,
    },
    "r8-png-overlay": {
        "label": "server-side Pillow PNG + L.imageOverlay (no feature JS)",
        "prefer_canvas": True,
        "shadow_mode": "png-overlay",
        "preload": False,
        "fade_in": True,
        "gzip_sidecar": False,
        "expected_total_ms": 395,
        "expected_preview_ms": None,
    },
    "r9-png-then-vector": {
        "label": "r8 PNG preview, then swap in async canvas vector layer",
        "prefer_canvas": True,
        "shadow_mode": "png-then-vector",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "expected_total_ms": 1940,
        "expected_preview_ms": 350,
    },
    "r10-simplify": {
        "label": "r9 + 3 m polygon simplification (smaller file + faster parse)",
        "prefer_canvas": True,
        "shadow_mode": "png-then-vector",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "simplify_tol_deg": 0.00003,
        "expected_total_ms": 2500,
        "expected_preview_ms": 350,
    },
    "r11-vectorgrid": {
        "label": "r9 + L.vectorGrid.slicer (no per-feature L.Polygon)",
        "prefer_canvas": True,
        "shadow_mode": "png-then-vector",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "render_mode": "vectorgrid",
        "expected_total_ms": 1500,
        "expected_preview_ms": 350,
    },
    "r12-colorbatch": {
        "label": "r9 + group features by color (one L.Polygon per color, no click)",
        "prefer_canvas": True,
        "shadow_mode": "png-then-vector",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "render_mode": "colorbatch",
        "expected_total_ms": 1000,
        "expected_preview_ms": 350,
    },
    "r13-hybrid": {
        "label": "r12 render + point-in-polygon click handler (fast AND clickable)",
        "prefer_canvas": True,
        "shadow_mode": "png-then-vector",
        "preload": True,
        "fade_in": True,
        "gzip_sidecar": True,
        "render_mode": "colorbatch",
        "pip_click": True,
        "expected_total_ms": 1200,
        "expected_preview_ms": 350,
    },
}
DEFAULT_RENDER_STRATEGY = "r13-hybrid"


def add_building_layer(m, building_data, *, out_dir, cfg=None):
    """Write building footprints as an async sidecar and render on canvas.

    Two-stage load: a small `buildings_initial.geojson` contains only
    the features intersecting INITIAL_BBOX. It is fetched with high
    priority and drawn within a few hundred ms. The full
    `buildings.geojson` replaces the initial layer once it arrives.

    When cfg.pip_click is True, buildings are rendered as a single
    L.polygon (much faster than 123 K L.Polygon objects) and their
    features are registered into `window._bdg_feats` so the shadow
    strategy's map click handler can fall through to them when a click
    misses a shadow.
    """
    cfg = cfg or {}
    pip_click = bool(cfg.get("pip_click"))
    features = (building_data or {}).get("features") or []
    if not features:
        return

    full_name = "buildings.geojson"
    full_path = os.path.join(out_dir, full_name)
    _write_geojson(full_path, features, gzip_sidecar=True)

    initial_features = _filter_features_by_bbox(features, INITIAL_BBOX)
    initial_name = "buildings_initial.geojson"
    initial_path = os.path.join(out_dir, initial_name)
    _write_geojson(initial_path, initial_features, gzip_sidecar=True)

    # Only the initial chunk is preloaded. The 4 MB full file is
    # fetched sequentially after the initial layer renders — otherwise
    # its JSON.parse blocks the main thread and the small initial
    # chunk's promise can't resolve in time.
    preload_hint = (
        f'<link rel="preload" href="{initial_name}" as="fetch" '
        f'type="application/geo+json" crossorigin="anonymous" '
        f'fetchpriority="high">'
    )
    m.get_root().header.add_child(folium.Element(preload_hint))

    map_var = m.get_name()
    if pip_click:
        # Colorbatch rendering: one L.polygon covers all buildings.
        # Click goes through the shadow strategy's shared map handler,
        # which falls through to window._bdg_feats when no shadow hits.
        mk_building_js = f"""
  function mkBuildingLayer(data) {{
    var feats = (data && data.features) || [];
    var latlngs = [];
    for (var i = 0; i < feats.length; i++) {{
      var coords = feats[i].geometry && feats[i].geometry.coordinates && feats[i].geometry.coordinates[0];
      if (!coords) continue;
      var ring = [];
      for (var j = 0; j < coords.length; j++) {{
        ring.push([coords[j][1], coords[j][0]]);
      }}
      latlngs.push(ring);
    }}
    return L.polygon(latlngs, {{
      pane: 'buildings',
      color: '#475569', weight: 0.5,
      fillColor: '#94a3b8', fillOpacity: 0.85,
    }});
  }}
  function _bComputeBbox(ring) {{
    var minx = ring[0][0], maxx = minx, miny = ring[0][1], maxy = miny;
    for (var i = 1; i < ring.length; i++) {{
      var x = ring[i][0], y = ring[i][1];
      if (x < minx) minx = x; else if (x > maxx) maxx = x;
      if (y < miny) miny = y; else if (y > maxy) maxy = y;
    }}
    return [minx, miny, maxx, maxy];
  }}
  function _indexBuildings(features) {{
    var out = [];
    for (var i = 0; i < features.length; i++) {{
      var f = features[i];
      var ring = f.geometry && f.geometry.coordinates && f.geometry.coordinates[0];
      if (!ring || ring.length < 3) continue;
      out.push({{ ring: ring, bbox: _bComputeBbox(ring), props: f.properties || {{}} }});
    }}
    return out;
  }}
"""
    else:
        mk_building_js = """
  function bindBuildingPopup(feat, layer) {
    var h = feat.properties && feat.properties.BLDG_HGT_2010;
    var html = '<div style="font-size:12px;">'
      + '<b>Building</b><br>'
      + 'Height: ' + (h != null ? h + ' ft' : 'unknown')
      + '</div>';
    layer.bindPopup(html);
  }
  function mkBuildingLayer(data) {
    return L.geoJSON(data, {
      pane: 'buildings',
      style: {
        color: '#475569', weight: 0.5,
        fillColor: '#94a3b8', fillOpacity: 0.85,
      },
      onEachFeature: bindBuildingPopup,
    });
  }
"""

    pip_register_initial = (
        "window._bdg_feats = _indexBuildings(data.features || []);"
        if pip_click else ""
    )
    pip_register_full = pip_register_initial

    script = f"""
<script>
(function() {{
{mk_building_js}
  function addBuildings() {{
    if (typeof {map_var} === 'undefined') {{
      setTimeout(addBuildings, 50);
      return;
    }}
    if (!{map_var}.getPane('buildings')) {{
      var pane = {map_var}.createPane('buildings');
      // zIndex 410 puts buildings above the default overlayPane (400)
      // where shadows live. Buildings should read as opaque roofs on
      // top of the ground-level shadow layer, not be dimmed by it.
      pane.style.zIndex = 410;
    }}
    var initialLayer = null;
    // Stage 1: fetch the MIT-area initial chunk. Small, fast.
    fetch('{initial_name}')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        initialLayer = mkBuildingLayer(data).addTo({map_var});
        {pip_register_initial}
        window.__lightmap_buildings_initial_at = performance.now();
        setTimeout(fetchFull, 0);
      }})
      .catch(function(err) {{
        console.error('buildings_initial failed:', err);
        fetchFull();
      }});

    function fetchFull() {{
      // Stage 2: fetch the full set. Swap out the initial layer on arrival.
      fetch('{full_name}')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          var full = mkBuildingLayer(data).addTo({map_var});
          if (initialLayer) {{ {map_var}.removeLayer(initialLayer); }}
          {pip_register_full}
          window.__lightmap_buildings_ready = performance.now();
        }})
        .catch(function(err) {{
          console.error('buildings full failed:', err);
        }});
    }}
  }}
  addBuildings();
}})();
</script>
"""
    m.get_root().html.add_child(folium.Element(script))


def shadow_color_stops_for_js(cmap):
    """Convert a branca LinearColormap into a plain JS-usable stop table.

    Returns a list of (threshold_ft, '#rrggbb') tuples sorted ascending,
    matching how branca interpolates colors. The browser-side style_fn
    uses this to pick a color per feature without depending on branca.
    """
    stops = []
    for i, color in enumerate(SHADOW_CMAP_COLORS):
        t = (cmap.vmax - cmap.vmin) * i / (len(SHADOW_CMAP_COLORS) - 1) + cmap.vmin
        stops.append((round(t, 1), color))
    return stops


def add_shadow_layer(m, shadows, cmap, *,
                     strategy=DEFAULT_RENDER_STRATEGY, out_dir):
    """Attach the shadow overlay to the map using the selected strategy.

    See RENDER_STRATEGIES for the catalog of variants and what each flag
    means. This function only dispatches: each branch below implements
    one version of the rendering path verbatim, so the benchmark runner
    can regenerate every variant and diff them.

    `out_dir` is where strategies that emit sidecar artifacts
    (shadows.geojson, shadows.png) write them. For inline and PNG-free
    strategies it is unused but kept in the signature so callers don't
    have to branch.
    """
    cfg = RENDER_STRATEGIES.get(strategy) or RENDER_STRATEGIES[DEFAULT_RENDER_STRATEGY]

    mode = cfg["shadow_mode"]
    if mode == "inline":
        _add_shadow_layer_inline(m, shadows, cmap)
    elif mode == "png-overlay":
        _add_shadow_layer_png_overlay(
            m, shadows, cmap, fade_in=cfg["fade_in"], out_dir=out_dir)
    elif mode == "png-then-vector":
        _add_shadow_layer_png_then_vector(
            m, shadows, cmap, cfg=cfg, out_dir=out_dir)
    else:
        _add_shadow_layer_async(
            m, shadows, cmap,
            preload=cfg["preload"],
            fade_in=cfg["fade_in"],
            gzip_sidecar=cfg["gzip_sidecar"],
            chunked_add=cfg.get("chunked_add", False),
            canvas_direct=cfg.get("canvas_direct", False),
            out_dir=out_dir,
        )


def _add_shadow_layer_png_then_vector(m, shadows, cmap, *, cfg, out_dir):
    """r9: PNG preview overlay first, then async vector layer replaces it.

    Combines r8 (fastest time-to-first-pixel) with r4-fade (interactive
    vector polygons with per-feature styling). The browser sees the PNG
    essentially instantly (360 ms), then the bigger GeoJSON sidecar
    arrives, parses, and the canvas vector layer fades in to replace
    the PNG preview. __lightmap.addedAt fires when the VECTOR layer is
    ready, so r9's "total" metric is comparable to r4. We add a new
    milestone `previewAt` that records when the PNG became visible.
    """
    from shapely.geometry import shape as _shape

    # Write both artifacts: PNG preview and vector sidecar (+gz).
    png_name = "shadows.png"
    png_path = os.path.join(out_dir, png_name)
    polys = []
    for feat in shadows:
        geom = feat.get("geometry") or {}
        if geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                polys.append(_shape(geom))
            except Exception:
                continue
    # Primary-visual PNG. 2 m grid + union + 1.5 px gaussian blur.
    # alpha=190 (~75 %) so shadows are clearly readable against the
    # light CARTO basemap instead of looking like faint tint. Union
    # merges touching per-building shadows so adjacent buildings share
    # one geometry, minimizing seam visibility without growing the
    # outer silhouette into sunny areas.
    _W, _H, bounds = render_shadows_png(
        polys, png_path, resolution_m=2.0, alpha=190,
        blur_px=1.5, shrink_px=0.0, union_all=True,
    )
    bounds_js = json.dumps(bounds)

    simplify_tol = cfg.get("simplify_tol_deg")

    # Merge shadows per color bucket into single geometries each so
    # neighboring same-color shadows share one path. Removes canvas-AA
    # seams that render as bright 1-2 px strips along abutting edges.
    merge_stops = shadow_color_stops_for_js(cmap)
    merged_full = _merge_shadows_by_bucket(shadows, merge_stops)
    print(f"  Shadow merge: {len(shadows)} features -> {len(merged_full)} "
          f"merged (by color bucket)")

    # Full sidecar.
    sidecar_name = "shadows.geojson"
    sidecar_path = os.path.join(out_dir, sidecar_name)
    _write_geojson(
        sidecar_path, merged_full,
        gzip_sidecar=bool(cfg.get("gzip_sidecar")),
        simplify_tol_deg=simplify_tol,
    )

    # Initial chunk: only features intersecting INITIAL_BBOX (~MIT campus).
    # Filter first (on per-building shadows) so the bbox intersection
    # is accurate, then merge within the filtered set.
    initial_name = "shadows_initial.geojson"
    initial_path = os.path.join(out_dir, initial_name)
    initial_features = _filter_features_by_bbox(shadows, INITIAL_BBOX)
    merged_initial = _merge_shadows_by_bucket(initial_features, merge_stops)
    _write_geojson(
        initial_path, merged_initial,
        gzip_sidecar=bool(cfg.get("gzip_sidecar")),
        simplify_tol_deg=simplify_tol,
    )

    # Preload hints: only the PNG + the initial chunk are preloaded.
    # The full sidecar is deliberately NOT preloaded — starting its
    # fetch+parse in parallel with the initial chunk would block the
    # main thread on a 28 MB JSON.parse before the initial chunk's
    # promise could resolve. We fetch the full set in the background
    # only AFTER the initial layer has rendered.
    hints = [
        f'<link rel="preload" href="{png_name}" as="image" '
        f'fetchpriority="high">',
        f'<link rel="preload" href="{initial_name}" as="fetch" '
        f'type="application/geo+json" crossorigin="anonymous" '
        f'fetchpriority="high">',
    ]
    for h in hints:
        m.get_root().header.add_child(folium.Element(h))

    # Some render_mode variants need external scripts from CDN. These
    # must load *after* Leaflet itself, which folium puts in <body>'s
    # JS block, so we append to the body rather than <head>.
    render_mode = cfg.get("render_mode", "geojson")
    if render_mode == "vectorgrid":
        m.get_root().html.add_child(folium.Element(
            '<script src="https://unpkg.com/leaflet.vectorgrid@1.3.0/'
            'dist/Leaflet.VectorGrid.bundled.min.js"></script>'
        ))

    stops = shadow_color_stops_for_js(cmap)
    stops_js = json.dumps(stops)
    map_var = m.get_name()

    if render_mode == "vectorgrid":
        # L.vectorGrid.slicer renders straight to canvas tiles without
        # creating a per-feature L.Polygon. At 123 K features this is
        # the single biggest instantiation-time win available without
        # switching off Leaflet.
        mk_shadow_js = f"""
  function mkShadowLayer(data) {{
    var layer = L.vectorGrid.slicer(data, {{
      vectorTileLayerStyles: {{
        sliced: function(properties, zoom) {{
          var h = (properties && properties.height_ft) || 0;
          var c = colorFor(h);
          return {{
            fillColor: c, color: c, weight: 0.3,
            fillOpacity: 0.45, fill: true,
          }};
        }},
      }},
      interactive: true,
      maxNativeZoom: 18,
    }});
    layer.on('click', function(e) {{
      var p = e.layer && e.layer.properties;
      if (!p) return;
      var h = p.height_ft;
      var s = p.shadow_len_ft;
      var html = '<div style="font-size:12px;">';
      if (h != null) html += '<b>Building Height:</b> ' + h + ' ft<br>';
      if (s != null) html += '<b>Shadow Length:</b> ' + s + ' ft';
      html += '</div>';
      L.popup().setLatLng(e.latlng).setContent(html).openOn({map_var});
    }});
    return layer;
  }}
"""
    elif render_mode == "colorbatch":
        # Group features by fill color. One L.polygon per color means
        # only a handful of Leaflet objects instead of 123 K. Trade-off:
        # click does not surface per-feature info; the whole color group
        # acts as one clickable layer.
        #
        # Drawing order matters: the last L.polygon addTo'd ends up on
        # top. We iterate `stops` (light → dark) so the darkest shadow
        # bucket is painted last and sits on top when shadows overlap.
        # Without this, Object.keys(byColor) follows insertion order and
        # can leave a lighter bucket on top, making it visually "cut"
        # into neighboring darker shadows.
        mk_shadow_js = f"""
  function mkShadowLayer(data) {{
    var byColor = {{}};
    var feats = (data && data.features) || [];
    for (var i = 0; i < feats.length; i++) {{
      var f = feats[i];
      var h = (f.properties && f.properties.height_ft) || 0;
      var c = colorFor(h);
      if (!byColor[c]) byColor[c] = [];
      var coords = f.geometry && f.geometry.coordinates && f.geometry.coordinates[0];
      if (!coords) continue;
      var latlngs = [];
      for (var j = 0; j < coords.length; j++) {{
        latlngs.push([coords[j][1], coords[j][0]]);
      }}
      byColor[c].push(latlngs);
    }}
    var grp = L.layerGroup();
    // Vector shadow layer renders INVISIBLE. The PNG overlay is the
    // primary visual. Features still exist here so the PIP click
    // handler can resolve per-feature height/shadow-length popups,
    // but any fill would paint its own polygon seams on top of the
    // clean PNG — so fillOpacity and weight are both zero.
    for (var si = 0; si < stops.length; si++) {{
      var c = stops[si][1];
      if (!byColor[c] || byColor[c].length === 0) continue;
      L.polygon(byColor[c], {{
        fillColor: c, color: c, weight: 0, fillOpacity: 0,
      }}).addTo(grp);
    }}
    return grp;
  }}
"""
    else:  # geojson (default / r9, r10)
        mk_shadow_js = """
  function mkShadowLayer(data) {
    return L.geoJSON(data, {
      style: styleFn,
      onEachFeature: bindShadowPopup,
    });
  }
"""

    script = f"""
<script>
(function() {{
  var stops = {stops_js};
  function colorFor(h) {{
    for (var i = stops.length - 1; i >= 0; i--) {{
      if (h >= stops[i][0]) return stops[i][1];
    }}
    return stops[0][1];
  }}
  function styleFn(feature) {{
    var h = (feature.properties && feature.properties.height_ft) || 0;
    var c = colorFor(h);
    return {{ fillColor: c, color: c, weight: 0.3, fillOpacity: 0.45 }};
  }}
  function bindShadowPopup(feature, layer) {{
    var p = feature.properties || {{}};
    var h = p.height_ft;
    var s = p.shadow_len_ft;
    var html = '<div style="font-size:12px;">';
    if (h != null) html += '<b>Building Height:</b> ' + h + ' ft<br>';
    if (s != null) html += '<b>Shadow Length:</b> ' + s + ' ft';
    html += '</div>';
    layer.bindPopup(html);
  }}
  window.__lightmap = {{
    fetchStart: null,
    fetchEnd: null,
    previewAt: null,
    initialAt: null,
    addedAt: null,
    featureCount: null,
    initialFeatureCount: null,
    status: 'pending',
  }};
  function mark(name) {{ window.__lightmap[name] = performance.now(); }}
{mk_shadow_js}

  // Point-in-polygon click handler. Active only for render modes that
  // do not bind per-feature popups via L.geoJSON (colorbatch / vectorgrid).
  // We keep the current feature set in window._sh_feats (with bbox pre-
  // indexes) and do bbox-filter + ray-cast PIP on click. 123K features
  // × ~10 ns bbox check = ~1.2 ms before PIP.
  var _pipEnabled = {str(bool(cfg.get("pip_click"))).lower()};
  var _pipInstalled = false;
  function _computeBbox(ring) {{
    var minx = ring[0][0], maxx = minx, miny = ring[0][1], maxy = miny;
    for (var i = 1; i < ring.length; i++) {{
      var x = ring[i][0], y = ring[i][1];
      if (x < minx) minx = x; else if (x > maxx) maxx = x;
      if (y < miny) miny = y; else if (y > maxy) maxy = y;
    }}
    return [minx, miny, maxx, maxy];
  }}
  function _pointInRing(x, y, ring) {{
    var inside = false;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {{
      var xi = ring[i][0], yi = ring[i][1];
      var xj = ring[j][0], yj = ring[j][1];
      if (((yi > y) !== (yj > y))
          && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) {{
        inside = !inside;
      }}
    }}
    return inside;
  }}
  function _indexFeatures(features) {{
    var out = [];
    for (var i = 0; i < features.length; i++) {{
      var f = features[i];
      var ring = f.geometry && f.geometry.coordinates && f.geometry.coordinates[0];
      if (!ring || ring.length < 3) continue;
      out.push({{ ring: ring, bbox: _computeBbox(ring), props: f.properties || {{}} }});
    }}
    return out;
  }}
  function _installPipClick() {{
    if (_pipInstalled) return;
    _pipInstalled = true;
    {map_var}.on('click', function(e) {{
      var lng = e.latlng.lng, lat = e.latlng.lat;
      var feats = window._sh_feats || [];
      for (var i = feats.length - 1; i >= 0; i--) {{
        var f = feats[i], b = f.bbox;
        if (lng < b[0] || lng > b[2] || lat < b[1] || lat > b[3]) continue;
        if (_pointInRing(lng, lat, f.ring)) {{
          var p = f.props, h = p.height_ft, s = p.shadow_len_ft;
          var html = '<div style="font-size:12px;">';
          if (h != null) html += '<b>Building Height:</b> ' + h + ' ft<br>';
          if (s != null) html += '<b>Shadow Length:</b> ' + s + ' ft';
          html += '</div>';
          L.popup().setLatLng(e.latlng).setContent(html).openOn({map_var});
          return;
        }}
      }}
      // No shadow match at this point — fall through to buildings if
      // the building layer has registered its own PIP index.
      var bFeats = window._bdg_feats || [];
      for (var i = bFeats.length - 1; i >= 0; i--) {{
        var f = bFeats[i], b = f.bbox;
        if (lng < b[0] || lng > b[2] || lat < b[1] || lat > b[3]) continue;
        if (_pointInRing(lng, lat, f.ring)) {{
          var h = f.props.BLDG_HGT_2010;
          var html = '<div style="font-size:12px;"><b>Building</b><br>'
            + 'Height: ' + (h != null ? h + ' ft' : 'unknown') + '</div>';
          L.popup().setLatLng(e.latlng).setContent(html).openOn({map_var});
          return;
        }}
      }}
    }});
  }}

  function addPreviewAndVector() {{
    if (typeof {map_var} === 'undefined') {{
      setTimeout(addPreviewAndVector, 50);
      return;
    }}
    if ('{render_mode}' === 'vectorgrid' && (typeof L === 'undefined' || !L.vectorGrid)) {{
      // VectorGrid CDN script hasn't finished loading yet. Retry in 50 ms.
      setTimeout(addPreviewAndVector, 50);
      return;
    }}
    // PNG is the primary shadow visual. Stays on the map permanently.
    // Vector features still load (for PIP click / popup) but render
    // invisible so they contribute no pixels and produce no seams.
    var preview = L.imageOverlay('{png_name}', {bounds_js}, {{
      opacity: 1.0, interactive: false,
    }});
    preview.on('load', function() {{ mark('previewAt'); }});
    preview.addTo({map_var});

    var initialLayer = null;
    window.__lightmap.status = 'fetching';
    mark('fetchStart');

    // Stage 2: fetch the INITIAL chunk (MIT-area features only).
    // Small + fast -> interactive shadows within the viewport in ~500 ms.
    // We wait for this to render before starting Stage 3 so the 28 MB
    // JSON.parse of the full sidecar does not block this callback.
    fetch('{initial_name}')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        window.__lightmap.initialFeatureCount = (data.features || []).length;
        initialLayer = mkShadowLayer(data).addTo({map_var});
        if (_pipEnabled) {{
          window._sh_feats = _indexFeatures(data.features || []);
          _installPipClick();
        }}
        // Do NOT remove preview. PNG is the permanent visual;
        // the vector layer renders with fillOpacity=0 below.
        mark('initialAt');
        // Defer the full fetch to the next macrotask so the browser
        // paints the initial layer before we start downloading+parsing
        // the 28 MB full sidecar.
        setTimeout(fetchFull, 0);
      }})
      .catch(function(err) {{
        console.error('shadows_initial failed:', err);
        fetchFull();
      }});

    function fetchFull() {{
      // Stage 3: fetch the FULL sidecar in the background and swap in
      // once it arrives. The initial layer is removed to avoid double
      // rendering (the full set is a superset).
      fetch('{sidecar_name}')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          mark('fetchEnd');
          window.__lightmap.featureCount = (data && data.features) ? data.features.length : 0;
          var full = mkShadowLayer(data).addTo({map_var});
          var cvs = full._renderer && full._renderer._container;
          if (cvs) {{
            cvs.style.transition = 'opacity 300ms ease-out';
            cvs.style.opacity = '0';
            requestAnimationFrame(function(){{ cvs.style.opacity = '1'; }});
          }}
          if (initialLayer) {{ {map_var}.removeLayer(initialLayer); }}
          if (_pipEnabled) {{
            window._sh_feats = _indexFeatures(data.features || []);
            _installPipClick();
          }}
          window.__lightmap.status = 'done';
          mark('addedAt');
        }})
        .catch(function(err) {{
          window.__lightmap.status = 'error';
          window.__lightmap.error = String(err);
          console.error('shadows full failed:', err);
        }});
    }}
  }}
  addPreviewAndVector();
}})();
</script>
"""
    m.get_root().html.add_child(folium.Element(script))


def _add_shadow_layer_png_overlay(m, shadows, cmap, *, fade_in=False, out_dir):
    """r8: rasterize shadows to a PNG server-side and add as L.imageOverlay.

    Skips the entire client-side geometry parsing pipeline. The cost is
    a fixed PNG decode (a few MB at most), one <img> in the DOM, and
    Leaflet's built-in image overlay which is a single transform.
    """
    from shapely.geometry import shape as _shape

    png_name = "shadows.png"
    png_path = os.path.join(out_dir, png_name)

    # Parse the GeoJSON features back into shapely polygons. Not free at
    # 123K features (~200 ms), but happens once at server build time
    # and never on the client.
    polys = []
    for feat in shadows:
        geom = feat.get("geometry") or {}
        if geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                polys.append(_shape(geom))
            except Exception:
                continue

    _W, _H, bounds = render_shadows_png(polys, png_path)

    map_var = m.get_name()
    # L.imageOverlay takes [[south, west], [north, east]].
    bounds_js = json.dumps(bounds)
    fade_css = (
        "img.style.transition='opacity 300ms ease-out';"
        "img.style.opacity='0';"
        "requestAnimationFrame(function(){img.style.opacity='1';});"
        if fade_in
        else ""
    )
    script = f"""
<script>
(function() {{
  window.__lightmap = {{
    fetchStart: null,
    fetchEnd: null,
    addedAt: null,
    featureCount: null,
    status: 'pending',
  }};
  function mark(name) {{
    window.__lightmap[name] = performance.now();
  }}
  function addOverlay() {{
    if (typeof {map_var} === 'undefined') {{
      setTimeout(addOverlay, 50);
      return;
    }}
    window.__lightmap.status = 'fetching';
    mark('fetchStart');
    var layer = L.imageOverlay('{png_name}', {bounds_js}, {{
      opacity: 1.0, interactive: false,
    }});
    layer.on('load', function() {{
      mark('fetchEnd');
      var img = layer.getElement && layer.getElement();
      if (img) {{ {fade_css} }}
      window.__lightmap.status = 'done';
      mark('addedAt');
    }});
    layer.addTo({map_var});
  }}
  if (document.readyState === 'complete') {{ addOverlay(); }}
  else {{ window.addEventListener('load', addOverlay); }}
}})();
</script>
"""
    m.get_root().html.add_child(folium.Element(script))


def _add_shadow_layer_inline(m, shadows, cmap):
    """Baseline: folium embeds every shadow feature inline into the HTML.

    At 123K features this produces a single ~32 MB JS string literal and
    the browser stalls parsing it. Preserved here as the reference bad
    case the other strategies are measured against.
    """
    folium.GeoJson(
        {"type": "FeatureCollection", "features": shadows},
        name="Shadows",
        style_function=lambda x: {
            "fillColor": cmap(x["properties"].get("height_ft", 0)),
            "color": cmap(x["properties"].get("height_ft", 0)),
            "weight": 0.3,
            "fillOpacity": 0.45,
        },
        highlight_function=lambda x: {
            "weight": 2,
            "fillOpacity": 0.65,
        },
        popup=GeoJsonPopup(
            fields=["height_ft", "shadow_len_ft"],
            aliases=["Building Height (ft):", "Shadow Length (ft):"],
            style="font-size:12px;",
        ),
    ).add_to(m)


def _add_shadow_layer_async(m, shadows, cmap, *, preload=False,
                             fade_in=False, gzip_sidecar=False,
                             chunked_add=False, canvas_direct=False,
                             out_dir):
    """Write shadow FeatureCollection to a side file and fetch it async.

    Skips folium's inline-embedding code path entirely. Writes the
    features as compact JSON (no extra whitespace), constructs the layer
    in raw Leaflet, and relies on `prefer_canvas=True` on the base map so
    the 123K features go to a single <canvas> element instead of the
    DOM.

    Flags let the caller toggle incremental improvements so the render
    benchmark can measure each in isolation:
      preload       - inject <link rel=preload> so the browser starts the
                      shadow fetch during HTML parse instead of after
                      window.load, overlapping it with base tile loading.
      fade_in       - animate the canvas opacity from 0 to 1 over 300 ms
                      so shadows materialize smoothly instead of popping.
      gzip_sidecar  - also write a .gz version of the sidecar. The page
                      still references the plain .geojson name; the
                      gzip-aware server (scripts/serve.py) will serve
                      the compressed copy with Content-Encoding: gzip.
    """
    sidecar_name = "shadows.geojson"
    sidecar_path = os.path.join(out_dir, sidecar_name)
    with open(sidecar_path, "w") as f:
        json.dump(
            {"type": "FeatureCollection", "features": shadows},
            f,
            separators=(",", ":"),
        )

    if gzip_sidecar:
        gz_path = sidecar_path + ".gz"
        with open(sidecar_path, "rb") as src, _gzip.open(gz_path, "wb", 6) as dst:
            dst.writelines(src)

    stops = shadow_color_stops_for_js(cmap)
    stops_js = json.dumps(stops)
    map_var = m.get_name()

    if preload:
        # Put the preload hint in <head> so the HTML preload scanner picks
        # it up during initial parse, before the main JS runs. The fetch()
        # call below will reuse the preloaded response from the HTTP cache.
        preload_hint = (
            f'<link rel="preload" href="{sidecar_name}" as="fetch" '
            f'type="application/geo+json" crossorigin="anonymous" '
            f'fetchpriority="high">'
        )
        m.get_root().header.add_child(folium.Element(preload_hint))

    # With preload enabled we kick off the fetch synchronously at script
    # parse time; the base tiles, leaflet scripts, and our fetch overlap.
    # Without it we fall back to the old "wait for load event" gate.
    start_gate_js = (
        "addShadows();"
        if preload
        else (
            "if (document.readyState === 'complete') { addShadows(); } "
            "else { window.addEventListener('load', addShadows); }"
        )
    )

    # Canvas fade-in uses Leaflet's internal renderer reference to grab
    # the single <canvas> element backing the shadow layer and animate
    # its opacity. The reveal feels smooth vs. the default pop-in.
    fade_in_js = (
        """
        var cvs = layer._renderer && layer._renderer._container;
        if (cvs) {
          cvs.style.transition = 'opacity 300ms ease-out';
          cvs.style.opacity = '0';
          requestAnimationFrame(function(){ cvs.style.opacity = '1'; });
        }
        """
        if fade_in
        else ""
    )

    # Chunked addition: create an empty L.geoJSON layer up front, add it
    # to the map, then addData() in batches under requestAnimationFrame.
    # Total wall time is the same or slightly worse, but the user sees
    # shadows materialize progressively instead of all at once at the
    # end. __lightmap.addedAt fires on the last chunk.
    chunked_js = f"""
        var layer = L.geoJSON(null, {{
          style: styleFn,
          onEachFeature: bindShadowPopup,
        }}).addTo({map_var});
        var feats = data.features || [];
        var chunkSize = 4000;
        var i = 0;
        function pump() {{
          if (i >= feats.length) {{
            window.__lightmap.status = 'done';
            mark('addedAt');{fade_in_js}
            return;
          }}
          var end = Math.min(i + chunkSize, feats.length);
          layer.addData({{
            type: 'FeatureCollection',
            features: feats.slice(i, end),
          }});
          i = end;
          if (i === chunkSize) {{
            // Report first visible chunk time so the harness can
            // separate "first pixels" from "all pixels".
            mark('firstChunkAt');
          }}
          requestAnimationFrame(pump);
        }}
        pump();
    """

    # Default: single L.geoJSON call with optional fade-in.
    default_add_js = f"""
        var layer = L.geoJSON(data, {{
          style: styleFn,
          onEachFeature: bindShadowPopup,
        }}).addTo({map_var});{fade_in_js}
        window.__lightmap.status = 'done';
        mark('addedAt');
    """

    # Custom CanvasLayer: skip L.Polygon entirely. We walk the flat
    # coordinate array once, convert each vertex via latLngToContainerPoint
    # inside the layer's draw callback, and issue moveTo/lineTo/fill calls
    # directly to the layer's 2D context. This removes 123K per-feature
    # allocations (one of the dominant costs in vanilla L.geoJSON).
    canvas_direct_js = f"""
        var LightmapCanvas = L.Layer.extend({{
          initialize: function (features, stops) {{
            this._features = features;
            this._stops = stops;
          }},
          onAdd: function (map) {{
            this._map = map;
            var pane = map.getPanes().overlayPane;
            if (!this._canvas) {{
              this._canvas = L.DomUtil.create('canvas', 'lightmap-canvas');
              pane.appendChild(this._canvas);
            }}
            map.on('moveend resize zoomend', this._reset, this);
            this._reset();
          }},
          onRemove: function (map) {{
            if (this._canvas && this._canvas.parentNode) {{
              this._canvas.parentNode.removeChild(this._canvas);
            }}
            map.off('moveend resize zoomend', this._reset, this);
          }},
          _reset: function () {{
            var map = this._map, size = map.getSize();
            var topLeft = map.containerPointToLayerPoint([0, 0]);
            L.DomUtil.setPosition(this._canvas, topLeft);
            this._canvas.width = size.x;
            this._canvas.height = size.y;
            this._canvas.style.width = size.x + 'px';
            this._canvas.style.height = size.y + 'px';
            this._draw();
          }},
          _colorFor: function (h) {{
            var stops = this._stops;
            for (var i = stops.length - 1; i >= 0; i--) {{
              if (h >= stops[i][0]) return stops[i][1];
            }}
            return stops[0][1];
          }},
          _draw: function () {{
            if (!this._canvas) return;
            var ctx = this._canvas.getContext('2d');
            ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
            ctx.globalAlpha = 0.45;
            var map = this._map;
            var feats = this._features;
            var byColor = {{}};
            // Group features by fill color so we minimize ctx state
            // changes (one beginPath + fill per color).
            for (var fi = 0; fi < feats.length; fi++) {{
              var f = feats[fi];
              var props = f.properties || {{}};
              var c = this._colorFor(props.height_ft || 0);
              (byColor[c] = byColor[c] || []).push(f);
            }}
            for (var color in byColor) {{
              ctx.fillStyle = color;
              ctx.beginPath();
              var group = byColor[color];
              for (var gi = 0; gi < group.length; gi++) {{
                var coords = group[gi].geometry.coordinates[0];
                if (!coords || !coords.length) continue;
                var p0 = map.latLngToContainerPoint([coords[0][1], coords[0][0]]);
                ctx.moveTo(p0.x, p0.y);
                for (var ci = 1; ci < coords.length; ci++) {{
                  var p = map.latLngToContainerPoint([coords[ci][1], coords[ci][0]]);
                  ctx.lineTo(p.x, p.y);
                }}
                ctx.closePath();
              }}
              ctx.fill();
            }}
          }},
        }});
        var layer = new LightmapCanvas(data.features || [], stops).addTo({map_var});
        window.__lightmap.status = 'done';
        mark('addedAt');
    """

    if canvas_direct:
        add_path_js = canvas_direct_js
    elif chunked_add:
        add_path_js = chunked_js
    else:
        add_path_js = default_add_js

    script_html = f"""
<script>
(function() {{
  var stops = {stops_js};
  function colorFor(h) {{
    for (var i = stops.length - 1; i >= 0; i--) {{
      if (h >= stops[i][0]) return stops[i][1];
    }}
    return stops[0][1];
  }}
  function styleFn(feature) {{
    var h = (feature.properties && feature.properties.height_ft) || 0;
    var c = colorFor(h);
    return {{
      fillColor: c, color: c, weight: 0.3, fillOpacity: 0.45
    }};
  }}
  function bindShadowPopup(feature, layer) {{
    var p = feature.properties || {{}};
    var h = p.height_ft;
    var s = p.shadow_len_ft;
    var html = '<div style="font-size:12px;">';
    if (h != null) html += '<b>Building Height:</b> ' + h + ' ft<br>';
    if (s != null) html += '<b>Shadow Length:</b> ' + s + ' ft';
    html += '</div>';
    layer.bindPopup(html);
  }}
  // Expose render milestones for headless verification. A separate test
  // harness can poll these flags or wait on the custom events emitted
  // below to build a timing profile without guessing.
  window.__lightmap = {{
    fetchStart: null,
    fetchEnd: null,
    addedAt: null,
    featureCount: null,
    status: 'pending',
  }};
  function mark(name) {{
    var t = performance.now();
    window.__lightmap[name] = t;
    try {{
      window.dispatchEvent(new CustomEvent('lightmap:' + name, {{
        detail: {{ t: t, status: window.__lightmap.status }}
      }}));
    }} catch (e) {{ /* older browsers, no CustomEvent ctor */ }}
  }}
  function addShadows() {{
    if (typeof {map_var} === 'undefined') {{
      setTimeout(addShadows, 50);
      return;
    }}
    window.__lightmap.status = 'fetching';
    mark('fetchStart');
    fetch('{sidecar_name}')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        mark('fetchEnd');
        window.__lightmap.featureCount = (data && data.features) ? data.features.length : 0;
        {add_path_js}
      }})
      .catch(function(err) {{
        window.__lightmap.status = 'error';
        window.__lightmap.error = String(err);
        console.error('failed to load shadows:', err);
      }});
  }}
  {start_gate_js}
}})();
</script>
"""
    m.get_root().html.add_child(folium.Element(script_html))
