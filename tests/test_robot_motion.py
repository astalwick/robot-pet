import os
import sys
import logging
import unittest
from collections import deque

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import SafetyConfig, SensorEntry, SensorsConfig
from control.commands import MotionCommand, WheelSpeedCommand
from control.motion_drive import DriveCommand
from control.safety_gate import SafetyState
from robot_motion import ENCODER_COUNTS_PER_METER, EncoderMove, MotionConfig, MotionRunner

logging.getLogger("robot-motion").disabled = True


class FakeMotor:
    def __init__(self):
        self.commands = []
        self.telemetry_reads = []
        # Encoder positions handed back one per read; once drained the last value
        # repeats, so a stalled move keeps reporting the same counts. (None, None)
        # simulates a recoverable read failure.
        self.positions = deque()
        self.last_position = (0, 0)
        self.position_calls = 0

    def set_wheel_speeds(self, left_qpps, right_qpps):
        self.commands.append((left_qpps, right_qpps))
        return True

    def read_wheel_positions(self):
        self.position_calls += 1
        if self.positions:
            self.last_position = self.positions.popleft()
        return self.last_position

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
        # A huge slew limit lets one tick reach the target, isolating the qpps clamp.
        runner = MotionRunner(
            MotionConfig(qpps=1000, loop_interval=0.05, qpps_slew_limit=10_000_000.0),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: runner.request_stop(),
            clock=lambda: 0.0,
            telemetry_publisher=lambda *_args: True,
        )
        runner._on_drive_command(drive_command(999999, -999999))

        runner._run_motor_loop(motor)

        self.assertIn((1000, -1000), motor.commands)

    def test_apply_slew_ramps_toward_target_over_ticks(self):
        # qpps_slew_limit 5000 over a 0.05s tick is 250 qpps per step, so a 600 qpps
        # target is reached over three ticks rather than snapping there at once.
        runner = self._runner(FakeMotor())
        self.assertEqual(runner._apply_slew(600, 600, 0.0).left_qpps, 250)
        self.assertEqual(runner._apply_slew(600, 600, 0.05).left_qpps, 500)
        self.assertEqual(runner._apply_slew(600, 600, 0.10).left_qpps, 600)

    def test_apply_slew_no_slew_snaps_immediately(self):
        # An explicit stop or safety block must halt now, not ramp down.
        runner = self._runner(FakeMotor())
        runner._apply_slew(600, 600, 0.0)
        snapped = runner._apply_slew(0, 0, 0.05, no_slew=True)
        self.assertEqual((snapped.left_qpps, snapped.right_qpps), (0, 0))

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

    def test_motor_reconnect_publishes_fresh_battery_before_retained_voltage(self):
        motor = FakeMotor()
        published = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.1
            if current_time > 0.15:
                runner.request_stop()

        runner = MotionRunner(
            MotionConfig(loop_interval=0.05, telemetry_interval=0.1),
            motor_factory=lambda: motor,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda _socket, message: published.append(message) or True,
        )
        runner._last_pack_voltage = 10.4
        runner._telemetry_read_slot = 0
        runner._on_drive_command(drive_command(100, 100))

        runner._run_motor_loop(motor)

        self.assertEqual(motor.telemetry_reads[0], "battery")
        self.assertEqual(published[0]["motor_battery"]["pack_voltage"], 11.5)

    def _odometry_runner(self, motor):
        return MotionRunner(
            MotionConfig(loop_interval=0.05),
            motor_factory=lambda: motor,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
            telemetry_publisher=lambda _socket, _message: True,
        )

    def test_odometry_first_read_sets_baseline_without_distance(self):
        motor = FakeMotor()
        motor.positions.extend([(1000, 1000)])
        runner = self._odometry_runner(motor)

        self.assertTrue(runner._accumulate_odometry(motor))
        payload = runner._odometry_payload()
        self.assertEqual(payload, {"left_distance_m": 0.0, "right_distance_m": 0.0})

    def test_odometry_accumulates_signed_per_wheel_distance(self):
        motor = FakeMotor()
        # Baseline, then a spin in place: left forward, right backward.
        motor.positions.extend([(0, 0), (500, -500)])
        runner = self._odometry_runner(motor)

        runner._accumulate_odometry(motor)
        runner._accumulate_odometry(motor)

        payload = runner._odometry_payload()
        self.assertAlmostEqual(payload["left_distance_m"], 500 / ENCODER_COUNTS_PER_METER, places=4)
        self.assertAlmostEqual(payload["right_distance_m"], -500 / ENCODER_COUNTS_PER_METER, places=4)

    def test_odometry_corrects_counter_wraparound(self):
        motor = FakeMotor()
        span = 1 << 32
        # Cross the 32-bit boundary forward: 150 counts of real travel.
        motor.positions.extend([(span - 100, span - 100), (50, 50)])
        runner = self._odometry_runner(motor)

        runner._accumulate_odometry(motor)
        runner._accumulate_odometry(motor)

        payload = runner._odometry_payload()
        self.assertAlmostEqual(payload["left_distance_m"], 150 / ENCODER_COUNTS_PER_METER, places=4)

    def test_odometry_unavailable_until_first_successful_read(self):
        motor = FakeMotor()
        motor.positions.extend([(None, None)])
        runner = self._odometry_runner(motor)

        self.assertFalse(runner._accumulate_odometry(motor))
        self.assertIsNone(runner._odometry_payload())

    def test_odometry_read_failure_rebaselines_without_folding_the_gap(self):
        motor = FakeMotor()
        # Good baseline at 0, drive to 500, then a read fails, then a good read
        # lands far away (1500) — the travel during the gap must be dropped,
        # not folded into one delta that the dashboard would draw as a jump.
        motor.positions.extend([(0, 0), (500, 500), (None, None), (1500, 1500)])
        runner = self._odometry_runner(motor)

        runner._accumulate_odometry(motor)  # baseline
        runner._accumulate_odometry(motor)  # +500
        self.assertFalse(runner._accumulate_odometry(motor))  # failure
        self.assertIsNone(runner._odometry_payload())  # freshness lost

        runner._accumulate_odometry(motor)  # re-baseline at 1500, no delta
        payload = runner._odometry_payload()
        self.assertAlmostEqual(payload["left_distance_m"], 500 / ENCODER_COUNTS_PER_METER, places=4)

    def test_odometry_rebaselines_after_reconnect_without_folding_the_gap(self):
        motor = FakeMotor()
        # Good baseline at 0, drive to 500. Then the RoboClaw reconnects and the
        # next read reports a low count (a reboot reset the counter). The drop
        # must not be diffed against the stale baseline as a huge reverse jump.
        motor.positions.extend([(0, 0), (500, 500), (10, 10)])
        runner = self._odometry_runner(motor)

        runner._accumulate_odometry(motor)  # baseline
        runner._accumulate_odometry(motor)  # +500

        runner._invalidate_odometry_baseline()  # what a reconnect does
        self.assertIsNone(runner._odometry_payload())

        runner._accumulate_odometry(motor)  # re-baseline at 10, no delta
        payload = runner._odometry_payload()
        self.assertAlmostEqual(payload["left_distance_m"], 500 / ENCODER_COUNTS_PER_METER, places=4)

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
        runner.intent_executor.start("move", now=0.0, distance_meters=0.1)
        runner.pending_intent_complete = completed.append
        motor.positions = deque([(0, 0), (50, 50), (100, 100), (200, 200)])

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
        runner._sensors_live = True
        runner._imu_yaw = 0.0

        runner._service_intent_requests(now=1.0)
        command = runner._tick_intent(now=1.1, gamepad_active=False)

        self.assertEqual(command.linear_x, 0.0)
        self.assertLess(command.angular_z, 0.0)

    def test_motion_intent_ignores_gamepad_drive_tuning(self):
        runner = self._runner(FakeMotor())
        drive = drive_command()
        drive.drive_tuning["speed_scale"] = 1.0
        drive.drive_tuning["turbo_scale"] = 1.0
        drive.controller["buttons"]["lb"] = True

        _, left_qpps, right_qpps = runner._target_from_drive_or_intent(
            drive,
            MotionCommand(linear_x=0.0, angular_z=0.3),
        )

        self.assertEqual(left_qpps, 181)
        self.assertEqual(right_qpps, -181)

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
        runner.intent_executor.start("move", now=0.0, distance_meters=0.5)
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
        runner.intent_executor.start("express", now=0.0, kind="wiggle")
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
        runner.intent_executor.start("express", now=0.0, kind="wiggle")
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
        runner.intent_executor.start("express", now=0.0, kind="wiggle")
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
            # Yaw advances with time so the turn closes its loop on real rotation.
            runner._imu_yaw = min(current_time * 40.0, 32.0)
            runner._imu_yaw_time = current_time
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
        runner._sensors_live = True
        runner._imu_yaw = 0.0
        runner._imu_yaw_time = 0.0

        runner._run_motor_loop(motor)

        self.assertIn({"ok": True, "result": "completed"}, completed)

    def _move_runner(self, motor, distance, completed):
        runner = self._runner(motor)
        runner.intent_executor.start("move", now=0.0, distance_meters=distance)
        runner.pending_intent_complete = completed.append
        return runner

    def test_encoder_move_snapshots_start_before_checking_travel(self):
        motor = FakeMotor()
        motor.positions = deque([(1000, 1000)])
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        stopped = runner._encoder_move_should_stop(motor, 0.0, True, SafetyState(blocked=False))

        self.assertFalse(stopped)
        self.assertEqual(completed, [])
        self.assertEqual(runner.encoder_move.left_start, 1000)
        self.assertEqual(runner.encoder_move.right_start, 1000)

    def test_encoder_move_completes_at_target_travel(self):
        motor = FakeMotor()
        target = 0.1 * ENCODER_COUNTS_PER_METER
        motor.positions = deque([(0, 0), (target, target)])
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        self.assertFalse(runner._encoder_move_should_stop(motor, 0.0, True, SafetyState(blocked=False)))
        self.assertTrue(runner._encoder_move_should_stop(motor, 0.1, True, SafetyState(blocked=False)))

        self.assertEqual(completed, [{"ok": True, "result": "completed"}])
        self.assertIsNone(runner.encoder_move)
        self.assertFalse(runner.intent_executor.is_active())

    def test_encoder_move_completes_reverse_on_absolute_travel(self):
        motor = FakeMotor()
        target = 0.1 * ENCODER_COUNTS_PER_METER
        motor.positions = deque([(0, 0), (-target, -target)])
        completed = []
        runner = self._move_runner(motor, -0.1, completed)

        self.assertFalse(runner._encoder_move_should_stop(motor, 0.0, False, SafetyState(blocked=False)))
        self.assertTrue(runner._encoder_move_should_stop(motor, 0.1, False, SafetyState(blocked=False)))

        self.assertEqual(completed, [{"ok": True, "result": "completed"}])

    def test_encoder_move_reverse_handles_unsigned_wraparound(self):
        motor = FakeMotor()
        target = 0.1 * ENCODER_COUNTS_PER_METER
        motor.positions = deque([
            (0, 0),
            ((1 << 32) - 10, (1 << 32) - 10),
            ((1 << 32) - target, (1 << 32) - target),
        ])
        completed = []
        runner = self._move_runner(motor, -0.1, completed)

        self.assertFalse(runner._encoder_move_should_stop(motor, 0.0, False, SafetyState(blocked=False)))
        self.assertFalse(runner._encoder_move_should_stop(motor, 0.1, False, SafetyState(blocked=False)))
        self.assertTrue(runner._encoder_move_should_stop(motor, 0.2, False, SafetyState(blocked=False)))

        self.assertEqual(completed, [{"ok": True, "result": "completed"}])

    def test_encoder_move_fails_when_start_read_fails(self):
        motor = FakeMotor()
        motor.positions = deque([(None, None)])
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        self.assertTrue(runner._encoder_move_should_stop(motor, 0.0, True, SafetyState(blocked=False)))

        self.assertEqual(completed, [{"ok": False, "error": "encoder_read_failed"}])
        self.assertIsNone(runner.encoder_move)
        self.assertFalse(runner.intent_executor.is_active())

    def test_encoder_move_fails_when_mid_read_fails(self):
        motor = FakeMotor()
        motor.positions = deque([(0, 0), (None, None)])
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        self.assertFalse(runner._encoder_move_should_stop(motor, 0.0, True, SafetyState(blocked=False)))
        self.assertTrue(runner._encoder_move_should_stop(motor, 0.1, True, SafetyState(blocked=False)))

        self.assertEqual(completed, [{"ok": False, "error": "encoder_read_failed"}])

    def test_encoder_move_fails_forward_on_safety_block(self):
        motor = FakeMotor()
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        stopped = runner._encoder_move_should_stop(
            motor, 0.0, True, SafetyState(blocked=True, reason="cliff_left_cliff")
        )

        self.assertTrue(stopped)
        self.assertEqual(completed, [{"ok": False, "error": "safety_blocked"}])
        self.assertEqual(motor.position_calls, 0)

    def test_encoder_move_reverse_survives_safety_block(self):
        motor = FakeMotor()
        motor.positions = deque([(0, 0)])
        completed = []
        runner = self._move_runner(motor, -0.1, completed)

        stopped = runner._encoder_move_should_stop(
            motor, 0.0, False, SafetyState(blocked=True, reason="cliff_left_cliff")
        )

        self.assertFalse(stopped)
        self.assertEqual(completed, [])
        self.assertIsNotNone(runner.encoder_move)

    def test_encoder_move_fails_on_no_progress(self):
        motor = FakeMotor()
        motor.positions = deque([(0, 0)])
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        self.assertFalse(runner._encoder_move_should_stop(motor, 0.0, True, SafetyState(blocked=False)))
        self.assertFalse(runner._encoder_move_should_stop(motor, 0.5, True, SafetyState(blocked=False)))
        self.assertTrue(runner._encoder_move_should_stop(motor, 1.0, True, SafetyState(blocked=False)))

        self.assertEqual(completed, [{"ok": False, "error": "encoder_no_progress"}])

    def test_encoder_move_progress_resets_no_progress_watchdog(self):
        motor = FakeMotor()
        motor.positions = deque([(0, 0), (10, 10), (20, 20)])
        completed = []
        runner = self._move_runner(motor, 0.1, completed)

        self.assertFalse(runner._encoder_move_should_stop(motor, 0.0, True, SafetyState(blocked=False)))
        self.assertFalse(runner._encoder_move_should_stop(motor, 0.9, True, SafetyState(blocked=False)))
        self.assertFalse(runner._encoder_move_should_stop(motor, 1.5, True, SafetyState(blocked=False)))

        self.assertEqual(completed, [])

    def test_gamepad_preemption_clears_encoder_move(self):
        motor = FakeMotor()
        completed = []
        runner = self._move_runner(motor, 0.1, completed)
        runner.encoder_move = EncoderMove(
            target_counts=178.3, left_start=0, right_start=0, last_travel=0.0, last_progress_at=0.0
        )

        command = runner._tick_intent(now=0.1, gamepad_active=True)

        self.assertIsNone(command)
        self.assertIsNone(runner.encoder_move)
        self.assertEqual(completed, [{"ok": False, "error": "preempted_by_gamepad"}])

    def test_stop_clears_encoder_move(self):
        motor = FakeMotor()
        completed = []
        runner = self._move_runner(motor, 0.1, completed)
        runner.encoder_move = EncoderMove(
            target_counts=178.3, left_start=0, right_start=0, last_travel=0.0, last_progress_at=0.0
        )

        class StopBridge:
            def take_stop(self):
                return True

            def discard_pending(self):
                pass

        runner.intent_bridge = StopBridge()
        runner._service_stop_requests()

        self.assertIsNone(runner.encoder_move)
        self.assertFalse(runner.intent_executor.is_active())
        self.assertEqual(completed, [{"ok": False, "error": "stopped"}])


if __name__ == "__main__":
    unittest.main()
