"""Dead-reckoning odometry for a differential-drive base.

Pure pose integration — no hardware, no sockets, no encoder counts. Feed it the
signed per-update wheel travel (meters, robot-forward positive) and it tracks the
robot's pose by exact-form midpoint dead reckoning. The counts->meters conversion
stays at the call site; this module only does geometry.

Coordinate frame: the pose is in a fixed frame anchored at the start pose (ROS
calls it `odom`), with REP-103 axes: +x is wherever the robot initially faced,
+y its left, angles in radians, distances in meters. Positive theta (yaw) is
counterclockwise, so a left turn increases theta.
"""

import math
from dataclasses import dataclass, replace


@dataclass
class Pose:
    x: float = 0.0       # meters, +forward from start
    y: float = 0.0       # meters, +left from start
    theta: float = 0.0   # radians, CCW positive, normalized to (-pi, pi]


def normalize_angle(theta: float) -> float:
    """Wrap an angle into (-pi, pi]."""
    wrapped = math.atan2(math.sin(theta), math.cos(theta))
    # atan2 returns [-pi, pi]; fold the -pi endpoint up to +pi to match (-pi, pi].
    if wrapped <= -math.pi:
        wrapped += 2 * math.pi
    return wrapped


class DiffDriveOdometry:
    def __init__(self, track_width_m: float):
        self.track_width_m = track_width_m
        self._pose = Pose()

    @property
    def pose(self) -> Pose:
        """A copy of the current pose (callers can't mutate internal state)."""
        return replace(self._pose)

    def update(self, left_delta_m: float, right_delta_m: float) -> Pose:
        d_center = (left_delta_m + right_delta_m) / 2
        # Right wheel ahead of left => CCW => positive theta, matching REP-103.
        d_theta = (right_delta_m - left_delta_m) / self.track_width_m

        theta_mid = self._pose.theta + d_theta / 2
        self._pose.x += d_center * math.cos(theta_mid)
        self._pose.y += d_center * math.sin(theta_mid)
        self._pose.theta = normalize_angle(self._pose.theta + d_theta)

        # Return a copy so callers can't mutate our internal state.
        return replace(self._pose)

    def reset(self) -> None:
        self._pose = Pose()
