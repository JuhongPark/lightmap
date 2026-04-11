import math
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from shapely.geometry import Polygon, mapping

from shadow.compute import (
    STUDY_AREA,
    compute_shadow,
    compute_shadow_coverage,
    get_sun_position,
)

BOSTON_TZ = ZoneInfo("US/Eastern")


class TestGetSunPosition(unittest.TestCase):
    def test_summer_afternoon_positive_altitude(self):
        dt = datetime(2025, 7, 15, 14, 0, tzinfo=BOSTON_TZ)
        alt, az = get_sun_position(dt)
        self.assertGreater(alt, 50)
        self.assertLess(alt, 80)

    def test_night_negative_altitude(self):
        dt = datetime(2025, 7, 15, 23, 0, tzinfo=BOSTON_TZ)
        alt, _ = get_sun_position(dt)
        self.assertLess(alt, 0)

    def test_winter_afternoon_lower_altitude(self):
        summer = datetime(2025, 7, 15, 14, 0, tzinfo=BOSTON_TZ)
        winter = datetime(2025, 1, 15, 14, 0, tzinfo=BOSTON_TZ)
        summer_alt, _ = get_sun_position(summer)
        winter_alt, _ = get_sun_position(winter)
        self.assertGreater(summer_alt, winter_alt)

    def test_azimuth_range(self):
        dt = datetime(2025, 7, 15, 14, 0, tzinfo=BOSTON_TZ)
        _, az = get_sun_position(dt)
        self.assertGreaterEqual(az, 0)
        self.assertLess(az, 360)


class TestComputeShadow(unittest.TestCase):
    def setUp(self):
        self.building = Polygon([
            (-71.08, 42.36),
            (-71.08, 42.3601),
            (-71.0799, 42.3601),
            (-71.0799, 42.36),
        ])

    def test_no_shadow_below_horizon(self):
        shadow, length = compute_shadow(self.building, 100, -5, 180)
        self.assertIsNone(shadow)
        self.assertEqual(length, 0)

    def test_shadow_length_formula(self):
        altitude = 45
        height_ft = 100
        _, length = compute_shadow(self.building, height_ft, altitude, 180)
        expected_m = (height_ft * 0.3048) / math.tan(math.radians(altitude))
        self.assertAlmostEqual(length, expected_m, places=1)

    def test_shadow_contains_building(self):
        shadow, _ = compute_shadow(self.building, 100, 45, 180)
        self.assertTrue(shadow.contains(self.building))

    def test_shadow_larger_than_building(self):
        shadow, _ = compute_shadow(self.building, 100, 45, 180)
        self.assertGreater(shadow.area, self.building.area)

    def test_low_sun_longer_shadow(self):
        _, length_high = compute_shadow(self.building, 100, 60, 180)
        _, length_low = compute_shadow(self.building, 100, 20, 180)
        self.assertGreater(length_low, length_high)

    def test_taller_building_longer_shadow(self):
        _, length_short = compute_shadow(self.building, 50, 45, 180)
        _, length_tall = compute_shadow(self.building, 200, 45, 180)
        self.assertGreater(length_tall, length_short)

    def test_max_shadow_length_cap(self):
        _, length = compute_shadow(self.building, 1000, 1, 180)
        self.assertLessEqual(length, 500)


class TestComputeShadowCoverage(unittest.TestCase):
    def test_empty_features(self):
        self.assertEqual(compute_shadow_coverage([]), 0.0)

    def test_coverage_is_percentage(self):
        small_poly = Polygon([
            (-71.10, 42.35),
            (-71.10, 42.351),
            (-71.099, 42.351),
            (-71.099, 42.35),
        ])
        features = [{
            "type": "Feature",
            "geometry": mapping(small_poly),
            "properties": {"height_ft": 50, "shadow_len_ft": 33, "type": "shadow"},
        }]
        coverage = compute_shadow_coverage(features)
        self.assertGreater(coverage, 0)
        self.assertLess(coverage, 100)

    def test_uses_fixed_study_area(self):
        self.assertIsNotNone(STUDY_AREA)
        self.assertAlmostEqual(STUDY_AREA.area, 0.013, places=3)


if __name__ == "__main__":
    unittest.main()
