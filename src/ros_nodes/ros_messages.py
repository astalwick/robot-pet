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


# sensor_msgs/BatteryState constants
POWER_SUPPLY_STATUS_UNKNOWN = 0
POWER_SUPPLY_STATUS_CHARGING = 1
POWER_SUPPLY_STATUS_DISCHARGING = 2
POWER_SUPPLY_STATUS_NOT_CHARGING = 3
POWER_SUPPLY_HEALTH_UNKNOWN = 0
POWER_SUPPLY_TECHNOLOGY_LION = 2
POWER_SUPPLY_TECHNOLOGY_LIPO = 3
# hub pi_battery power_state -> BatteryState.power_supply_status
PI_POWER_SUPPLY_STATUS = {
    "charging": POWER_SUPPLY_STATUS_CHARGING,
    "discharging": POWER_SUPPLY_STATUS_DISCHARGING,
    "standby": POWER_SUPPLY_STATUS_NOT_CHARGING,
}

# nav_msgs/Odometry covariances (6x6 row-major: x, y, z, roll, pitch, yaw).
# Planar robot: large values on the axes we never estimate.
UNUSED_AXIS = 1e6
POSE_COVARIANCE = [0.0] * 36
TWIST_COVARIANCE = [0.0] * 36
for _index, _value in ((0, 0.01), (7, 0.01), (14, UNUSED_AXIS), (21, UNUSED_AXIS), (28, UNUSED_AXIS), (35, 0.03)):
    POSE_COVARIANCE[_index] = _value
    TWIST_COVARIANCE[_index] = _value


def odometry_fields(x: float, y: float, theta: float, linear_x: float, angular_z: float) -> dict[str, Any]:
    """Map the dead-reckoned pose and body twist to nav_msgs/Odometry and TF fields."""
    return {
        "frame_id": "odom",
        "child_frame_id": "base_link",
        "position": (x, y, 0.0),
        "orientation": quaternion_from_euler_degrees(0.0, 0.0, math.degrees(theta)),
        "linear": (linear_x, 0.0, 0.0),
        "angular": (0.0, 0.0, angular_z),
        "pose_covariance": POSE_COVARIANCE,
        "twist_covariance": TWIST_COVARIANCE,
    }


def battery_state_fields(motor_battery: dict[str, Any]) -> dict[str, Any] | None:
    """Map the hub motor_battery dict to sensor_msgs/BatteryState fields, or None without a reading."""
    if motor_battery.get("pack_voltage") is None:
        return None
    percent = motor_battery.get("percent_estimate")
    return {
        "frame_id": "base_link",
        "voltage": float(motor_battery["pack_voltage"]),
        "percentage": percent / 100 if percent is not None else math.nan,
        "power_supply_status": POWER_SUPPLY_STATUS_UNKNOWN,
        "power_supply_health": POWER_SUPPLY_HEALTH_UNKNOWN,
        "power_supply_technology": POWER_SUPPLY_TECHNOLOGY_LIPO,
        "present": True,
    }


def pi_battery_state_fields(pi_battery: dict[str, Any]) -> dict[str, Any] | None:
    """Map the hub pi_battery dict (Waveshare UPS HAT E, 4x 21700 Li-ion) to BatteryState fields."""
    if pi_battery.get("pack_voltage") is None:
        return None
    remaining_mah = pi_battery.get("remaining_mah")
    return {
        "frame_id": "base_link",
        "voltage": float(pi_battery["pack_voltage"]),
        "current": float(pi_battery["current_amps"]),
        "charge": remaining_mah / 1000 if remaining_mah is not None else math.nan,
        "percentage": pi_battery["percent"] / 100,
        "power_supply_status": PI_POWER_SUPPLY_STATUS.get(pi_battery.get("power_state"), POWER_SUPPLY_STATUS_UNKNOWN),
        "power_supply_health": POWER_SUPPLY_HEALTH_UNKNOWN,
        "power_supply_technology": POWER_SUPPLY_TECHNOLOGY_LION,
        "present": True,
        "cell_voltage": [float(volts) for volts in pi_battery.get("cell_voltages", [])],
    }
