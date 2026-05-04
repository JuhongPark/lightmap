"""City profile loading for LightMap.

City profiles keep public data paths and map framing out of the renderer.
The default profile preserves the current Boston and Cambridge build.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CITIES_DIR = os.path.join(REPO_ROOT, "cities")
DEFAULT_CITY_ID = "boston-cambridge"


@dataclass(frozen=True)
class CityProfile:
    id: str
    name: str
    display_name: str
    timezone: str
    center: tuple[float, float]
    bbox: tuple[float, float, float, float]
    paths: dict[str, str]
    building_sources: tuple[dict[str, Any], ...]
    streetlight_sources: tuple[dict[str, Any], ...]
    source_notes: dict[str, str]


def _resolve_path(path: str | None) -> str | None:
    if not path:
        return None
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(REPO_ROOT, path))


def _resolve_source_paths(sources: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    out = []
    for source in sources:
        item = dict(source)
        if item.get("path"):
            item["path"] = _resolve_path(item["path"])
        out.append(item)
    return tuple(out)


def _profile_path(city_id: str) -> str:
    if city_id.endswith(".json") or os.path.sep in city_id:
        return _resolve_path(city_id) or city_id
    return os.path.join(CITIES_DIR, f"{city_id}.json")


def list_city_ids() -> list[str]:
    if not os.path.isdir(CITIES_DIR):
        return []
    ids = []
    for name in sorted(os.listdir(CITIES_DIR)):
        if name.endswith(".json"):
            ids.append(name[:-5])
    return ids


def load_city_profile(city_id: str = DEFAULT_CITY_ID) -> CityProfile:
    path = _profile_path(city_id)
    with open(path) as f:
        raw = json.load(f)

    center = raw.get("center") or []
    bbox = raw.get("bbox") or []
    if len(center) != 2:
        raise ValueError(f"{path}: center must be [lat, lon]")
    if len(bbox) != 4:
        raise ValueError(f"{path}: bbox must be [min_lat, min_lon, max_lat, max_lon]")

    paths = {
        key: resolved
        for key, value in (raw.get("paths") or {}).items()
        if (resolved := _resolve_path(value)) is not None
    }
    return CityProfile(
        id=raw.get("id") or os.path.splitext(os.path.basename(path))[0],
        name=raw.get("name") or raw.get("display_name") or city_id,
        display_name=raw.get("display_name") or raw.get("name") or city_id,
        timezone=raw.get("timezone") or "UTC",
        center=(float(center[0]), float(center[1])),
        bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        paths=paths,
        building_sources=_resolve_source_paths(raw.get("building_sources") or []),
        streetlight_sources=_resolve_source_paths(raw.get("streetlight_sources") or []),
        source_notes=dict(raw.get("source_notes") or {}),
    )


def default_data_path(city_id: str, *parts: str) -> str:
    if city_id == DEFAULT_CITY_ID:
        return os.path.abspath(os.path.join(REPO_ROOT, "data", *parts))
    return os.path.abspath(os.path.join(REPO_ROOT, "data", "cities", city_id, *parts))


def profile_data_path(profile: CityProfile, key: str, *parts: str) -> str:
    return profile.paths.get(key) or default_data_path(profile.id, *parts)


def height_from_properties_ft(props: dict[str, Any], source: dict[str, Any]) -> float | None:
    fields = source.get("height_fields")
    if not fields:
        fields = [source.get("height_field") or "height_ft"]
    elif isinstance(fields, str):
        fields = [fields]

    raw_value = None
    for field in fields:
        if field in props and props[field] not in (None, ""):
            raw_value = props[field]
            break

    if raw_value in (None, ""):
        default_ft = source.get("default_height_ft")
        return float(default_ft) if default_ft is not None else None

    try:
        height = float(raw_value)
    except (TypeError, ValueError):
        default_ft = source.get("default_height_ft")
        return float(default_ft) if default_ft is not None else None

    unit = (source.get("height_unit") or "ft").lower()
    if unit in ("m", "meter", "meters"):
        height *= 3.28084
    elif unit not in ("ft", "foot", "feet"):
        multiplier = float(source.get("height_multiplier") or 1.0)
        height *= multiplier

    if height <= 0:
        default_ft = source.get("default_height_ft")
        return float(default_ft) if default_ft is not None else None

    return round(height, 1)


def point_in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, min_lon, max_lat, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
