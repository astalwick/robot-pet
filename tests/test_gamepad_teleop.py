import os
import sys
import unittest
import logging
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gamepad_teleop import GamepadTeleopRunner, TeleopConfig

logging.getLogger("gamepad-teleop").disabled = True


class FakeController:
    def __init__(self, state, connects=True):
        self.state = state
        self.connects = connects
        self.on_disconnect = None
        self.cleaned_up = False

    def connect(self):
        return self.connects

    def start(self, on_disconnect=None):
        self.on_disconnect = on_disconnect

    def cleanup(self):
        self.cleaned_up = True


class FakeMotor:
    def __init__(self, fail_nonzero=False, results=None):
        self.fail_nonzero = fail_nonzero
        self.results = list(results or [])
        self.commands = []
        self.cleaned_up = False

    def set_wheel_speeds(self, left_qpps, right_qpps):
        self.commands.append((left_qpps, right_qpps))
        if self.results:
            return self.results.pop(0)
        if self.fail_nonzero and (left_qpps != 0 or right_qpps != 0):
            return False
        return True

    def cleanup(self):
        self.cleaned_up = True


class GamepadTeleopRunnerTest(unittest.TestCase):
    def test_deadman_release_sends_zero_speed(self):
        state = SimpleNamespace(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
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

        runner = GamepadTeleopRunner(TeleopConfig(qpps=1000), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertIn((250, 250), motor.commands)
        self.assertIn((0, 0), motor.commands)

    def test_disconnect_sends_zero_speed(self):
        state = SimpleNamespace(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()

        def sleep(_seconds):
            controller.on_disconnect()

        runner = GamepadTeleopRunner(TeleopConfig(qpps=1000), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertEqual(motor.commands[-1], (0, 0))

    def test_failed_speed_command_sends_zero_speed(self):
        state = SimpleNamespace(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor(fail_nonzero=True)
        runner = GamepadTeleopRunner(TeleopConfig(qpps=1000), sleep=lambda _seconds: None)

        runner._run_connected(controller, motor)

        self.assertEqual(motor.commands, [(250, 250), (0, 0)])

    def test_steady_motion_is_heartbeated_to_roboclaw(self):
        state = SimpleNamespace(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        motor = FakeMotor()
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                runner.request_stop()

        runner = GamepadTeleopRunner(TeleopConfig(qpps=1000), sleep=sleep)

        runner._run_connected(controller, motor)

        self.assertGreaterEqual(motor.commands.count((250, 250)), 2)
        self.assertEqual(motor.commands[-1], (0, 0))

    def test_run_forever_retries_failed_initial_zero_before_motion(self):
        state = SimpleNamespace(left_stick_y=-1.0, right_stick_x=0.0, rb=True, lb=False)
        controller = FakeController(state)
        failed_motor = FakeMotor(results=[False])
        ready_motor = FakeMotor()
        motors = iter([failed_motor, ready_motor])

        def sleep(_seconds):
            if ready_motor.commands:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            TeleopConfig(qpps=1000),
            controller_factory=lambda: controller,
            motor_factory=lambda: next(motors),
            sleep=sleep,
        )

        runner.run_forever()

        self.assertEqual(failed_motor.commands, [(0, 0)])
        self.assertEqual(ready_motor.commands[0], (0, 0))
        self.assertIn((250, 250), ready_motor.commands)

    def test_run_forever_waits_for_controller(self):
        state = SimpleNamespace(left_stick_y=0.0, right_stick_x=0.0, rb=False, lb=False)
        missing_controller = FakeController(state, connects=False)
        controller = FakeController(state)
        motor = FakeMotor()
        controllers = iter([missing_controller, controller])

        def sleep(_seconds):
            if motor.commands:
                runner.request_stop()

        runner = GamepadTeleopRunner(
            TeleopConfig(qpps=1000),
            controller_factory=lambda: next(controllers),
            motor_factory=lambda: motor,
            sleep=sleep,
        )

        runner.run_forever()

        self.assertFalse(missing_controller.cleaned_up)
        self.assertIn((0, 0), motor.commands)


if __name__ == "__main__":
    unittest.main()
