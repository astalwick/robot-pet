import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import SensorEntry, SensorsConfig
from drivers.imu import ImuReading
from drivers.range import RangeReading, RangeSensorConfig
from robot_sensors import SensorsService


def write_config(path: str, values: dict) -> None:
    with open(path, "w") as file_obj:
        json.dump(values, file_obj)


def bump_mtime(path: str, seconds: float = 2.0) -> None:
    """Use wall time so Pi filesystems with 1s mtime resolution still see a change."""
    now = time.time()
    os.utime(path, (now, now + seconds))


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRangeDriver:
    def __init__(self, sensors: list[RangeSensorConfig]):
        self.sensors = sensors
        self.cleaned_up = False

    def read_all(self) -> list[RangeReading]:
        return [
            RangeReading(
                name=sensor.name,
                kind=sensor.kind,
                channel=sensor.channel,
                distance_mm=100 + sensor.channel,
                ok=True,
            )
            for sensor in self.sensors
        ]

    def cleanup(self) -> None:
        self.cleaned_up = True


class FakeImuDriver:
    def __init__(self):
        self.cleaned_up = False

    def read(self) -> ImuReading:
        return ImuReading(
            yaw_degrees=90.0,
            pitch_degrees=1.5,
            roll_degrees=-0.5,
            ok=True,
        )

    def cleanup(self) -> None:
        self.cleaned_up = True


class SensorsServiceTest(unittest.TestCase):
    def _make_service(self, config_path: str, clock: FakeClock | None = None):
        published: list[dict] = []
        drivers: list[FakeRangeDriver] = []
        imu_drivers: list[FakeImuDriver] = []

        def driver_factory(config: SensorsConfig) -> FakeRangeDriver:
            driver = FakeRangeDriver(config.driver_sensors())
            drivers.append(driver)
            return driver

        def imu_driver_factory(config: SensorsConfig) -> FakeImuDriver | None:
            if config.driver_imu() is None:
                return None
            driver = FakeImuDriver()
            imu_drivers.append(driver)
            return driver

        service = SensorsService(
            config_path=config_path,
            publish=published.append,
            driver_factory=driver_factory,
            imu_driver_factory=imu_driver_factory,
            time_fn=(clock or FakeClock()),
        )
        return service, published, drivers, imu_drivers

    def test_disabled_config_publishes_disabled_without_driver(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_config(
                path,
                {
                    "enabled": False,
                    "poll_rate_hz": 10,
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0},
                    ],
                },
            )
            service, published, drivers, imu_drivers = self._make_service(path)
            os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 2))

            service.tick()

        self.assertEqual(published[-1]["status"], "disabled")
        self.assertEqual(drivers, [])
        self.assertEqual(imu_drivers, [])

    def test_enabled_config_polls_and_publishes_readings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 10,
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0},
                    ],
                },
            )
            clock = FakeClock()
            service, published, drivers, _imu_drivers = self._make_service(path, clock)
            bump_mtime(path)

            service.tick()

        self.assertEqual(len(drivers), 1)
        message = published[-1]
        self.assertEqual(message["status"], "polling")
        self.assertEqual(message["readings"][0]["distance_mm"], 100)

    def test_driver_recreated_when_sensor_list_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 10,
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0},
                    ],
                },
            )
            clock = FakeClock()
            service, _published, drivers, _imu_drivers = self._make_service(path, clock)
            bump_mtime(path)
            service.tick()

            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 10,
                    "sensors": [
                        {"name": "cliff_right", "kind": "vl53l0x", "mux_channel": 2},
                    ],
                },
            )
            bump_mtime(path)
            service.tick()

        self.assertEqual(len(drivers), 2)
        self.assertTrue(drivers[0].cleaned_up)

    def test_enabled_imu_publishes_orientation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 10,
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
                        "axis_map": {
                            "yaw_degrees": "-pitch_degrees",
                            "pitch_degrees": "+roll_degrees",
                            "roll_degrees": "+yaw_degrees",
                        },
                    },
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0},
                    ],
                },
            )
            clock = FakeClock()
            service, published, _drivers, imu_drivers = self._make_service(path, clock)
            bump_mtime(path)

            service.tick()

        self.assertEqual(len(imu_drivers), 1)
        self.assertEqual(
            published[-1]["imu"],
            {
                "ok": True,
                "yaw_degrees": 90.0,
                "pitch_degrees": 1.5,
                "roll_degrees": -0.5,
            },
        )


if __name__ == "__main__":
    unittest.main()
