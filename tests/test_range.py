import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.range import RangeDriver, RangeSensorConfig


class FakeChannelBus:
    pass


class FakeMux:
    def __init__(self, channels: dict[int, FakeChannelBus]):
        self.channels = channels
        self.requested_channels: list[int] = []

    def __getitem__(self, channel: int) -> FakeChannelBus:
        self.requested_channels.append(channel)
        return self.channels[channel]


class FakeRangeSensor:
    def __init__(self, distance_mm: int, fail: bool = False):
        self.distance_mm = distance_mm
        self.fail = fail
        self.continuous = False

    def start_continuous(self) -> None:
        self.continuous = True

    def stop_continuous(self) -> None:
        self.continuous = False

    @property
    def range(self) -> int:
        if self.fail:
            raise OSError("sensor read failed")
        return self.distance_mm


class FakeL1XSensor:
    def __init__(self, distance_mm: int, fail: bool = False, no_reading: bool = False):
        self.distance_mm = distance_mm
        self.fail = fail
        self.no_reading = no_reading
        self.ranging = False
        self.distance_mode = None
        self.timing_budget = None
        self.data_ready_values = [True]
        self.interrupt_cleared = False

    def start_ranging(self) -> None:
        self.ranging = True

    def stop_ranging(self) -> None:
        self.ranging = False

    def clear_interrupt(self) -> None:
        self.interrupt_cleared = True

    @property
    def data_ready(self) -> bool:
        if len(self.data_ready_values) > 1:
            return self.data_ready_values.pop(0)
        return self.data_ready_values[0]

    @data_ready.setter
    def data_ready(self, value: bool) -> None:
        self.data_ready_values = [value]

    @property
    def distance(self) -> float | None:
        if self.fail:
            raise OSError("sensor read failed")
        return None if self.no_reading else self.distance_mm / 10


class RangeDriverTest(unittest.TestCase):
    def _make_driver(self, configs, channel_distances, fail_channels=None):
        fail_channels = fail_channels or set()
        channels = {
            config.channel: FakeChannelBus()
            for config in configs
        }
        mux = FakeMux(channels)

        def mux_factory(i2c, address):
            self.assertEqual(address, 0x70)
            return mux

        sensors_by_channel = {}
        for config in configs:
            distance = channel_distances[config.channel]
            sensors_by_channel[config.channel] = FakeRangeSensor(
                distance,
                fail=config.channel in fail_channels,
            )
        self.sensors_by_channel = sensors_by_channel

        def vl53l0x_factory(channel_bus, address):
            self.assertEqual(address, 0x29)
            for channel, bus in channels.items():
                if bus is channel_bus:
                    return sensors_by_channel[channel]
            raise AssertionError("unknown channel bus")

        return RangeDriver(
            configs,
            i2c_factory=lambda: object(),
            mux_factory=mux_factory,
            vl53l0x_factory=vl53l0x_factory,
        ), mux

    def test_read_returns_distance_from_fake_sensor(self):
        configs = [RangeSensorConfig("cliff_left", "vl53l0x", 0)]
        driver, _mux = self._make_driver(configs, {0: 123})

        reading = driver.read("cliff_left")

        self.assertTrue(reading.ok)
        self.assertEqual(reading.distance_mm, 123)
        self.assertEqual(reading.kind, "vl53l0x")
        self.assertEqual(reading.channel, 0)

    def test_read_all_follows_configured_sensor_order(self):
        configs = [
            RangeSensorConfig("cliff_left", "vl53l0x", 0),
            RangeSensorConfig("cliff_center", "vl53l0x", 1),
            RangeSensorConfig("cliff_right", "vl53l0x", 2),
        ]
        driver, mux = self._make_driver(configs, {0: 100, 1: 200, 2: 300})

        readings = driver.read_all()

        self.assertEqual([reading.name for reading in readings], [
            "cliff_left",
            "cliff_center",
            "cliff_right",
        ])
        self.assertEqual([reading.distance_mm for reading in readings], [100, 200, 300])
        self.assertEqual(mux.requested_channels, [0, 1, 2])

    def test_read_failure_on_one_channel_does_not_block_others(self):
        configs = [
            RangeSensorConfig("cliff_left", "vl53l0x", 0),
            RangeSensorConfig("cliff_center", "vl53l0x", 1),
            RangeSensorConfig("cliff_right", "vl53l0x", 2),
        ]
        driver, _mux = self._make_driver(
            configs,
            {0: 50, 1: 60, 2: 70},
            fail_channels={1},
        )

        readings = driver.read_all()

        self.assertTrue(readings[0].ok)
        self.assertEqual(readings[0].distance_mm, 50)
        self.assertFalse(readings[1].ok)
        self.assertIsNone(readings[1].distance_mm)
        self.assertTrue(readings[2].ok)
        self.assertEqual(readings[2].distance_mm, 70)

    def test_offset_is_subtracted_from_reading(self):
        configs = [RangeSensorConfig("cliff_left", "vl53l0x", 0, offset_mm=20)]
        driver, _mux = self._make_driver(configs, {0: 120})

        reading = driver.read("cliff_left")

        self.assertEqual(reading.distance_mm, 100)

    def test_offset_never_produces_negative_distance(self):
        configs = [RangeSensorConfig("cliff_left", "vl53l0x", 0, offset_mm=50)]
        driver, _mux = self._make_driver(configs, {0: 10})

        reading = driver.read("cliff_left")

        self.assertEqual(reading.distance_mm, 0)

    def test_continuous_mode_started_and_stopped(self):
        configs = [RangeSensorConfig("cliff_left", "vl53l0x", 0)]
        driver, _mux = self._make_driver(configs, {0: 100})

        self.assertTrue(self.sensors_by_channel[0].continuous)

        driver.cleanup()

        self.assertFalse(self.sensors_by_channel[0].continuous)

    def test_vl53l1x_reads_distance_in_centimeters(self):
        channels = {6: FakeChannelBus()}
        mux = FakeMux(channels)
        sensor = FakeL1XSensor(420)

        driver = RangeDriver(
            [RangeSensorConfig("forward", "vl53l1x", 6)],
            i2c_factory=lambda: object(),
            mux_factory=lambda i2c, address: mux,
            vl53l1x_factory=lambda bus, address: sensor,
        )

        reading = driver.read("forward")

        self.assertTrue(reading.ok)
        self.assertEqual(reading.distance_mm, 420)
        self.assertTrue(sensor.ranging)
        self.assertEqual(sensor.distance_mode, 1)
        self.assertEqual(sensor.timing_budget, 50)
        self.assertEqual(sensor.roi_xy, (16, 2))
        self.assertEqual(sensor.roi_center, 198)
        self.assertTrue(sensor.interrupt_cleared)

        driver.cleanup()

        self.assertFalse(sensor.ranging)

    def test_vl53l1x_out_of_range_is_ok_with_no_distance(self):
        channels = {6: FakeChannelBus()}
        mux = FakeMux(channels)
        sensor = FakeL1XSensor(420)

        driver = RangeDriver(
            [RangeSensorConfig("forward", "vl53l1x", 6)],
            i2c_factory=lambda: object(),
            mux_factory=lambda i2c, address: mux,
            vl53l1x_factory=lambda bus, address: sensor,
        )

        sensor.no_reading = True
        reading = driver.read("forward")

        self.assertTrue(reading.ok)
        self.assertIsNone(reading.distance_mm)
        self.assertTrue(sensor.interrupt_cleared)

    def test_vl53l1x_not_ready_without_previous_reading_fails(self):
        channels = {6: FakeChannelBus()}
        mux = FakeMux(channels)
        sensor = FakeL1XSensor(420)

        driver = RangeDriver(
            [RangeSensorConfig("forward", "vl53l1x", 6)],
            i2c_factory=lambda: object(),
            mux_factory=lambda i2c, address: mux,
            vl53l1x_factory=lambda bus, address: sensor,
        )

        sensor.data_ready = False
        with patch("drivers.range.time.sleep") as sleep:
            reading = driver.read("forward")

        self.assertFalse(reading.ok)
        self.assertIsNone(reading.distance_mm)
        self.assertFalse(sensor.interrupt_cleared)
        sleep.assert_called_once_with(0.005)

    def test_vl53l1x_not_ready_returns_previous_reading(self):
        channels = {6: FakeChannelBus()}
        mux = FakeMux(channels)
        sensor = FakeL1XSensor(420)

        driver = RangeDriver(
            [RangeSensorConfig("forward", "vl53l1x", 6)],
            i2c_factory=lambda: object(),
            mux_factory=lambda i2c, address: mux,
            vl53l1x_factory=lambda bus, address: sensor,
        )

        self.assertEqual(driver.read("forward").distance_mm, 420)

        sensor.distance_mm = 500
        sensor.interrupt_cleared = False
        sensor.data_ready = False
        with patch("drivers.range.time.sleep") as sleep:
            reading = driver.read("forward")

        self.assertTrue(reading.ok)
        self.assertEqual(reading.distance_mm, 420)
        self.assertFalse(sensor.interrupt_cleared)
        sleep.assert_called_once_with(0.005)

    def test_vl53l1x_rechecks_ready_data_once(self):
        channels = {6: FakeChannelBus()}
        mux = FakeMux(channels)
        sensor = FakeL1XSensor(420)

        driver = RangeDriver(
            [RangeSensorConfig("forward", "vl53l1x", 6)],
            i2c_factory=lambda: object(),
            mux_factory=lambda i2c, address: mux,
            vl53l1x_factory=lambda bus, address: sensor,
        )

        sensor.data_ready_values = [False, True]
        with patch("drivers.range.time.sleep") as sleep:
            reading = driver.read("forward")

        self.assertTrue(reading.ok)
        self.assertEqual(reading.distance_mm, 420)
        self.assertTrue(sensor.interrupt_cleared)
        sleep.assert_called_once_with(0.005)

    def test_unknown_kind_raises_at_init(self):
        configs = [RangeSensorConfig("bad", "vl53l99", 0)]
        channels = {0: FakeChannelBus()}
        mux = FakeMux(channels)

        with self.assertRaises(ValueError):
            RangeDriver(
                configs,
                i2c_factory=lambda: object(),
                mux_factory=lambda i2c, address: mux,
                vl53l0x_factory=lambda bus, address: FakeRangeSensor(1),
            )


if __name__ == "__main__":
    unittest.main()
