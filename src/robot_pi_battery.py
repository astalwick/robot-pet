#!/usr/bin/env python3
"""Pi UPS battery telemetry service."""

from __future__ import annotations

import argparse
import signal
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
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET


class PiBatteryService:
    def __init__(
        self,
        config: PiBatteryConfig,
        publish: Callable[[dict[str, Any]], Any],
        driver_factory: Callable[[], Any] | None = None,
    ):
        self.config = config
        self.publish = publish
        self.driver_factory = driver_factory or self._driver_factory
        self.driver = None

    def tick(self) -> None:
        try:
            if self.driver is None:
                self.driver = self.driver_factory()
            self.publish(pi_battery_update(pi_battery_message(self.driver.read())))
        except Exception as exc:
            log.warning("UPS HAT E read failed: %s", exc)
            self._release_driver()
            self.publish(pi_battery_update(pi_battery_message(None, error=str(exc))))

    def cleanup(self) -> None:
        self._release_driver()

    def _release_driver(self) -> None:
        if self.driver is None:
            return
        self.driver.cleanup()
        self.driver = None

    def _driver_factory(self) -> UpsHatEDriver:
        return UpsHatEDriver(bus=self.config.bus, address=self.config.address)


def run_loop(service: PiBatteryService, stop: threading.Event) -> None:
    while not stop.is_set():
        service.tick()
        stop.wait(service.config.poll_interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish Pi UPS battery telemetry.")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=UPS_ADDRESS)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = PiBatteryConfig(
        bus=args.bus,
        address=args.address,
        poll_interval=args.poll_interval,
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
