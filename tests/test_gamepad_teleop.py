import os
import sys
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


class FakeMotion:
    def __init__(self, connect_results=None, fail_send=False):
        self.connect_results = list(connect_results or [True])
        self.fail_send = fail_send
        self.commands = []
        self.closed = False

    def connect(self):
        if not self.connect_results:
            return True
        return self.connect_results.pop(0)

    def send(self, command):
        if self.fail_send:
            return False
        self.commands.append((command.left_qpps, command.right_qpps))
        return True

    def close(self):
        self.closed = True


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
        motion = FakeMotion()
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

        runner._run_connected(controller, motion)

        self.assertEqual(motion.commands[:3], [(250, 250), (500, 500), (750, 750)])

    def test_deadman_release_sends_zero_speed(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                state.rb = False
            else:
                runner.request_stop()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motion)

        self.assertIn((250, 250), motion.commands)
        self.assertIn((0, 0), motion.commands)

    def test_disconnect_sends_zero_speed(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion()

        def sleep(_seconds):
            controller.on_disconnect()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motion)

        self.assertEqual(motion.commands[-1], (0, 0))

    def test_dead_controller_reader_sends_zero_speed(self):
        state = controller_state(left_stick_y=0.0, right_stick_x=1.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            controller.reader_is_alive = False
            controller.disconnect_reason = "controller input reader crashed: fake"
            if sleeps > 2:
                runner.request_stop()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motion)

        self.assertIn((250, -250), motion.commands)
        self.assertEqual(motion.commands[-1], (0, 0))

    def test_motion_send_failure_stops_loop(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion(fail_send=True)
        runner = GamepadTeleopRunner(fast_config(), sleep=lambda _seconds: None)

        runner._run_connected(controller, motion)

        self.assertEqual(motion.commands, [])
        self.assertEqual(runner.drive_state, "motion_send_failed")

    def test_steady_motion_heartbeats_drive_commands(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                runner.request_stop()

        runner = GamepadTeleopRunner(fast_config(), sleep=sleep)

        runner._run_connected(controller, motion)

        self.assertGreaterEqual(motion.commands.count((250, 250)), 2)
        self.assertEqual(motion.commands[-1], (0, 0))

    def test_run_forever_retries_motion_connect(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        failed_motion = FakeMotion(connect_results=[False])
        ready_motion = FakeMotion()
        motions = iter([failed_motion, ready_motion])

        def sleep(_seconds):
            if ready_motion.commands:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(),
            controller_factory=lambda: controller,
            motion_factory=lambda: next(motions),
            sleep=sleep,
        )

        runner.run_forever()

        self.assertEqual(failed_motion.commands, [])
        self.assertIn((250, 250), ready_motion.commands)

    def test_run_forever_waits_for_controller(self):
        state = controller_state(left_stick_y=0.0, right_stick_x=0.0, rb=False, lb=False)
        missing_controller = FakeController(state, connects=False)
        controller = FakeController(state)
        motion = FakeMotion()
        controllers = iter([missing_controller, controller])

        def sleep(_seconds):
            if motion.commands:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            fast_config(),
            controller_factory=lambda: next(controllers),
            motion_factory=lambda: motion,
            sleep=sleep,
        )

        runner.run_forever()

        self.assertFalse(missing_controller.cleaned_up)
        self.assertIn((0, 0), motion.commands)

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


if __name__ == "__main__":
    unittest.main()
