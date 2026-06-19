import os
import sys
import json
import logging
import tempfile
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


def snapshot(
    voltage,
    stale=False,
    gamepad_stale=None,
    controller_connected=False,
    motion_power_requested=False,
    motion_stale=None,
):
    return {
        "sources": {
            "gamepad": {"stale": stale if gamepad_stale is None else gamepad_stale},
            "gamepad_teleop": {"stale": stale},
            "robot_motion": {"stale": stale if motion_stale is None else motion_stale},
        },
        "gamepad": {"connected": controller_connected},
        "controller": {"connected": controller_connected},
        "motor_battery": {"pack_voltage": voltage},
        "drive_status": {"motion_power_requested": motion_power_requested},
    }


class RobotBatteryTest(unittest.TestCase):
    def test_startup_keeps_motor_rail_off(self):
        mosfet = FakeMosfet()
        published = []
        runner = BatteryRunner(
            BatteryConfig(),
            mosfet_factory=lambda *_args, **_kwargs: mosfet,
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )

        runner.run_forever()

        self.assertEqual(mosfet.on_count, 0)
        self.assertEqual(published[0]["state"], "off")

    def test_gamepad_connected_turns_motor_rail_on(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, controller_connected=True))

        self.assertEqual(mosfet.on_count, 1)
        self.assertEqual(runner.state, "on")
        self.assertEqual(runner.reason, "gamepad_connected")

    def test_gamepad_disconnected_turns_motor_rail_off(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
        )
        runner.mosfet = mosfet
        runner._handle_snapshot(snapshot(None, controller_connected=True))

        runner._handle_snapshot(snapshot(None, controller_connected=False))

        self.assertEqual(mosfet.off_count, 1)
        self.assertEqual(runner.state, "off")
        self.assertEqual(runner.reason, "idle_no_gamepad")

    def test_turning_motor_rail_off_caches_last_fresh_battery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "motor-battery.json")
            mosfet = FakeMosfet()
            runner = BatteryRunner(
                BatteryConfig(motor_battery_cache_path=cache_path),
                telemetry_subscriber=lambda _socket: [],
                telemetry_publisher=lambda *_args: True,
            )
            runner.mosfet = mosfet
            runner._handle_snapshot(
                {
                    **snapshot(11.7, controller_connected=True),
                    "motor_battery": {
                        "pack_voltage": 11.7,
                        "cell_voltage": 3.9,
                        "status": "ok",
                        "percent_estimate": 60,
                    },
                }
            )

            runner._handle_snapshot(snapshot(None, controller_connected=False))

            with open(cache_path) as file_obj:
                cached = json.load(file_obj)

        self.assertEqual(cached["reason"], "idle_no_gamepad")
        self.assertEqual(cached["motor_battery"]["pack_voltage"], 11.7)
        self.assertEqual(cached["motor_battery"]["percent_estimate"], 60)

    def test_fresh_gamepad_source_turns_motor_rail_on_when_motion_telemetry_is_stale(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, stale=True, gamepad_stale=False, controller_connected=True))

        self.assertEqual(mosfet.off_count, 0)
        self.assertEqual(runner.state, "on")

    def test_motion_power_request_turns_motor_rail_on_without_gamepad(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, motion_power_requested=True))

        self.assertEqual(mosfet.on_count, 1)
        self.assertEqual(runner.state, "on")
        self.assertEqual(runner.reason, "motion_power_requested")

    def test_stale_motion_source_does_not_turn_motor_rail_on(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: 0.0,
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, motion_stale=True, motion_power_requested=True))

        self.assertEqual(mosfet.on_count, 0)
        self.assertEqual(runner.state, "off")

    def test_fresh_gamepad_voltage_does_not_trigger_cutoff(self):
        mosfet = FakeMosfet()
        times = iter([0.0, 1.0, 3.1])
        runner = BatteryRunner(
            BatteryConfig(low_voltage_cutoff=10.8, low_voltage_seconds=2.0),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.mosfet = mosfet
        runner._rail_on("test")

        for _index in range(3):
            runner._handle_snapshot(
                snapshot(10.7, motion_stale=True, gamepad_stale=False, controller_connected=True)
            )

        self.assertNotEqual(runner.state, "low_battery_cutoff")
        self.assertEqual(mosfet.off_count, 0)

    def test_low_battery_cutoff_blocks_restart(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
        )
        runner.mosfet = mosfet
        runner.state = "low_battery_cutoff"

        runner._handle_snapshot(snapshot(None, controller_connected=True, motion_power_requested=True))

        self.assertEqual(mosfet.on_count, 0)
        self.assertEqual(runner.state, "low_battery_cutoff")

    def test_last_critical_voltage_blocks_start(self):
        mosfet = FakeMosfet()
        runner = BatteryRunner(
            BatteryConfig(low_voltage_cutoff=10.8),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
        )
        runner.mosfet = mosfet
        runner.last_pack_voltage = 10.7

        runner._handle_snapshot(snapshot(None, controller_connected=True))

        self.assertEqual(mosfet.on_count, 0)
        self.assertEqual(runner.state, "low_battery_cutoff")

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

        runner._handle_snapshot(snapshot(10.7, controller_connected=True))
        self.assertEqual(runner.state, "warning")

        runner._handle_snapshot(snapshot(10.7, controller_connected=True))
        self.assertEqual(runner.state, "warning")

        runner._handle_snapshot(snapshot(10.7, controller_connected=True))
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

        runner._handle_snapshot(snapshot(10.0, stale=True, gamepad_stale=False, controller_connected=True))

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

        runner._handle_snapshot(snapshot(10.7, controller_connected=True))
        runner._handle_snapshot(snapshot(10.7, stale=True, gamepad_stale=False, controller_connected=True))
        runner._handle_snapshot(snapshot(10.7, controller_connected=True))

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

    def test_motion_power_hold_keeps_rail_on_after_request_clears(self):
        mosfet = FakeMosfet()
        times = iter([0.0, 1.0])
        runner = BatteryRunner(
            BatteryConfig(motion_power_hold_seconds=5.0),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, motion_power_requested=True))
        runner._handle_snapshot(snapshot(None, motion_power_requested=False))

        self.assertEqual(runner.state, "on")
        self.assertEqual(mosfet.off_count, 0)

    def test_motion_power_hold_expires_and_turns_rail_off(self):
        mosfet = FakeMosfet()
        times = iter([0.0, 6.0])
        runner = BatteryRunner(
            BatteryConfig(motion_power_hold_seconds=5.0),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, motion_power_requested=True))
        runner._handle_snapshot(snapshot(None, motion_power_requested=False))

        self.assertEqual(runner.state, "off")
        self.assertEqual(runner.reason, "idle_no_gamepad")
        self.assertEqual(mosfet.off_count, 1)

    def test_low_battery_cutoff_overrides_motion_power_hold(self):
        mosfet = FakeMosfet()
        times = iter([0.0, 1.0, 1.1, 3.2])
        runner = BatteryRunner(
            BatteryConfig(motion_power_hold_seconds=10.0, low_voltage_cutoff=10.8, low_voltage_seconds=2.0),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: next(times),
        )
        runner.mosfet = mosfet

        runner._handle_snapshot(snapshot(None, motion_power_requested=True))
        runner._handle_snapshot(snapshot(10.7, motion_power_requested=True))
        runner._handle_snapshot(snapshot(10.7, motion_power_requested=True))
        runner._handle_snapshot(snapshot(10.7, motion_power_requested=True))

        self.assertEqual(runner.state, "low_battery_cutoff")
        self.assertEqual(mosfet.off_count, 1)

    def test_warning_voltage_does_not_cut_motor_rail(self):
        runner = BatteryRunner(
            BatteryConfig(low_voltage_cutoff=10.8, warning_voltage=11.1),
            telemetry_subscriber=lambda _socket: [],
            telemetry_publisher=lambda *_args: True,
            clock=lambda: 0.0,
        )

        runner._handle_snapshot(snapshot(10.9, controller_connected=True))

        self.assertEqual(runner.state, "warning")
        self.assertEqual(runner.reason, "low_battery_warning")


if __name__ == "__main__":
    unittest.main()
