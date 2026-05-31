#!/usr/bin/env python3
"""Motor rail power service: owns the MOSFET GPIO and low-LiPo cutoff."""

from __future__ import annotations

import argparse
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from lib.log import setup_logging
from telemetry.messages import motor_rail_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET, DEFAULT_SUBSCRIBE_SOCKET
from telemetry.socket_client import publish_message, subscribe


log = setup_logging("robot-battery")


@dataclass(frozen=True)
class BatteryConfig:
    mosfet_gpio: int = 24
    low_voltage_cutoff: float = 10.8
    warning_voltage: float = 11.1
    low_voltage_seconds: float = 2.0
    cutoff_log_interval: float = 30.0
    telemetry_interval: float = 1.0
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET
    telemetry_subscribe_socket: str = DEFAULT_SUBSCRIBE_SOCKET


class BatteryRunner:
    def __init__(
        self,
        config: BatteryConfig,
        mosfet_factory: Callable[..., Any] | None = None,
        telemetry_subscriber: Callable[[str], Iterable[dict[str, Any]]] | None = None,
        telemetry_publisher: Callable[[str, dict[str, Any]], bool] = publish_message,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.mosfet_factory = mosfet_factory or self._mosfet_factory
        self.telemetry_subscriber = telemetry_subscriber or self._telemetry_subscriber
        self.telemetry_publisher = telemetry_publisher
        self.clock = clock

        self.stop_requested = False
        self.state = "starting"
        self.reason: str | None = None
        self.last_pack_voltage: float | None = None
        self.low_voltage_seen_at: float | None = None
        self.next_cutoff_log_at = 0.0
        self.next_telemetry_at = 0.0
        self.mosfet = None

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def run_forever(self) -> None:
        self.mosfet = self.mosfet_factory(
            self.config.mosfet_gpio,
            active_high=True,
            initial_value=False,
        )
        self._rail_on("startup")

        try:
            for snapshot in self.telemetry_subscriber(self.config.telemetry_subscribe_socket):
                if self.stop_requested:
                    return
                self._handle_snapshot(snapshot)
        finally:
            self._rail_off("service_stopped")
            if self.mosfet is not None:
                self.mosfet.close()

    def _handle_snapshot(self, snapshot: dict[str, Any]) -> None:
        now = self.clock()
        voltage = self._fresh_pack_voltage(snapshot)
        if voltage is None:
            self.low_voltage_seen_at = None
        else:
            self._handle_voltage(voltage, now)
        if self.state == "low_battery_cutoff" and now >= self.next_cutoff_log_at:
            log.warning(
                "motor LiPo discharged; motor rail is off until robot-battery restarts "
                "(last pack voltage: %s)",
                f"{self.last_pack_voltage:.2f}V" if self.last_pack_voltage is not None else "unknown",
            )
            self.next_cutoff_log_at = now + self.config.cutoff_log_interval
        if now >= self.next_telemetry_at:
            self._publish_status()
            self.next_telemetry_at = now + self.config.telemetry_interval

    def _handle_voltage(self, voltage: float, now: float) -> None:
        self.last_pack_voltage = voltage
        if self.state == "low_battery_cutoff":
            return

        if voltage > self.config.low_voltage_cutoff:
            self.low_voltage_seen_at = None
            if voltage < self.config.warning_voltage:
                self.state = "warning"
                self.reason = "low_battery_warning"
            else:
                self.state = "on"
                self.reason = None
            return

        if self.low_voltage_seen_at is None:
            self.low_voltage_seen_at = now
            self.state = "warning"
            self.reason = "low_battery_pending_cutoff"
            return

        if now - self.low_voltage_seen_at >= self.config.low_voltage_seconds:
            log.warning("motor LiPo %.2fV at/below %.2fV; cutting motor rail", voltage, self.config.low_voltage_cutoff)
            self._rail_off("low_battery_cutoff")
            self.state = "low_battery_cutoff"
            self._publish_status()

    def _fresh_pack_voltage(self, snapshot: dict[str, Any]) -> float | None:
        source = (snapshot.get("sources") or {}).get("gamepad_teleop") or {}
        if source.get("stale") is not False:
            return None

        battery = snapshot.get("motor_battery")
        if not isinstance(battery, dict):
            return None
        voltage = battery.get("pack_voltage")
        if voltage is None:
            return None
        return float(voltage)

    def _rail_on(self, reason: str) -> None:
        if self.mosfet is not None:
            self.mosfet.on()
        self.state = "on"
        self.reason = reason
        self._publish_status()
        log.info("motor rail on: %s", reason)

    def _rail_off(self, reason: str) -> None:
        if self.mosfet is not None:
            self.mosfet.off()
        self.state = "off" if reason != "low_battery_cutoff" else "low_battery_cutoff"
        self.reason = reason
        self._publish_status()
        log.info("motor rail off: %s", reason)

    def _publish_status(self) -> None:
        self.telemetry_publisher(
            self.config.telemetry_socket,
            motor_rail_update(
                state=self.state,
                mosfet_gpio=self.config.mosfet_gpio,
                last_pack_voltage=self.last_pack_voltage,
                reason=self.reason,
                low_voltage_cutoff=self.config.low_voltage_cutoff,
                warning_voltage=self.config.warning_voltage,
            ),
        )

    def _mosfet_factory(self, *args, **kwargs):
        from gpiozero import OutputDevice

        return OutputDevice(*args, **kwargs)

    def _telemetry_subscriber(self, socket_path: str):
        return subscribe(socket_path, reconnect_interval=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Motor rail MOSFET and LiPo protection service.")
    parser.add_argument("--mosfet-gpio", type=int, default=24)
    parser.add_argument("--low-voltage-cutoff", type=float, default=10.8)
    parser.add_argument("--warning-voltage", type=float, default=11.1)
    parser.add_argument("--low-voltage-seconds", type=float, default=2.0)
    parser.add_argument("--cutoff-log-interval", type=float, default=30.0)
    parser.add_argument("--telemetry-interval", type=float, default=1.0)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--telemetry-subscribe-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = BatteryRunner(
        BatteryConfig(
            mosfet_gpio=args.mosfet_gpio,
            low_voltage_cutoff=args.low_voltage_cutoff,
            warning_voltage=args.warning_voltage,
            low_voltage_seconds=args.low_voltage_seconds,
            cutoff_log_interval=args.cutoff_log_interval,
            telemetry_interval=args.telemetry_interval,
            telemetry_socket=args.telemetry_socket,
            telemetry_subscribe_socket=args.telemetry_subscribe_socket,
        )
    )

    def stop(*_args) -> None:
        runner.request_stop()
        raise SystemExit

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runner.run_forever()


if __name__ == "__main__":
    main()
