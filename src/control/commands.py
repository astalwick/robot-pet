"""Shared command types for robot motion control."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionCommand:
    """Twist-like body command: forward velocity and yaw rate, normalized."""

    linear_x: float
    angular_z: float


@dataclass(frozen=True)
class WheelCommand:
    """Normalized wheel intent, where positive left and right means robot-forward."""

    left: float
    right: float


@dataclass(frozen=True)
class WheelSpeedCommand:
    """Closed-loop RoboClaw wheel speed targets in encoder counts per second."""

    left_qpps: int
    right_qpps: int
