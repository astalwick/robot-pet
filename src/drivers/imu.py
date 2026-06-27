"""BNO085 orientation helpers."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any


Quaternion = tuple[float, float, float, float]
Vector = tuple[float, float, float]


@dataclass(frozen=True)
class BNO085Config:
    channel: int
    address: int
    mode: str
    zero_quaternion: Quaternion
    zero_gravity: Vector


@dataclass(frozen=True)
class ImuReading:
    yaw_degrees: float | None
    pitch_degrees: float | None
    roll_degrees: float | None
    ok: bool


def normalize_quaternion(quaternion: Quaternion) -> Quaternion:
    x, y, z, w = quaternion
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length == 0:
        raise ValueError("quaternion cannot be zero")
    return (x / length, y / length, z / length, w / length)


def normalize_vector(vector: Vector) -> Vector:
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if length == 0:
        raise ValueError("vector cannot be zero")
    return (x / length, y / length, z / length)


def average_vectors(vectors: list[Vector]) -> Vector:
    if not vectors:
        raise ValueError("need at least one vector")
    x_total = y_total = z_total = 0.0
    for x, y, z in vectors:
        x_total += x
        y_total += y
        z_total += z
    return normalize_vector((x_total, y_total, z_total))


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


def vector_rotation_degrees(start: Vector, current: Vector) -> Vector:
    start_x, start_y, start_z = normalize_vector(start)
    current_x, current_y, current_z = normalize_vector(current)
    axis_x = start_y * current_z - start_z * current_y
    axis_y = start_z * current_x - start_x * current_z
    axis_z = start_x * current_y - start_y * current_x
    axis_length = math.sqrt(axis_x * axis_x + axis_y * axis_y + axis_z * axis_z)
    if axis_length < 0.000001:
        return (0.0, 0.0, 0.0)

    dot = start_x * current_x + start_y * current_y + start_z * current_z
    angle = math.degrees(math.atan2(axis_length, max(-1.0, min(1.0, dot))))
    scale = angle / axis_length
    return (axis_x * scale, axis_y * scale, axis_z * scale)


def read_bno085_gravity(sensor: Any, timeout: float = 5.0) -> Vector:
    deadline = time.monotonic() + timeout
    while True:
        try:
            gravity = sensor.gravity
        except RuntimeError:
            gravity = None
        if gravity is not None and any(gravity):
            return gravity
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("BNO085 did not publish a gravity report")
        time.sleep(min(0.05, remaining))


def read_bno085_quaternion(sensor: Any, mode: str, timeout: float = 5.0) -> Quaternion:
    deadline = time.monotonic() + timeout
    while True:
        try:
            quaternion = sensor.game_quaternion if mode == "game" else sensor.quaternion
        except RuntimeError:
            quaternion = None
        if quaternion is not None and any(quaternion):
            return normalize_quaternion(quaternion)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("BNO085 did not publish an orientation report")
        time.sleep(min(0.05, remaining))


def _default_i2c_factory() -> Any:
    try:
        import board
    except ImportError as exc:
        raise RuntimeError(
            "adafruit-blinka not installed (run: pip install -e . from the repo venv on the Pi)"
        ) from exc

    return board.I2C()


def _default_mux_factory(i2c: Any, address: int) -> Any:
    import adafruit_tca9548a

    return adafruit_tca9548a.TCA9548A(i2c, address=address)


def _default_bno085_factory(channel_bus: Any, address: int) -> Any:
    from adafruit_bno08x.i2c import BNO08X_I2C

    return BNO08X_I2C(channel_bus, address=address)


class ImuDriver:
    """Read calibrated BNO085 orientation."""

    def __init__(
        self,
        config: BNO085Config,
        mux_address: int = 0x70,
        i2c_factory: Any = None,
        mux_factory: Any = None,
        bno085_factory: Any = None,
    ):
        from adafruit_bno08x import (
            BNO_REPORT_GAME_ROTATION_VECTOR,
            BNO_REPORT_GRAVITY,
            BNO_REPORT_ROTATION_VECTOR,
        )

        if i2c_factory is None:
            i2c_factory = _default_i2c_factory
        if mux_factory is None:
            mux_factory = _default_mux_factory
        if bno085_factory is None:
            bno085_factory = _default_bno085_factory

        self.config = config
        mux = mux_factory(i2c_factory(), mux_address)
        self.sensor = bno085_factory(mux[config.channel], config.address)
        if config.mode == "game":
            self.sensor.enable_feature(BNO_REPORT_GAME_ROTATION_VECTOR)
        else:
            self.sensor.enable_feature(BNO_REPORT_ROTATION_VECTOR)
        self.sensor.enable_feature(BNO_REPORT_GRAVITY)

    def read(self, timeout: float = 5.0) -> ImuReading:
        # The quaternion and gravity reads share one budget so the whole read
        # stays within `timeout` -- otherwise each could block for the full
        # timeout and a single tick would take twice as long.
        deadline = time.monotonic() + timeout
        try:
            _sensor_x, sensor_y, _sensor_z = quaternion_to_rotation_vector_degrees(
                relative_quaternion(
                    self.config.zero_quaternion,
                    read_bno085_quaternion(self.sensor, self.config.mode, timeout),
                )
            )
            gravity_x, _gravity_y, gravity_z = vector_rotation_degrees(
                self.config.zero_gravity,
                read_bno085_gravity(self.sensor, max(0.0, deadline - time.monotonic())),
            )
            # Yaw comes from the drift-free game rotation vector; pitch/roll come
            # from the gravity vector. This axis wiring is specific to how the
            # BNO085 is currently mounted -- a different mounting means changing
            # which components map to yaw/pitch/roll here (and recalibrating the
            # zero_* references).
            return ImuReading(
                yaw_degrees=-sensor_y,
                pitch_degrees=gravity_x,
                roll_degrees=gravity_z,
                ok=True,
            )
        except (RuntimeError, ValueError, OSError):
            # OSError covers I2C/bus failures from the BNO stack -- a failed IMU
            # read must not take down the tick before range readings publish.
            return ImuReading(
                yaw_degrees=None,
                pitch_degrees=None,
                roll_degrees=None,
                ok=False,
            )

    def cleanup(self) -> None:
        pass
