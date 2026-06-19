#!/usr/bin/env python3
"""Pi UPS battery telemetry service."""

from __future__ import annotations

import argparse
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from drivers.ups_hat_e import UPS_ADDRESS, UpsHatEDriver
from lib.log import setup_logging
from telemetry.messages import pi_battery_message, pi_battery_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


log = setup_logging("robot-pi-battery")


@dataclass(frozen=True)
class PiBatteryConfig:
    bus: int = 1
    address: int = UPS_ADDRESS
    poll_interval: float = 1.0
    warning_voltage: float = 13.3
    shutdown_voltage: float = 13.0
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET
    shutdown_command: tuple[str, ...] = ("sudo", "shutdown", "-h", "now")


class PiBatteryService:
    def __init__(
        self,
        config: PiBatteryConfig,
        publish: Callable[[dict[str, Any]], Any],
        driver_factory: Callable[[], Any] | None = None,
        command_runner: Callable[[tuple[str, ...]], Any] | None = None,
    ):
        self.config = config
        self.publish = publish
        self.driver_factory = driver_factory or self._driver_factory
        self.command_runner = command_runner or self._command_runner
        self.driver = None
        self.shutdown_requested = False

    def tick(self) -> None:
        try:
            if self.driver is None:
                self.driver = self.driver_factory()
            reading = self.driver.read()
            shutdown_pending = self.shutdown_requested or reading.battery_mv / 1000.0 <= self.config.shutdown_voltage
            self.publish(
                pi_battery_update(
                    pi_battery_message(
                        reading,
                        warning_voltage=self.config.warning_voltage,
                        shutdown_voltage=self.config.shutdown_voltage,
                        shutdown_pending=shutdown_pending,
                    )
                )
            )
            if shutdown_pending and not self.shutdown_requested:
                self.shutdown_requested = True
                self._shutdown(reading.battery_mv / 1000.0)
        except Exception as exc:
            log.warning("UPS HAT E read failed: %s", exc)
            self._release_driver()
            self.publish(
                pi_battery_update(
                    pi_battery_message(
                        None,
                        error=str(exc),
                        warning_voltage=self.config.warning_voltage,
                        shutdown_voltage=self.config.shutdown_voltage,
                        shutdown_pending=self.shutdown_requested,
                    )
                )
            )

    def cleanup(self) -> None:
        self._release_driver()

    def _release_driver(self) -> None:
        if self.driver is None:
            return
        self.driver.cleanup()
        self.driver = None

    def _driver_factory(self) -> UpsHatEDriver:
        return UpsHatEDriver(bus=self.config.bus, address=self.config.address)

    def _shutdown(self, voltage: float) -> None:
        log.warning("Pi UPS %.2fV at/below %.2fV; shutting down", voltage, self.config.shutdown_voltage)
        try:
            result = self.command_runner(self.config.shutdown_command)
        except Exception as exc:
            log.error("shutdown command failed: %s", exc)
            return
        if getattr(result, "returncode", 0) != 0:
            output = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
            log.error("shutdown command failed: %s", output or f"exit {result.returncode}")

    def _command_runner(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)


def run_loop(service: PiBatteryService, stop: threading.Event) -> None:
    while not stop.is_set():
        service.tick()
        stop.wait(service.config.poll_interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish Pi UPS battery telemetry.")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=UPS_ADDRESS)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--warning-voltage", type=float, default=13.3)
    parser.add_argument("--shutdown-voltage", type=float, default=13.0)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = PiBatteryConfig(
        bus=args.bus,
        address=args.address,
        poll_interval=args.poll_interval,
        warning_voltage=args.warning_voltage,
        shutdown_voltage=args.shutdown_voltage,
        telemetry_socket=args.telemetry_socket,
    )
    service = PiBatteryService(
        config,
        publish=lambda message: publish_message(config.telemetry_socket, message),
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info("pi battery service starting")
    try:
        run_loop(service, stop)
    finally:
        service.cleanup()
    log.info("pi battery service stopped")


if __name__ == "__main__":
    main()
