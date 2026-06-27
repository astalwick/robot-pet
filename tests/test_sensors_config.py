import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import (
    IMU_AXIS_MAP,
    SafetyConfig,
    SensorEntry,
    SensorsConfig,
    SensorsConfigError,
    cliff_trip_mm,
    forward_stop_mm,
    load_sensors_config,
    save_sensors_config,
)
from drivers.range import RangeSensorConfig


class SensorsConfigTest(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_sensors_config(os.path.join(tmpdir, "missing.json"))

        self.assertEqual(config, SensorsConfig())

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

    def test_safety_and_per_sensor_thresholds(self):
        config = SensorsConfig.from_dict(
            {
                "safety": {
                    "enabled": True,
                    "cliff_trip_above_mm": 180,
                    "forward_stop_below_mm": 120,
                },
                "sensors": [
                    {
                        "name": "cliff_left",
                        "kind": "vl53l0x",
                        "mux_channel": 0,
                        "role": "cliff",
                        "trip_above_mm": 160,
                    },
                    {
                        "name": "forward_center",
                        "kind": "vl53l1x",
                        "mux_channel": 3,
                        "role": "forward",
                        "stop_below_mm": 100,
                    },
                ],
            }
        )

        self.assertTrue(config.safety.enabled)
        self.assertEqual(cliff_trip_mm(config.sensors[0], config.safety), 160)
        self.assertEqual(forward_stop_mm(config.sensors[1], config.safety), 100)
        self.assertIsNone(forward_stop_mm(config.sensors[0], config.safety))

    def test_offset_round_trips_and_reaches_driver(self):
        config = SensorsConfig.from_dict(
            {
                "sensors": [
                    {
                        "name": "cliff_left",
                        "kind": "vl53l0x",
                        "mux_channel": 0,
                        "role": "cliff",
                        "offset_mm": 20,
                    },
                    {
                        "name": "cliff_right",
                        "kind": "vl53l0x",
                        "mux_channel": 1,
                        "role": "cliff",
                        "offset_mm": -5,
                    },
                ]
            }
        )

        self.assertEqual(config.sensors[0].offset_mm, 20)
        self.assertEqual(config.sensors[1].offset_mm, -5)
        driver_sensors = config.driver_sensors()
        self.assertEqual(driver_sensors[0].offset_mm, 20)
        self.assertEqual(driver_sensors[1].offset_mm, -5)

    def test_offset_must_be_an_integer(self):
        with self.assertRaises(TypeError):
            SensorsConfig.from_dict(
                {
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0, "offset_mm": 1.5},
                    ]
                }
            )

    def test_save_round_trip_includes_safety(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            config = SensorsConfig(
                safety=SafetyConfig(enabled=True, cliff_trip_above_mm=175, forward_stop_below_mm=140),
                sensors=(
                    SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),
                ),
            )
            save_sensors_config(config, path)

            loaded = load_sensors_config(path)

        self.assertTrue(loaded.safety.enabled)
        self.assertEqual(loaded.safety.cliff_trip_above_mm, 175)
        self.assertEqual(loaded.sensors[0].role, "cliff")

    def test_imu_config_round_trips_and_reaches_driver(self):
        config = SensorsConfig.from_dict(
            {
                "imu": {
                    "enabled": True,
                    "kind": "bno085",
                    "mux_channel": 5,
                    "address": "0x4a",
                    "mode": "game",
                    "zero_quaternion": [
                        -0.48814613,
                        -0.51180947,
                        0.48788612,
                        0.51159707,
                    ],
                    "zero_gravity": [0.0479263, -0.99885052, -0.00083945],
                    "axis_map": IMU_AXIS_MAP,
                }
            }
        )

        imu = config.driver_imu()

        self.assertIsNotNone(imu)
        self.assertEqual(imu.channel, 5)
        self.assertEqual(imu.address, 0x4A)
        self.assertEqual(imu.mode, "game")
        self.assertEqual(imu.zero_gravity, (0.0479263, -0.99885052, -0.00083945))

    def test_enabled_imu_without_calibration_does_not_create_driver_config(self):
        config = SensorsConfig.from_dict(
            {
                "imu": {
                    "enabled": True,
                    "kind": "bno085",
                    "mux_channel": 5,
                    "address": "0x4a",
                    "mode": "game",
                }
            }
        )

        self.assertIsNone(config.driver_imu())


if __name__ == "__main__":
    unittest.main()
