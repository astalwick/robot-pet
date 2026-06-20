import os
import sys
import logging
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import SafetyConfig, SensorEntry, SensorsConfig
from control.commands import WheelSpeedCommand
from control.motion_drive import DriveCommand
from robot_motion import MotionConfig, MotionRunner

logging.getLogger("robot-motion").disabled = True


class FakeMotor:
    def __init__(self):
        self.commands = []
        self.telemetry_reads = []

    def set_wheel_speeds(self, left_qpps, right_qpps):
        self.commands.append((left_qpps, right_qpps))
        return True

    def read_wheel_speeds(self):
        self.telemetry_reads.append("speeds")
        return 100, 100

    def get_battery_voltage(self):
        self.telemetry_reads.append("battery")
        return 11.5

    def get_currents(self):
        self.telemetry_reads.append("currents")
        return 1.0, 1.0

    def read_max_qpps(self):
        return 1000, 1000

    def stop(self):
        pass

    def cleanup(self):
        pass


def drive_command(left_qpps=0, right_qpps=0):
    return DriveCommand(
        left_qpps=left_qpps,
        right_qpps=right_qpps,
        controller={"connected": True, "buttons": {}},
        wheels={"left_command": 0.5, "right_command": 0.5},
        drive_tuning={"speed_scale": 0.25, "turbo_scale": 0.75, "turn_scale": 1.0, "qpps_slew_limit": 1000000.0},
        drive_status={"state": "driving", "controller_reader_alive": True},
        link_loop={},
    )


class RobotMotionTest(unittest.TestCase):
    def _runner(self, motor):
        runner = MotionRunner(
            MotionConfig(loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: runner.request_stop(),
            clock=lambda: 0.0,
            telemetry_publisher=lambda *_args: True,
        )
        return runner

    def test_safety_blocks_forward_qpps(self):
        motor = FakeMotor()
        runner = self._runner(motor)
        runner.sensors_config = SensorsConfig(
            safety=SafetyConfig(enabled=True, cliff_trip_above_mm=200),
            sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
        )
        runner._sensor_readings = [{"name": "cliff_left", "ok": True, "distance_mm": 300}]
        runner._sensors_live = True
        runner._on_drive_command(drive_command(250, 250))

        runner._run_motor_loop(motor)

        self.assertIn((0, 0), motor.commands)

    def test_safety_disabled_allows_forward(self):
        motor = FakeMotor()
        runner = self._runner(motor)
        runner.sensors_config = SensorsConfig(safety=SafetyConfig(enabled=False))
        runner._sensor_readings = [{"name": "cliff_left", "ok": True, "distance_mm": 300}]
        runner._sensors_live = True
        runner._on_drive_command(drive_command(250, 250))

        runner._run_motor_loop(motor)

        self.assertIn((250, 250), motor.commands)

    def test_safety_stale_sensors_remove_forward_motion(self):
        motor = FakeMotor()
        runner = self._runner(motor)
        runner.sensors_config = SensorsConfig(
            safety=SafetyConfig(enabled=True),
            sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
        )
        runner._sensors_live = False
        runner._on_drive_command(drive_command(250, 250))

        runner._run_motor_loop(motor)

        self.assertIn((0, 0), motor.commands)
        self.assertEqual(runner._last_safety_reason, "sensors_stale")

    def test_motor_commands_are_clamped_to_motion_qpps(self):
        motor = FakeMotor()
        runner = MotionRunner(
            MotionConfig(qpps=1000, loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: runner.request_stop(),
            clock=lambda: 0.0,
            telemetry_publisher=lambda *_args: True,
        )
        runner._on_drive_command(drive_command(999999, -999999))

        runner._run_motor_loop(motor)

        self.assertIn((1000, -1000), motor.commands)

    def test_telemetry_reads_rotate_across_publish_ticks(self):
        motor = FakeMotor()
        published = []
        runner = MotionRunner(
            MotionConfig(loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )
        drive = drive_command(100, 100)

        for _index in range(3):
            runner._publish_telemetry(
                drive,
                drive.wheels,
                WheelSpeedCommand(100, 100),
                motor,
                (1000, 1000),
                safety_blocked=False,
                safety_reason=None,
            )

        self.assertEqual(motor.telemetry_reads, ["speeds", "battery", "currents"])
        self.assertEqual(published[0]["source"], "robot_motion")
        self.assertEqual(published[0]["wheels"]["left_actual_qpps"], 100)
        self.assertEqual(published[1]["wheels"]["left_actual_qpps"], 100)
        self.assertEqual(published[1]["motor_battery"]["pack_voltage"], 11.5)
        self.assertEqual(published[2]["wheels"]["left_current_amps"], 1.0)

    def test_stale_drive_command_stops_motor(self):
        motor = FakeMotor()
        current_time = 0.0
        sleeps = 0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time, sleeps
            sleeps += 1
            current_time += 0.6
            if sleeps == 2:
                runner.request_stop()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda *_args: True,
        )
        runner._on_drive_command(drive_command(250, 250))

        runner._run_motor_loop(motor)

        self.assertEqual(motor.commands[:2], [(250, 250), (0, 0)])

    def test_motion_intent_runs_without_gamepad_drive_command(self):
        motor = FakeMotor()
        completed = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.1
            if current_time > 1.0:
                runner.request_stop()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda *_args: True,
        )
        runner.intent_executor.start("move_forward", now=0.0)
        runner.pending_intent_complete = completed.append

        runner._run_motor_loop(motor)

        self.assertTrue(any(left > 0 and right > 0 for left, right in motor.commands))
        self.assertEqual(completed, [{"ok": True, "result": "completed"}])

    def test_motion_service_starts_parameterized_diagnostic_turn(self):
        motor = FakeMotor()
        completed = []
        runner = self._runner(motor)

        class FakeIntentBridge:
            def take_pending(self):
                return (
                    {
                        "tool": "diagnostic_turn",
                        "direction": "toward_left_wheel",
                        "duration_seconds": 0.5,
                    },
                    completed.append,
                )

        runner.intent_bridge = FakeIntentBridge()

        runner._service_intent_requests(now=1.0)
        command = runner._tick_intent(now=1.1, gamepad_active=False)

        self.assertEqual(command.linear_x, 0.0)
        self.assertLess(command.angular_z, 0.0)

    def test_motion_service_starts_parameterized_face_me(self):
        motor = FakeMotor()
        completed = []
        runner = self._runner(motor)

        class FakeIntentBridge:
            def take_pending(self):
                return ({"tool": "face_me", "relative_degrees": 90}, completed.append)

        runner.intent_bridge = FakeIntentBridge()

        runner._service_intent_requests(now=1.0)
        command = runner._tick_intent(now=1.1, gamepad_active=False)

        self.assertEqual(command.linear_x, 0.0)
        self.assertLess(command.angular_z, 0.0)

    def test_wait_for_roboclaw_does_not_probe_without_power_reason(self):
        calls = 0

        def motor_factory():
            nonlocal calls
            calls += 1
            return FakeMotor()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, retry_interval=0.05),
            motor_factory=motor_factory,
            sleep=lambda _seconds: runner.request_stop(),
            clock=lambda: 0.0,
            telemetry_publisher=lambda *_args: True,
        )

        self.assertIsNone(runner._wait_for_roboclaw())
        self.assertEqual(calls, 0)

    def test_motion_intent_requests_power_before_waiting_for_roboclaw(self):
        motor = FakeMotor()
        published = []
        completed = []
        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, retry_interval=0.05),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: runner.request_stop(),
            clock=lambda: 0.0,
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )
        runner.intent_executor.start("move_forward", now=0.0)
        runner.pending_intent_complete = completed.append

        self.assertIs(runner._wait_for_roboclaw(), motor)

        self.assertEqual(published[0]["source"], "robot_motion")
        self.assertTrue(published[0]["drive_status"]["motion_power_requested"])
        self.assertFalse(published[0]["drive_status"]["roboclaw_ready"])
        self.assertEqual(motor.commands[0], (0, 0))

    def test_wait_for_roboclaw_restarts_intent_timer_when_motor_becomes_ready(self):
        motor = FakeMotor()
        current_time = 0.0
        attempts = 0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 1.0

        def motor_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("not ready")
            return motor

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, retry_interval=1.0),
            motor_factory=motor_factory,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda *_args: True,
        )
        runner.intent_executor.start("wiggle", now=0.0)
        runner.pending_intent_complete = lambda _result: None

        self.assertIs(runner._wait_for_roboclaw(), motor)
        command = runner._tick_intent(now=1.1, gamepad_active=False)

        self.assertEqual(command.linear_x, 0.0)
        self.assertGreater(command.angular_z, 0.0)

    def test_wait_for_roboclaw_publishes_motion_source_while_waiting(self):
        motor = FakeMotor()
        published = []
        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, retry_interval=0.05),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: runner.request_stop(),
            clock=lambda: 0.0,
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )
        runner._on_drive_command(drive_command(250, 250))

        self.assertIs(runner._wait_for_roboclaw(), motor)

        self.assertEqual(published[0]["source"], "robot_motion")
        self.assertNotIn("drive_tuning", published[0])
        self.assertFalse(published[0]["drive_status"]["roboclaw_ready"])

    def test_stuck_intent_fails_after_wait_timeout(self):
        motor = FakeMotor()
        motor.set_wheel_speeds = lambda _left, _right: False
        completed = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 2.0
            if current_time > 12.0:
                runner.request_stop()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, retry_interval=2.0, intent_wait_timeout=8.0),
            motor_factory=lambda: motor,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda *_args: True,
        )
        runner.intent_executor.start("wiggle", now=0.0)
        runner.pending_intent_complete = completed.append

        self.assertIsNone(runner._wait_for_roboclaw())

        self.assertIn({"ok": False, "error": "roboclaw_unavailable"}, completed)
        self.assertIsNone(runner.pending_intent_complete)
        self.assertFalse(runner.intent_executor.is_active())
        self.assertFalse(runner._motion_power_requested())

    def test_motion_intent_publishes_telemetry_and_reports_completed(self):
        motor = FakeMotor()
        completed = []
        published = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.1
            if current_time > 0.8:
                runner.request_stop()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, telemetry_interval=0.1),
            motor_factory=lambda: motor,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )
        runner.intent_executor.start("wiggle", now=0.0)
        runner.pending_intent_complete = completed.append

        runner._run_motor_loop(motor)

        self.assertEqual(completed, [{"ok": True, "result": "completed"}])
        self.assertTrue(published)

    def test_face_me_reports_completion_after_motion_finishes(self):
        motor = FakeMotor()
        completed = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.1
            if current_time > 2.0:
                runner.request_stop()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda *_args: True,
        )
        runner.intent_executor.start("face_me", now=0.0, relative_degrees=30)
        runner.pending_intent_complete = completed.append

        runner._run_motor_loop(motor)

        self.assertIn({"ok": True, "result": "completed"}, completed)


if __name__ == "__main__":
    unittest.main()
