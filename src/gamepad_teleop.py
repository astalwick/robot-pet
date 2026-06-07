#!/usr/bin/env python3
"""Gamepad teleop: reads controller input and sends drive commands to robot-motion."""

import argparse
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from config.teleop import DEFAULT_CONFIG_PATH, DEFAULT_QPPS, DriveTuning, DriveTuningConfigError, load_drive_tuning
from control.commands import WheelSpeedCommand
from control.differential_drive import DifferentialDriveMixer
from control.motion_drive import DriveCommand, MotionDrivePublisher
from control.teleop import GamepadTeleopPolicy
from drivers.controller import ControllerDriver
from lib.log import setup_logging
from telemetry.messages import (
    controller_message,
    drive_status_message,
    gamepad_update,
    gamepad_teleop_update,
    link_loop_message,
    motor_battery_message,
)
from telemetry.paths import DEFAULT_MOTION_DRIVE_SOCKET, DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


log = setup_logging("gamepad-teleop")
COMMAND_LOOP_STALL_SECONDS = 0.25
COMMAND_LOOP_STALL_LOG_INTERVAL_SECONDS = 5.0
STATUS_PUBLISH_INTERVAL = 0.5


@dataclass(frozen=True)
class TeleopConfig:
    device: str | None = None
    qpps: int = DEFAULT_QPPS
    drive_tuning: DriveTuning = field(default_factory=DriveTuning)
    loop_interval: float = 0.05
    retry_interval: float = 1.0
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET
    motion_drive_socket: str = DEFAULT_MOTION_DRIVE_SOCKET


class GamepadTeleopRunner:
    def __init__(
        self,
        config: TeleopConfig,
        controller_factory: Callable[[], Any] | None = None,
        motion_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        telemetry_publisher: Callable[[str, dict[str, Any]], bool] = publish_message,
    ):
        self.config = config
        self.controller_factory = controller_factory or self._controller_factory
        self.motion_factory = motion_factory or self._motion_factory
        self.sleep = sleep
        self.clock = clock
        self.telemetry_publisher = telemetry_publisher
        self.policy = GamepadTeleopPolicy(
            left_stick_deadzone=config.drive_tuning.left_stick_deadzone,
            right_stick_deadzone=config.drive_tuning.right_stick_deadzone,
            turn_scale=config.drive_tuning.turn_scale,
        )
        self.mixer = DifferentialDriveMixer(
            qpps=config.qpps,
            speed_scale=config.drive_tuning.speed_scale,
            turbo_scale=config.drive_tuning.turbo_scale,
        )
        self.last_target = WheelSpeedCommand(0, 0)
        self.last_target_at: float | None = None
        self.target_active = False
        self.loop_samples: deque[float] = deque(maxlen=25)
        self.drive_state = "stopped"
        self.stop_reason: str | None = None
        self.stop_requested = False
        self.telemetry_publish_failures = 0
        self.last_telemetry_publish_ok: bool | None = None

    def request_stop(self, *_args):
        self.stop_requested = True

    def run_forever(self):
        try:
            while not self.stop_requested:
                controller = self._wait_for_controller()
                if self.stop_requested:
                    break

                self._publish_gamepad_update(True)
                motion = self._wait_for_motion(controller)
                if self.stop_requested:
                    controller.cleanup()
                    break

                self._run_connected(controller, motion)
                controller.cleanup()
                motion.close()
        finally:
            pass

    def _wait_for_controller(self):
        self._set_drive_state("waiting_for_controller")
        while not self.stop_requested:
            self._publish_gamepad_update(False)
            self._publish_status_update()
            controller = self.controller_factory()
            if controller.connect():
                log.info("controller connected")
                return controller
            self._sleep_with_status_updates(self.config.retry_interval, connected=False)
        return None

    def _wait_for_motion(self, controller):
        self._set_drive_state("waiting_for_motion")
        while not self.stop_requested:
            self._publish_gamepad_update(True)
            self._publish_status_update(controller)
            motion = self.motion_factory()
            if motion.connect():
                log.info("robot-motion drive socket connected")
                return motion
            log.info("waiting for robot-motion")
            self._sleep_with_status_updates(self.config.retry_interval, controller=controller, connected=True)
        return None

    def _sleep_with_status_updates(self, duration: float, controller=None, connected: bool = False):
        deadline = self.clock() + duration
        next_publish_at = self.clock() + STATUS_PUBLISH_INTERVAL
        while self.clock() < deadline and not self.stop_requested:
            if self.clock() >= next_publish_at:
                self._publish_gamepad_update(connected)
                self._publish_status_update(controller)
                next_publish_at = self.clock() + STATUS_PUBLISH_INTERVAL
            remaining = min(next_publish_at, deadline) - self.clock()
            if remaining > 0:
                self.sleep(remaining)

    def _run_connected(self, controller, motion):
        disconnected = threading.Event()
        controller.start(on_disconnect=disconnected.set)
        last_stall_log_at = 0.0
        next_status_at = 0.0
        stop_reason = None
        self._reset_slew()
        self._set_drive_state("driving")

        while not self.stop_requested and not disconnected.is_set():
            cycle_started = self.clock()
            now = cycle_started
            self.loop_samples.append(now)
            if now >= next_status_at:
                self._publish_gamepad_update(True)
                next_status_at = now + STATUS_PUBLISH_INTERVAL

            command = self.policy.motion_from_state(controller.state)
            wheels = self.mixer.mix(command)
            target = self.mixer.to_wheel_speeds(command, turbo=controller.state.lb)
            if controller.state.rb:
                target = self._slew_target(target, now)
            else:
                self._reset_slew()

            target_is_zero = target.left_qpps == 0 and target.right_qpps == 0
            if not controller.reader_alive() and not target_is_zero:
                stop_reason = controller.disconnect_reason or "controller input reader stopped"
                log.error("%s; stopping", stop_reason)
                break

            drive_command = DriveCommand(
                left_qpps=target.left_qpps,
                right_qpps=target.right_qpps,
                controller=controller_message(controller.state),
                wheels={
                    "left_command": wheels.left,
                    "right_command": wheels.right,
                    "left_target_qpps": target.left_qpps,
                    "right_target_qpps": target.right_qpps,
                },
                drive_tuning=self.config.drive_tuning.to_dict(),
                drive_status=self._drive_status_payload(controller.reader_alive()),
                link_loop=self._link_loop_payload(),
            )
            if not motion.send(drive_command):
                stop_reason = "robot-motion drive socket send failed"
                log.error("%s", stop_reason)
                break

            cycle_elapsed = self.clock() - cycle_started
            if (
                cycle_elapsed > COMMAND_LOOP_STALL_SECONDS
                and now - last_stall_log_at >= COMMAND_LOOP_STALL_LOG_INTERVAL_SECONDS
            ):
                log.warning("command loop stall %.3fs target=(%d,%d)", cycle_elapsed, target.left_qpps, target.right_qpps)
                last_stall_log_at = now

            self.sleep(self.config.loop_interval)

        motion.send(
            DriveCommand(
                left_qpps=0,
                right_qpps=0,
                controller=controller_message(controller.state),
                wheels={"left_command": 0.0, "right_command": 0.0, "left_target_qpps": 0, "right_target_qpps": 0},
                drive_tuning=self.config.drive_tuning.to_dict(),
                drive_status=self._drive_status_payload(controller.reader_alive()),
                link_loop=self._link_loop_payload(),
            )
        )
        self._reset_slew()
        if disconnected.is_set():
            reason = controller.disconnect_reason or "controller disconnected"
            self._set_drive_state("controller_lost", reason)
            self._publish_gamepad_update(False)
            log.warning("%s; waiting for reconnect", reason)
        elif stop_reason is not None:
            self._set_drive_state("motion_send_failed", stop_reason)
            log.warning("%s; waiting for reconnect", stop_reason)
        else:
            self._set_drive_state("stopped")

    def _reset_slew(self):
        self.last_target = WheelSpeedCommand(0, 0)
        self.last_target_at = None
        self.target_active = False

    def _slew_target(self, target: WheelSpeedCommand, now: float) -> WheelSpeedCommand:
        if not self.target_active:
            self.target_active = True
            self.last_target = WheelSpeedCommand(0, 0)
            self.last_target_at = now - self.config.loop_interval

        elapsed = max(0.0, now - self.last_target_at)
        max_delta = self.config.drive_tuning.qpps_slew_limit * elapsed
        target = WheelSpeedCommand(
            left_qpps=int(self._move_toward(self.last_target.left_qpps, target.left_qpps, max_delta)),
            right_qpps=int(self._move_toward(self.last_target.right_qpps, target.right_qpps, max_delta)),
        )
        self.last_target = target
        self.last_target_at = now
        return target

    def _move_toward(self, current: float, target: float, max_delta: float) -> float:
        if abs(target - current) <= max_delta:
            return target
        if target > current:
            return current + max_delta
        return current - max_delta

    def _link_loop_payload(self) -> dict[str, Any]:
        return link_loop_message(
            read_success_rate=None,
            consecutive_read_failures=0,
            last_good_read_age_seconds=None,
            telemetry_latency_ms=None,
            command_loop_hz=self._command_loop_hz(),
        )

    def _command_loop_hz(self) -> float | None:
        if len(self.loop_samples) < 2:
            return None
        elapsed = self.loop_samples[-1] - self.loop_samples[0]
        if elapsed <= 0:
            return None
        return (len(self.loop_samples) - 1) / elapsed

    def _set_drive_state(self, state: str, reason: str | None = None):
        if state != self.drive_state or reason != self.stop_reason:
            log.info("drive state old=%s new=%s reason=%s", self.drive_state, state, reason or "")
        self.drive_state = state
        self.stop_reason = reason

    def _drive_status_payload(self, controller_reader_alive: bool | None = None) -> dict[str, Any]:
        return drive_status_message(
            state=self.drive_state,
            stop_reason=self.stop_reason,
            controller_reader_alive=controller_reader_alive,
            motor_command_ok=None,
            consecutive_motor_command_failures=0,
            last_motor_command_ack_age_seconds=None,
            telemetry_publish_failures=self.telemetry_publish_failures,
            last_telemetry_publish_ok=self.last_telemetry_publish_ok,
        )

    def _publish_gamepad_update(self, connected: bool):
        self._publish_message(gamepad_update(connected=connected, state=self.drive_state))

    def _publish_status_update(self, controller=None, controller_reader_alive: bool | None = None):
        controller_payload = controller_message(controller.state) if controller is not None else {"connected": False}
        message = gamepad_teleop_update(
            controller=controller_payload,
            wheels={"read_ok": False},
            motor_battery=motor_battery_message(None),
            link_loop=self._link_loop_payload(),
            drive_tuning=self.config.drive_tuning.to_dict(),
            drive_status=self._drive_status_payload(controller_reader_alive),
        )
        self._publish_message(message)

    def _publish_message(self, message: dict[str, Any]):
        try:
            published = self.telemetry_publisher(self.config.telemetry_socket, message)
        except Exception as exc:
            log.warning("telemetry publish failed: %s", exc)
            published = False
        self.last_telemetry_publish_ok = bool(published)
        if not published:
            self.telemetry_publish_failures += 1

    def _controller_factory(self):
        return ControllerDriver(deadzone=0.0, device_path=self.config.device)

    def _motion_factory(self):
        return MotionDrivePublisher(self.config.motion_drive_socket)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gamepad teleop service (sends commands to robot-motion).")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Drive tuning JSON config path")
    parser.add_argument("--device", help="Read a specific /dev/input/event* controller device")
    parser.add_argument("--qpps", type=int, default=DEFAULT_QPPS, help="Configured RoboClaw max speed in encoder counts/sec")
    parser.add_argument("--speed-scale", type=float, help="Normal-mode fraction of --qpps")
    parser.add_argument("--turbo-scale", type=float, help="Turbo-mode fraction of --qpps while LB is held")
    parser.add_argument("--turn-scale", type=float, help="Turn command multiplier")
    parser.add_argument("--left-stick-deadzone", type=float, help="Left stick deadzone from 0.0 to 1.0")
    parser.add_argument("--right-stick-deadzone", type=float, help="Right stick deadzone from 0.0 to 1.0")
    parser.add_argument("--qpps-slew-limit", type=float, help="Wheel target slew limit in QPPS/sec")
    parser.add_argument("--loop-interval", type=float, default=0.05, help="Main control loop interval in seconds")
    parser.add_argument("--retry-interval", type=float, default=1.0, help="Reconnect retry interval in seconds")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET, help="Telemetry hub publisher socket")
    parser.add_argument(
        "--motion-drive-socket",
        default=DEFAULT_MOTION_DRIVE_SOCKET,
        help="robot-motion drive command socket",
    )
    return parser


def main():
    args = build_parser().parse_args()
    try:
        tuning_values = load_drive_tuning(args.config).to_dict()
    except DriveTuningConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc

    for key in (
        "speed_scale",
        "turbo_scale",
        "turn_scale",
        "left_stick_deadzone",
        "right_stick_deadzone",
        "qpps_slew_limit",
    ):
        value = getattr(args, key)
        if value is not None:
            tuning_values[key] = value
    drive_tuning = DriveTuning.from_dict(tuning_values)

    config = TeleopConfig(
        device=args.device,
        qpps=args.qpps,
        drive_tuning=drive_tuning,
        loop_interval=args.loop_interval,
        retry_interval=args.retry_interval,
        telemetry_socket=args.telemetry_socket,
        motion_drive_socket=args.motion_drive_socket,
    )
    runner = GamepadTeleopRunner(config)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    runner.run_forever()


if __name__ == "__main__":
    main()
