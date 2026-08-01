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


def write_imu_config(path: str, poll_rate_hz: float = 10) -> None:
    write_config(
        path,
        {
            "enabled": True,
            "poll_rate_hz": poll_rate_hz,
            "imu": {
                "enabled": True,
                "kind": "bno085",
                "mux_channel": 5,
                "address": "0x4a",
                "mode": "game",
                "zero_quaternion": [-0.48814613, -0.51180947, 0.48788612, 0.51159707],
                "zero_gravity": [0.0479263, -0.99885052, -0.00083945],
            },
            "sensors": [{"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0}],
        },
    )


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
    def __init__(self, ok: bool = True):
        self.cleaned_up = False
        self.ok = ok
        self.read_timeout: float | None = None

    def read(self, timeout: float = 5.0) -> ImuReading:
        self.read_timeout = timeout
        if not self.ok:
            return ImuReading(yaw_degrees=None, pitch_degrees=None, roll_degrees=None, ok=False)
        return ImuReading(
            yaw_degrees=90.0,
            pitch_degrees=1.5,
            roll_degrees=-0.5,
            ok=True,
        )

    def cleanup(self) -> None:
        self.cleaned_up = True


class SensorsServiceTest(unittest.TestCase):
    def _make_service(
        self, config_path: str, clock: FakeClock | None = None, imu_ok: bool = True
    ):
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
            driver = FakeImuDriver(ok=imu_ok)
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

    def test_published_readings_include_role_and_thresholds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 10,
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0, "role": "cliff"},
                        {"name": "front_center", "kind": "vl53l1x", "mux_channel": 1, "role": "forward"},
                        {"name": "debug_raw", "kind": "vl53l0x", "mux_channel": 2},
                    ],
                },
            )
            service, published, _drivers, _imu_drivers = self._make_service(path)
            bump_mtime(path)
            service.tick()

        readings = {item["name"]: item for item in published[-1]["readings"]}
        self.assertEqual(readings["cliff_left"]["role"], "cliff")
        self.assertEqual(readings["cliff_left"]["trip_above_mm"], 200)
        self.assertEqual(readings["front_center"]["role"], "forward")
        self.assertEqual(readings["front_center"]["stop_below_mm"], 150)
        self.assertNotIn("role", readings["debug_raw"])

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
            write_imu_config(path, poll_rate_hz=10)
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
        # At 10 Hz the poll period (0.1s) is under the cap, so it is the budget.
        self.assertEqual(imu_drivers[0].read_timeout, 0.1)

    def test_imu_read_timeout_capped_at_low_poll_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_imu_config(path, poll_rate_hz=1)
            service, _published, _drivers, imu_drivers = self._make_service(path)
            bump_mtime(path)

            service.tick()

        # Poll period is 1.0s here, but the IMU read stays capped at 0.25s so a
        # slow IMU can't pile its own delay on top of the poll period.
        self.assertEqual(imu_drivers[0].read_timeout, 0.25)

    def test_imu_init_failure_keeps_range_driver_polling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_imu_config(path)
            clock = FakeClock()
            published: list[dict] = []
            drivers: list[FakeRangeDriver] = []

            def driver_factory(config: SensorsConfig) -> FakeRangeDriver:
                driver = FakeRangeDriver(config.driver_sensors())
                drivers.append(driver)
                return driver

            def imu_driver_factory(config: SensorsConfig):
                raise RuntimeError("no IMU on the bus")

            service = SensorsService(
                config_path=path,
                publish=published.append,
                driver_factory=driver_factory,
                imu_driver_factory=imu_driver_factory,
                time_fn=clock,
            )
            bump_mtime(path)

            service.tick()
            clock.advance(1.0)
            service.tick()

        # The failed IMU must not release and rebuild the range driver each tick.
        self.assertEqual(len(drivers), 1)
        self.assertFalse(drivers[0].cleaned_up)
        message = published[-1]
        self.assertEqual(message["status"], "polling")
        self.assertEqual(message["imu"], {"ok": False, "reason": "no IMU on the bus"})

    def test_imu_read_failure_still_publishes_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_imu_config(path)
            service, published, _drivers, _imu_drivers = self._make_service(path, imu_ok=False)
            bump_mtime(path)

            service.tick()

        message = published[-1]
        self.assertEqual(message["status"], "polling")
        self.assertEqual(message["readings"][0]["distance_mm"], 100)
        self.assertFalse(message["imu"]["ok"])

    def test_uncalibrated_imu_publishes_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sensors.json")
            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 10,
                    "imu": {"enabled": True, "kind": "bno085", "mux_channel": 5},
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0},
                    ],
                },
            )
            service, published, _drivers, imu_drivers = self._make_service(path)
            bump_mtime(path)

            service.tick()

        message = published[-1]
        self.assertEqual(imu_drivers, [])
        self.assertEqual(message["readings"][0]["distance_mm"], 100)
        self.assertEqual(message["imu"], {"ok": False, "reason": "uncalibrated"})

    def test_poll_rate_only_edit_keeps_driver(self):
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
            service, _published, drivers, _imu_drivers = self._make_service(path)
            bump_mtime(path)
            service.tick()

            write_config(
                path,
                {
                    "enabled": True,
                    "poll_rate_hz": 5,
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0},
                    ],
                },
            )
            bump_mtime(path)
            service.tick()

        # Only the poll rate changed, so the driver must not be rebuilt.
        self.assertEqual(len(drivers), 1)
        self.assertFalse(drivers[0].cleaned_up)


if __name__ == "__main__":
    unittest.main()
