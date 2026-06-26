"""
Range sensors behind a TCA9548A I2C mux (VL53L0X / VL53L1X).

Uses Adafruit CircuitPython libraries via Blinka on the Pi. Tests inject fake
mux and sensor factories so CI never needs hardware or Blinka.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


log = logging.getLogger(__name__)

VL53L1X_READY_RECHECK_SECONDS = 0.005


@dataclass(frozen=True)
class RangeSensorConfig:
    name: str
    kind: str
    channel: int
    offset_mm: int = 0


@dataclass(frozen=True)
class RangeReading:
    name: str
    kind: str
    channel: int
    distance_mm: int | None
    ok: bool


DEFAULT_SENSORS = [
    RangeSensorConfig("cliff_left", "vl53l0x", 0),
    RangeSensorConfig("cliff_center", "vl53l0x", 1),
    RangeSensorConfig("cliff_right", "vl53l0x", 2),
]


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


def _default_vl53l0x_factory(channel_bus: Any, address: int) -> Any:
    import adafruit_vl53l0x

    return adafruit_vl53l0x.VL53L0X(channel_bus, address=address)


def _default_vl53l1x_factory(channel_bus: Any, address: int) -> Any:
    import adafruit_vl53l1x

    return adafruit_vl53l1x.VL53L1X(channel_bus, address=address)


class RangeDriver:
    """Read distance in mm from VL53 sensors on a TCA9548A mux."""

    def __init__(
        self,
        sensors: list[RangeSensorConfig],
        mux_address: int = 0x70,
        range_address: int = 0x29,
        i2c_factory: Callable[[], Any] | None = None,
        mux_factory: Callable[..., Any] | None = None,
        vl53l0x_factory: Callable[..., Any] | None = None,
        vl53l1x_factory: Callable[..., Any] | None = None,
    ):
        self.mux_address = mux_address
        self.range_address = range_address
        self._lock = threading.Lock()
        self._entries: list[tuple[RangeSensorConfig, Any]] = []

        if i2c_factory is None:
            i2c_factory = _default_i2c_factory
        if mux_factory is None:
            mux_factory = _default_mux_factory
        if vl53l0x_factory is None:
            vl53l0x_factory = _default_vl53l0x_factory
        if vl53l1x_factory is None:
            vl53l1x_factory = _default_vl53l1x_factory

        i2c = i2c_factory()
        mux = mux_factory(i2c, mux_address)

        for config in sensors:
            channel_bus = mux[config.channel]
            if config.kind == "vl53l0x":
                sensor = vl53l0x_factory(channel_bus, range_address)
                # Continuous mode: the sensor keeps measuring on its own clock,
                # so every sensor integrates in parallel and read() just grabs
                # the latest result instead of blocking for a fresh measurement.
                sensor.start_continuous()
            elif config.kind == "vl53l1x":
                sensor = vl53l1x_factory(channel_bus, range_address)
                # Narrow vertical FOV: full width, thin horizontal strip,
                # nudged one SPAD upward from center.
                sensor.stop_ranging()
                sensor.distance_mode = 1
                sensor.timing_budget = 50
                sensor.roi_xy = (16, 2)
                sensor.roi_center = 198
                sensor.start_ranging()
            else:
                raise ValueError(f"unknown range sensor kind: {config.kind!r}")
            self._entries.append((config, sensor))

        self._by_name = {config.name: (config, sensor) for config, sensor in self._entries}

    def read(self, name: str) -> RangeReading:
        config, sensor = self._by_name[name]
        with self._lock:
            return self._read_locked(config, sensor)

    def read_all(self) -> list[RangeReading]:
        readings = []
        with self._lock:
            for config, sensor in self._entries:
                readings.append(self._read_locked(config, sensor))
        return readings

    def cleanup(self) -> None:
        with self._lock:
            for config, sensor in self._entries:
                try:
                    if config.kind == "vl53l0x":
                        sensor.stop_continuous()
                    elif config.kind == "vl53l1x":
                        sensor.stop_ranging()
                except Exception as exc:
                    log.warning("sensor cleanup failed for %s: %s", config.name, exc)

    def _read_locked(self, config: RangeSensorConfig, sensor: Any) -> RangeReading:
        try:
            if config.kind == "vl53l1x":
                if not sensor.data_ready:
                    time.sleep(VL53L1X_READY_RECHECK_SECONDS)
                if not sensor.data_ready:
                    return RangeReading(
                        name=config.name,
                        kind=config.kind,
                        channel=config.channel,
                        distance_mm=None,
                        ok=False,
                    )
                distance_cm = sensor.distance
                sensor.clear_interrupt()
                if distance_cm is None:
                    # Valid measurement: nothing in range (treat as infinity).
                    return RangeReading(
                        name=config.name,
                        kind=config.kind,
                        channel=config.channel,
                        distance_mm=None,
                        ok=True,
                    )
                distance_mm = max(0, int(distance_cm * 10) - config.offset_mm)
            else:
                distance_mm = max(0, int(sensor.range) - config.offset_mm)
            return RangeReading(
                name=config.name,
                kind=config.kind,
                channel=config.channel,
                distance_mm=distance_mm,
                ok=True,
            )
        except Exception as exc:
            log.warning(
                "range read failed for %s (channel %s): %s",
                config.name,
                config.channel,
                exc,
            )
            return RangeReading(
                name=config.name,
                kind=config.kind,
                channel=config.channel,
                distance_mm=None,
                ok=False,
            )
