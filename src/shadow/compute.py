import json
import math
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib.solarposition
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
    mapping,
    shape,
)
from shapely.ops import unary_union

_parsed_buildings = {}

BOSTON_TZ = ZoneInfo("US/Eastern")
LAT_CENTER = 42.36
M_PER_DEG_LAT = 111320
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT_CENTER))
MAX_SHADOW_LENGTH = 500


def get_sun_position(dt, lat=LAT_CENTER, lon=-71.06):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BOSTON_TZ)
    times = pd.DatetimeIndex([dt])
    pos = pvlib.solarposition.get_solarposition(times, lat, lon)
    altitude = float(pos["apparent_elevation"].iloc[0])
    azimuth = float(pos["azimuth"].iloc[0])
    return altitude, azimuth


def compute_shadow(building_polygon, height_ft, sun_altitude, sun_azimuth):
    if sun_altitude <= 0:
        return None, 0

    height_m = height_ft * 0.3048
    shadow_length_m = height_m / math.tan(math.radians(sun_altitude))
    shadow_length_m = min(shadow_length_m, MAX_SHADOW_LENGTH)

    shadow_dir = math.radians(sun_azimuth + 180)
    dx_m = shadow_length_m * math.sin(shadow_dir)
    dy_m = shadow_length_m * math.cos(shadow_dir)

    dx_deg = dx_m / M_PER_DEG_LON
    dy_deg = dy_m / M_PER_DEG_LAT

    translated = affinity.translate(building_polygon, xoff=dx_deg, yoff=dy_deg)
    result = unary_union([building_polygon, translated]).convex_hull

    if not result.is_valid:
        result = result.buffer(0)

    return result, shadow_length_m


def _extract_polygon(geom):
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None
        largest = max(polys, key=lambda g: g.area)
        if isinstance(largest, MultiPolygon):
            return max(largest.geoms, key=lambda g: g.area)
        return largest
    return None


def compute_all_shadows(geojson_path, dt, height_field="BLDG_HGT_2010"):
    if geojson_path not in _parsed_buildings:
        with open(geojson_path) as f:
            data = json.load(f)
        buildings = []
        for feat in data["features"]:
            props = feat.get("properties", {})
            height = props.get(height_field)
            if height is None or height <= 0:
                continue
            geom = shape(feat["geometry"])
            poly = _extract_polygon(geom)
            if poly is None or poly.is_empty:
                continue
            buildings.append((poly, float(height)))
        _parsed_buildings[geojson_path] = buildings

    buildings = _parsed_buildings[geojson_path]
    altitude, azimuth = get_sun_position(dt)

    if altitude <= 0:
        return [], altitude, azimuth

    features = []
    for poly, height_ft in buildings:
        shadow, shadow_len = compute_shadow(poly, height_ft, altitude, azimuth)
        if shadow is None:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "height_ft": height_ft,
                "shadow_len_m": round(shadow_len, 1),
                "type": "shadow",
            },
            "geometry": mapping(shadow),
        })

    return features, altitude, azimuth
