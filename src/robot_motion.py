#!/usr/bin/env python3
"""Motion service: owns RoboClaw, applies range-sensor safety, executes drive commands."""

from __future__ import annotations

import argparse
import math
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from config.drive_tuning import (
    DEFAULT_CONFIG_PATH as DRIVE_TUNING_CONFIG_PATH,
    DEFAULT_QPPS,
    DriveTuning,
    DriveTuningConfigError,
    load_drive_tuning,
)
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
from control.safety_gate import cancel_forward_qpps_when_blocked, evaluate_safety, is_forward_motion
from drivers.motor import MotorDriver, is_recoverable_roboclaw_error
from lib.log import setup_logging
from telemetry.messages import (
    drive_status_message,
    link_loop_message,
    motor_battery_message,
    robot_motion_update,
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

# Encoder-distance move physics. Distance completion is derived from these
# physical facts rather than a single opaque counts-per-meter constant: goBILDA
# 5203-2402-0019 motors (537.7 counts per output-shaft revolution) with nominal
# 96 mm Hogback wheels mounted directly on the output shaft. The wheel diameter
# is the calibrated effective rolling diameter: 1.00 m commanded measured 0.97 m.
WHEEL_DIAMETER_METERS = 0.096
ENCODER_COUNTS_PER_WHEEL_REVOLUTION = 537.7
ENCODER_COUNTS_PER_METER = ENCODER_COUNTS_PER_WHEEL_REVOLUTION / (math.pi * WHEEL_DIAMETER_METERS)
ENCODER_COUNT_BITS = 32
# A move commanding motion must keep gaining encoder travel; if it stalls this
# long the no-progress watchdog stops and fails it.
ENCODER_MOVE_NO_PROGRESS_TIMEOUT_SECONDS = 1.0


@dataclass
class EncoderMove:
    """Live state for an in-flight encoder-distance move."""

    target_counts: float
    left_start: int
    right_start: int
    last_travel: float
    last_progress_at: float


@dataclass(frozen=True)
class MotionConfig:
    port: str = "/dev/serial0"
    address: int = 0x80
    baud: int = 38400
    qpps: int = DEFAULT_QPPS
    qpps_slew_limit: float = 5000.0
    loop_interval: float = 0.05
    retry_interval: float = 1.0
    intent_wait_timeout: float = 8.0
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
        self._intent_wait_started_at: float | None = None
        self.encoder_move: EncoderMove | None = None

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
        self._telemetry_read_slot = 0
        self._last_left_actual: int | None = None
        self._last_right_actual: int | None = None
        self._last_left_current: float | None = None
        self._last_right_current: float | None = None
        self._last_pack_voltage: float | None = None

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
                try:
                    self._run_motor_loop(motor)
                except Exception as exc:
                    if not is_recoverable_roboclaw_error(exc):
                        raise
                    log.warning("RoboClaw connection lost; reconnecting: %s", exc)
                finally:
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
            self._service_stop_requests()
            self._service_intent_requests(now)
            self._fail_intent_if_roboclaw_wait_timed_out(now)
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
                self.intent_executor.reset_active_start(self.clock())
                self._intent_wait_started_at = None
                return motor
            log.warning("initial zero-speed command was not acknowledged")
            motor.cleanup()
            self.sleep(self.config.retry_interval)
        return None

    def _run_motor_loop(self, motor: Any) -> None:
        self.encoder_move = None
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
            stop_serviced = self._service_stop_requests()
            self._service_intent_requests(now)

            drive = self._get_drive_command()
            if drive is None:
                intent_command = self._tick_intent(now, gamepad_active=False)
                if intent_command is None:
                    # No motion source this tick. Ease any residual wheel speed down
                    # to zero (an explicit stop snaps instead) before idling, so a
                    # finished move or turn coasts to a halt rather than slamming off.
                    ramped = self._apply_slew(0, 0, now, no_slew=stop_serviced)
                    if ramped.left_qpps != 0 or ramped.right_qpps != 0:
                        if not self._set_wheel_speeds(motor, ramped.left_qpps, ramped.right_qpps, now=now):
                            stop_reason = "RoboClaw speed command was not acknowledged"
                            log.error("%s", stop_reason)
                            break
                        closed_loop_active = True
                        idle_released = False
                        self.sleep(self.config.loop_interval)
                        continue
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

                drive = self._intent_only_drive_command()
            else:
                gamepad_active = self._gamepad_active_from_wheels(drive.wheels)
                intent_command = self._tick_intent(now, gamepad_active)

            wheels, left_qpps, right_qpps = self._target_from_drive_or_intent(drive, intent_command)

            with self._sensors_lock:
                readings = list(self._sensor_readings)
                sensors_live = self._sensors_live
            safety = evaluate_safety(readings, self.sensors_config, sensors_live=sensors_live)
            commanding_forward = is_forward_motion(left_qpps, right_qpps)
            left_qpps, right_qpps = cancel_forward_qpps_when_blocked(left_qpps, right_qpps, safety)
            self._last_safety_blocked = safety.blocked
            self._last_safety_reason = safety.reason

            move_stop = None
            if self.intent_executor.active_move_distance_meters() is not None:
                move_stop = self._encoder_move_should_stop(motor, now, commanding_forward, safety)
                if move_stop is not None:
                    left_qpps, right_qpps = 0, 0

            # Shape acceleration centrally so both gamepad and intent motion ease in
            # and out. A safety-blocked forward command and a faulted encoder move
            # (lost feedback or a stall) must stop now, not coast, so they bypass the
            # ramp; an explicit stop does the same via stop_serviced. A completed move
            # ramps down normally.
            encoder_fault = move_stop in ("encoder_read_failed", "encoder_no_progress")
            no_slew = stop_serviced or encoder_fault or (safety.blocked and commanding_forward)
            ramped = self._apply_slew(left_qpps, right_qpps, now, no_slew=no_slew)
            left_qpps, right_qpps = ramped.left_qpps, ramped.right_qpps

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

    def _intent_only_drive_command(self) -> DriveCommand:
        tuning = DriveTuning()
        return DriveCommand(
            left_qpps=0,
            right_qpps=0,
            controller={"connected": False, "buttons": {}},
            wheels={"left_command": 0.0, "right_command": 0.0},
            drive_tuning=tuning.to_dict(),
            drive_status={"state": self.drive_state, "controller_reader_alive": None},
            link_loop={},
        )

    def _target_from_drive_or_intent(
        self,
        drive: DriveCommand,
        intent_command: MotionCommand | None,
    ) -> tuple[dict[str, Any], int, int]:
        if intent_command is None:
            return drive.wheels, drive.left_qpps, drive.right_qpps

        tuning = DriveTuning()
        intent_mixer = DifferentialDriveMixer(
            qpps=self.config.qpps,
            speed_scale=tuning.speed_scale,
            turbo_scale=tuning.turbo_scale,
        )
        mixed_wheels = intent_mixer.mix(intent_command)
        intent_target = intent_mixer.to_wheel_speeds(
            intent_command,
            turbo=False,
        )
        return (
            {"left_command": mixed_wheels.left, "right_command": mixed_wheels.right},
            intent_target.left_qpps,
            intent_target.right_qpps,
        )

    def _publish_waiting_telemetry(self, drive: DriveCommand | None = None, roboclaw_ready: bool = False) -> None:
        drive_status = self._drive_status_payload(roboclaw_ready=roboclaw_ready)
        if drive is None:
            wheels = {"read_ok": False}
            link_loop = link_loop_message(
                read_success_rate=None,
                consecutive_read_failures=0,
                last_good_read_age_seconds=None,
                telemetry_latency_ms=None,
                command_loop_hz=None,
            )
        else:
            wheels = dict(drive.wheels)
            wheels["read_ok"] = False
            link_loop = drive.link_loop
            drive_status["controller_reader_alive"] = drive.drive_status.get("controller_reader_alive")

        message = robot_motion_update(
            wheels=wheels,
            motor_battery=motor_battery_message(None),
            link_loop=link_loop,
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
        read_ok = self._read_next_telemetry_value(motor)

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

        message = robot_motion_update(
            wheels=wheel_message(
                left_command=wheels.get("left_command", 0.0),
                right_command=wheels.get("right_command", 0.0),
                left_target_qpps=target.left_qpps,
                right_target_qpps=target.right_qpps,
                left_actual_qpps=self._last_left_actual,
                right_actual_qpps=self._last_right_actual,
                left_max_qpps=motor_max_qpps[0],
                right_max_qpps=motor_max_qpps[1],
                left_current_amps=self._last_left_current,
                right_current_amps=self._last_right_current,
                read_ok=read_ok,
            ),
            motor_battery=motor_battery_message(self._last_pack_voltage),
            link_loop=link_loop,
            drive_status=drive_status,
        )
        self._publish_message(message)

    def _read_next_telemetry_value(self, motor: Any) -> bool:
        try:
            if self._telemetry_read_slot == 0:
                self._last_left_actual, self._last_right_actual = motor.read_wheel_speeds()
                return self._last_left_actual is not None and self._last_right_actual is not None
            if self._telemetry_read_slot == 1:
                self._last_pack_voltage = motor.get_battery_voltage()
                return self._last_pack_voltage is not None
            currents = motor.get_currents()
            if currents is None:
                return False
            self._last_left_current, self._last_right_current = currents
            return True
        except Exception as exc:
            log.warning("telemetry read failed: %s", exc)
            return False
        finally:
            self._telemetry_read_slot = (self._telemetry_read_slot + 1) % 3

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

    def _fail_intent_if_roboclaw_wait_timed_out(self, now: float) -> None:
        if not self._motion_power_requested():
            self._intent_wait_started_at = None
            return
        if self._intent_wait_started_at is None:
            self._intent_wait_started_at = now
            return
        if now - self._intent_wait_started_at < self.config.intent_wait_timeout:
            return
        log.warning("RoboClaw unavailable after %.1fs; failing motion intent", self.config.intent_wait_timeout)
        self._fail_pending_intent("roboclaw_unavailable")
        self.intent_executor.cancel()
        self._intent_wait_started_at = None

    def _service_intent_requests(self, now: float) -> None:
        if self.intent_bridge is None or self.pending_intent_complete is not None:
            return
        pending = self.intent_bridge.take_pending()
        if pending is None:
            return
        request, complete = pending
        error = self.intent_executor.start(
            request["tool"],
            now,
            direction=request.get("direction"),
            duration_seconds=request.get("duration_seconds"),
            distance_meters=request.get("distance_meters"),
            relative_degrees=request.get("relative_degrees"),
            degrees=request.get("degrees"),
            kind=request.get("kind"),
        )
        if error is not None:
            complete({"ok": False, "error": error})
            return
        self.pending_intent_complete = complete

    def _service_stop_requests(self) -> bool:
        # A stop arrives out-of-band: it must cancel an intent that is mid-drive, so
        # it runs every loop, even while pending_intent_complete is set. Returns True
        # when a stop fired this tick so the loop can halt the wheels without slewing.
        if self.intent_bridge is None or not self.intent_bridge.take_stop():
            return False
        self.intent_executor.cancel()
        self._fail_pending_intent("stopped")
        self.intent_bridge.discard_pending()
        return True

    def _tick_intent(self, now: float, gamepad_active: bool) -> MotionCommand | None:
        if not self.intent_executor.is_active():
            return None
        tick = self.intent_executor.tick(now, gamepad_active)
        if tick.finished and self.pending_intent_complete is not None:
            complete = self.pending_intent_complete
            self.pending_intent_complete = None
            self.encoder_move = None
            if tick.result == "completed":
                complete({"ok": True, "result": "completed"})
            else:
                complete({"ok": False, "error": tick.result or "unknown"})
        return tick.command

    def _encoder_move_should_stop(
        self,
        motor: Any,
        now: float,
        commanding_forward: bool,
        safety: Any,
    ) -> str | None:
        """Drive completion/failure for the active encoder-distance move.

        Returns the stop reason when the move has ended this loop (so the caller
        commands zero speed), or None to keep going. Reads encoder positions,
        snapshots the start on the first loop, completes at the target travel,
        and fails on read error, a safety block of forward progress, or a
        no-progress stall.
        """
        distance = self.intent_executor.active_move_distance_meters()

        # A forward move that safety blocks is no longer making progress.
        if distance > 0 and commanding_forward and safety.blocked:
            self._end_encoder_move("safety_blocked")
            return "safety_blocked"

        if self.encoder_move is None:
            left_start, right_start = motor.read_wheel_positions()
            if left_start is None or right_start is None:
                self._end_encoder_move("encoder_read_failed")
                return "encoder_read_failed"
            self.encoder_move = EncoderMove(
                target_counts=abs(distance) * ENCODER_COUNTS_PER_METER,
                left_start=left_start,
                right_start=right_start,
                last_travel=0.0,
                last_progress_at=now,
            )
            return None

        left_now, right_now = motor.read_wheel_positions()
        if left_now is None or right_now is None:
            self._end_encoder_move("encoder_read_failed")
            return "encoder_read_failed"

        travel = (
            abs(_encoder_delta(left_now, self.encoder_move.left_start))
            + abs(_encoder_delta(right_now, self.encoder_move.right_start))
        ) / 2
        if travel >= self.encoder_move.target_counts:
            self._complete_encoder_move()
            return "completed"

        if travel > self.encoder_move.last_travel:
            self.encoder_move.last_travel = travel
            self.encoder_move.last_progress_at = now
        elif now - self.encoder_move.last_progress_at >= ENCODER_MOVE_NO_PROGRESS_TIMEOUT_SECONDS:
            self._end_encoder_move("encoder_no_progress")
            return "encoder_no_progress"

        return None

    def _complete_encoder_move(self) -> None:
        self.intent_executor.cancel()
        complete = self.pending_intent_complete
        self.pending_intent_complete = None
        self.encoder_move = None
        if complete is not None:
            complete({"ok": True, "result": "completed"})

    def _end_encoder_move(self, reason: str) -> None:
        self.intent_executor.cancel()
        self._fail_pending_intent(reason)

    def _fail_pending_intent(self, reason: str) -> None:
        self.encoder_move = None
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
        left_qpps = max(-self.config.qpps, min(self.config.qpps, int(left_qpps)))
        right_qpps = max(-self.config.qpps, min(self.config.qpps, int(right_qpps)))
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

    def _apply_slew(
        self, left_qpps: int, right_qpps: int, now: float, no_slew: bool = False
    ) -> WheelSpeedCommand:
        """Ramp the wheel target toward (left, right), bounded by qpps_slew_limit.

        This is the single place every motion source — gamepad and voice/agent
        intents alike — has its acceleration shaped, so motion eases in and out
        instead of snapping to a target in one tick. Pass no_slew=True for stops
        that must be instant: an explicit stop command or a safety block.
        """
        # Clamp to the deliverable range before shaping so the stored slew state
        # never decays from a value the motor was never actually sent.
        limit = self.config.qpps
        left_qpps = max(-limit, min(limit, int(left_qpps)))
        right_qpps = max(-limit, min(limit, int(right_qpps)))
        target = WheelSpeedCommand(left_qpps=left_qpps, right_qpps=right_qpps)
        if no_slew:
            self.last_target = target
            self.last_target_at = now
            self.target_active = True
            return target
        # Seed from a standstill on the first ramping tick, crediting one loop of
        # elapsed time so motion can actually begin moving toward the target.
        if not self.target_active:
            self.target_active = True
            self.last_target = WheelSpeedCommand(0, 0)
            self.last_target_at = now - self.config.loop_interval
        elapsed = max(0.0, now - self.last_target_at)
        max_delta = self.config.qpps_slew_limit * elapsed
        ramped = WheelSpeedCommand(
            left_qpps=int(_move_toward(self.last_target.left_qpps, target.left_qpps, max_delta)),
            right_qpps=int(_move_toward(self.last_target.right_qpps, target.right_qpps, max_delta)),
        )
        self.last_target = ramped
        self.last_target_at = now
        return ramped

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


def _move_toward(current: float, target: float, max_delta: float) -> float:
    if abs(target - current) <= max_delta:
        return target
    return current + max_delta if target > current else current - max_delta


def _encoder_delta(current: float, start: float) -> float:
    delta = current - start
    span = 1 << ENCODER_COUNT_BITS
    max_signed = (1 << (ENCODER_COUNT_BITS - 1)) - 1
    min_signed = -(1 << (ENCODER_COUNT_BITS - 1))
    if delta > max_signed:
        return delta - span
    if delta < min_signed:
        return delta + span
    return delta


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot motion service.")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x80)
    parser.add_argument("--baud", type=int, default=38400)
    parser.add_argument("--qpps", type=int, default=DEFAULT_QPPS)
    parser.add_argument(
        "--drive-tuning-config",
        default=DRIVE_TUNING_CONFIG_PATH,
        help="Drive tuning JSON config path; supplies the wheel slew limit",
    )
    parser.add_argument("--loop-interval", type=float, default=0.05)
    parser.add_argument("--retry-interval", type=float, default=1.0)
    parser.add_argument("--intent-wait-timeout", type=float, default=8.0)
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
    try:
        drive_tuning = load_drive_tuning(args.drive_tuning_config)
    except DriveTuningConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc
    runner = MotionRunner(
        MotionConfig(
            port=args.port,
            address=args.address,
            baud=args.baud,
            qpps=args.qpps,
            qpps_slew_limit=drive_tuning.qpps_slew_limit,
            loop_interval=args.loop_interval,
            retry_interval=args.retry_interval,
            intent_wait_timeout=args.intent_wait_timeout,
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
