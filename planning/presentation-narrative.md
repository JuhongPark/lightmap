# LightMap Presentation Narrative

> Proposal anchor: "Shade by day. Light by night."
> Use this as the public-facing line. Keep shadow and light pipeline names as
> implementation details, not as a replacement slogan.

## One-Sentence Version

Shade by day. Light by night. LightMap answers the proposal's two questions:
where is shade right now, and where is light right now?

## Short Version

The public-facing story should preserve the proposal hierarchy. The slogan is
"Shade by day. Light by night." Under that slogan, the day pipeline computes
shade from sun position, building heights, and tree canopy. The night pipeline
shows light through streetlight density and open venues, making the environment
brighter and more visible after dark.

The opening should use a simple personal hook: on a hot afternoon, you want the
shaded side of the street. Then broaden the meaning. Shade is not only comfort.
It is also a public-health layer during heat, especially when paired with live
weather, cooling options, and 24-hour emergency resources.

Historic incident records are available only as an optional reference layer.
They are not the claim. The claim is time-aware urban shade and visibility
through two public-data pipelines.

## Proposal Alignment

Keep these proposal elements alive:

- Title promise: "Shade by day. Light by night."
- Two questions: "Where is shade right now?" and "Where is light right now?"
- Evidence pillars: 122K buildings, 80K streetlights, 138K trees, and 3K stores.
- Technology phrase: "Two pipelines, one map."
- Risk story: data freshness and speed are explicit risks. The latest speed
  pass gives a strong mitigation story for the time slider.

## Slide Arc

1. **Title**
   LightMap Boston & Cambridge. Shade by day. Light by night.

2. **Problem**
   On a hot afternoon, the useful question is simple: where is shade right now?
   After dark, the question changes: where is light right now?

3. **Why It Matters**
   Shade starts as a comfort question for walking and running, then becomes a
   public-health question during heat. Light supports nighttime visibility,
   especially for people who do not already know the area. The data is public
   but scattered.

4. **Two Pipelines, One Map**
   Daytime shadow pipeline: sun position, building footprints, heights, and
   tree canopy. Nighttime brightness pipeline: streetlights and open venues.

5. **Daytime Shade**
   Building footprints plus heights become moving shadows. The user scrubs time
   and sees the city change as the sun moves. This is the first demo moment.

6. **Daytime Shade Decision Criteria**
   A clicked point is treated as a small local area, not a whole block. The map
   samples 13 points inside a 17 m ring around the selected point and reports
   how many of those samples fall inside computed building shadows.

   - 0-1 shaded samples: sun or open exposure at the selected spot.
   - 2-10 shaded samples: partial building shade.
   - 11-13 shaded samples: near-full building shade.

   The blue label reports the current slider time and the percent of the check
   ring covered by building shadow, such as "Shadow here: 62% at 14:00".
   Shadows are only evaluated when the sun is at least 15 degrees above the
   horizon, because lower sun angles create very long projected shadows that
   stop being locally meaningful.

7. **Nighttime Light**
   Streetlight density becomes a brightness layer. Open venues add activity
   context through OSM `opening_hours`. This is the second demo moment.

8. **Optional Incident Reference**
   Historic incident records can be toggled on, but they are not used to label
   a route safe or unsafe. They support context only when explicitly requested.

9. **Risk And Mitigation**
   Data freshness is handled by showing source years. Speed is now handled by
   drawing moving shadows directly onto one canvas instead of rebuilding
   thousands of map features per tick.

10. **What This Enables Next**
   Once the map feels live, an AI layer can answer evidence-bound questions:
   explain this view, where is shade good near me right now, and where is it
   bright after dark?

## Demo Script

1. Open with the proposal line: "Shade by day. Light by night."
2. Start with the hot-afternoon hook: "Where is shade right now?"
3. Drag the slider slowly and show building shadows moving.
4. Click a point and show the blue shadow label changing with the slider.
5. Explain that the point check samples a 17 m ring and reports the percent
   covered by building shadow.
6. Point out tree canopy as static shade context.
7. Mention the public-health angle: heat, cooling options, and 24-hour ER
   markers.
8. Continue into evening and show the basemap darken.
9. Let the streetlight heatmap become the main night layer.
10. Show time-aware venue dots turning on.
11. Frame the AI next step: explain this view, find shade near me, and show
   bright nearby streets at night.
12. Toggle incidents only briefly, framing them as optional historic reference.
13. Return to the slider speed: "This now feels live because the moving shadow
   layer is drawn directly to canvas."

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
- Keep lightweight `[height, ring]` tuples.
- Paint the rings directly onto one canvas.

Numbers to cite:

| Metric | Value |
| --- | ---: |
| Visible shadows in smoke run | 6,921 |
| Shadow compute | 10.2 ms |
| Canvas draw | 29.3 ms |
| Total redraw | 39.6 ms |

Phrase carefully: this is a local Playwright smoke result, not a best-of-N
benchmark. The strong claim is architectural: object rebuild left the hot path.

## Language To Use

- "Shade by day. Light by night."
- "Where is shade right now? Where is light right now?"
- "Two pipelines, one map."
- "Brightness and visibility context."
- "Historic incident reference."
- "The slider feels live because shadows are painted as an animation layer."
- "AI can explain the computed map state."
- "Explain this view."
- "Where is shade good near me right now?"
- "Where is it bright near me tonight?"

## Language To Avoid

- "This route is safe."
- "Crime prediction."
- "AI safety score."
- "The map tells you where you should walk."
- "Night mode is mainly a crime map."
- Any internal technical slogan that competes with the proposal catchphrase.

## Q&A Anchors

**How does the shadow engine fit the proposal slogan?**
The public slogan stays "Shade by day. Light by night." The shadow engine is
the technical work that makes the shade part real and interactive.

**What about the proposal's safety motivation?**
Keep it as a motivation around nighttime visibility and unfamiliar streets, not
as a prediction claim. The demo should show light first. Historic incidents are
optional context only.

**Why not make incidents the main layer?**
Historic incidents are useful context, but they are not live conditions and
should not be treated as a safety guarantee. LightMap's main night claim is
lighting visibility.

**Why did performance improve so much?**
The old version rebuilt map features. The new version treats moving shadows as
a canvas animation layer.

**How does the map decide whether a clicked point is shaded?**
The clicked point is treated as a 17 m local check ring. The map samples 13
locations inside that ring and counts how many are inside building-shadow
polygons at the selected hour. Zero or one shaded sample means sun or open
exposure. Two to ten means partial shade. Eleven to thirteen means near-full
shade. The blue label shows the current hour and shadow coverage percentage.

**Why hide building-shadow checks below 15 degrees solar altitude?**
Below 15 degrees, projected building shadows become very long and start merging
into broad walls. That is visually dramatic but weak for answering whether this
small clicked spot has useful local shade. Sunrise and sunset labels still show
the true solar events.

**Where does AI fit?**
AI should not invent new facts. It should explain the map's computed evidence:
shade, heat, light, location context, and visible data limits. It can recommend
nearby shaded or brighter areas only from those computed layers.
