import os
import sys
import unittest
import logging
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.drive_tuning import DriveTuning
from control.differential_drive import body_twist_to_wheel_qpps
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
    """Captures published twists, exposing them as the (left, right) qpps they map to."""

    def __init__(self):
        self.twists = []

    def publish(self, twist):
        self.twists.append(twist)

    @property
    def commands(self):
        commands = []
        for twist in self.twists:
            wheels = body_twist_to_wheel_qpps(twist.linear_x, twist.angular_z)
            commands.append((wheels.left_qpps, wheels.right_qpps))
        return commands


class GamepadTeleopRunnerTest(unittest.TestCase):
    def test_raw_target_sent_without_slew(self):
        # Slew now lives in robot-motion; the gamepad sends the raw mixer target.
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                state.lb = True
            elif sleeps == 3:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            TeleopConfig(qpps=1000, drive_tuning=DriveTuning(speed_scale=0.25, turbo_scale=0.75)),
            twist_publisher=motion.publish,
            sleep=sleep,
        )

        runner._run_connected(controller)

        # Full forward: 250 qpps normal, then 750 qpps once turbo (LB) engages — no ramp.
        self.assertEqual(motion.commands[:3], [(250, 250), (750, 750), (750, 750)])

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

        runner = GamepadTeleopRunner(fast_config(), twist_publisher=motion.publish, sleep=sleep)

        runner._run_connected(controller)

        self.assertIn((250, 250), motion.commands)
        self.assertIn((0, 0), motion.commands)

    def test_disconnect_sends_zero_speed(self):
        state = controller_state(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motion = FakeMotion()

        def sleep(_seconds):
            controller.on_disconnect()

        runner = GamepadTeleopRunner(fast_config(), twist_publisher=motion.publish, sleep=sleep)

        runner._run_connected(controller)

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

        runner = GamepadTeleopRunner(fast_config(), twist_publisher=motion.publish, sleep=sleep)

        runner._run_connected(controller)

        self.assertIn((250, -250), motion.commands)
        # One zero twist, then silence: robot-motion's deadman takes over.
        self.assertEqual(motion.commands[-1], (0, 0))
        self.assertEqual(motion.commands.count((0, 0)), 1)
        self.assertEqual(runner.drive_state, "controller_lost")

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

        runner = GamepadTeleopRunner(fast_config(), twist_publisher=motion.publish, sleep=sleep)

        runner._run_connected(controller)

        self.assertGreaterEqual(motion.commands.count((250, 250)), 2)
        self.assertEqual(motion.commands[-1], (0, 0))

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
            twist_publisher=motion.publish,
            controller_factory=lambda: next(controllers),
            sleep=sleep,
        )

        runner.run_forever()

        self.assertFalse(missing_controller.cleaned_up)
        self.assertIn((0, 0), motion.commands)

    def test_turn_publishes_positive_angular_z_for_left_turn(self):
        # Right stick left spins left wheel back / right wheel forward: a left
        # (counterclockwise) turn, which REP-103 reports as positive angular_z.
        state = controller_state(left_stick_y=0.0, right_stick_x=-1.0, rb=True, lb=False)
        motion = FakeMotion()
        runner = GamepadTeleopRunner(
            fast_config(), twist_publisher=motion.publish, sleep=lambda _seconds: runner.request_stop()
        )

        runner._run_connected(FakeController(state))

        self.assertEqual(motion.commands[0], (-250, 250))
        self.assertAlmostEqual(motion.twists[0].linear_x, 0.0)
        self.assertGreater(motion.twists[0].angular_z, 0.0)

    def test_run_connected_heartbeats_connected_gamepad(self):
        state = controller_state()
        controller = FakeController(state)
        motion = FakeMotion()
        published = []
        runner = GamepadTeleopRunner(
            fast_config(),
            twist_publisher=motion.publish,
            sleep=lambda _seconds: runner.request_stop(),
            telemetry_publisher=lambda _socket_path, message: published.append(message) or True,
        )

        runner._run_connected(controller)

        self.assertTrue(any(message.get("source") == "gamepad" and message["connected"] for message in published))
        # the full status too: it carries the sticks and buttons the dashboard's Pilot Inputs card shows
        self.assertTrue(
            any(message.get("source") == "gamepad_teleop" and message["controller"]["connected"] for message in published)
        )

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
            twist_publisher=lambda _twist: None,
            sleep=sleep,
            clock=clock,
            telemetry_publisher=lambda _socket_path, message: published.append(message) or True,
        )

        runner._sleep_with_status_updates(1.0)

        self.assertEqual(len(published), 2)
        self.assertEqual(published[0]["source"], "gamepad")
        self.assertFalse(published[0]["connected"])
        self.assertEqual(published[1]["drive_status"]["state"], "stopped")

    def test_status_update_carries_controller_and_drive_tuning_to_hub(self):
        # These no longer ride on the DriveCommand, so this publish is the only
        # route for controller state and drive tuning to reach the dashboard.
        state = controller_state()
        controller = FakeController(state)
        published = []
        runner = GamepadTeleopRunner(
            fast_config(),
            twist_publisher=lambda _twist: None,
            telemetry_publisher=lambda _socket_path, message: published.append(message) or True,
        )

        runner._publish_status_update(controller)

        message = published[0]
        self.assertEqual(message["source"], "gamepad_teleop")
        self.assertTrue(message["controller"]["connected"])
        self.assertIn("speed_scale", message["drive_tuning"])


if __name__ == "__main__":
    unittest.main()
