"""Range-sensor and lidar safety rules for robot-motion."""

from __future__ import annotations

import math
from dataclasses import dataclass

from config.sensors import SensorsConfig, cliff_trip_mm, forward_stop_mm
from robot_model import (
    FOOTPRINT_FRONT_X_METERS,
    FOOTPRINT_HALF_WIDTH_METERS,
    FOOTPRINT_REAR_X_METERS,
    LIDAR_MOUNT,
)

# A scan older than this no longer gates motion: the lidar idles with the
# motor rail, and the ToFs keep covering the front.
LIDAR_STALE_SECONDS = 1.0
# Lidar returns this far outside the body's width still count as "in the way".
CLEARANCE_SIDE_MARGIN_METERS = 0.03


@dataclass(frozen=True)
class SafetyState:
    blocked: bool
    reason: str | None = None
    reverse_blocked: bool = False
    reverse_reason: str | None = None


@dataclass(frozen=True)
class Clearances:
    """Nearest lidar return past the body's front and rear edges, in meters.

    None means nothing in the body's width was seen on that side.
    """

    forward_m: float | None
    rear_m: float | None


def is_forward_motion(left_qpps: int, right_qpps: int) -> bool:
    return left_qpps + right_qpps > 0


def scan_clearances(
    ranges,
    angle_min: float,
    angle_increment: float,
    range_min: float,
    range_max: float,
) -> Clearances:
    """Reduce one LaserScan to how much room there is straight ahead and behind.

    Each return goes through LIDAR_MOUNT into base_link. Returns outside the
    body's width (plus a small margin) are ignored, as are returns inside the
    footprint, which are the robot's own deck furniture.
    """
    half_width = FOOTPRINT_HALF_WIDTH_METERS + CLEARANCE_SIDE_MARGIN_METERS
    forward_m = None
    rear_m = None
    for index, distance in enumerate(ranges):
        if not range_min <= distance <= range_max:
            continue
        angle = LIDAR_MOUNT.yaw + angle_min + index * angle_increment
        x = LIDAR_MOUNT.x + distance * math.cos(angle)
        y = LIDAR_MOUNT.y + distance * math.sin(angle)
        if abs(y) > half_width:
            continue
        if x > FOOTPRINT_FRONT_X_METERS:
            ahead = x - FOOTPRINT_FRONT_X_METERS
            forward_m = ahead if forward_m is None else min(forward_m, ahead)
        elif x < FOOTPRINT_REAR_X_METERS:
            behind = FOOTPRINT_REAR_X_METERS - x
            rear_m = behind if rear_m is None else min(rear_m, behind)
    return Clearances(forward_m=forward_m, rear_m=rear_m)


def evaluate_safety(
    readings: list[dict],
    config: SensorsConfig,
    *,
    sensors_live: bool,
    clearances: Clearances | None = None,
    clearances_age: float | None = None,
) -> SafetyState:
    if not config.safety.enabled:
        return SafetyState(blocked=False)
    reason = _range_sensor_trip(readings, config) if sensors_live else "sensors_stale"

    reverse_reason = None
    if clearances is not None and clearances_age <= LIDAR_STALE_SECONDS:
        stop_below_m = config.safety.lidar_stop_below_mm / 1000
        if reason is None and clearances.forward_m is not None and clearances.forward_m < stop_below_m:
            reason = "lidar_forward_obstacle"
        if clearances.rear_m is not None and clearances.rear_m < stop_below_m:
            reverse_reason = "lidar_rear_obstacle"

    return SafetyState(
        blocked=reason is not None,
        reason=reason,
        reverse_blocked=reverse_reason is not None,
        reverse_reason=reverse_reason,
    )


def _range_sensor_trip(readings: list[dict], config: SensorsConfig) -> str | None:
    readings_by_name = {reading["name"]: reading for reading in readings if reading.get("name")}

    for entry in config.sensors:
        reading = readings_by_name.get(entry.name)
        if reading is None or not reading.get("ok"):
            continue
        distance_mm = reading.get("distance_mm")
        if distance_mm is None:
            continue

        if entry.role == "cliff":
            trip_above = cliff_trip_mm(entry, config.safety)
            if trip_above is not None and distance_mm > trip_above:
                return f"{entry.name}_cliff"

        if entry.role == "forward":
            stop_below = forward_stop_mm(entry, config.safety)
            if stop_below is not None and distance_mm < stop_below:
                return f"{entry.name}_obstacle"

    return None


def blocking_reason(left_qpps: int, right_qpps: int, safety: SafetyState) -> str | None:
    """The safety reason that applies to the commanded direction, or None.

    Turning in place is never blocked: it has no straight-line component.
    """
    if safety.blocked and is_forward_motion(left_qpps, right_qpps):
        return safety.reason
    if safety.reverse_blocked and left_qpps + right_qpps < 0:
        return safety.reverse_reason
    return None


def cancel_blocked_qpps(left_qpps: int, right_qpps: int, safety: SafetyState) -> tuple[int, int]:
    """Cancel the blocked straight-line component while preserving rotation."""
    if blocking_reason(left_qpps, right_qpps, safety) is None:
        return left_qpps, right_qpps
    straight_qpps = (left_qpps + right_qpps) / 2
    return int(left_qpps - straight_qpps), int(right_qpps - straight_qpps)
