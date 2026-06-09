"""Range-sensor safety rules for robot-motion."""

from __future__ import annotations

from dataclasses import dataclass

from config.sensors import SensorsConfig, cliff_trip_mm, forward_stop_mm


@dataclass(frozen=True)
class SafetyState:
    blocked: bool
    reason: str | None = None


def is_forward_motion(left_qpps: int, right_qpps: int) -> bool:
    return left_qpps + right_qpps > 0


def evaluate_safety(
    readings: list[dict],
    config: SensorsConfig,
    *,
    sensors_live: bool,
) -> SafetyState:
    if not config.safety.enabled:
        return SafetyState(blocked=False)
    if not sensors_live:
        return SafetyState(blocked=True, reason="sensors_stale")

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
                return SafetyState(blocked=True, reason=f"{entry.name}_cliff")

        if entry.role == "forward":
            stop_below = forward_stop_mm(entry, config.safety)
            if stop_below is not None and distance_mm < stop_below:
                return SafetyState(blocked=True, reason=f"{entry.name}_obstacle")

    return SafetyState(blocked=False)


def cancel_forward_qpps_when_blocked(
    left_qpps: int,
    right_qpps: int,
    safety: SafetyState,
) -> tuple[int, int]:
    """Cancel unsafe forward motion while preserving rotation and reverse."""
    if not safety.blocked:
        return left_qpps, right_qpps
    if not is_forward_motion(left_qpps, right_qpps):
        return left_qpps, right_qpps
    forward_qpps = (left_qpps + right_qpps) / 2
    return int(left_qpps - forward_qpps), int(right_qpps - forward_qpps)
