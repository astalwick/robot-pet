"""Pure conversions from hub sensor dicts to ROS message fields.

No rclpy or message imports here, so the math is unit-testable on the Mac.
sensors_node.py copies these dicts into real sensor_msgs objects.
"""

from __future__ import annotations

import math
from typing import Any


# sensor_msgs/Range.radiation_type
INFRARED = 1

# Per-kind optics. VL53L0X is the cliff sensor (default ranging mode, 25 deg
# cone). VL53L1X is the forward sensor in short distance mode with the ROI
# narrowed to a 16x2 SPAD strip, so the cone is mostly its 27 deg width.
RANGE_LIMITS = {
    "vl53l0x": {"field_of_view": math.radians(25), "min_range": 0.03, "max_range": 1.2},
    "vl53l1x": {"field_of_view": math.radians(27), "min_range": 0.04, "max_range": 1.3},
}

# REP-145: a covariance whose first element is -1 means "unknown".
UNKNOWN_COVARIANCE = [-1.0] + [0.0] * 8
# We publish orientation only, so the covariance is unknown but the estimate
# exists. ROS consumers treat an all-zero covariance as "unknown but present".
ORIENTATION_COVARIANCE = [0.0] * 9


def range_fields(reading: dict[str, Any]) -> dict[str, Any]:
    """Map one hub reading dict to sensor_msgs/Range fields (REP-117 encoding)."""
    limits = RANGE_LIMITS[reading["kind"]]
    if not reading["ok"]:
        meters = math.nan
    elif reading["distance_mm"] is None or reading["distance_mm"] / 1000 > limits["max_range"]:
        meters = math.inf
    elif reading["distance_mm"] / 1000 < limits["min_range"]:
        meters = -math.inf
    else:
        meters = reading["distance_mm"] / 1000
    return {
        "frame_id": reading["name"],
        "radiation_type": INFRARED,
        "range": meters,
        **limits,
    }


def quaternion_from_euler_degrees(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    """ZYX (yaw, then pitch, then roll) body rotation as an (x, y, z, w) quaternion."""
    half_roll = math.radians(roll) / 2
    half_pitch = math.radians(pitch) / 2
    half_yaw = math.radians(yaw) / 2
    cos_roll, sin_roll = math.cos(half_roll), math.sin(half_roll)
    cos_pitch, sin_pitch = math.cos(half_pitch), math.sin(half_pitch)
    cos_yaw, sin_yaw = math.cos(half_yaw), math.sin(half_yaw)
    return (
        sin_roll * cos_pitch * cos_yaw - cos_roll * sin_pitch * sin_yaw,
        cos_roll * sin_pitch * cos_yaw + sin_roll * cos_pitch * sin_yaw,
        cos_roll * cos_pitch * sin_yaw - sin_roll * sin_pitch * cos_yaw,
        cos_roll * cos_pitch * cos_yaw + sin_roll * sin_pitch * sin_yaw,
    )


def imu_fields(imu: dict[str, Any]) -> dict[str, Any] | None:
    """Map the hub imu dict to sensor_msgs/Imu fields, or None when there is no fix."""
    if not imu.get("ok"):
        return None
    return {
        "frame_id": "imu_link",
        "orientation": quaternion_from_euler_degrees(
            imu["roll_degrees"], imu["pitch_degrees"], imu["yaw_degrees"]
        ),
        "orientation_covariance": ORIENTATION_COVARIANCE,
        "angular_velocity_covariance": UNKNOWN_COVARIANCE,
        "linear_acceleration_covariance": UNKNOWN_COVARIANCE,
    }
