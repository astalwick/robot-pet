#!/usr/bin/env python3
"""Range sensor service: polls ToF sensors and publishes telemetry."""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

from config.sensors import (
    DEFAULT_CONFIG_PATH,
    ImuEntry,
    SensorsConfig,
    SensorsConfigError,
    cliff_trip_mm,
    forward_stop_mm,
    load_sensors_config,
)
from drivers.imu import ImuDriver
from drivers.range import RangeDriver
from lib.log import setup_logging
from telemetry.messages import imu_reading_to_dict, reading_to_dict, sensors_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


CONFIG_POLL_INTERVAL = 1.0
DISABLED_PUBLISH_INTERVAL = 1.0
# Cap the IMU's contribution to publish latency: a slow or absent IMU can add at
# most this much delay, so it can't push an otherwise-fresh range+cliff publish
# past the telemetry stale threshold (1.0s). This bounds the IMU only -- it does
# not guarantee freshness, since at low poll rates the period itself can exceed
# the threshold (which the motion safety gate already fails safe on by blocking
# forward motion while sensors read stale).
MAX_IMU_READ_SECONDS = 0.25

log = setup_logging("robot-sensors")


def _sensor_signature(config: SensorsConfig) -> tuple[tuple[str, str, int], ...]:
    return tuple((entry.name, entry.kind, entry.mux_channel) for entry in config.sensors)


def _imu_signature(imu: ImuEntry) -> tuple[Any, ...]:
    return (
        imu.enabled,
        imu.kind,
        imu.mux_channel,
        imu.address,
        imu.mode,
        imu.zero_quaternion,
        imu.zero_gravity,
    )


def _reading_dict_with_config(reading: Any, config: SensorsConfig) -> dict[str, Any]:
    payload = reading_to_dict(reading)
    entry = next((sensor for sensor in config.sensors if sensor.name == reading.name), None)
    if entry is None or entry.role is None:
        return payload
    payload["role"] = entry.role
    if entry.role == "forward":
        stop_below_mm = forward_stop_mm(entry, config.safety)
        if stop_below_mm is not None:
            payload["stop_below_mm"] = stop_below_mm
    if entry.role == "cliff":
        trip_above_mm = cliff_trip_mm(entry, config.safety)
        if trip_above_mm is not None:
            payload["trip_above_mm"] = trip_above_mm
    return payload


class SensorsService:
    """Poll range sensors and publish readings through telemetry."""

    def __init__(
        self,
        *,
        config_path: str,
        publish: Callable[[dict[str, Any]], Any],
        driver_factory: Callable[[SensorsConfig], RangeDriver],
        imu_driver_factory: Callable[[SensorsConfig], ImuDriver | None] | None = None,
        time_fn: Callable[[], float] = time.time,
    ):
        self.config_path = config_path
        self.publish = publish
        self.driver_factory = driver_factory
        self.imu_driver_factory = imu_driver_factory or default_imu_driver_factory
        self.time_fn = time_fn

        self.config = SensorsConfig()
        self._config_mtime: float | None = None
        self._config_error: str | None = None
        self._driver: RangeDriver | None = None
        self._imu_driver: ImuDriver | None = None
        self._driver_signature: tuple[Any, ...] | None = None
        self._imu_driver_signature: tuple[Any, ...] | None = None
        self._driver_error: str | None = None
        self._last_disabled_publish: float | None = None
        self._next_poll_time: float | None = None

    def tick(self) -> float:
        self._reload_config_if_changed()

        if self._config_error is not None:
            self._publish_status("error", error=self._config_error)
            return CONFIG_POLL_INTERVAL

        if not self.config.enabled:
            self._release_drivers()
            now = self.time_fn()
            if (
                self._last_disabled_publish is None
                or now - self._last_disabled_publish >= DISABLED_PUBLISH_INTERVAL
            ):
                self._publish_status("disabled")
                self._last_disabled_publish = now
            return CONFIG_POLL_INTERVAL

        if not self._ensure_driver():
            return CONFIG_POLL_INTERVAL

        now = self.time_fn()
        if self._next_poll_time is not None and now < self._next_poll_time:
            return min(self._next_poll_time - now, CONFIG_POLL_INTERVAL)

        self._next_poll_time = now + self._poll_period()
        readings = self._driver.read_all()
        imu = None
        if self._imu_driver is not None:
            imu = imu_reading_to_dict(self._imu_driver.read(timeout=min(self._poll_period(), MAX_IMU_READ_SECONDS)))
        elif self.config.imu.enabled:
            imu = {"ok": False, "reason": "uncalibrated"}
        self.publish(
            sensors_update(
                enabled=True,
                status="polling",
                readings=[_reading_dict_with_config(reading, self.config) for reading in readings],
                poll_rate_hz=self.config.poll_rate_hz,
                imu=imu,
                now=now,
            )
        )
        return self._next_sleep(now)

    def cleanup(self) -> None:
        self._release_drivers()

    def _poll_period(self) -> float:
        return 1.0 / self.config.poll_rate_hz

    def _next_sleep(self, now: float) -> float:
        if self._next_poll_time is None:
            return CONFIG_POLL_INTERVAL
        return min(max(self._next_poll_time - now, 0.0), CONFIG_POLL_INTERVAL)

    def _ensure_driver(self) -> bool:
        range_signature = _sensor_signature(self.config)
        imu_signature = _imu_signature(self.config.imu)
        if (
            self._driver is not None
            and range_signature == self._driver_signature
            and imu_signature == self._imu_driver_signature
        ):
            return True

        self._release_drivers()
        try:
            self._driver = self.driver_factory(self.config)
            self._imu_driver = self.imu_driver_factory(self.config)
            self._driver_signature = range_signature
            self._imu_driver_signature = imu_signature
            self._driver_error = None
            log.info("range driver ready for %d sensor(s)", len(self.config.sensors))
            return True
        except Exception as exc:  # noqa: BLE001 -- hardware init can fail many ways
            self._driver_error = str(exc)
            log.warning("sensor driver init failed: %s", exc)
            self._publish_status("driver_unavailable", error=self._driver_error)
            self._next_poll_time = self.time_fn() + self._poll_period()
            return False

    def _release_drivers(self) -> None:
        if self._driver is not None:
            self._driver.cleanup()
        self._driver = None
        if self._imu_driver is not None:
            self._imu_driver.cleanup()
        self._imu_driver = None
        self._driver_signature = None
        self._imu_driver_signature = None

    def _publish_status(self, status: str, error: str | None = None) -> None:
        self.publish(
            sensors_update(
                enabled=self.config.enabled,
                status=status,
                readings=[],
                poll_rate_hz=self.config.poll_rate_hz,
                error=error,
            )
        )

    def _reload_config_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            mtime = None

        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime

        if mtime is None:
            self.config = SensorsConfig()
            self._config_error = None
            self._next_poll_time = None
            return

        try:
            # _ensure_driver() owns the driver lifecycle: it compares signatures
            # and rebuilds only when the sensor/IMU hardware config actually
            # changes, so a poll-rate-only edit here won't churn the drivers.
            self.config = load_sensors_config(self.config_path)
            self._config_error = None
            self._next_poll_time = None
            log.info(
                "sensors config loaded: enabled=%s rate_hz=%.2f sensors=%d",
                self.config.enabled,
                self.config.poll_rate_hz,
                len(self.config.sensors),
            )
        except SensorsConfigError as exc:
            self._config_error = str(exc)
            log.warning("sensors config invalid, keeping last good config: %s", exc)


def default_driver_factory(config: SensorsConfig) -> RangeDriver:
    return RangeDriver(config.driver_sensors())


def default_imu_driver_factory(config: SensorsConfig) -> ImuDriver | None:
    imu_config = config.driver_imu()
    if imu_config is None:
        return None
    return ImuDriver(imu_config)


def run_loop(service: SensorsService, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            sleep_seconds = service.tick()
        except Exception as exc:  # noqa: BLE001 -- never let the service die mid-loop
            log.exception("sensors tick failed: %s", exc)
            sleep_seconds = CONFIG_POLL_INTERVAL
        stop.wait(sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot range sensor service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    service = SensorsService(
        config_path=args.config,
        publish=lambda message: publish_message(args.telemetry_socket, message),
        driver_factory=default_driver_factory,
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info("sensors service starting")
    try:
        run_loop(service, stop)
    finally:
        service.cleanup()
    log.info("sensors service stopped")


if __name__ == "__main__":
    main()
