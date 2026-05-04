import csv
import json
import os
import tempfile
import unittest

from city_config import (
    CityProfile,
    DEFAULT_CITY_ID,
    height_from_properties_ft,
    load_city_profile,
    profile_data_path,
)
import prototype


def _write_geojson(path, features):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


class TestCityConfig(unittest.TestCase):
    def test_default_profile_loads(self):
        city = load_city_profile(DEFAULT_CITY_ID)
        self.assertEqual(city.id, DEFAULT_CITY_ID)
        self.assertEqual(city.timezone, "America/New_York")
        self.assertEqual(len(city.building_sources), 2)
        self.assertEqual(len(city.streetlight_sources), 2)

    def test_height_conversion(self):
        self.assertEqual(
            height_from_properties_ft({"height_m": 10}, {
                "height_field": "height_m",
                "height_unit": "m",
            }),
            32.8,
        )
        self.assertEqual(
            height_from_properties_ft({}, {"default_height_ft": 20}),
            20,
        )

    def test_non_default_paths_fall_under_city_data_dir(self):
        profile = CityProfile(
            id="sample",
            name="Sample",
            display_name="Sample",
            timezone="UTC",
            center=(1.0, 2.0),
            bbox=(0.0, 1.0, 2.0, 3.0),
            paths={},
            building_sources=(),
            streetlight_sources=(),
            source_notes={},
        )
        self.assertTrue(
            profile_data_path(profile, "osm_pois", "osm", "pois.geojson")
            .endswith(os.path.join("data", "cities", "sample", "osm", "pois.geojson"))
        )


class TestPrototypeGenericCity(unittest.TestCase):
    def test_loads_configured_buildings_and_streetlights(self):
        old_city = prototype.CITY
        with tempfile.TemporaryDirectory() as td:
            buildings_path = os.path.join(td, "buildings.geojson")
            lights_path = os.path.join(td, "streetlights.csv")
            _write_geojson(buildings_path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [1.0, 1.0], [1.001, 1.0],
                            [1.001, 1.001], [1.0, 1.001],
                            [1.0, 1.0],
                        ]],
                    },
                    "properties": {"height_m": 12},
                },
            ])
            with open(lights_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["lat", "lon"])
                writer.writeheader()
                writer.writerow({"lat": "1.0005", "lon": "1.0005"})
                writer.writerow({"lat": "9.0", "lon": "9.0"})

            city = CityProfile(
                id="sample",
                name="Sample",
                display_name="Sample City",
                timezone="UTC",
                center=(1.0, 1.0),
                bbox=(0.9, 0.9, 1.1, 1.1),
                paths={"buildings_db": os.path.join(td, "missing.db")},
                building_sources=({
                    "id": "sample-buildings",
                    "label": "Sample buildings",
                    "path": buildings_path,
                    "format": "geojson",
                    "height_field": "height_m",
                    "height_unit": "m",
                },),
                streetlight_sources=({
                    "id": "sample-lights",
                    "label": "Sample lights",
                    "path": lights_path,
                    "format": "csv",
                    "lat_field": "lat",
                    "lon_field": "lon",
                },),
                source_notes={},
            )
            try:
                prototype.set_active_city(city)
                buildings = prototype.load_buildings(100)
                lights = prototype.load_streetlights(100)
            finally:
                prototype.set_active_city(old_city)

        self.assertEqual(len(buildings["features"]), 1)
        self.assertEqual(
            buildings["features"][0]["properties"]["BLDG_HGT_2010"],
            39.4,
        )
        self.assertEqual(lights, [[1.0005, 1.0005]])


if __name__ == "__main__":
    unittest.main()
