import os
import sys
import logging
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_battery import BatteryConfig, BatteryRunner

logging.getLogger("robot-battery").disabled = True


class FakeMosfet:
    def __init__(self):
        self.on_count = 0
        self.off_count = 0
        self.closed = False

    def on(self):
        self.on_count += 1

    def off(self):
        self.off_count += 1

    def close(self):
        self.closed = True


def snapshot(voltage, stale=False):
    return {
        "sources": {"gamepad_teleop": {"stale": stale}},
        "motor_battery": {"pack_voltage": voltage},
    }


class RobotBatteryTest(unittest.TestCase):
    def test_startup_turns_motor_rail_on(self):
        mosfet = FakeMosfet()
        published = []
        runner = BatteryRunner(
            BatteryConfig(),
            mosfet_factory=lambda *_args, **_kwargs: mosfet,
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )

        runner.run_forever()

        self.assertEqual(mosfet.on_count, 1)
        self.assertEqual(published[0]["state"], "on")

    def test_fresh_low_voltage_cuts_motor_rail_after_debounce(self):
        mosfet = FakeMosfet()
        times = iter([0.0, 1.0, 3.1])
        runner = BatteryRunner(
            BatteryConfig(low_voltage_cutoff=10.8, low_voltage_seconds=2.0),
            mosfet_factory=lambda *_args, **_kwargs: mosfet,
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.mosfet = mosfet
        runner._rail_on("test")

        runner._handle_snapshot(snapshot(10.7))
        self.assertEqual(runner.state, "warning")

        runner._handle_snapshot(snapshot(10.7))
        self.assertEqual(runner.state, "warning")

        runner._handle_snapshot(snapshot(10.7))
        self.assertEqual(runner.state, "low_battery_cutoff")
        self.assertEqual(mosfet.off_count, 1)

    def test_stale_voltage_does_not_cut_motor_rail(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            mosfet_factory=lambda *_args, **_kwargs: mosfet,
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: 10.0,
        )
        runner.mosfet = mosfet
        runner._rail_on("test")

        runner._handle_snapshot(snapshot(10.0, stale=True))

        self.assertEqual(runner.state, "on")
        self.assertEqual(mosfet.off_count, 0)

    def test_stale_voltage_resets_pending_cutoff(self):
        mosfet = FakeMosfet()
        times = iter([0.0, 3.0, 3.1])
        runner = BatteryRunner(
            BatteryConfig(low_voltage_cutoff=10.8, low_voltage_seconds=2.0),
            mosfet_factory=lambda *_args, **_kwargs: mosfet,
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.mosfet = mosfet
        runner._rail_on("test")

        runner._handle_snapshot(snapshot(10.7))
        runner._handle_snapshot(snapshot(10.7, stale=True))
        runner._handle_snapshot(snapshot(10.7))

        self.assertEqual(runner.state, "warning")
        self.assertEqual(mosfet.off_count, 0)

    def test_cutoff_logs_periodic_reminder(self):
        times = iter([30.0, 59.0, 60.0])
        runner = BatteryRunner(
            BatteryConfig(cutoff_log_interval=30.0),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.state = "low_battery_cutoff"
        runner.last_pack_voltage = 10.7

        logger = logging.getLogger("robot-battery")
        disabled = logger.disabled
        logger.disabled = False
        try:
            with self.assertLogs("robot-battery", level="WARNING") as logs:
                runner._handle_snapshot({})
                runner._handle_snapshot({})
                runner._handle_snapshot({})
        finally:
            logger.disabled = disabled

        self.assertEqual(len(logs.output), 2)
        self.assertIn("motor LiPo discharged", logs.output[0])

    def test_warning_voltage_does_not_cut_motor_rail(self):
        runner = BatteryRunner(
            BatteryConfig(low_voltage_cutoff=10.8, warning_voltage=11.1),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: 0.0,
        )

        runner._handle_snapshot(snapshot(10.9))

        self.assertEqual(runner.state, "warning")
        self.assertEqual(runner.reason, "low_battery_warning")


if __name__ == "__main__":
    unittest.main()
