import os
import sys
import tempfile
import unittest
from collections import deque

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

ROBOT_DASHBOARD_IMPORT_ERROR = None
try:
    from config.drive_tuning import DriveTuning
    from robot_dashboard import RobotDashboard, fix_wraparound, sparkline
except ModuleNotFoundError as exc:
    if exc.name not in {"rich", "textual"}:
        raise
    ROBOT_DASHBOARD_IMPORT_ERROR = exc
    RobotDashboard = None
    fix_wraparound = None
    sparkline = None


@unittest.skipIf(ROBOT_DASHBOARD_IMPORT_ERROR is not None, "robot dashboard dependencies are not installed")
class RobotDashboardTest(unittest.TestCase):
    def test_fix_wraparound_handles_unsigned_speed_and_derived_error(self):
        self.assertEqual(fix_wraparound((1 << 32) - 96), -96)
        self.assertEqual(fix_wraparound(-((1 << 32) - 96)), 96)

    def test_wheel_qpps_recomputes_error_after_signed_normalization(self):
        dashboard = RobotDashboard("/tmp/missing.sock")

        target, actual, error = dashboard._wheel_qpps(
            {
                "left_target_qpps": 0,
                "left_actual_qpps": (1 << 32) - 96,
                "left_error_qpps": -((1 << 32) - 96),
            },
            "left",
        )

        self.assertEqual(target, 0)
        self.assertEqual(actual, -96)
        self.assertEqual(error, 96)

    def test_stale_gamepad_payload_does_not_pollute_history(self):
        dashboard = RobotDashboard("/tmp/missing.sock")
        snapshot = {
            "wheels": {
                "left_target_qpps": 1000,
                "left_actual_qpps": 1000,
                "right_target_qpps": 1000,
                "right_actual_qpps": 1000,
            },
            "motor_battery": {"pack_voltage": 11.9},
        }

        dashboard._record_history(snapshot, gamepad_live=False)

        self.assertEqual(len(dashboard.history["left_actual"]), 0)
        self.assertEqual(dashboard.max_abs_speed_qpps, 1.0)

    def test_invalid_drive_tuning_config_uses_defaults_with_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "drive_tuning.json")
            with open(path, "w") as file_obj:
                file_obj.write("{not json")

            dashboard = RobotDashboard("/tmp/missing.sock", path)

        self.assertEqual(dashboard.drive_tuning, DriveTuning())
        self.assertIsNotNone(dashboard.drive_tuning_error)
        self.assertIn("Invalid drive tuning config", dashboard.drive_tuning_error)

    def test_live_history_tracks_session_speed_max(self):
        dashboard = RobotDashboard("/tmp/missing.sock")

        dashboard._record_history(
            {
                "wheels": {
                    "left_target_qpps": 1000,
                    "left_actual_qpps": 750,
                    "right_target_qpps": -1200,
                    "right_actual_qpps": -900,
                },
                "motor_battery": {"pack_voltage": 11.9},
            },
            gamepad_live=True,
        )

        self.assertEqual(dashboard.max_abs_speed_qpps, 1200)

    def test_speed_sparkline_uses_fixed_limit_for_recent_slow_values(self):
        self.assertEqual(sparkline(deque([1000, 0, 10]), width=2), "▁█")
        self.assertEqual(sparkline(deque([1000, 0, 10]), width=2, limit=1000, absolute=True), "▁▁")

    def test_stale_drive_telemetry_is_hold_not_caution(self):
        dashboard = RobotDashboard("/tmp/missing.sock")

        status, notes = dashboard._drive_status(
            "stale",
            "live",
            {"connected": True},
            {"read_ok": True},
            {"status": "ok"},
            {"throttled_flags": "0x0"},
            {},
        )

        self.assertEqual(status, "hold")
        self.assertEqual(notes, ["drive telemetry stale"])

    def test_waiting_drive_state_shown_when_telemetry_is_stale(self):
        dashboard = RobotDashboard("/tmp/missing.sock")

        status, notes = dashboard._drive_status(
            "stale",
            "live",
            {"connected": False},
            {"read_ok": False},
            {"status": "unknown"},
            {"throttled_flags": "0x0"},
            {"state": "waiting_for_controller"},
        )

        self.assertEqual(status, "waiting for controller")
        self.assertEqual(notes, ["drive telemetry stale"])


if __name__ == "__main__":
    unittest.main()
