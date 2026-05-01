# Shade by Day, Light by Night Roadmap

> Updated 2026-05-01. This is the active narrative and development direction
> for the time-slider version of LightMap.

## Narrative

The proposal-level slogan remains:

> Shade by day. Light by night.

That public phrase should stay on top. Under it, the implementation story starts
with the daytime problem:

> Where is shade right now?

The primary experience is moving daytime shade. A user scrubs time and sees
building shadows move with the sun while tree canopy provides separate shade
context.

The paired nighttime experience is light and open-place activity:

> Where is light right now?

At night, the map emphasizes streetlight glow and venues that are open at the
selected hour. The story is brightness, visibility, and activity context. It is
not a safety predictor.

## Product Rules

- Preserve "Shade by day. Light by night." as the presentation catchphrase.
- Keep LightMap as the product name.
- Use LightTime Agent as the agent feature name.
- Do not replace the catchphrase with an internal technical slogan.
- Lead with day shadows in demos, docs, and AI summaries.
- Explain night mode as brightness and open-place activity.
- Do not claim that LightMap certifies a safe route.
- Keep the current presentation surface focused on shade, light, venues,
  weather, and LightTime Agent.

## Current UI Direction

- Daytime actions: `Shadow Time` and `Sunny Time`.
- Nighttime actions: `Active Time` and `Inactive Time`.
- Day colors: blue shadow overlay and blue shadow percentage.
- Night colors: soft warm orange activity treatment and yellow light context.
- Time phase labels: Dawn, Day, Dusk, and Night.
- Dawn and Dusk refer to the app's transition theme periods, not broad clock
  labels.
- The slider starts at the nearest previous hour.
- The night timeline reads continuously from 04:00 to 04:00 the next day.

## Daytime Criteria

The selected-point ring stays small because useful shade is local. The current
implementation samples 13 points inside a 17 m check ring.

- 0 to 1 shaded samples: mostly sunny or open exposure.
- 2 to 10 shaded samples: partial building shade.
- 11 to 13 shaded samples: near-full building shade.

Building shadow checks use a 15 degree solar-altitude cutoff so low-sun shadows
do not overwhelm the local answer.

## Nighttime Criteria

Night activity should remain simple and objective:

```text
activity score = open venue count * 10
```

No `/100` suffix is shown. The score is count-based. Visual brightness is capped
for readability, while streetlight glow remains the separate light context.

This is not measured foot traffic. It is an explainable proxy from public venue
hours and lighting context.

## Performance Focus

The core speed story is the direct-canvas time-slider pass. The old path rebuilt
Leaflet GeoJSON polygons on every slider tick, creating thousands of objects
and making the app feel sluggish.

The current path treats moving shadows as an animation layer:

1. Compute shadow rings.
2. Keep lightweight shadow geometry.
3. Clear one canvas.
4. Fill the rings directly with the 2D canvas API.

Presentation version:

> We did not make the shadow math simpler. We removed the rendering overhead
> between the math and the pixels.

Local browser smoke evidence:

```text
time-slider shadows: 6,921
compute: 10.2 ms
draw: 29.3 ms
render total: 39.6 ms
```

This is not a complete production benchmark. It is evidence that the hot path
moved from Leaflet object reconstruction to direct canvas drawing.

## Next Candidates

- Spatially bucket buildings in the browser so viewport culling does not scan
  the whole building array every tick.
- Add a level-of-detail building ring for lower zooms and keep full geometry
  for high zooms.
- Move shadow computation into a Web Worker if UI thread blocking remains.
- Improve LightTime Agent summaries with compact timeline data.
