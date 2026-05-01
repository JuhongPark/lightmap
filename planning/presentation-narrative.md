# LightMap Presentation Narrative

> Proposal anchor: "Shade by day. Light by night."
> Keep this as the public-facing line. LightTime Agent, shadow checks, and
> activity checks are supporting features, not replacement slogans.

## One-Sentence Version

Shade by day. Light by night. LightMap answers two time-aware questions on one
map: where shade exists during the day, and where light and open-place activity
exist at night.

## Current Framing

The story starts with a simple personal need. On a hot afternoon, the useful
question is not only "how far is it?" It is "which side of the street has
shade right now?"

That personal question can broaden into a public-health point. Shade affects
comfort, heat exposure, walking, running, and how people use streets during hot
days.

At night, the question changes. The app should not claim that a route is safe.
The night story is visibility and activity context: where public streetlights
and currently open venues make the street environment brighter and more active.

## Proposal Alignment

Keep these proposal elements alive:

- Title promise: "Shade by day. Light by night."
- Day question: "Where is shade right now?"
- Night question: "Where is light right now?"
- Evidence pillars: buildings, trees, streetlights, venues, and weather.
- Technology frame: "Two pipelines, one map."
- Risk story: data freshness and interactive speed.

## Current App Truths

- The public artifact is `docs/LightMap.html`.
- LightMap is the product name.
- LightTime Agent is the button-driven helper inside the app.
- Daytime controls are `Shadow Time` and `Sunny Time`.
- Nighttime controls are `Active Time` and `Inactive Time`.
- Day labels use a blue shadow percentage at the selected point.
- Night labels use open-venue activity with warmer orange color.
- The time slider starts at the nearest previous hour.
- Time labels distinguish Dawn, Day, Dusk, and Night.
- The presentation surface is intentionally limited to shade, light, venues,
  weather, and LightTime Agent.

## Professor Rubric Strategy

| Rubric area | What the presentation should make visible |
| --- | --- |
| Video and Presentation | A clear 3 minute 40 second story with a polished live demo and concise voiceover. |
| Complexity and Sophistication | Time-aware shade geometry, night light context, phase-aware UI, and local point analysis. |
| User Interface | The day-night theme shift, readable timeline, point ring, dynamic labels, and LightTime Agent controls. |
| Effort | Public data collection, generated static artifact, iteration on visual tuning, and tested rendering behavior. |
| Design | Well-scoped algorithms: local shade sampling, hourly time model, direct-canvas shadow rendering, and simple venue-count activity scoring. |
| Bonus | The map feels like a real product because the core interaction is visible, branded, and fast. |

## Slide Arc

1. **Title**
   LightMap. Shade by day. Light by night.

2. **Problem**
   Existing maps know distance and direction. They do not answer what the street
   feels like at a specific hour.

3. **Daytime Question**
   Where is shade right now? Buildings, sun position, and tree canopy create a
   moving shade layer.

4. **Nighttime Question**
   Where is light and open-place activity right now? Streetlights and open
   venues create a nighttime context layer.

5. **Two Pipelines, One Map**
   Public data becomes a static web artifact: Python preprocessing, geometry
   computation, Folium and Leaflet, browser-side time interaction.

6. **LightTime Agent**
   The agent is a set of direct actions: Shadow Time, Sunny Time, Active Time,
   and Inactive Time.

7. **Decision Criteria**
   Day uses local shadow coverage. Night uses open venue count and light
   context. Both are intentionally simple and explainable.

8. **Performance**
   The first version rebuilt thousands of map objects. The current version
   paints moving shadows directly onto one canvas.

9. **Risks And Limits**
   Hourly time resolution, public-data freshness, and proxy-based venue
   activity are stated clearly.

10. **Close**
   LightMap turns public city data into a time-aware view of shade by day and
   light by night.

## Demo Script

1. Open `docs/LightMap.html` and start with the catchphrase.
2. Show the current time loading to the nearest previous hour.
3. Drag the slider through day hours and show blue shadows moving.
4. Click a point and show the dynamic shadow percentage label.
5. Use `Shadow Time` and `Sunny Time` to show the LightTime Agent answer.
6. Move into Dusk and Night so the theme transition is visible.
7. Show streetlights and open venues becoming the night evidence.
8. Use `Active Time` and `Inactive Time` to show the night agent answer.
9. Mention that activity is based on open venue count and light context, not
   measured foot traffic.
10. Close by returning to the line: "Shade by day. Light by night."

## Daytime Criteria

A clicked point is treated as a small local area, not a whole block. The map
samples 13 points inside a 17 m check ring around the selected point and counts
how many samples fall inside computed building shadows.

- 0 to 1 shaded samples: mostly sunny or open exposure.
- 2 to 10 shaded samples: partial building shade.
- 11 to 13 shaded samples: near-full building shade.

The blue label reports the current slider time and the percent of the check
ring covered by building shadow, such as "Shadow here: 62% at 14:00". Shadows
are evaluated when the sun is at least 15 degrees above the horizon. Below that
angle, projected shadows become very long and stop being a clean local answer
for a small point.

## Nighttime Criteria

Night activity is intentionally simple:

```text
activity score = open venue count * 10
```

The label does not use `/100` because the score is a count-based signal, not a
percentage. Visual brightness is capped so the map remains readable. Streetlight
density stays visible as light context, while open venues make the selected
area feel more active.

This is not measured foot traffic. It is an explainable proxy from public venue
hours and lighting context.

## Performance Story

Use this exact framing:

> We did not make the shadow math simpler. We removed the rendering overhead
> between the math and the pixels.

Before:

- Compute shadow geometry.
- Wrap every shadow in GeoJSON.
- Ask Leaflet to rebuild thousands of polygon objects.
- Then draw.

After:

- Compute shadow geometry.
- Keep lightweight shadow rings.
- Paint the rings directly onto one canvas.

Numbers to cite:

| Metric | Value |
| --- | ---: |
| Visible shadows in smoke run | 6,921 |
| Shadow compute | 10.2 ms |
| Canvas draw | 29.3 ms |
| Total redraw | 39.6 ms |

Phrase carefully: this is a local Playwright smoke result, not a full best-of-N
benchmark. The strong claim is architectural: object rebuild left the hot path.

## Language To Use

- "Shade by day. Light by night."
- "Where is shade right now?"
- "Where is light right now?"
- "Two pipelines, one map."
- "Brightness and visibility context."
- "Open-place activity from public venue hours."
- "LightTime Agent."
- "Shadow Time."
- "Sunny Time."
- "Active Time."
- "Inactive Time."
- "The slider feels live because moving shadows are painted as an animation layer."

## Language To Avoid

- "This route is safe."
- "Crime prediction."
- "AI safety score."
- "The map tells you where you should walk."
- "Night mode proves an area is safe."
- Any internal technical slogan that competes with the proposal catchphrase.

## Q&A Anchors

**How does the shadow engine fit the proposal slogan?**
The public slogan stays "Shade by day. Light by night." The shadow engine is
the technical work that makes the shade part real and interactive.

**Why is the daytime ring small?**
Useful shade is local. A huge circle can say "some shade exists nearby" even
when the selected spot is exposed. The smaller ring keeps the answer tied to
the place the user clicked.

**Why use 15 degrees as the shadow cutoff?**
Below 15 degrees, building shadows become long and merge into broad bands. That
is visually dramatic, but weak for answering whether this small clicked spot has
usable local shade.

**How does night activity work?**
The app counts currently open venues near the selected area and multiplies by
10. Streetlights provide light context. The result is a simple visibility and
activity proxy, not a foot-traffic measurement.

**Where does AI fit?**
AI should not invent new facts. LightTime Agent explains and highlights the
computed map state through direct buttons.
