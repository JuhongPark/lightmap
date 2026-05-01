# LightTime Agent Development Plan

> Current direction: LightTime Agent should explain and highlight LightMap's
> computed state through direct buttons in the primary demo.

## Principle

AI should be an interpreter of LightMap, not the source of truth.

The map computes:

- Sun position.
- Building shadows.
- Tree shade context.
- Streetlight density.
- Open venue context.
- Weather and phase labels.
- Selected-point summaries.

LightTime Agent uses those computed layers to answer constrained questions. It
should not make route guarantees, invent risk scores, or imply that an area is
safe.

## Current Product Shape

LightMap is the product. LightTime Agent is the helper layer.

The agent should be triggered by buttons:

- `Shadow Time`
- `Sunny Time`
- `Active Time`
- `Inactive Time`

The user should not need to type a question. A selected point plus current map
state is enough context.

## Daytime Agent

### Shadow Time

Input:

- Clicked point.
- Selected date.
- Hourly shadow state.
- Local check ring.

Output:

- Highlight the hours when building shade reaches the selected point.
- Show a compact map region around the selected point.
- Show the strongest shaded hour and current shadow percentage.

### Sunny Time

Input:

- Same clicked point and hourly shadow state.

Output:

- Highlight the hours when the selected point is mostly sunny.
- Show the current sunny or shadow percentage on the timeline.
- Keep tree canopy as separate visual context.

### Daytime Decision Rule

The selected point uses a small local ring. The current implementation samples
13 points inside a 17 m check ring.

- 0 to 1 shaded samples: mostly sunny or open exposure.
- 2 to 10 shaded samples: partial building shade.
- 11 to 13 shaded samples: near-full building shade.

Building shadows are evaluated when solar altitude is at least 15 degrees. This
keeps the answer local and avoids very long low-sun shadows overwhelming the
selected point.

## Nighttime Agent

### Active Time

Input:

- Clicked point.
- Selected date.
- Hourly venue opening state.
- Streetlight context.

Output:

- Highlight the hours when the nearby area has open-place activity.
- Use a warm orange visual treatment.
- Show the open venue count and activity score.

### Inactive Time

Input:

- Same clicked point and hourly venue opening state.

Output:

- Highlight the hours when nearby open-place activity is zero or low.
- Keep the streetlight layer visible so the user still sees light context.
- Avoid language that makes activity sound like measured crowd data.

### Nighttime Decision Rule

Night activity is intentionally simple:

```text
activity score = open venue count * 10
```

No `/100` suffix should be shown. The score is count-based, not a percentage.
Visual brightness can be capped so a few venues do not wash out the map.

Streetlight density is light context. Open venue count is activity context.
Together they support the phrase "Light by night" without claiming measured
foot traffic.

## Time Model

The app works in hourly states. On load, the selected time should snap to the
nearest previous hour.

Example:

- Current time 14:40.
- Initial slider hour 14:00.

The night slider should read as a continuous day from 04:00 to 04:00 the next
day, so late-night hours appear after 23:00 instead of wrapping awkwardly.

## Future Local Architecture

For a richer local-first version, use compact summaries instead of sending raw
geometry to an AI model:

```text
Browser map
  |
  | selected point, date, hour, viewport, active mode
  v
Local summary endpoint
  |-- shade timeline
  |-- sun timeline
  |-- light context
  |-- venue activity timeline
  v
LightTime Agent response
```

The browser should send summaries such as shade percentages, open venue counts,
and streetlight density near the selected point.

## Data Summaries To Build Next

1. **Shade timeline**
   Hourly shadow coverage for the selected point.

2. **Sunny timeline**
   Complement of building shadow coverage, with tree canopy shown separately.

3. **Light context**
   Nearby streetlight density and visible light intensity.

4. **Venue activity timeline**
   Count of open venues per hour near the selected point.

5. **Phase summary**
   Dawn, Day, Dusk, and Night labels for the selected date and hour.

## Guardrails

- Never claim a route is safe.
- Never convert light or venue data into a safety score.
- Always distinguish open-place activity from measured foot traffic.
- Always state hourly resolution when timing precision matters.
- If data is missing for a viewport, say the map has no reliable coverage
  there.
- Prefer "brighter", "more visible", "more shaded", "less shaded", "active",
  and "inactive".

## Presentation Version

Use this short wording:

> LightTime Agent is button-driven. The user clicks a point, then asks four
> direct timing questions: when it is shaded, when it is sunny, when the area is
> active, and when it becomes inactive. The agent reads LightMap's computed
> shadows, streetlights, and open venue hours. It explains the map. It does not
> invent a safety score.
