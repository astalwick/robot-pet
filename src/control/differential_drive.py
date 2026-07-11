"""Differential drive mixing for closed-loop wheel speed control."""

from dataclasses import dataclass

from control.commands import MotionCommand, WheelCommand, WheelSpeedCommand
from robot_model import ENCODER_COUNTS_PER_METER, TRACK_WIDTH_METERS


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def body_twist_to_wheel_qpps(linear_x_mps: float, angular_z_radps: float) -> WheelSpeedCommand:
    """REP-103 diff-drive inverse kinematics: body twist -> wheel QPPS targets.

    Positive angular_z is counterclockwise (a left turn), so the right wheel runs
    faster. This matches DiffDriveOdometry.update, where a right wheel ahead of the
    left produces positive theta.
    """
    left_mps = linear_x_mps - angular_z_radps * TRACK_WIDTH_METERS / 2
    right_mps = linear_x_mps + angular_z_radps * TRACK_WIDTH_METERS / 2
    return WheelSpeedCommand(
        left_qpps=round(left_mps * ENCODER_COUNTS_PER_METER),
        right_qpps=round(right_mps * ENCODER_COUNTS_PER_METER),
    )


def wheel_qpps_to_body_twist(left_qpps: int, right_qpps: int) -> MotionCommand:
    """Diff-drive forward kinematics: wheel QPPS -> REP-103 body twist."""
    left_mps = left_qpps / ENCODER_COUNTS_PER_METER
    right_mps = right_qpps / ENCODER_COUNTS_PER_METER
    return MotionCommand(
        linear_x=(left_mps + right_mps) / 2,
        angular_z=(right_mps - left_mps) / TRACK_WIDTH_METERS,
    )


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
