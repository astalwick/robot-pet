#!/usr/bin/env python3
"""Motor rail power service: owns the MOSFET GPIO and low-LiPo cutoff."""

from __future__ import annotations

import argparse
import json
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.log import setup_logging
from telemetry.messages import MOTOR_BATTERY_CUTOFF_VOLTAGE, MOTOR_BATTERY_WARNING_VOLTAGE, motor_rail_update
from telemetry.paths import DEFAULT_MOTOR_BATTERY_CACHE, DEFAULT_PUBLISH_SOCKET, DEFAULT_SUBSCRIBE_SOCKET
from telemetry.socket_client import publish_message, subscribe


log = setup_logging("robot-battery")


@dataclass(frozen=True)
class BatteryConfig:
    mosfet_gpio: int = 24
    low_voltage_cutoff: float = MOTOR_BATTERY_CUTOFF_VOLTAGE
    warning_voltage: float = MOTOR_BATTERY_WARNING_VOLTAGE
    low_voltage_seconds: float = 2.0
    cutoff_log_interval: float = 30.0
    telemetry_interval: float = 1.0
    motion_power_hold_seconds: float = 5.0
    recovery_probe_seconds: float = 3.0
    recovery_retry_seconds: float = 30.0
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET
    telemetry_subscribe_socket: str = DEFAULT_SUBSCRIBE_SOCKET
    motor_battery_cache_path: str = DEFAULT_MOTOR_BATTERY_CACHE


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
        self.state = "off"
        self.reason: str | None = "startup"
        self.last_pack_voltage: float | None = None
        self.low_voltage_seen_at: float | None = None
        self.next_cutoff_log_at = 0.0
        self.next_telemetry_at = 0.0
        self.motion_power_hold_until = 0.0
        self.recovery_probe_until = 0.0
        self.next_recovery_probe_at = 0.0
        self.mosfet = None
        self.last_motor_battery: dict[str, Any] | None = None

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def run_forever(self) -> None:
        self.mosfet = self.mosfet_factory(
            self.config.mosfet_gpio,
            active_high=True,
            initial_value=False,
        )
        self._publish_status()

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
            self._remember_motor_battery(snapshot)
        self._sync_power(snapshot, now)
        if self.state == "low_battery_cutoff" and now >= self.next_cutoff_log_at:
            log.warning(
                "motor LiPo discharged; motor rail off, will re-check on the next motion request "
                "(last pack voltage: %s)",
                f"{self.last_pack_voltage:.2f}V" if self.last_pack_voltage is not None else "unknown",
            )
            self.next_cutoff_log_at = now + self.config.cutoff_log_interval
        if now >= self.next_telemetry_at:
            self._publish_status()
            self.next_telemetry_at = now + self.config.telemetry_interval

    def _handle_voltage(self, voltage: float, now: float) -> None:
        self.last_pack_voltage = voltage
        if self.state == "recovering":
            self._decide_recovery(voltage, now)
            return
        if self.state == "low_battery_cutoff":
            return

        if voltage > self.config.low_voltage_cutoff:
            self.low_voltage_seen_at = None
            if self.state != "off" and voltage < self.config.warning_voltage:
                self.state = "warning"
                self.reason = "low_battery_warning"
            elif self.state != "off":
                self.state = "on"
                self.reason = None
            return

        if self.low_voltage_seen_at is None:
            self.low_voltage_seen_at = now
            if self.state != "off":
                self.state = "warning"
            self.reason = "low_battery_pending_cutoff"
            return

        if now - self.low_voltage_seen_at >= self.config.low_voltage_seconds:
            log.warning("motor LiPo %.2fV at/below %.2fV; cutting motor rail", voltage, self.config.low_voltage_cutoff)
            self.next_recovery_probe_at = now + self.config.recovery_retry_seconds
            self._rail_off("low_battery_cutoff")

    def _decide_recovery(self, voltage: float, now: float) -> None:
        # We powered the rail just to read the pack under no load. Only return to
        # normal once it sits above the warning line; recovering anywhere inside the
        # warning band would let a sagging pack chatter the rail on and off.
        if voltage > self.config.warning_voltage:
            self.low_voltage_seen_at = None
            self.state = "on"
            self.reason = None
            self._publish_status()
            log.info("motor LiPo recovered at %.2fV; motor rail restored", voltage)
            return
        log.warning(
            "motor LiPo still low at %.2fV (need above %.2fV to recover); back to cutoff",
            voltage,
            self.config.warning_voltage,
        )
        self.next_recovery_probe_at = now + self.config.recovery_retry_seconds
        self._rail_off("low_battery_cutoff")

    def _fresh_pack_voltage(self, snapshot: dict[str, Any]) -> float | None:
        source = (snapshot.get("sources") or {}).get("robot_motion") or {}
        if source.get("stale") is not False:
            return None

        battery = snapshot.get("motor_battery")
        if not isinstance(battery, dict):
            return None
        if battery.get("stale") is True:
            return None
        voltage = battery.get("pack_voltage")
        if voltage is None:
            return None
        return float(voltage)

    def _remember_motor_battery(self, snapshot: dict[str, Any]) -> None:
        battery = snapshot.get("motor_battery")
        if isinstance(battery, dict):
            self.last_motor_battery = dict(battery)

    def _sync_power(self, snapshot: dict[str, Any], now: float) -> None:
        wants_motion = self._gamepad_connected(snapshot) or self._motion_power_requested(snapshot)

        if self.state == "recovering":
            # Hold the rail on until a fresh reading decides recover-or-cutoff; give up
            # if the request went away or no reading arrived before the probe timed out.
            if not wants_motion or now >= self.recovery_probe_until:
                self.next_recovery_probe_at = now + self.config.recovery_retry_seconds
                self._rail_off("low_battery_cutoff")
            return

        if self.state == "low_battery_cutoff":
            # A motion request powers the rail briefly to re-read the pack, rate-limited
            # so a still-low battery can't power-cycle the RoboClaw on every snapshot.
            if wants_motion and now >= self.next_recovery_probe_at:
                self._start_recovery_probe(now)
            return

        if self._gamepad_connected(snapshot):
            self._rail_on("gamepad_connected")
        elif self._motion_power_requested(snapshot):
            self.motion_power_hold_until = now + self.config.motion_power_hold_seconds
            self._rail_on("motion_power_requested")
        elif now < self.motion_power_hold_until:
            self._rail_on("motion_power_hold")
        elif self.state != "off":
            self._rail_off("idle_no_gamepad")

    def _gamepad_connected(self, snapshot: dict[str, Any]) -> bool:
        source = (snapshot.get("sources") or {}).get("gamepad") or {}
        if source.get("stale") is not False:
            return False
        gamepad = snapshot.get("gamepad")
        return isinstance(gamepad, dict) and gamepad.get("connected") is True

    def _motion_power_requested(self, snapshot: dict[str, Any]) -> bool:
        source = (snapshot.get("sources") or {}).get("robot_motion") or {}
        if source.get("stale") is not False:
            return False
        drive_status = snapshot.get("drive_status")
        return isinstance(drive_status, dict) and drive_status.get("motion_power_requested") is True

    def _start_recovery_probe(self, now: float) -> None:
        self.low_voltage_seen_at = None
        self.recovery_probe_until = now + self.config.recovery_probe_seconds
        if self.mosfet is not None:
            self.mosfet.on()
        self.state = "recovering"
        self.reason = "recovery_probe"
        self._publish_status()
        log.info("motor LiPo cutoff: powering rail to re-check battery")

    def _rail_on(self, reason: str) -> None:
        if self.state in ("low_battery_cutoff", "recovering"):
            return
        if self.state in ("on", "warning"):
            return
        if self.last_pack_voltage is not None and self.last_pack_voltage <= self.config.low_voltage_cutoff:
            self._rail_off("low_battery_cutoff")
            return
        if self.mosfet is not None:
            self.mosfet.on()
        if self.last_pack_voltage is not None and self.last_pack_voltage < self.config.warning_voltage:
            self.state = "warning"
            self.reason = "low_battery_warning"
        else:
            self.state = "on"
            self.reason = reason
        self._publish_status()
        log.info("motor rail on: %s", reason)

    def _rail_off(self, reason: str) -> None:
        if self.state == "off" and self.reason == reason:
            return
        self._cache_motor_battery(reason)
        if self.mosfet is not None:
            self.mosfet.off()
        self.state = "off" if reason != "low_battery_cutoff" else "low_battery_cutoff"
        self.reason = reason
        self._publish_status()
        log.info("motor rail off: %s", reason)

    def _cache_motor_battery(self, reason: str) -> None:
        if self.last_motor_battery is None:
            return
        try:
            path = Path(self.config.motor_battery_cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f".{path.name}.{id(self)}.tmp")
            tmp_path.write_text(
                json.dumps(
                    {
                        "cached_at": time.time(),
                        "reason": reason,
                        "motor_battery": self.last_motor_battery,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            tmp_path.replace(path)
        except OSError as exc:
            log.warning("motor battery cache write failed: %s", exc)

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
    parser.add_argument("--low-voltage-cutoff", type=float, default=MOTOR_BATTERY_CUTOFF_VOLTAGE)
    parser.add_argument("--warning-voltage", type=float, default=MOTOR_BATTERY_WARNING_VOLTAGE)
    parser.add_argument("--low-voltage-seconds", type=float, default=2.0)
    parser.add_argument("--cutoff-log-interval", type=float, default=30.0)
    parser.add_argument("--telemetry-interval", type=float, default=1.0)
    parser.add_argument("--motion-power-hold-seconds", type=float, default=5.0)
    parser.add_argument("--recovery-probe-seconds", type=float, default=3.0)
    parser.add_argument("--recovery-retry-seconds", type=float, default=30.0)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--telemetry-subscribe-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    parser.add_argument("--motor-battery-cache", default=DEFAULT_MOTOR_BATTERY_CACHE)
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
            motion_power_hold_seconds=args.motion_power_hold_seconds,
            recovery_probe_seconds=args.recovery_probe_seconds,
            recovery_retry_seconds=args.recovery_retry_seconds,
            telemetry_socket=args.telemetry_socket,
            telemetry_subscribe_socket=args.telemetry_subscribe_socket,
            motor_battery_cache_path=args.motor_battery_cache,
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
