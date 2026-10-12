import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from ros_nodes.ros_messages import (
    INFRARED,
    POWER_SUPPLY_TECHNOLOGY_LIPO,
    UNKNOWN_COVARIANCE,
    battery_state_fields,
    imu_fields,
    odometry_fields,
    quaternion_from_euler_degrees,
    range_fields,
)


def reading(distance_mm, ok=True, kind="vl53l0x", name="cliff_left"):
    return {"name": name, "kind": kind, "channel": 0, "distance_mm": distance_mm, "ok": ok}


class RangeFieldsTest(unittest.TestCase):
    def test_good_reading_converts_mm_to_meters(self):
        fields = range_fields(reading(250))
        self.assertEqual(fields["frame_id"], "cliff_left")
        self.assertEqual(fields["radiation_type"], INFRARED)
        self.assertAlmostEqual(fields["range"], 0.25)
        self.assertAlmostEqual(fields["field_of_view"], math.radians(25))
        self.assertEqual(fields["min_range"], 0.03)
        self.assertEqual(fields["max_range"], 1.2)

    def test_forward_sensor_uses_vl53l1x_limits(self):
        fields = range_fields(reading(900, kind="vl53l1x", name="forward_center"))
        self.assertEqual(fields["frame_id"], "forward_center")
        self.assertEqual(fields["max_range"], 1.3)
        self.assertAlmostEqual(fields["range"], 0.9)

    def test_failed_read_is_nan(self):
        self.assertTrue(math.isnan(range_fields(reading(None, ok=False))["range"]))

    def test_nothing_in_range_is_positive_infinity(self):
        self.assertEqual(range_fields(reading(None, ok=True, kind="vl53l1x"))["range"], math.inf)

    def test_beyond_max_range_is_positive_infinity(self):
        self.assertEqual(range_fields(reading(8190))["range"], math.inf)

    def test_below_min_range_is_negative_infinity(self):
        self.assertEqual(range_fields(reading(10))["range"], -math.inf)


class QuaternionTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(quaternion_from_euler_degrees(0, 0, 0), (0.0, 0.0, 0.0, 1.0))

    def test_yaw_only_rotates_about_z(self):
        x, y, z, w = quaternion_from_euler_degrees(0, 0, 90)
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 0.0)
        self.assertAlmostEqual(z, math.sqrt(0.5))
        self.assertAlmostEqual(w, math.sqrt(0.5))

    def test_pitch_only_rotates_about_y(self):
        x, y, z, w = quaternion_from_euler_degrees(0, 30, 0)
        self.assertAlmostEqual(y, math.sin(math.radians(15)))
        self.assertAlmostEqual(w, math.cos(math.radians(15)))

    def test_roll_only_rotates_about_x(self):
        x, y, z, w = quaternion_from_euler_degrees(-45, 0, 0)
        self.assertAlmostEqual(x, -math.sin(math.radians(22.5)))
        self.assertAlmostEqual(w, math.cos(math.radians(22.5)))

    def test_unit_length(self):
        x, y, z, w = quaternion_from_euler_degrees(12.5, -70, 160)
        self.assertAlmostEqual(x * x + y * y + z * z + w * w, 1.0)


class ImuFieldsTest(unittest.TestCase):
    def test_no_fix_returns_none(self):
        self.assertIsNone(imu_fields({"ok": False, "reason": "uncalibrated"}))
        self.assertIsNone(imu_fields({}))

    def test_orientation_only_with_unknown_rate_covariances(self):
        fields = imu_fields({"ok": True, "yaw_degrees": 90, "pitch_degrees": 0, "roll_degrees": 0})
        self.assertEqual(fields["frame_id"], "imu_link")
        self.assertAlmostEqual(fields["orientation"][2], math.sqrt(0.5))
        self.assertEqual(len(fields["orientation_covariance"]), 9)
        self.assertEqual(fields["angular_velocity_covariance"], UNKNOWN_COVARIANCE)
        self.assertEqual(fields["linear_acceleration_covariance"][0], -1.0)


class OdometryFieldsTest(unittest.TestCase):
    def test_pose_and_twist_land_in_planar_slots(self):
        fields = odometry_fields(1.5, -0.25, math.pi / 2, 0.2, 0.8)
        self.assertEqual(fields["frame_id"], "odom")
        self.assertEqual(fields["child_frame_id"], "base_link")
        self.assertEqual(fields["position"], (1.5, -0.25, 0.0))
        self.assertAlmostEqual(fields["orientation"][2], math.sqrt(0.5))
        self.assertAlmostEqual(fields["orientation"][3], math.sqrt(0.5))
        self.assertEqual(fields["linear"], (0.2, 0.0, 0.0))
        self.assertEqual(fields["angular"], (0.0, 0.0, 0.8))

    def test_covariances_are_6x6_with_unused_axes_marked_huge(self):
        fields = odometry_fields(0, 0, 0, 0, 0)
        for covariance in (fields["pose_covariance"], fields["twist_covariance"]):
            self.assertEqual(len(covariance), 36)
            self.assertGreater(covariance[0], 0)
            self.assertGreater(covariance[35], 0)
            self.assertGreaterEqual(covariance[14], 1e6)


class BatteryStateFieldsTest(unittest.TestCase):
    def test_no_voltage_returns_none(self):
        self.assertIsNone(battery_state_fields({"pack_voltage": None}))
        self.assertIsNone(battery_state_fields({}))

    def test_voltage_and_percentage(self):
        fields = battery_state_fields({"pack_voltage": 11.7, "percent_estimate": 62})
        self.assertEqual(fields["voltage"], 11.7)
        self.assertAlmostEqual(fields["percentage"], 0.62)
        self.assertEqual(fields["power_supply_technology"], POWER_SUPPLY_TECHNOLOGY_LIPO)
        self.assertTrue(fields["present"])

    def test_unknown_percentage_is_nan(self):
        self.assertTrue(math.isnan(battery_state_fields({"pack_voltage": 11.7})["percentage"]))


if __name__ == "__main__":
    unittest.main()
