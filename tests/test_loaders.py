"""Regression tests for the time-slider data loaders.

These loaders were added late in the project and ship the actual
features (OSM POIs, tree canopy, crime, crash records) to the
browser. A quiet drift in their expected input shape would silently
break the time-slider without a shadow-engine error. The tests here
exercise:

  - Missing input file returns [] without raising.
  - A minimal valid input yields well-formed output.
  - Malformed features are skipped, not crashed on.

Each test writes a tiny GeoJSON to a temp directory, monkey-patches
the loader's path constant to point at it, calls the loader, and
asserts the shape of the returned records. No network calls.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import prototype  # imported via sys.path insertion below


THIS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(THIS_DIR, ".."))


def _write_geojson(path, features):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


class TestLoadOsmPois(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = os.path.join(td, "does_not_exist.geojson")
            with patch.object(prototype, "OSM_POIS_PATH", fake_path):
                self.assertEqual(prototype.load_osm_pois(), [])

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pois.geojson")
            _write_geojson(path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point", "coordinates": [-71.09, 42.36]
                    },
                    "properties": {
                        "name": "A Cafe", "amenity": "cafe",
                        "opening_hours": "Mo-Fr 08:00-17:00",
                    },
                },
            ])
            with patch.object(prototype, "OSM_POIS_PATH", path):
                pois = prototype.load_osm_pois()
            self.assertEqual(len(pois), 1)
            self.assertAlmostEqual(pois[0]["lat"], 42.36)
            self.assertAlmostEqual(pois[0]["lon"], -71.09)
            self.assertEqual(pois[0]["amenity"], "cafe")
            self.assertEqual(pois[0]["hours"], "Mo-Fr 08:00-17:00")

    def test_drops_features_without_opening_hours(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pois.geojson")
            _write_geojson(path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point", "coordinates": [-71.09, 42.36]
                    },
                    "properties": {"name": "NoHours", "amenity": "bar"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point", "coordinates": [-71.09, 42.36]
                    },
                    "properties": {
                        "name": "HasHours", "amenity": "bar",
                        "opening_hours": "24/7",
                    },
                },
            ])
            with patch.object(prototype, "OSM_POIS_PATH", path):
                pois = prototype.load_osm_pois()
            self.assertEqual(len(pois), 1)
            self.assertEqual(pois[0]["name"], "HasHours")


class TestLoadTrees(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = os.path.join(td, "does_not_exist.geojson")
            with patch.object(prototype, "TREES_PATH", fake_path):
                self.assertEqual(prototype.load_trees(), [])

    def test_happy_path_uses_height_property(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "trees.geojson")
            _write_geojson(path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-71.09, 42.36], [-71.089, 42.36],
                            [-71.089, 42.361], [-71.09, 42.361],
                            [-71.09, 42.36],
                        ]],
                    },
                    "properties": {"height_m": 12.5},
                },
            ])
            with patch.object(prototype, "TREES_PATH", path):
                trees = prototype.load_trees()
            self.assertEqual(len(trees), 1)
            self.assertEqual(trees[0]["h_m"], 12.5)
            self.assertEqual(len(trees[0]["ring"]), 5)

    def test_defaults_height_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "trees.geojson")
            _write_geojson(path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-71.09, 42.36], [-71.089, 42.36],
                            [-71.089, 42.361], [-71.09, 42.361],
                            [-71.09, 42.36],
                        ]],
                    },
                    "properties": {},
                },
            ])
            with patch.object(prototype, "TREES_PATH", path):
                trees = prototype.load_trees()
            self.assertEqual(trees[0]["h_m"], 10.0)


class TestLoadSafety(unittest.TestCase):
    def test_missing_files_return_empty(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "nope.geojson")
            with patch.object(prototype, "CRIME_PATH", missing):
                self.assertEqual(prototype.load_safety_crime(), [])
            with patch.object(prototype, "CRASH_PATH", missing):
                self.assertEqual(prototype.load_safety_crashes(), [])

    def test_crime_returns_lat_lon_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "crime.geojson")
            _write_geojson(path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point", "coordinates": [-71.09, 42.36]
                    },
                    "properties": {"hour": "22", "descript": "ASSAULT"},
                },
            ])
            with patch.object(prototype, "CRIME_PATH", path):
                pts = prototype.load_safety_crime()
            self.assertEqual(pts, [[42.36, -71.09]])

    def test_crash_carries_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "crashes.geojson")
            _write_geojson(path, [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point", "coordinates": [-71.09, 42.36]
                    },
                    "properties": {"mode": "bike", "ts": "2024-05-01 22:13"},
                },
            ])
            with patch.object(prototype, "CRASH_PATH", path):
                rows = prototype.load_safety_crashes()
            self.assertEqual(rows, [{"lat": 42.36, "lon": -71.09, "mode": "bike"}])


if __name__ == "__main__":
    unittest.main()
