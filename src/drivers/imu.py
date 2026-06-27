"""BNO085 orientation helpers."""

from __future__ import annotations

import math
import time
from typing import Any


Quaternion = tuple[float, float, float, float]


def normalize_quaternion(quaternion: Quaternion) -> Quaternion:
    x, y, z, w = quaternion
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length == 0:
        raise ValueError("quaternion cannot be zero")
    return (x / length, y / length, z / length, w / length)


def multiply_quaternion(left: Quaternion, right: Quaternion) -> Quaternion:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return normalize_quaternion(
        (
            lw * rx + lx * rw + ly * rz - lz * ry,
            lw * ry - lx * rz + ly * rw + lz * rx,
            lw * rz + lx * ry - ly * rx + lz * rw,
            lw * rw - lx * rx - ly * ry - lz * rz,
        )
    )


def inverse_quaternion(quaternion: Quaternion) -> Quaternion:
    x, y, z, w = normalize_quaternion(quaternion)
    return (-x, -y, -z, w)


def relative_quaternion(
    zero_quaternion: Quaternion, current_quaternion: Quaternion
) -> Quaternion:
    return multiply_quaternion(inverse_quaternion(zero_quaternion), current_quaternion)


def average_quaternions(quaternions: list[Quaternion]) -> Quaternion:
    if not quaternions:
        raise ValueError("need at least one quaternion")

    first = normalize_quaternion(quaternions[0])
    x_total = y_total = z_total = w_total = 0.0
    for quaternion in quaternions:
        x, y, z, w = normalize_quaternion(quaternion)
        if x * first[0] + y * first[1] + z * first[2] + w * first[3] < 0:
            x, y, z, w = -x, -y, -z, -w
        x_total += x
        y_total += y
        z_total += z
        w_total += w
    return normalize_quaternion((x_total, y_total, z_total, w_total))


def quaternion_to_euler_degrees(quaternion: Quaternion) -> tuple[float, float, float]:
    x, y, z, w = normalize_quaternion(quaternion)

    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch_term = 2 * (w * y - z * x)
    pitch = math.asin(max(-1.0, min(1.0, pitch_term)))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def quaternion_to_rotation_vector_degrees(quaternion: Quaternion) -> tuple[float, float, float]:
    x, y, z, w = normalize_quaternion(quaternion)
    if w < 0:
        x, y, z, w = -x, -y, -z, -w

    vector_length = math.sqrt(x * x + y * y + z * z)
    if vector_length < 0.000001:
        return (0.0, 0.0, 0.0)

    angle = 2 * math.atan2(vector_length, w)
    scale = math.degrees(angle) / vector_length
    return (x * scale, y * scale, z * scale)


def read_bno085_quaternion(sensor: Any, mode: str, timeout: float = 5.0) -> Quaternion:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            quaternion = sensor.game_quaternion if mode == "game" else sensor.quaternion
        except RuntimeError:
            quaternion = None
        if quaternion is not None and any(quaternion):
            return normalize_quaternion(quaternion)
        time.sleep(0.05)
    raise RuntimeError("BNO085 did not publish an orientation report")
