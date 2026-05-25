import os
import sys
import tempfile
import unittest
import logging
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.teleop import DriveTuning
from gamepad_teleop import GamepadTeleopRunner, TeleopConfig

logging.getLogger("gamepad-teleop").disabled = True


class FakeController:
    def __init__(self, state, connects=True):
        self.state = state
        self.connects = connects
        self.on_disconnect = None
        self.cleaned_up = False
        self.reader_is_alive = True
        self.disconnect_reason = None

    def connect(self):
        return self.connects

    def start(self, on_disconnect=None):
        self.on_disconnect = on_disconnect

    def reader_alive(self):
        return self.reader_is_alive

    def cleanup(self):
        self.cleaned_up = True


def controller_state(**overrides):
    state = SimpleNamespace(
        left_stick_x=0.0,
        left_stick_y=0.0,
        right_stick_x=0.0,
        right_stick_y=0.0,
        left_trigger=0.0,
        right_trigger=0.0,
        dpad_x=0,
        dpad_y=0,
        a=False,
        b=False,
        x=False,
        y=False,
        lb=False,
        rb=False,
        back=False,
        start=False,
        guide=False,
        left_stick_click=False,
        right_stick_click=False,
    )
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


def fast_config(**overrides):
    return TeleopConfig(qpps=1000, drive_tuning=DriveTuning(qpps_slew_limit=1000000.0), **overrides)


class FakeMotor:
    def __init__(self, fail_nonzero=False, results=None, read_fails=False):
        self.fail_nonzero = fail_nonzero
        self.results = list(results or [])
        self.read_fails = read_fails
        self.commands = []
        self.cleaned_up = False

    def set_wheel_speeds(self, left_qpps, right_qpps):
        self.commands.append((left_qpps, right_qpps))
        if self.results:
            return self.results.pop(0)
        if self.fail_nonzero and (left_qpps != 0 or right_qpps != 0):
            return False
        return True

    def read_wheel_speeds(self):
        if self.read_fails:
            raise RuntimeError("read failed")
        return 230, 240

    def get_battery_voltage(self):
        if self.read_fails:
            raise RuntimeError("battery failed")
        return 11.7

    def get_currents(self):
        if self.read_fails:
            raise RuntimeError("current failed")
        return 1.2, 1.1

    def read_max_qpps(self):
        if self.read_fails:
            raise RuntimeError("max qpps failed")
        return 11180, 11190

    def stop(self):
        self.commands.append(("duty", 0, 0))

    def cleanup(self):
        self.cleaned_up = True


class FakeIntentBridge:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.started = False
        self.stopped = False
        self.pending = None

    def start(self):
        self.started = True
        with open(self.socket_path, "w"):
            pass

    def stop(self):
        self.stopped = True
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def take_pending(self):
        pending = self.pending
        self.pending = None
        return pending


class GamepadTeleopRunnerTest(unittest.TestCase):
    def test_slew_limits_wheel_target_changes(self):
        runner = GamepadTeleopRunner(TeleopConfig(qpps=1000, loop_interval=0.05), sleep=lambda _seconds: None)

        first = runner._slew_target(SimpleNamespace(left_qpps=1000, right_qpps=1000), now=1.0)
        second = runner._slew_target(SimpleNamespace(left_qpps=1000, right_qpps=1000), now=1.05)

        self.assertEqual(first.left_qpps, 250)
        self.assertEqual(second.left_qpps, 500)

    def test_slew_smooths_normal_to_turbo_transition(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        current_time = 0.0
        sleeps = 0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time, sleeps
            sleeps += 1
            current_time += 0.05
            if sleeps == 1:
                state.lb = True
            elif sleeps == 3:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            TeleopConfig(qpps=1000, drive_tuning=DriveTuning(qpps_slew_limit=5000.0)),
            sleep=sleep,
            clock=clock,
        )

        runner._run_connected(controller, motor)

        self.assertEqual(motor.commands[:3], [(250, 250), (500, 500), (750, 750)])

    def test_deadman_release_sends_zero_speed(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                state.rb = False
            else:
                runner.request_stop()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertIn((250, 250), motor.commands)
        self.assertIn((0, 0), motor.commands)

    def test_disconnect_sends_zero_speed(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()

        def sleep(_seconds):
            controller.on_disconnect()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertEqual(motor.commands[-1], (0, 0))

    def test_dead_controller_reader_sends_zero_speed(self):
        state = controller_state(left_stick_y=0.0, right_stick_x=1.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            controller.reader_is_alive = False
            controller.disconnect_reason = "controller input reader crashed: fake"
            if sleeps > 2:
                runner.request_stop()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertIn((250, -250), motor.commands)
        self.assertEqual(motor.commands[-1], (0, 0))

    def test_failed_speed_command_sends_zero_speed(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor(fail_nonzero=True)
        runner = GamepadTeleopRunner(fast_config(), sleep=lambda _seconds: None)

        runner._run_connected(controller, motor)

        self.assertEqual(motor.commands, [(250, 250), (0, 0)])

    def test_steady_motion_is_heartbeated_to_roboclaw(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                runner.request_stop()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertGreaterEqual(motor.commands.count((250, 250)), 2)
        self.assertEqual(motor.commands[-1], (0, 0))

    def test_idle_after_motion_releases_to_zero_duty_once(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        current_time = 0.0
        sleeps = 0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time, sleeps
            sleeps += 1
            current_time += 0.1
            if sleeps == 1:
                state.rb = False
            elif sleeps == 5:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(idle_release_delay=0.2),
            sleep=sleep,
            clock=clock,
        )

        runner._run_connected(controller, motor)

        self.assertEqual(motor.commands.count((250, 250)), 1)
        self.assertEqual(motor.commands.count((0, 0)), 2)
        self.assertEqual(motor.commands.count(("duty", 0, 0)), 1)

    def test_run_forever_retries_failed_initial_zero_before_motion(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        failed_motor = FakeMotor(results=[False])
        ready_motor = FakeMotor()
        motors = iter([failed_motor, ready_motor])

        def sleep(_seconds):
            if ready_motor.commands:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(),
            controller_factory=lambda: controller,
            motor_factory=lambda: next(motors),
            sleep=sleep,
        )

        runner.run_forever()

        self.assertEqual(failed_motor.commands, [(0, 0)])
        self.assertEqual(ready_motor.commands[0], (0, 0))
        self.assertIn((250, 250), ready_motor.commands)

    def test_run_forever_waits_for_controller(self):
        state = controller_state(left_stick_y=0.0, right_stick_x=0.0, rb=False, lb=False)
        missing_controller = FakeController(state, connects=False)
        controller = FakeController(state)
        motor = FakeMotor()
        controllers = iter([missing_controller, controller])

        def sleep(_seconds):
            if motor.commands:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(),
            controller_factory=lambda: next(controllers),
            motor_factory=lambda: motor,
            sleep=sleep,
        )

        runner.run_forever()

        self.assertFalse(missing_controller.cleaned_up)
        self.assertIn((0, 0), motor.commands)

    def test_telemetry_publish_includes_controller_wheels_and_motor_reads(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False, a=True)
        controller = FakeController(state)
        motor = FakeMotor()
        published = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.2
            if published:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(telemetry_socket="/tmp/test.sock"),
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda socket_path, message: published.append((socket_path, message)) or True,
        )

        runner._run_connected(controller, motor)

        socket_path, message = published[0]
        self.assertEqual(socket_path, "/tmp/test.sock")
        self.assertTrue(message["controller"]["buttons"]["a"])
        self.assertEqual(message["wheels"]["left_target_qpps"], 250)
        self.assertEqual(message["wheels"]["left_actual_qpps"], 230)
        self.assertEqual(message["wheels"]["left_max_qpps"], 11180)
        self.assertEqual(message["wheels"]["left_current_amps"], 1.2)
        self.assertEqual(message["motor_battery"]["pack_voltage"], 11.7)
        self.assertEqual(message["link_loop"]["read_success_rate"], 1.0)
        self.assertEqual(message["link_loop"]["consecutive_read_failures"], 0)
        self.assertIsNotNone(message["link_loop"]["telemetry_latency_ms"])
        self.assertIsNotNone(message["link_loop"]["command_loop_hz"])
        self.assertEqual(message["drive_status"]["state"], "driving")
        self.assertTrue(message["drive_status"]["controller_reader_alive"])
        self.assertTrue(message["drive_status"]["motor_command_ok"])
        self.assertEqual(message["drive_status"]["consecutive_motor_command_failures"], 0)
        self.assertIsNotNone(message["drive_status"]["last_motor_command_ack_age_seconds"])
        self.assertEqual(message["drive_status"]["telemetry_publish_failures"], 0)
        self.assertEqual(message["drive_tuning"]["speed_scale"], 0.25)

    def test_optional_telemetry_read_failure_does_not_stop_driving(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor(read_fails=True)
        published = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.2
            if published:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(),
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda _socket_path, message: published.append(message) or True,
        )

        runner._run_connected(controller, motor)

        self.assertGreaterEqual(motor.commands.count((250, 250)), 2)
        self.assertFalse(published[0]["wheels"]["read_ok"])
        self.assertIsNone(published[0]["motor_battery"]["pack_voltage"])
        self.assertEqual(published[0]["link_loop"]["read_success_rate"], 0.0)
        self.assertEqual(published[0]["link_loop"]["consecutive_read_failures"], 1)
        self.assertTrue(published[0]["drive_status"]["motor_command_ok"])

    def test_telemetry_publish_failure_does_not_stop_driving(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        current_time = 0.0
        attempts = 0

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.2
            if attempts:
                runner.request_stop()

        def publish(_socket_path, _message):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("hub unavailable")

        runner = GamepadTeleopRunner(
            fast_config(),
            sleep=sleep,
            clock=clock,
            telemetry_publisher=publish,
        )

        runner._run_connected(controller, motor)

        self.assertGreaterEqual(motor.commands.count((250, 250)), 2)

    def test_telemetry_publish_false_is_counted_in_next_status(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        current_time = 0.0
        published = []

        def clock():
            return current_time

        def sleep(_seconds):
            nonlocal current_time
            current_time += 0.2
            if len(published) >= 1:
                runner.request_stop()

        def publish(_socket_path, message):
            published.append(message)
            return len(published) > 1

        runner = GamepadTeleopRunner(
            fast_config(),
            sleep=sleep,
            clock=clock,
            telemetry_publisher=publish,
        )

        runner._run_connected(controller, motor)

        self.assertGreaterEqual(len(published), 2)
        self.assertEqual(published[-1]["drive_status"]["telemetry_publish_failures"], 1)
        self.assertFalse(published[-1]["drive_status"]["last_telemetry_publish_ok"])

    def test_motor_command_failure_publishes_drive_status(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor(fail_nonzero=True)
        published = []
        runner = GamepadTeleopRunner(
            fast_config(),
            sleep=lambda _seconds: None,
            telemetry_publisher=lambda _socket_path, message: published.append(message) or True,
        )

        runner._run_connected(controller, motor)

        self.assertEqual(published[-1]["drive_status"]["state"], "motor_command_failed")
        self.assertEqual(
            published[-1]["drive_status"]["stop_reason"],
            "RoboClaw speed command was not acknowledged",
        )
        self.assertFalse(published[-1]["drive_status"]["motor_command_ok"])
        self.assertEqual(published[-1]["drive_status"]["consecutive_motor_command_failures"], 1)

    def test_sleep_with_status_updates_publishes_during_retry_sleep(self):
        published = []
        current_time = 0.0

        def clock():
            return current_time

        def sleep(seconds):
            nonlocal current_time
            current_time += seconds

        runner = GamepadTeleopRunner(
            fast_config(),
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda _socket_path, message: published.append(message) or True,
        )

        runner._sleep_with_status_updates(1.0)

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["drive_status"]["state"], "stopped")

    def test_intent_bridge_rebinds_when_socket_path_disappears(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "motion.sock")
            bridges = []

            def bridge_factory():
                bridge = FakeIntentBridge(socket_path)
                bridges.append(bridge)
                return bridge

            runner = GamepadTeleopRunner(
                fast_config(motion_intent_socket=socket_path),
                intent_bridge_factory=bridge_factory,
                sleep=lambda _seconds: None,
            )

            runner._ensure_intent_bridge()
            runner._ensure_intent_bridge()
            os.unlink(socket_path)
            runner._ensure_intent_bridge()

            self.assertEqual(len(bridges), 2)
            self.assertTrue(bridges[0].stopped)
            self.assertTrue(bridges[1].started)

    def test_waiting_for_controller_rejects_motion_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = []
            bridge = FakeIntentBridge(os.path.join(tmpdir, "motion.sock"))
            bridge.pending = ("wiggle", result.append)
            runner = GamepadTeleopRunner(fast_config(), sleep=lambda _seconds: None)
            runner.intent_bridge = bridge
            runner._set_drive_state("waiting_for_controller")

            runner._reject_intent_requests_while_not_driving()

            self.assertEqual(result, [{"ok": False, "error": "waiting_for_controller"}])


if __name__ == "__main__":
    unittest.main()
