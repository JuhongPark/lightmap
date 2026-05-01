# LightMap Narrative Evaluation

> Working critique for feedback alignment. This evaluates the current narrative
> against the submitted proposal, recent user direction, the professor rubric,
> and the current LightMap artifact.

## Bottom Line

The strongest public center remains:

> Shade by day. Light by night.

Everything else should prove that line. The moving shadow layer, night light
layer, LightTime Agent buttons, and direct-canvas performance work are evidence
that the slogan is real.

## Evidence Base

### Proposal Evidence

The proposal's strongest language is simple and user-centered:

- Title: "LightMap Boston & Cambridge"
- Catchphrase: "Shade by day. Light by night."
- Problem: "Two questions no map can answer today"
- Day question: "Where is shade right now?"
- Night question: "Where is light right now?"
- Technology frame: "Two pipelines, one map"
- Risk frame: data freshness and speed

### User Direction Evidence

Recent direction has narrowed the story:

- Keep the original catchphrase because it got a good reaction.
- Use LightMap as the product name.
- Use LightTime Agent as the agent name.
- Lead with daytime shadow first.
- Make the day-night theme transition visible but not harsh.
- Keep UI labels simple: Shade Time, Sunny Time, Active Time, Inactive Time.
- Keep the demo focused on the current day-night controls.
- Night should use objective inputs: streetlights and currently open venues.
- The night activity score should be simple and explainable.

### Current Artifact Evidence

The current artifact supports the narrative with concrete behavior:

- Day: building shadows move with the time slider.
- Day point check: a small local ring reports shadow coverage percentage.
- Shade context: tree canopy is shown separately.
- Night: streetlight glow and time-aware venue dots show light context.
- Night point check: open venues drive a count-based activity score.
- Time: the slider starts from the nearest previous hourly tick.
- Theme: Dawn, Day, Dusk, and Night have distinct visual states.
- Agent: button-driven LightTime Agent actions update map overlays.
- Performance: a local smoke check observed roughly 40 ms redraw for about 6.9K
  visible shadows after the direct-canvas pass.

## Evaluation

| Criterion | Score | Assessment |
| --- | ---: | --- |
| Catchphrase fidelity | 9/10 | The public line is stable and still matches the original proposal. |
| Proposal alignment | 8/10 | The two questions, two-pipeline frame, and risk story remain intact. |
| Day-first structure | 9/10 | The best demo moment is moving blue shadows and a point-level shade percentage. |
| Night-light framing | 7.5/10 | The night story is now more objective, but it is less emotionally vivid than the day story. Keep it simple and avoid overclaiming. |
| UI clarity | 8.5/10 | The renamed controls and phase labels are easier to present than typed prompts. |
| Speed story | 9/10 | The direct-canvas change gives a strong Design and Effort proof point. |
| Rubric fit | 8.5/10 | The current story can hit all criteria if the video shows the UI first and explains code second. |

## Current Strengths

1. The project center is clear: shade and light.
2. The app has a memorable live interaction, not only a static map.
3. The daytime model has an intuitive visual and quantitative answer.
4. The night model now uses objective public inputs.
5. The speed work gives the project a credible engineering story.

## Current Weaknesses

1. The night story is still weaker than the day story.
2. The presentation can become too technical if it leads with data sources.
3. The activity score must be framed as a proxy, not measured foot traffic.
4. The professor rubric puts 40 points on video quality, so the demo has to be
   polished and concise.

## Recommended Recenter

Use this hierarchy:

1. **Catchphrase**
   Shade by day. Light by night.

2. **Human Need**
   In the day, you want the shaded side. At night, you want visible, active
   streets.

3. **Public Data**
   Buildings, trees, streetlights, venues, and weather.

4. **Working Map**
   A time slider shows the city changing across day, twilight, and night.

5. **Credibility**
   The map feels live because moving shadows are painted directly onto a canvas.

6. **LightTime Agent**
   The agent is a button layer that answers time questions from computed map
   evidence.

## Better Presentation Spine

```text
Shade by day. Light by night.

Most maps answer where a place is. LightMap answers what the street is like at
this hour.

During the day, the question is shade. LightMap combines sun position, building
height, and tree canopy to show moving shadow across the city.

At night, the question is light. LightMap combines streetlights and currently
open venues to show brighter, more active areas.

The hardest part was making the map feel live. The first version rebuilt
thousands of map objects every time the slider moved. The current version
computes the same shadows and paints them directly to one canvas.

LightTime Agent turns that computed map state into direct answers: when this
point is shaded, when it is sunny, when this area is active, and when it becomes
inactive.
```

## Decisions

1. Open with the hot-afternoon shade need, then broaden to public-health value.
2. Keep nighttime language around brightness, visibility, and activity.
3. Present LightTime Agent as direct action buttons.
4. Use the shadow percentage label as the main daytime proof point.
5. Use open venue count and streetlight glow as the main nighttime proof point.
6. Keep performance as the main Design and Effort proof point.
