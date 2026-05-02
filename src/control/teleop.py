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

    deadzone: float = 0.15

    def motion_from_state(self, state: ControllerStateLike) -> MotionCommand:
        if not state.rb:
            return MotionCommand(0.0, 0.0)

        forward = -self._apply_deadzone(state.left_stick_y)
        turn = self._apply_deadzone(state.right_stick_x)
        return MotionCommand(linear_x=forward, angular_z=turn)

    def _apply_deadzone(self, value: float) -> float:
        if abs(value) < self.deadzone:
            return 0.0
        return value
