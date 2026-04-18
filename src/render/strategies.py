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


SHADOW_CMAP_COLORS = ["#cbd5e1", "#64748b", "#334155", "#0f172a"]


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
}
DEFAULT_RENDER_STRATEGY = "r9-png-then-vector"


def add_building_layer(m, building_data, *, out_dir):
    """Write building footprints as an async sidecar and render on canvas.

    At scale=100 the building set (~123 K polygons) is too large to inline
    into the HTML, and Leaflet's SVG renderer chokes on that many DOM
    nodes. We write the features to `docs/buildings.geojson` (compact
    JSON + gzip sidecar), preload the .geojson during HTML parse, and
    fetch+render on canvas so every building is visible and clickable —
    the popup shows height in feet.

    Buildings are rendered under the shadow layer (L.geoJSON `pane` +
    explicit `zIndex`) so they stay visible through the semi-transparent
    shadow canvas.
    """
    features = (building_data or {}).get("features") or []
    if not features:
        return

    sidecar_name = "buildings.geojson"
    sidecar_path = os.path.join(out_dir, sidecar_name)
    with open(sidecar_path, "w") as f:
        json.dump(
            {"type": "FeatureCollection", "features": features},
            f,
            separators=(",", ":"),
        )
    gz_path = sidecar_path + ".gz"
    with open(sidecar_path, "rb") as src, _gzip.open(gz_path, "wb", 6) as dst:
        dst.writelines(src)

    # Preload during HTML parse so the fetch overlaps with Leaflet init.
    preload_hint = (
        f'<link rel="preload" href="{sidecar_name}" as="fetch" '
        f'type="application/geo+json" crossorigin="anonymous" '
        f'fetchpriority="low">'
    )
    m.get_root().header.add_child(folium.Element(preload_hint))

    map_var = m.get_name()
    script = f"""
<script>
(function() {{
  function addBuildings() {{
    if (typeof {map_var} === 'undefined') {{
      setTimeout(addBuildings, 50);
      return;
    }}
    // Dedicated pane below the shadow layer so buildings stay visible
    // behind the semi-transparent shadow canvas.
    if (!{map_var}.getPane('buildings')) {{
      var pane = {map_var}.createPane('buildings');
      pane.style.zIndex = 390;
    }}
    fetch('{sidecar_name}')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        L.geoJSON(data, {{
          pane: 'buildings',
          style: {{
            color: '#475569',
            weight: 0.5,
            fillColor: '#94a3b8',
            fillOpacity: 0.35,
          }},
          onEachFeature: function(feat, layer) {{
            var h = feat.properties && feat.properties.BLDG_HGT_2010;
            var html = '<div style="font-size:12px;">'
              + '<b>Building</b><br>'
              + 'Height: ' + (h != null ? h + ' ft' : 'unknown')
              + '</div>';
            layer.bindPopup(html);
          }},
        }}).addTo({map_var});
        window.__lightmap_buildings_ready = performance.now();
      }})
      .catch(function(err) {{
        console.error('failed to load buildings:', err);
      }});
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
    # Preview PNG: 10 m grid at higher opacity. At 20 m the shadows
    # visually blend into the light CARTO basemap at typical zoom (13-15)
    # and the user reported "preview invisible". 10 m stays lightweight
    # (~200 KB) but is crisp enough to register, and alpha=180/255 (~70 %)
    # makes it clearly visible against the basemap.
    _W, _H, bounds = render_shadows_png(
        polys, png_path, resolution_m=10.0, alpha=180,
    )
    bounds_js = json.dumps(bounds)

    sidecar_name = "shadows.geojson"
    sidecar_path = os.path.join(out_dir, sidecar_name)
    with open(sidecar_path, "w") as f:
        json.dump(
            {"type": "FeatureCollection", "features": shadows},
            f,
            separators=(",", ":"),
        )
    if cfg.get("gzip_sidecar"):
        with open(sidecar_path, "rb") as src, _gzip.open(
            sidecar_path + ".gz", "wb", 6
        ) as dst:
            dst.writelines(src)

    # Preload the PNG preview at high priority so the HTML preload
    # scanner kicks it off during parse, before even the Leaflet JS
    # loads. Also preload the vector sidecar at low priority so it
    # starts after the PNG but still overlaps with Leaflet init.
    hints = [
        f'<link rel="preload" href="{png_name}" as="image" '
        f'fetchpriority="high">',
    ]
    if cfg.get("preload"):
        hints.append(
            f'<link rel="preload" href="{sidecar_name}" as="fetch" '
            f'type="application/geo+json" crossorigin="anonymous" '
            f'fetchpriority="low">'
        )
    for h in hints:
        m.get_root().header.add_child(folium.Element(h))

    stops = shadow_color_stops_for_js(cmap)
    stops_js = json.dumps(stops)
    map_var = m.get_name()

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
    addedAt: null,
    featureCount: null,
    status: 'pending',
  }};
  function mark(name) {{ window.__lightmap[name] = performance.now(); }}

  function addPreviewAndVector() {{
    if (typeof {map_var} === 'undefined') {{
      setTimeout(addPreviewAndVector, 50);
      return;
    }}
    // Stage 1: PNG preview overlay.
    var preview = L.imageOverlay('{png_name}', {bounds_js}, {{
      opacity: 1.0, interactive: false,
    }});
    preview.on('load', function() {{ mark('previewAt'); }});
    preview.addTo({map_var});

    // Stage 2: fetch the vector sidecar in the background. Replaces
    // the preview as soon as it is drawn.
    window.__lightmap.status = 'fetching';
    mark('fetchStart');
    fetch('{sidecar_name}')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        mark('fetchEnd');
        window.__lightmap.featureCount = (data && data.features) ? data.features.length : 0;
        var layer = L.geoJSON(data, {{
          style: styleFn,
          onEachFeature: bindShadowPopup,
        }}).addTo({map_var});
        var cvs = layer._renderer && layer._renderer._container;
        if (cvs) {{
          cvs.style.transition = 'opacity 300ms ease-out';
          cvs.style.opacity = '0';
          requestAnimationFrame(function(){{ cvs.style.opacity = '1'; }});
        }}
        // Remove the PNG preview on the next frame so the swap is
        // visually seamless.
        setTimeout(function() {{
          {map_var}.removeLayer(preview);
        }}, 300);
        window.__lightmap.status = 'done';
        mark('addedAt');
      }})
      .catch(function(err) {{
        window.__lightmap.status = 'error';
        window.__lightmap.error = String(err);
        console.error('failed to load shadows:', err);
      }});
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
