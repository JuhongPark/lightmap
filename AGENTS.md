# LightMap Agent Notes

## Product Narrative

LightMap is primarily a daytime ShadowMap and secondarily a nighttime LightMap.
Keep the story anchored in:

- Day: where shade falls as the sun moves.
- Night: where streetlights make routes more visible.
- Optional context: historic incident records are reference overlays only.

Do not frame the app as a safety predictor or as a map that can certify a route
as safe. Use language such as "brightness", "visibility", "lighting context",
and "historic incident reference". Avoid "safe route" claims.

## Current Technical Direction

- Local development matters more than GitHub Pages size for the next phase.
- Performance is the top priority before adding AI features.
- The time-slider should keep daytime shadow rendering as the first-class
experience, with night lighting as the paired second mode.
- Tree canopy is a static shade overlay (`docs/trees_canopy.png`) in the
time-slider, not a per-tick projected shadow layer.

## AI Roadmap Guardrails

AI should explain and summarize LightMap's computed data; it should not invent
risk scores or make personal safety guarantees. Future AI features should answer
questions from the current map state, cite the visible data layer used, and state
data limits when incident history is involved.

## Useful Commands

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_shadow tests.test_loaders
PYTHONPATH=src .venv/bin/python src/prototype.py --time-slider --out prototype_timeslider.html --scale 1
PYTHONPATH=src .venv/bin/python src/prototype.py --time-slider --out prototype_timeslider.html --scale 100
```

The full unittest discovery includes a Playwright/local-socket render smoke test,
which may need a less restricted sandbox.
