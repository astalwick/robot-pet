import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import (
    SensorEntry,
    SensorsConfig,
    SensorsConfigError,
    load_sensors_config,
    save_sensors_config,
)
from drivers.range import RangeSensorConfig


class SensorsConfigTest(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_sensors_config(os.path.join(tmpdir, "missing.json"))

        self.assertTrue(config.enabled)
        self.assertEqual(config.poll_rate_hz, 10.0)
        self.assertEqual(len(config.sensors), 3)
        self.assertEqual(config.sensors[0].name, "cliff_left")

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            config = SensorsConfig(
                enabled=False,
                poll_rate_hz=5.0,
                sensors=(
                    SensorEntry("cliff_left", "vl53l0x", 0),
                    SensorEntry("forward_center", "vl53l1x", 3),
                ),
            )
            save_sensors_config(config, path)

            loaded = load_sensors_config(path)

        self.assertFalse(loaded.enabled)
        self.assertEqual(loaded.poll_rate_hz, 5.0)
        self.assertEqual(loaded.sensors[1].kind, "vl53l1x")

    def test_driver_sensors_maps_mux_channel(self):
        config = SensorsConfig(
            sensors=(SensorEntry("cliff_left", "vl53l0x", 2),),
        )

        driver_sensors = config.driver_sensors()

        self.assertEqual(driver_sensors, [RangeSensorConfig("cliff_left", "vl53l0x", 2)])

    def test_malformed_file_raises_clear_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            with open(path, "w") as file_obj:
                file_obj.write("{not json")

            with self.assertRaisesRegex(SensorsConfigError, "Invalid sensors config"):
                load_sensors_config(path)

    def test_poll_rate_is_clamped(self):
        too_low = SensorsConfig.from_dict({"poll_rate_hz": 0.0})
        too_high = SensorsConfig.from_dict({"poll_rate_hz": 100.0})

        self.assertEqual(too_low.poll_rate_hz, 0.5)
        self.assertEqual(too_high.poll_rate_hz, 20.0)

    def test_unknown_kind_raises(self):
        with self.assertRaises(TypeError):
            SensorsConfig.from_dict(
                {
                    "sensors": [
                        {"name": "bad", "kind": "unknown", "mux_channel": 0},
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
