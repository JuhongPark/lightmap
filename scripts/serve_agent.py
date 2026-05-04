"""Local LightMap static server with an OpenAI-backed agent endpoint.

The browser never receives an API key. It posts the current map state to
`/api/agent`, and this local server forwards that compact context to the
Responses API.

Usage
-----
    .venv/bin/python scripts/serve_agent.py 8765 docs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import Any

from serve import DEFAULT_PORT, DEFAULT_ROOT, REPO_ROOT, GzipStaticHandler


DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_REASONING_EFFORT = "medium"
MAX_REQUEST_BYTES = 128 * 1024


DEVELOPER_PROMPT = """You are LightMap Agent for a city map.

Answer from the supplied map context only. You may reason about which nearby
areas appear more shaded or brighter, but do not invent street names, live
conditions, or facts that are not present in context.

Project framing:
- Public slogan: Shade by day. Light by night.
- Day answers explain shade from sun position, building shadows, tree canopy,
  weather, and heat state.
- Night answers explain brightness from streetlight visibility context and
  open venues.
- Historic incidents are optional reference context only.

Safety rules:
- Never claim a route or place is safe.
- Never make crime predictions or personal risk scores.
- If context is insufficient, say what is missing and suggest the next map view.

Return compact JSON only:
{
  "answer": "short user-facing explanation",
  "highlight": {
    "candidateId": "one supplied candidate id, if available",
    "label": "short map label"
  }
}

When candidates are supplied, choose exactly one candidate. Do not invent
coordinates. If no candidate is adequate, choose the viewport candidate.
"""


def _load_env_file(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _json_response(handler: GzipStaticHandler, status: HTTPStatus,
                   payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _extract_text(response: dict[str, Any]) -> str:
    text = response.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    parts: list[str] = []
    for item in response.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content, dict):
                value = content.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(parts).strip()


def _candidate_highlight(context: dict[str, Any],
                         candidate_id: str | None = None,
                         label: str | None = None) -> dict[str, Any] | None:
    candidates = context.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        viewport = context.get("viewport")
        if isinstance(viewport, dict):
            center = viewport.get("center")
            if isinstance(center, dict):
                lat = center.get("lat")
                lon = center.get("lon")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    return {
                        "lat": lat, "lon": lon, "radiusM": 180,
                        "label": label or "Current view",
                        "kind": context.get("action") or "view",
                    }
        return None

    picked = None
    if candidate_id:
        for c in candidates:
            if isinstance(c, dict) and str(c.get("id")) == str(candidate_id):
                picked = c
                break
    if picked is None:
        picked = max(
            (c for c in candidates if isinstance(c, dict)),
            key=lambda c: float(c.get("score") or 0),
            default=None,
        )
    if picked is None:
        return None

    lat = picked.get("lat")
    lon = picked.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    return {
        "lat": lat,
        "lon": lon,
        "radiusM": picked.get("radiusM") or 180,
        "label": label or picked.get("label") or picked.get("evidence")
        or "Recommended area",
        "kind": picked.get("kind") or context.get("action") or "view",
        "candidateId": picked.get("id"),
    }


def _coerce_agent_result(text: str, context: dict[str, Any]) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return {
            "answer": text,
            "highlight": _candidate_highlight(context),
            "source": "openai",
        }
    if not isinstance(data, dict):
        return {
            "answer": text,
            "highlight": _candidate_highlight(context),
            "source": "openai",
        }
    answer = str(data.get("answer") or text).strip()
    h = data.get("highlight")
    candidate_id = None
    label = None
    if isinstance(h, dict):
        candidate_id = h.get("candidateId")
        label = h.get("label")
    return {
        "answer": answer,
        "highlight": _candidate_highlight(context, candidate_id, label),
        "source": "openai",
    }


def _call_openai(question: str, context: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OpenAI API key is not configured. Add OPENAI_API_KEY to .env "
            "and restart scripts/serve_agent.py."
        )

    model = os.environ.get("LIGHTMAP_OPENAI_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("LIGHTMAP_REASONING_EFFORT",
                            DEFAULT_REASONING_EFFORT)
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": DEVELOPER_PROMPT}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps({
                            "question": question,
                            "map_context": context,
                        }, ensure_ascii=True),
                    }
                ],
            },
        ],
        "max_output_tokens": 700,
    }
    if effort and effort != "none":
        payload["reasoning"] = {"effort": effort}

    req = urllib.request.Request(
        base_url.rstrip("/") + "/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 429 or "insufficient_quota" in detail:
            return _local_demo_agent(question, context)
        raise RuntimeError(f"OpenAI request failed: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI request failed: {exc.reason}")

    data = json.loads(raw.decode("utf-8"))
    answer = _extract_text(data)
    if not answer:
        raise RuntimeError("OpenAI returned no text output.")
    return _coerce_agent_result(answer, context)


def _mode_label(context: dict[str, Any]) -> str:
    mode = str(context.get("mode") or "").lower()
    if mode == "night":
        return "night"
    if mode == "twilight":
        return "twilight"
    return "day"


def _location_label(context: dict[str, Any]) -> str:
    loc = context.get("location")
    if not isinstance(loc, dict):
        return "No current location or dropped pin is available."
    lat = loc.get("lat")
    lon = loc.get("lon")
    source = loc.get("source") or "location"
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return f"Using {source} at {lat:.5f}, {lon:.5f}."
    return "A location was provided, but its coordinates were incomplete."


def _weather_label(context: dict[str, Any]) -> str:
    heat = context.get("heat")
    if not isinstance(heat, dict):
        return "Weather context is unavailable."
    weather = str(heat.get("weather") or "weather unavailable").strip()
    active = bool(heat.get("active"))
    if active:
        return f"Heat badge is active. Weather strip: {weather}."
    return f"Heat badge is not active. Weather strip: {weather}."


def _visible_label(context: dict[str, Any]) -> str:
    visible = context.get("visible")
    counts = context.get("counts")
    if not isinstance(visible, dict):
        visible = {}
    if not isinstance(counts, dict):
        counts = {}
    parts = [
        f"{visible.get('shadowCount', 0)} visible shadow shapes",
        f"{counts.get('streetlights', 0)} streetlights in scope",
        f"{counts.get('venues', 0)} venues in scope",
        f"{counts.get('coolingOptions', 0)} cooling options in scope",
        f"{counts.get('emergencyRooms', 0)} 24h ER markers in scope",
    ]
    return ", ".join(parts) + "."


def _local_demo_agent(question: str, context: dict[str, Any]) -> dict[str, Any]:
    """No-key fallback so the UI works during local demos.

    This is intentionally conservative. It does not invent street names or
    compute a route. It explains how to use the current map evidence and tells
    the user what to change next.
    """
    q = question.lower()
    action = str(context.get("action") or "").lower()
    mode = _mode_label(context)
    where = _location_label(context)
    weather = _weather_label(context)
    visible = _visible_label(context)
    highlight = _candidate_highlight(context)
    label = (highlight or {}).get("label") or "selected area"

    if action == "bright" or "bright" in q or "light" in q or "night" in q:
        answer = (
            f"Highlighted {label}. This area ranks best in the current view "
            "from streetlight density and open venue context. It is a "
            "visibility cue, not a safe-route claim.\n\n"
            f"Evidence used: Mode {mode}. {where} {visible}"
        )
    elif action == "shade" or "shade" in q or "shadow" in q or "shaded" in q:
        answer = (
            f"Highlighted {label}. This area ranks best in the current view "
            "from the computed building-shadow candidates. Move the slider to "
            "compare whether the shade improves later.\n\n"
            f"Evidence used: Mode {mode}. {where} {weather} {visible}"
        )
    else:
        answer = (
            f"Highlighted {label}. The current view combines time-based "
            "building shadows, static tree canopy, weather and heat state, "
            "streetlight density, open venues, and optional historic incident "
            f"reference.\n\nEvidence used: Mode {mode}. {where} {weather} "
            f"{visible}"
        )

    return {"answer": answer, "highlight": highlight, "source": "local-fallback"}


class AgentHandler(GzipStaticHandler):
    def do_GET(self) -> None:
        if self.path == "/api/agent/health":
            _json_response(self, HTTPStatus.OK, {
                "ok": True,
                "openaiConfigured": bool(os.environ.get("OPENAI_API_KEY")),
                "mode": "openai" if os.environ.get("OPENAI_API_KEY")
                else "setup-required",
                "model": os.environ.get("LIGHTMAP_OPENAI_MODEL",
                                        DEFAULT_MODEL),
                "reasoningEffort": os.environ.get(
                    "LIGHTMAP_REASONING_EFFORT", DEFAULT_REASONING_EFFORT
                ),
            })
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/agent":
            _json_response(self, HTTPStatus.NOT_FOUND,
                           {"error": "Unknown endpoint."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            _json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "error": "Agent request is empty or too large."
            })
            return

        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _json_response(self, HTTPStatus.BAD_REQUEST,
                           {"error": "Agent request must be JSON."})
            return

        question = str(payload.get("question") or "").strip()
        context = payload.get("context")
        if not question:
            _json_response(self, HTTPStatus.BAD_REQUEST,
                           {"error": "Question is required."})
            return
        if not isinstance(context, dict):
            _json_response(self, HTTPStatus.BAD_REQUEST,
                           {"error": "Map context is required."})
            return

        try:
            result = _call_openai(question, context)
        except RuntimeError as exc:
            _json_response(self, HTTPStatus.SERVICE_UNAVAILABLE,
                           {"error": str(exc)})
            return

        _json_response(self, HTTPStatus.OK, result)


def run(port: int, root: str) -> None:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"error: {root} is not a directory", file=sys.stderr)
        sys.exit(2)
    os.chdir(root)
    server = ThreadingHTTPServer(("", port), AgentHandler)
    print(f"serving {root} on http://localhost:{port}/ (agent + gzip)")
    print("agent health: /api/agent/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutdown")
    finally:
        server.server_close()


def main() -> int:
    _load_env_file(os.path.join(REPO_ROOT, ".env"))

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    parser.add_argument("--port", dest="port_kw", type=int, default=None)
    parser.add_argument("--root", dest="root_kw", default=None)
    args = parser.parse_args()

    port = args.port_kw if args.port_kw is not None else args.port
    root = args.root_kw if args.root_kw is not None else args.root
    run(port, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
