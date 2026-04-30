# LightMap Narrative Evaluation

> Working critique for feedback alignment. This evaluates the current narrative
> against the submitted proposal, recent user direction, and the current working
> prototype.

## Bottom Line

The current narrative is now directionally correct, but it is still too
technical in places. The best public-facing center is the original proposal
line:

> Shade by day. Light by night.

Everything else should support that line. The shadow engine, light layer,
incident toggle, speed pass, and future AI work are evidence that the slogan is
real, not replacement slogans.

## Evidence Base

### Proposal Evidence

The proposal's strongest language is simple and user-centered:

- Title: "LightMap Boston & Cambridge"
- Catchphrase: "Shade by day. Light by night."
- Problem: "Two questions no map can answer today"
- Day question: "Where is shade right now?"
- Night question: "Where is light right now?"
- Day user need: "The sun is intense. You want the shaded side."
- Night user need: "Some streets are bright, others dark."
- Technology frame: "Two pipelines, one map"
- Risk frame: data freshness and speed
- Next steps: shadow engine, nighttime light, trees + weather, deploy

### User Direction Evidence

Recent direction has narrowed the story:

- Keep the original catchphrase because it got a good reaction.
- Do not replace it with a technical slogan.
- The core is daytime shadow first, then nighttime light.
- Night should focus on bright, visible places.
- Incident history should be optional reference only.
- Do not center comparisons with other maps yet.
- GitHub Pages is not the current priority. Local growth, speed, AI, and
  narrative matter more now.
- Use a hot-afternoon personal hook, then broaden to the public-health meaning
  of shade during heat.
- The AI scope should be practical and map-bound: explain this view, find shade
  near me, and identify brighter nearby streets at night.

### Prototype Evidence

The current prototype supports the narrative with concrete artifacts:

- Day: building shadows move with the time slider.
- Shade context: tree canopy is a static shade overlay.
- Night: streetlight heatmap and time-aware venue dots show brighter activity
  corridors.
- Optional context: incidents are behind a toggle, not the default night story.
- Speed: local Playwright smoke showed roughly 40 ms redraw for about 6.9K
  visible shadows after the direct-canvas pass.

## Evaluation

| Criterion | Score | Assessment |
| --- | ---: | --- |
| Catchphrase fidelity | 9/10 | The docs now protect "Shade by day. Light by night." as the only public line. Remaining risk: internal technical terms can still leak into live explanation if not disciplined. |
| Proposal alignment | 8/10 | The two questions, data pillars, two-pipeline frame, and risk story are restored. The current wording is more polished than the proposal, but slightly less vivid. |
| Day-first structure | 8/10 | The demo starts with moving daytime shadows, which matches the project core. The narrative should keep the day demo as the first "wow" moment. |
| Night-light framing | 8/10 | Night is now brightness/visibility first. The safety motivation can stay, but only as "why visibility matters", not as a safety claim. |
| Incident handling | 9/10 | Optional historic reference is the right position. It should not appear early in the demo unless asked. |
| Speed story | 9/10 | The speed improvement has a strong technical explanation and memorable line. Need to keep numbers labeled as local smoke, not benchmark. |
| AI direction | 8.5/10 | The AI story is now presentation-ready: explain the current view, find nearby shade, and find brighter nearby streets at night. Remaining risk is implementation scope. |
| Presentation energy | 8/10 | The hot-afternoon hook restores the proposal's plain-life language, and the public-health turn gives the project broader significance without changing the core claim. |

## Current Strengths

1. The project center is clearer: shade and light, not crime.
2. The original catchphrase is protected.
3. The speed breakthrough turns a risk slide into a credible accomplishment.
4. AI is constrained to explanation, which fits the project better than making
   predictive claims.

## Current Weaknesses

1. The narrative can become too technical too soon.
2. The night story is still emotionally weaker than the day story.
3. The next risk is implementation scope. The first AI demo must stay narrow
   enough to build from deterministic map summaries.
4. The night story still needs a crisp demo moment that avoids sounding like a
   safety predictor.

## Recommended Recenter

Use this hierarchy:

1. **Catchphrase**
   Shade by day. Light by night.

2. **Human Need**
   In the day, you want the shaded side. At night, you want to know which
   streets are bright.

3. **Public Data**
   Buildings, trees, streetlights, venues, weather.

4. **Working Map**
   Time slider shows the city changing across the day and night.

5. **Credibility**
   The speed bottleneck was real. The direct-canvas pass made the demo feel
   live.

6. **AI Next**
   AI does not decide what is safe. It explains the visible evidence, finds
   nearby shade from the current location, and identifies brighter nearby
   streets at night.

## Better Presentation Spine

```text
Shade by day. Light by night.

On a hot afternoon, the useful question is simple:
Where is shade right now?

That is a comfort question, but it is also a public-health question during
extreme heat.

After dark, the useful question changes:
Where is light right now?

LightMap uses public city data to answer both questions on one time-aware map:
buildings and trees for shade, streetlights and open venues for light.

The hardest part was making the map feel live.
The old version rebuilt thousands of map objects every time the slider moved.
The new version computes the same shadows and paints them directly to canvas.

That makes the next step possible:
an AI assistant that explains the current view, finds stronger shade near the
user, and identifies brighter nearby streets at night without inventing safety
claims.
```

## Decisions

1. Open with the personal hot-afternoon shade hook, then broaden to walking,
   running, and public health during heat.
2. Keep safety motivation mostly in Q&A. In the main talk, use nighttime
   brightness and visibility language.
3. Present AI as the next local build, focused on Explain This View, Find Shade
   Near Me, and Where Is It Bright At Night.
4. Keep performance as a proof point inside the risk and mitigation slide unless
   the presentation audience is technical.
