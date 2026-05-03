import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.teleop import DriveTuning, DriveTuningConfigError, load_drive_tuning, save_drive_tuning


class DriveTuningConfigTest(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning = load_drive_tuning(os.path.join(tmpdir, "missing.json"))

        self.assertEqual(tuning.speed_scale, 0.25)
        self.assertEqual(tuning.turbo_scale, 0.75)

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "teleop.json")
            save_drive_tuning(DriveTuning(speed_scale=0.3, left_stick_deadzone=0.2), path)

            tuning = load_drive_tuning(path)

        self.assertEqual(tuning.speed_scale, 0.3)
        self.assertEqual(tuning.left_stick_deadzone, 0.2)

    def test_malformed_file_raises_clear_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "teleop.json")
            with open(path, "w") as file_obj:
                file_obj.write("{not json")

            with self.assertRaisesRegex(DriveTuningConfigError, "Invalid drive tuning config"):
                load_drive_tuning(path)

    def test_non_object_file_raises_clear_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "teleop.json")
            with open(path, "w") as file_obj:
                file_obj.write("[]")

            with self.assertRaisesRegex(DriveTuningConfigError, "expected a JSON object"):
                load_drive_tuning(path)

    def test_values_are_clamped_to_safe_ranges(self):
        tuning = DriveTuning.from_dict(
            {
                "speed_scale": 2.0,
                "turbo_scale": -1.0,
                "turn_scale": 1.5,
                "left_stick_deadzone": -0.2,
                "right_stick_deadzone": 2.0,
                "qpps_slew_limit": 0.0,
            }
        )

        self.assertEqual(tuning.speed_scale, 1.0)
        self.assertEqual(tuning.turbo_scale, 0.0)
        self.assertEqual(tuning.turn_scale, 1.0)
        self.assertEqual(tuning.left_stick_deadzone, 0.0)
        self.assertEqual(tuning.right_stick_deadzone, 1.0)
        self.assertEqual(tuning.qpps_slew_limit, 100.0)


if __name__ == "__main__":
    unittest.main()
