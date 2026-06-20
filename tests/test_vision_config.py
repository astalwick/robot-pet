import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.vision import VisionConfig, VisionConfigError, load_vision_config, save_vision_config


class VisionConfigTest(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_vision_config(os.path.join(tmpdir, "missing.json"))

        self.assertEqual(config, VisionConfig())

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "vision.json")
            save_vision_config(
                VisionConfig(
                    enabled=False,
                    detection_rate_hz=4.0,
                    detection_max_width=480,
                    haar_scale_factor=1.2,
                    haar_min_size=32,
                ),
                path,
            )

            config = load_vision_config(path)

        self.assertFalse(config.enabled)
        self.assertEqual(config.detection_rate_hz, 4.0)
        self.assertEqual(config.detection_max_width, 480)
        self.assertEqual(config.haar_scale_factor, 1.2)
        self.assertEqual(config.haar_min_size, 32)

    def test_malformed_file_raises_clear_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "vision.json")
            with open(path, "w") as file_obj:
                file_obj.write("{not json")

            with self.assertRaisesRegex(VisionConfigError, "Invalid vision config"):
                load_vision_config(path)

    def test_non_object_file_raises_clear_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "vision.json")
            with open(path, "w") as file_obj:
                file_obj.write("[]")

            with self.assertRaisesRegex(VisionConfigError, "expected a JSON object"):
                load_vision_config(path)

    def test_detection_rate_is_clamped(self):
        too_low = VisionConfig.from_dict({"detection_rate_hz": 0.0})
        too_high = VisionConfig.from_dict({"detection_rate_hz": 100.0})

        self.assertEqual(too_low.detection_rate_hz, 0.2)
        self.assertEqual(too_high.detection_rate_hz, 10.0)

    def test_detector_tuning_values_are_clamped(self):
        too_low = VisionConfig.from_dict(
            {
                "detection_max_width": 1,
                "haar_scale_factor": 1.0,
                "haar_min_size": 1,
            }
        )
        too_high = VisionConfig.from_dict(
            {
                "detection_max_width": 9999,
                "haar_scale_factor": 9.0,
                "haar_min_size": 9999,
            }
        )

        self.assertEqual(too_low.detection_max_width, 160)
        self.assertEqual(too_low.haar_scale_factor, 1.05)
        self.assertEqual(too_low.haar_min_size, 8)
        self.assertEqual(too_high.detection_max_width, 1280)
        self.assertEqual(too_high.haar_scale_factor, 1.5)
        self.assertEqual(too_high.haar_min_size, 240)

    def test_enabled_is_parsed_as_bool(self):
        truthy = VisionConfig.from_dict({"enabled": 1})
        falsy = VisionConfig.from_dict({"enabled": 0})
        explicit_false = VisionConfig.from_dict({"enabled": False})

        self.assertTrue(truthy.enabled)
        self.assertFalse(falsy.enabled)
        self.assertFalse(explicit_false.enabled)


if __name__ == "__main__":
    unittest.main()
