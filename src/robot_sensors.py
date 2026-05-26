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
    SensorsConfig,
    SensorsConfigError,
    load_sensors_config,
)
from drivers.range import RangeDriver
from lib.log import setup_logging
from telemetry.messages import reading_to_dict, sensors_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


CONFIG_POLL_INTERVAL = 1.0
DISABLED_PUBLISH_INTERVAL = 1.0

log = setup_logging("robot-sensors")


def _sensor_signature(config: SensorsConfig) -> tuple[tuple[str, str, int], ...]:
    return tuple((entry.name, entry.kind, entry.mux_channel) for entry in config.sensors)


class SensorsService:
    """Poll range sensors and publish readings through telemetry."""

    def __init__(
        self,
        *,
        config_path: str,
        publish: Callable[[dict[str, Any]], Any],
        driver_factory: Callable[[SensorsConfig], RangeDriver],
        time_fn: Callable[[], float] = time.time,
    ):
        self.config_path = config_path
        self.publish = publish
        self.driver_factory = driver_factory
        self.time_fn = time_fn

        self.config = SensorsConfig()
        self._config_mtime: float | None = None
        self._config_error: str | None = None
        self._driver: RangeDriver | None = None
        self._driver_signature: tuple[Any, ...] | None = None
        self._driver_error: str | None = None
        self._last_disabled_publish: float | None = None
        self._next_poll_time: float | None = None

    def tick(self) -> float:
        self._reload_config_if_changed()

        if self._config_error is not None:
            self._publish_status("error", error=self._config_error)
            return CONFIG_POLL_INTERVAL

        if not self.config.enabled:
            self._release_driver()
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
        self.publish(
            sensors_update(
                enabled=True,
                status="polling",
                readings=[reading_to_dict(reading) for reading in readings],
                poll_rate_hz=self.config.poll_rate_hz,
            )
        )
        return self._next_sleep(now)

    def cleanup(self) -> None:
        self._release_driver()

    def _poll_period(self) -> float:
        return 1.0 / self.config.poll_rate_hz

    def _next_sleep(self, now: float) -> float:
        if self._next_poll_time is None:
            return CONFIG_POLL_INTERVAL
        return min(max(self._next_poll_time - now, 0.0), CONFIG_POLL_INTERVAL)

    def _ensure_driver(self) -> bool:
        signature = _sensor_signature(self.config)
        if self._driver is not None and signature == self._driver_signature:
            return True

        self._release_driver()
        try:
            self._driver = self.driver_factory(self.config)
            self._driver_signature = signature
            self._driver_error = None
            log.info("range driver ready for %d sensor(s)", len(self.config.sensors))
            return True
        except Exception as exc:  # noqa: BLE001 -- hardware init can fail many ways
            self._driver_error = str(exc)
            log.warning("range driver init failed: %s", exc)
            self._publish_status("driver_unavailable", error=self._driver_error)
            self._next_poll_time = self.time_fn() + self._poll_period()
            return False

    def _release_driver(self) -> None:
        if self._driver is None:
            return
        self._driver.cleanup()
        self._driver = None
        self._driver_signature = None

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
            self._driver_signature = None
            return

        try:
            new_config = load_sensors_config(self.config_path)
            new_signature = _sensor_signature(new_config)
            if new_signature != _sensor_signature(self.config):
                self._release_driver()
            self.config = new_config
            self._config_error = None
            self._next_poll_time = None
            self._driver_signature = None
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
