#!/usr/bin/env python3
"""Motion service: owns RoboClaw, applies range-sensor safety, executes drive commands."""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from config.teleop import DriveTuning
from config.sensors import (
    DEFAULT_CONFIG_PATH,
    SensorsConfig,
    SensorsConfigError,
    load_sensors_config,
)
from control.commands import MotionCommand, WheelSpeedCommand
from control.differential_drive import DifferentialDriveMixer
from control.motion_drive import DriveCommand, DriveCommandListener
from control.motion_intent import MotionIntentBridge, MotionIntentExecutor
from control.safety_gate import apply_safety_to_qpps, evaluate_safety
from drivers.motor import MotorDriver
from lib.log import setup_logging
from telemetry.messages import (
    drive_status_message,
    gamepad_teleop_update,
    link_loop_message,
    motor_battery_message,
    wheel_message,
)
from telemetry.paths import (
    DEFAULT_MOTION_DRIVE_SOCKET,
    DEFAULT_MOTION_INTENT_SOCKET,
    DEFAULT_PUBLISH_SOCKET,
    DEFAULT_SUBSCRIBE_SOCKET,
)
from telemetry.socket_client import publish_message, subscribe


log = setup_logging("robot-motion")

DRIVE_COMMAND_STALE_SECONDS = 0.5
SENSOR_STALE_SECONDS = 1.0
STATUS_PUBLISH_INTERVAL = 0.5
SLOW_TELEMETRY_WARNING_SECONDS = 0.025


@dataclass(frozen=True)
class MotionConfig:
    port: str = "/dev/serial0"
    address: int = 0x80
    baud: int = 38400
    qpps: int = 2425
    loop_interval: float = 0.05
    retry_interval: float = 1.0
    telemetry_interval: float = 0.2
    idle_release_delay: float = 0.25
    roboclaw_timeout: float = 0.5
    sensors_config_path: str = DEFAULT_CONFIG_PATH
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET
    telemetry_subscribe_socket: str = DEFAULT_SUBSCRIBE_SOCKET
    drive_socket: str = DEFAULT_MOTION_DRIVE_SOCKET
    motion_intent_socket: str = DEFAULT_MOTION_INTENT_SOCKET


class MotionRunner:
    def __init__(
        self,
        config: MotionConfig,
        motor_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        telemetry_publisher: Callable[[str, dict[str, Any]], bool] = publish_message,
        intent_bridge_factory: Callable[[], MotionIntentBridge] | None = None,
    ):
        self.config = config
        self.motor_factory = motor_factory or self._motor_factory
        self.sleep = sleep
        self.clock = clock
        self.telemetry_publisher = telemetry_publisher
        self.intent_bridge_factory = intent_bridge_factory or self._intent_bridge_factory

        self.sensors_config = SensorsConfig()
        self._sensors_config_mtime: float | None = None
        self._sensors_config_error: str | None = None
        self._sensor_readings: list[dict[str, Any]] = []
        self._sensors_live = False
        self._sensors_lock = threading.Lock()

        self._drive_lock = threading.Lock()
        self._latest_drive: DriveCommand | None = None
        self._latest_drive_at: float | None = None
        self._drive_listener: DriveCommandListener | None = None

        self.intent_executor = MotionIntentExecutor()
        self.intent_bridge: MotionIntentBridge | None = None
        self.pending_intent_complete: Callable[[dict[str, Any]], None] | None = None

        self.mixer = DifferentialDriveMixer(qpps=config.qpps, speed_scale=1.0, turbo_scale=1.0)
        self.stop_requested = False
        self.drive_state = "stopped"
        self.stop_reason: str | None = None

        self.last_target = WheelSpeedCommand(0, 0)
        self.last_target_at: float | None = None
        self.target_active = False
        self.read_results: deque[bool] = deque(maxlen=50)
        self.consecutive_read_failures = 0
        self.last_good_read_at: float | None = None
        self.loop_samples: deque[float] = deque(maxlen=25)
        self.consecutive_motor_command_failures = 0
        self.last_motor_command_ack_at: float | None = None
        self.last_motor_command_ok: bool | None = None
        self.telemetry_publish_failures = 0
        self.last_telemetry_publish_ok: bool | None = None
        self._last_safety_blocked = False
        self._last_safety_reason: str | None = None

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def run_forever(self) -> None:
        telemetry_thread = threading.Thread(target=self._telemetry_subscribe_loop, daemon=True)
        telemetry_thread.start()
        self._start_drive_listener()
        self._start_intent_bridge()

        try:
            while not self.stop_requested:
                motor = self._wait_for_roboclaw()
                if self.stop_requested:
                    break
                self._run_motor_loop(motor)
                motor.cleanup()
        finally:
            self._stop_intent_bridge()
            self._stop_drive_listener()

    def _start_drive_listener(self) -> None:
        self._drive_listener = DriveCommandListener(self.config.drive_socket, self._on_drive_command)
        self._drive_listener.start()
        log.info("motion drive socket at %s", self.config.drive_socket)

    def _stop_drive_listener(self) -> None:
        if self._drive_listener is not None:
            self._drive_listener.stop()
            self._drive_listener = None

    def _on_drive_command(self, command: DriveCommand) -> None:
        with self._drive_lock:
            self._latest_drive = command
            self._latest_drive_at = self.clock()

    def _get_drive_command(self) -> DriveCommand | None:
        with self._drive_lock:
            command = self._latest_drive
            seen_at = self._latest_drive_at
        if command is None or seen_at is None:
            return None
        if self.clock() - seen_at > DRIVE_COMMAND_STALE_SECONDS:
            return None
        return command

    def _telemetry_subscribe_loop(self) -> None:
        for snapshot in subscribe(self.config.telemetry_subscribe_socket, reconnect_interval=1.0):
            if self.stop_requested:
                return
            self._reload_sensors_config_if_changed()
            sensors = snapshot.get("sensors")
            sources = snapshot.get("sources") or {}
            sensors_source = sources.get("sensors") or {}
            sensors_live = sensors_source.get("stale") is False
            readings = list(sensors.get("readings", [])) if isinstance(sensors, dict) else []
            with self._sensors_lock:
                self._sensor_readings = readings
                self._sensors_live = sensors_live

    def _reload_sensors_config_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.config.sensors_config_path)
        except OSError:
            mtime = None
        if mtime == self._sensors_config_mtime:
            return
        self._sensors_config_mtime = mtime
        if mtime is None:
            self.sensors_config = SensorsConfig()
            self._sensors_config_error = None
            return
        try:
            self.sensors_config = load_sensors_config(self.config.sensors_config_path)
            self._sensors_config_error = None
            log.info(
                "sensors config loaded: safety_enabled=%s cliff_above=%s forward_below=%s",
                self.sensors_config.safety.enabled,
                self.sensors_config.safety.cliff_trip_above_mm,
                self.sensors_config.safety.forward_stop_below_mm,
            )
        except SensorsConfigError as exc:
            self._sensors_config_error = str(exc)
            log.warning("sensors config invalid, keeping last good config: %s", exc)

    def _start_intent_bridge(self) -> None:
        try:
            bridge = self.intent_bridge_factory()
            bridge.start()
        except OSError as exc:
            log.error("motion intent bridge unavailable (%s): %s", self.config.motion_intent_socket, exc)
            return
        self.intent_bridge = bridge
        log.info("motion intent socket at %s", self.config.motion_intent_socket)

    def _stop_intent_bridge(self) -> None:
        self._fail_pending_intent("shutdown")
        if self.intent_bridge is not None:
            self.intent_bridge.stop()
            self.intent_bridge = None

    def _intent_bridge_factory(self) -> MotionIntentBridge:
        return MotionIntentBridge(self.config.motion_intent_socket)

    def _wait_for_roboclaw(self) -> Any:
        self._set_drive_state("waiting_for_roboclaw")
        while not self.stop_requested:
            now = self.clock()
            self._service_intent_requests(now)
            drive = self._get_drive_command()
            self._publish_waiting_telemetry(drive, roboclaw_ready=False)
            if drive is None and not self._motion_power_requested():
                self.sleep(self.config.retry_interval)
                continue
            try:
                motor = self.motor_factory()
            except Exception as exc:
                log.warning("waiting for RoboClaw: %s", exc)
                self.sleep(self.config.retry_interval)
                continue
            if self._set_wheel_speeds(motor, 0, 0):
                log.info("RoboClaw ready")
                return motor
            log.warning("initial zero-speed command was not acknowledged")
            motor.cleanup()
            self.sleep(self.config.retry_interval)
        return None

    def _run_motor_loop(self, motor: Any) -> None:
        closed_loop_active = False
        idle_started_at = self.clock()
        idle_released = False
        motor_max_qpps = self._read_motor_max_qpps(motor)
        next_telemetry = self.clock() + self.config.telemetry_interval
        self._reset_slew()
        self._set_drive_state("driving")

        while not self.stop_requested:
            cycle_started = self.clock()
            now = cycle_started
            self.loop_samples.append(now)
            self._service_intent_requests(now)

            drive = self._get_drive_command()
            if drive is None:
                intent_command = self._tick_intent(now, gamepad_active=False)
                if intent_command is None:
                    if closed_loop_active:
                        if not self._set_wheel_speeds(motor, 0, 0, now=now):
                            stop_reason = "RoboClaw zero-speed command was not acknowledged"
                            log.error("%s", stop_reason)
                            break
                        closed_loop_active = False
                        idle_started_at = now
                        idle_released = False
                    elif not idle_released and now - idle_started_at >= self.config.idle_release_delay:
                        self._release_idle(motor)
                        idle_released = True
                    elif idle_released:
                        self._publish_waiting_telemetry(None, roboclaw_ready=True)
                        break
                    self.sleep(self.config.loop_interval)
                    continue

                tuning = DriveTuning()
                drive = DriveCommand(
                    left_qpps=0,
                    right_qpps=0,
                    controller={"connected": False, "buttons": {}},
                    wheels={"left_command": 0.0, "right_command": 0.0},
                    drive_tuning=tuning.to_dict(),
                    drive_status={"state": self.drive_state, "controller_reader_alive": None},
                    link_loop={},
                )
            else:
                gamepad_active = self._gamepad_active_from_wheels(drive.wheels)
                intent_command = self._tick_intent(now, gamepad_active)

            if intent_command is not None:
                tuning = DriveTuning.from_dict(drive.drive_tuning)
                intent_mixer = DifferentialDriveMixer(
                    qpps=self.config.qpps,
                    speed_scale=tuning.speed_scale,
                    turbo_scale=tuning.turbo_scale,
                )
                mixed_wheels = intent_mixer.mix(intent_command)
                wheels = {
                    "left_command": mixed_wheels.left,
                    "right_command": mixed_wheels.right,
                }
                intent_target = intent_mixer.to_wheel_speeds(
                    intent_command,
                    turbo=drive.controller.get("buttons", {}).get("lb", False),
                )
                left_qpps = intent_target.left_qpps
                right_qpps = intent_target.right_qpps
            else:
                left_qpps = drive.left_qpps
                right_qpps = drive.right_qpps
                wheels = drive.wheels

            with self._sensors_lock:
                readings = list(self._sensor_readings)
                sensors_live = self._sensors_live
            safety = evaluate_safety(readings, self.sensors_config, sensors_live=sensors_live)
            left_qpps, right_qpps = apply_safety_to_qpps(left_qpps, right_qpps, safety)
            self._last_safety_blocked = safety.blocked
            self._last_safety_reason = safety.reason

            target = WheelSpeedCommand(left_qpps=left_qpps, right_qpps=right_qpps)
            target_is_zero = target.left_qpps == 0 and target.right_qpps == 0

            reader_alive = drive.drive_status.get("controller_reader_alive")
            if reader_alive is False and (closed_loop_active or not target_is_zero):
                stop_reason = drive.drive_status.get("stop_reason") or "controller reader stopped"
                log.error("%s; stopping motors", stop_reason)
                break

            if target_is_zero:
                if closed_loop_active:
                    if not self._set_wheel_speeds(motor, 0, 0, now=now):
                        stop_reason = "RoboClaw zero-speed command was not acknowledged"
                        log.error("%s", stop_reason)
                        break
                    closed_loop_active = False
                    idle_started_at = now
                    idle_released = False
                elif not idle_released and now - idle_started_at >= self.config.idle_release_delay:
                    self._release_idle(motor)
                    idle_released = True
            else:
                if not self._set_wheel_speeds(motor, target.left_qpps, target.right_qpps, now=now):
                    stop_reason = "RoboClaw speed command was not acknowledged"
                    log.error("%s", stop_reason)
                    break
                closed_loop_active = True
                idle_released = False

            if now >= next_telemetry:
                self._publish_telemetry(
                    drive,
                    wheels,
                    target,
                    motor,
                    motor_max_qpps,
                    safety_blocked=safety.blocked,
                    safety_reason=safety.reason,
                )
                next_telemetry = now + self.config.telemetry_interval

            self.sleep(self.config.loop_interval)

        self._set_wheel_speeds(motor, 0, 0, record=False)
        self._reset_slew()
        self._set_drive_state("stopped")

    def _gamepad_active_from_wheels(self, wheels: dict[str, Any]) -> bool:
        left = wheels.get("left_command", 0.0) or 0.0
        right = wheels.get("right_command", 0.0) or 0.0
        return left != 0.0 or right != 0.0

    def _publish_waiting_telemetry(self, drive: DriveCommand | None = None, roboclaw_ready: bool = False) -> None:
        drive_status = self._drive_status_payload(roboclaw_ready=roboclaw_ready)
        if drive is None:
            controller = {"connected": False}
            wheels = {"read_ok": False}
            drive_tuning = None
            link_loop = link_loop_message(
                read_success_rate=None,
                consecutive_read_failures=0,
                last_good_read_age_seconds=None,
                telemetry_latency_ms=None,
                command_loop_hz=None,
            )
        else:
            controller = drive.controller
            wheels = dict(drive.wheels)
            wheels["read_ok"] = False
            drive_tuning = drive.drive_tuning
            link_loop = drive.link_loop
            drive_status["controller_reader_alive"] = drive.drive_status.get("controller_reader_alive")

        message = gamepad_teleop_update(
            controller=controller,
            wheels=wheels,
            motor_battery=motor_battery_message(None),
            link_loop=link_loop,
            drive_tuning=drive_tuning,
            drive_status=drive_status,
        )
        self._publish_message(message)

    def _publish_telemetry(
        self,
        drive: DriveCommand,
        wheels: dict[str, Any],
        target: WheelSpeedCommand,
        motor: Any,
        motor_max_qpps: tuple[int | None, int | None],
        safety_blocked: bool,
        safety_reason: str | None,
    ) -> None:
        now = self.clock()
        left_actual = None
        right_actual = None
        left_current = None
        right_current = None
        pack_voltage = None
        read_ok = True
        try:
            left_actual, right_actual = motor.read_wheel_speeds()
            pack_voltage = motor.get_battery_voltage()
            currents = motor.get_currents()
            if currents is not None:
                left_current, right_current = currents
            read_ok = left_actual is not None and right_actual is not None
        except Exception as exc:
            log.warning("telemetry read failed: %s", exc)
            read_ok = False

        self._record_read_result(read_ok, now)
        link_loop = dict(drive.link_loop)
        link_loop["read_success_rate"] = self._read_success_rate()
        link_loop["consecutive_read_failures"] = self.consecutive_read_failures
        link_loop["last_good_read_age_seconds"] = (
            now - self.last_good_read_at if self.last_good_read_at is not None else None
        )
        link_loop["command_loop_hz"] = self._command_loop_hz()

        drive_status = dict(drive.drive_status)
        drive_status["motor_command_ok"] = self.last_motor_command_ok
        drive_status["consecutive_motor_command_failures"] = self.consecutive_motor_command_failures
        drive_status["last_motor_command_ack_age_seconds"] = self._last_motor_command_ack_age(now)
        drive_status["safety_blocked"] = safety_blocked
        drive_status["safety_reason"] = safety_reason
        drive_status["motion_power_requested"] = self._motion_power_requested()
        drive_status["roboclaw_ready"] = True

        message = gamepad_teleop_update(
            controller=drive.controller,
            wheels=wheel_message(
                left_command=wheels.get("left_command", 0.0),
                right_command=wheels.get("right_command", 0.0),
                left_target_qpps=target.left_qpps,
                right_target_qpps=target.right_qpps,
                left_actual_qpps=left_actual,
                right_actual_qpps=right_actual,
                left_max_qpps=motor_max_qpps[0],
                right_max_qpps=motor_max_qpps[1],
                left_current_amps=left_current,
                right_current_amps=right_current,
                read_ok=read_ok,
            ),
            motor_battery=motor_battery_message(pack_voltage),
            link_loop=link_loop,
            drive_tuning=drive.drive_tuning,
            drive_status=drive_status,
        )
        self._publish_message(message)

    def _drive_status_payload(self, roboclaw_ready: bool | None = None) -> dict[str, Any]:
        return drive_status_message(
            state=self.drive_state,
            stop_reason=self.stop_reason,
            controller_reader_alive=None,
            motor_command_ok=self.last_motor_command_ok,
            consecutive_motor_command_failures=self.consecutive_motor_command_failures,
            last_motor_command_ack_age_seconds=self._last_motor_command_ack_age(self.clock()),
            telemetry_publish_failures=self.telemetry_publish_failures,
            last_telemetry_publish_ok=self.last_telemetry_publish_ok,
            safety_blocked=self._last_safety_blocked,
            safety_reason=self._last_safety_reason,
            motion_power_requested=self._motion_power_requested(),
            roboclaw_ready=roboclaw_ready,
        )

    def _motion_power_requested(self) -> bool:
        return self.intent_executor.is_active() or self.pending_intent_complete is not None

    def _service_intent_requests(self, now: float) -> None:
        if self.intent_bridge is None or self.pending_intent_complete is not None:
            return
        pending = self.intent_bridge.take_pending()
        if pending is None:
            return
        tool, complete = pending
        error = self.intent_executor.start(tool, now)
        if error is not None:
            complete({"ok": False, "error": error})
            return
        self.pending_intent_complete = complete

    def _tick_intent(self, now: float, gamepad_active: bool) -> MotionCommand | None:
        if not self.intent_executor.is_active():
            return None
        tick = self.intent_executor.tick(now, gamepad_active)
        if tick.finished and self.pending_intent_complete is not None:
            complete = self.pending_intent_complete
            self.pending_intent_complete = None
            if tick.result == "completed":
                complete({"ok": True, "result": "completed"})
            else:
                complete({"ok": False, "error": tick.result or "unknown"})
        return tick.command

    def _fail_pending_intent(self, reason: str) -> None:
        if self.pending_intent_complete is None:
            return
        complete = self.pending_intent_complete
        self.pending_intent_complete = None
        complete({"ok": False, "error": reason})

    def _set_drive_state(self, state: str, reason: str | None = None) -> None:
        if state != self.drive_state or reason != self.stop_reason:
            log.info("drive state old=%s new=%s reason=%s", self.drive_state, state, reason or "")
        self.drive_state = state
        self.stop_reason = reason

    def _set_wheel_speeds(
        self,
        motor: Any,
        left_qpps: int,
        right_qpps: int,
        now: float | None = None,
        record: bool = True,
    ) -> bool:
        ok = motor.set_wheel_speeds(left_qpps, right_qpps)
        if record:
            self._record_motor_command_result(ok, self.clock() if now is None else now)
        return ok

    def _record_motor_command_result(self, ok: bool, now: float) -> None:
        self.last_motor_command_ok = ok
        if ok:
            self.consecutive_motor_command_failures = 0
            self.last_motor_command_ack_at = now
        else:
            self.consecutive_motor_command_failures += 1

    def _last_motor_command_ack_age(self, now: float) -> float | None:
        if self.last_motor_command_ack_at is None:
            return None
        return max(0.0, now - self.last_motor_command_ack_at)

    def _release_idle(self, motor: Any) -> None:
        motor.stop()

    def _reset_slew(self) -> None:
        self.last_target = WheelSpeedCommand(0, 0)
        self.last_target_at = None
        self.target_active = False

    def _read_motor_max_qpps(self, motor: Any) -> tuple[int | None, int | None]:
        try:
            return motor.read_max_qpps()
        except Exception as exc:
            log.warning("RoboClaw max QPPS read failed: %s", exc)
            return None, None

    def _record_read_result(self, read_ok: bool, now: float) -> None:
        self.read_results.append(read_ok)
        if read_ok:
            self.consecutive_read_failures = 0
            self.last_good_read_at = now
        else:
            self.consecutive_read_failures += 1

    def _read_success_rate(self) -> float | None:
        if not self.read_results:
            return None
        return sum(1 for result in self.read_results if result) / len(self.read_results)

    def _command_loop_hz(self) -> float | None:
        if len(self.loop_samples) < 2:
            return None
        elapsed = self.loop_samples[-1] - self.loop_samples[0]
        if elapsed <= 0:
            return None
        return (len(self.loop_samples) - 1) / elapsed

    def _publish_message(self, message: dict[str, Any]) -> None:
        try:
            published = self.telemetry_publisher(self.config.telemetry_socket, message)
        except Exception as exc:
            log.warning("telemetry publish failed: %s", exc)
            published = False
        self.last_telemetry_publish_ok = bool(published)
        if not published:
            self.telemetry_publish_failures += 1

    def _motor_factory(self) -> MotorDriver:
        return MotorDriver(
            port=self.config.port,
            address=self.config.address,
            baud=self.config.baud,
            serial_timeout=self.config.roboclaw_timeout,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot motion service.")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x80)
    parser.add_argument("--baud", type=int, default=38400)
    parser.add_argument("--qpps", type=int, default=2425)
    parser.add_argument("--loop-interval", type=float, default=0.05)
    parser.add_argument("--retry-interval", type=float, default=1.0)
    parser.add_argument("--telemetry-interval", type=float, default=0.2)
    parser.add_argument("--idle-release-delay", type=float, default=0.25)
    parser.add_argument("--roboclaw-timeout", type=float, default=0.5)
    parser.add_argument("--sensors-config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--telemetry-subscribe-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    parser.add_argument("--drive-socket", default=DEFAULT_MOTION_DRIVE_SOCKET)
    parser.add_argument("--motion-intent-socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = MotionRunner(
        MotionConfig(
            port=args.port,
            address=args.address,
            baud=args.baud,
            qpps=args.qpps,
            loop_interval=args.loop_interval,
            retry_interval=args.retry_interval,
            telemetry_interval=args.telemetry_interval,
            idle_release_delay=args.idle_release_delay,
            roboclaw_timeout=args.roboclaw_timeout,
            sensors_config_path=args.sensors_config,
            telemetry_socket=args.telemetry_socket,
            telemetry_subscribe_socket=args.telemetry_subscribe_socket,
            drive_socket=args.drive_socket,
            motion_intent_socket=args.motion_intent_socket,
        )
    )
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    log.info("motion service starting")
    runner.run_forever()
    log.info("motion service stopped")


if __name__ == "__main__":
    main()
