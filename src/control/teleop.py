"""Gamepad teleop policy that is independent of motor hardware and ROS2."""

from dataclasses import dataclass
from typing import Protocol

from control.commands import MotionCommand


class ControllerStateLike(Protocol):
    left_stick_y: float
    right_stick_x: float
    rb: bool


@dataclass(frozen=True)
class GamepadTeleopPolicy:
    """Convert normalized controller state into a Twist-like motion command."""

    left_stick_deadzone: float = 0.15
    right_stick_deadzone: float = 0.15
    turn_scale: float = 1.0

    def motion_from_state(self, state: ControllerStateLike) -> MotionCommand:
        if not state.rb:
            return MotionCommand(0.0, 0.0)

        forward = -self._apply_deadzone(state.left_stick_y, self.left_stick_deadzone)
        turn = self._apply_deadzone(state.right_stick_x, self.right_stick_deadzone) * self.turn_scale
        return MotionCommand(linear_x=forward, angular_z=turn)

    def _apply_deadzone(self, value: float, deadzone: float) -> float:
        if abs(value) < deadzone:
            return 0.0
        return value
