import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from control.commands import MotionCommand
from control.differential_drive import DifferentialDriveMixer
from control.teleop import GamepadTeleopPolicy


class GamepadTeleopPolicyTest(unittest.TestCase):
    def test_deadman_released_returns_zero_motion(self):
        policy = GamepadTeleopPolicy(left_stick_deadzone=0.15, right_stick_deadzone=0.15)
        state = SimpleNamespace(left_stick_y=-1.0, right_stick_x=0.5, rb=False)

        self.assertEqual(policy.motion_from_state(state), MotionCommand(0.0, 0.0))

    def test_deadman_held_uses_speed_test_sign_conventions(self):
        policy = GamepadTeleopPolicy(left_stick_deadzone=0.15, right_stick_deadzone=0.15)
        state = SimpleNamespace(left_stick_y=-0.75, right_stick_x=0.25, rb=True)

        self.assertEqual(policy.motion_from_state(state), MotionCommand(0.75, 0.25))

    def test_per_stick_deadzones_zero_small_stick_noise(self):
        policy = GamepadTeleopPolicy(left_stick_deadzone=0.20, right_stick_deadzone=0.10)
        state = SimpleNamespace(left_stick_y=-0.15, right_stick_x=0.09, rb=True)

        self.assertEqual(policy.motion_from_state(state), MotionCommand(0.0, 0.0))

    def test_turn_scale_reduces_turn_command(self):
        policy = GamepadTeleopPolicy(
            left_stick_deadzone=0.15,
            right_stick_deadzone=0.15,
            turn_scale=0.5,
        )
        state = SimpleNamespace(left_stick_y=0.0, right_stick_x=0.8, rb=True)

        self.assertEqual(policy.motion_from_state(state), MotionCommand(0.0, 0.4))


class DifferentialDriveMixerTest(unittest.TestCase):
    def test_arcade_mix_matches_speed_test(self):
        mixer = DifferentialDriveMixer(qpps=1000)

        self.assertEqual(mixer.mix(MotionCommand(0.5, 0.25)).left, 0.75)
        self.assertEqual(mixer.mix(MotionCommand(0.5, 0.25)).right, 0.25)

    def test_mix_clamps_normalized_wheel_commands(self):
        mixer = DifferentialDriveMixer(qpps=1000)

        wheels = mixer.mix(MotionCommand(0.75, 0.75))

        self.assertEqual(wheels.left, 1.0)
        self.assertEqual(wheels.right, 0.0)

    def test_mix_preserves_turn_ratio_when_saturated(self):
        mixer = DifferentialDriveMixer(qpps=1000)

        wheels = mixer.mix(MotionCommand(1.0, 0.5))

        self.assertEqual(wheels.left, 1.0)
        self.assertAlmostEqual(wheels.right, 1.0 / 3.0)

    def test_wheel_speeds_use_normal_and_turbo_caps(self):
        mixer = DifferentialDriveMixer(qpps=2000, speed_scale=0.25, turbo_scale=0.75)

        normal = mixer.to_wheel_speeds(MotionCommand(1.0, 0.0), turbo=False)
        turbo = mixer.to_wheel_speeds(MotionCommand(1.0, 0.0), turbo=True)

        self.assertEqual(normal.left_qpps, 500)
        self.assertEqual(normal.right_qpps, 500)
        self.assertEqual(turbo.left_qpps, 1500)
        self.assertEqual(turbo.right_qpps, 1500)

    def test_positive_left_and_right_qpps_mean_forward(self):
        mixer = DifferentialDriveMixer(qpps=2425, speed_scale=0.25)

        speeds = mixer.to_wheel_speeds(MotionCommand(1.0, 0.0))

        self.assertGreater(speeds.left_qpps, 0)
        self.assertGreater(speeds.right_qpps, 0)

    def test_qpps_output_never_exceeds_configured_cap(self):
        mixer = DifferentialDriveMixer(qpps=2425, speed_scale=0.25)

        speeds = mixer.to_wheel_speeds(MotionCommand(2.0, 2.0))
        cap = int(2425 * 0.25)

        self.assertLessEqual(abs(speeds.left_qpps), cap)
        self.assertLessEqual(abs(speeds.right_qpps), cap)


if __name__ == "__main__":
    unittest.main()
