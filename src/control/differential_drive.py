"""Differential drive mixing for closed-loop wheel speed control."""

from dataclasses import dataclass

from control.commands import MotionCommand, WheelCommand, WheelSpeedCommand


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class DifferentialDriveMixer:
    """Convert body motion commands into normalized wheel and QPPS targets."""

    qpps: int
    speed_scale: float = 0.25
    turbo_scale: float = 0.75

    def mix(self, command: MotionCommand) -> WheelCommand:
        left = command.linear_x + command.angular_z
        right = command.linear_x - command.angular_z
        scale = max(1.0, abs(left), abs(right))
        left = clamp(left / scale, -1.0, 1.0)
        right = clamp(right / scale, -1.0, 1.0)
        return WheelCommand(left=left, right=right)

    def to_wheel_speeds(self, command: MotionCommand, turbo: bool = False) -> WheelSpeedCommand:
        wheels = self.mix(command)
        speed_limit = self._speed_limit(turbo)
        return WheelSpeedCommand(
            left_qpps=int(wheels.left * speed_limit),
            right_qpps=int(wheels.right * speed_limit),
        )

    def _speed_limit(self, turbo: bool) -> int:
        scale = self.turbo_scale if turbo else self.speed_scale
        return int(self.qpps * scale)
