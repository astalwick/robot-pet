#!/usr/bin/env python3
"""Boot-ready gamepad teleop service for RoboClaw closed-loop speed control."""

import argparse
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from config.teleop import DEFAULT_CONFIG_PATH, DriveTuning, DriveTuningConfigError, load_drive_tuning
from control.commands import WheelSpeedCommand
from control.differential_drive import DifferentialDriveMixer
from control.teleop import GamepadTeleopPolicy
from drivers.controller import ControllerDriver
from drivers.motor import MotorDriver
from lib.log import setup_logging
from telemetry.messages import controller_message, gamepad_teleop_update, link_loop_message, motor_battery_message, wheel_message
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


log = setup_logging("gamepad-teleop")
SLOW_TELEMETRY_WARNING_SECONDS = 0.025


def parse_address(value: str) -> int:
    return int(value, 0)


@dataclass(frozen=True)
class TeleopConfig:
    device: str | None = None
    port: str = "/dev/serial0"
    address: int = 0x80
    baud: int = 38400
    qpps: int = 2425
    drive_tuning: DriveTuning = field(default_factory=DriveTuning)
    loop_interval: float = 0.05
    retry_interval: float = 1.0
    telemetry_interval: float = 0.2
    idle_release_delay: float = 0.25
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET


class GamepadTeleopRunner:
    def __init__(
        self,
        config: TeleopConfig,
        controller_factory: Callable[[], Any] | None = None,
        motor_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        telemetry_publisher: Callable[[str, dict[str, Any]], bool] = publish_message,
    ):
        self.config = config
        self.controller_factory = controller_factory or self._controller_factory
        self.motor_factory = motor_factory or self._motor_factory
        self.sleep = sleep
        self.clock = clock
        self.telemetry_publisher = telemetry_publisher
        self.stop_requested = False
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
        self.read_results: deque[bool] = deque(maxlen=50)
        self.consecutive_read_failures = 0
        self.last_good_read_at: float | None = None
        self.loop_samples: deque[float] = deque(maxlen=25)

    def request_stop(self, *_args):
        self.stop_requested = True

    def run_forever(self):
        while not self.stop_requested:
            controller = self._wait_for_controller()
            if self.stop_requested:
                break

            motor = self._wait_for_roboclaw()
            if self.stop_requested:
                controller.cleanup()
                break

            self._run_connected(controller, motor)
            controller.cleanup()
            motor.cleanup()

    def _wait_for_controller(self):
        while not self.stop_requested:
            controller = self.controller_factory()
            if controller.connect():
                log.info("controller connected")
                return controller

            log.info("waiting for controller")
            self.sleep(self.config.retry_interval)

        return None

    def _wait_for_roboclaw(self):
        while not self.stop_requested:
            try:
                motor = self.motor_factory()
            except Exception as exc:
                log.warning("waiting for RoboClaw: %s", exc)
                self.sleep(self.config.retry_interval)
                continue

            if motor.set_wheel_speeds(0, 0):
                log.info("RoboClaw ready")
                return motor

            log.warning("initial zero-speed command was not acknowledged")
            motor.cleanup()
            self.sleep(self.config.retry_interval)

        return None

    def _run_connected(self, controller, motor):
        disconnected = threading.Event()
        controller.start(on_disconnect=disconnected.set)
        next_telemetry = self.clock() + self.config.telemetry_interval
        closed_loop_active = False
        idle_started_at = self.clock()
        idle_released = False
        motor_max_qpps = self._read_motor_max_qpps(motor)
        self._reset_slew()

        while not self.stop_requested and not disconnected.is_set():
            now = self.clock()
            self.loop_samples.append(now)
            command = self.policy.motion_from_state(controller.state)
            wheels = self.mixer.mix(command)
            target = self.mixer.to_wheel_speeds(command, turbo=controller.state.lb)
            if controller.state.rb:
                target = self._slew_target(target, now)
            else:
                self._reset_slew()
            target_is_zero = target.left_qpps == 0 and target.right_qpps == 0

            if target_is_zero:
                if closed_loop_active:
                    if not motor.set_wheel_speeds(0, 0):
                        log.error("RoboClaw zero-speed command was not acknowledged")
                        self._safe_zero_speed(motor)
                        return
                    closed_loop_active = False
                    idle_started_at = now
                    idle_released = False
                elif not idle_released and now - idle_started_at >= self.config.idle_release_delay:
                    self._release_idle(motor)
                    idle_released = True
            else:
                if not motor.set_wheel_speeds(target.left_qpps, target.right_qpps):
                    log.error("RoboClaw speed command was not acknowledged")
                    self._safe_zero_speed(motor)
                    return
                closed_loop_active = True
                idle_released = False

            if now >= next_telemetry:
                telemetry_started = self.clock()
                self._publish_telemetry(controller.state, wheels, target, motor, motor_max_qpps)
                telemetry_elapsed = self.clock() - telemetry_started
                if telemetry_elapsed > SLOW_TELEMETRY_WARNING_SECONDS:
                    log.warning("telemetry update took %.3fs", telemetry_elapsed)
                next_telemetry = now + self.config.telemetry_interval

            self.sleep(self.config.loop_interval)

        self._safe_zero_speed(motor)
        self._reset_slew()
        if disconnected.is_set():
            log.warning("controller disconnected; waiting for reconnect")

    def _reset_slew(self):
        self.last_target = WheelSpeedCommand(0, 0)
        self.last_target_at = None
        self.target_active = False

    def _read_motor_max_qpps(self, motor) -> tuple[int | None, int | None]:
        try:
            return motor.read_max_qpps()
        except Exception as exc:
            log.warning("RoboClaw max QPPS read failed: %s", exc)
            return None, None

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

    def _safe_zero_speed(self, motor):
        motor.set_wheel_speeds(0, 0)

    def _release_idle(self, motor):
        motor.stop()

    def _publish_telemetry(self, state, wheels, target, motor, motor_max_qpps):
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
        telemetry_latency_ms = (self.clock() - now) * 1000.0

        message = gamepad_teleop_update(
            controller=controller_message(state),
            wheels=wheel_message(
                left_command=wheels.left,
                right_command=wheels.right,
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
            link_loop=link_loop_message(
                read_success_rate=self._read_success_rate(),
                consecutive_read_failures=self.consecutive_read_failures,
                last_good_read_age_seconds=(
                    now - self.last_good_read_at if self.last_good_read_at is not None else None
                ),
                telemetry_latency_ms=telemetry_latency_ms,
                command_loop_hz=self._command_loop_hz(),
            ),
            drive_tuning=self.config.drive_tuning.to_dict(),
        )
        try:
            self.telemetry_publisher(self.config.telemetry_socket, message)
        except Exception as exc:
            log.warning("telemetry publish failed: %s", exc)

    def _record_read_result(self, read_ok: bool, now: float):
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

    def _controller_factory(self):
        return ControllerDriver(deadzone=0.0, device_path=self.config.device)

    def _motor_factory(self):
        return MotorDriver(
            port=self.config.port,
            address=self.config.address,
            baud=self.config.baud,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Boot-ready gamepad teleop service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Drive tuning JSON config path")
    parser.add_argument("--device", help="Read a specific /dev/input/event* controller device")
    parser.add_argument("--port", default="/dev/serial0", help="RoboClaw serial port")
    parser.add_argument("--address", type=parse_address, default=0x80, help="RoboClaw packet serial address")
    parser.add_argument("--baud", type=int, default=38400, help="RoboClaw serial baud rate")
    parser.add_argument("--qpps", type=int, default=2425, help="Configured RoboClaw max speed in encoder counts/sec")
    parser.add_argument("--speed-scale", type=float, help="Normal-mode fraction of --qpps")
    parser.add_argument("--turbo-scale", type=float, help="Turbo-mode fraction of --qpps while LB is held")
    parser.add_argument("--turn-scale", type=float, help="Turn command multiplier")
    parser.add_argument("--left-stick-deadzone", type=float, help="Left stick deadzone from 0.0 to 1.0")
    parser.add_argument("--right-stick-deadzone", type=float, help="Right stick deadzone from 0.0 to 1.0")
    parser.add_argument("--qpps-slew-limit", type=float, help="Wheel target slew limit in QPPS/sec")
    parser.add_argument("--loop-interval", type=float, default=0.05, help="Main control loop interval in seconds")
    parser.add_argument("--retry-interval", type=float, default=1.0, help="Hardware reconnect retry interval in seconds")
    parser.add_argument("--telemetry-interval", type=float, default=0.2, help="Telemetry publish interval in seconds")
    parser.add_argument("--idle-release-delay", type=float, default=0.25, help="Seconds after stopping before releasing to zero duty")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET, help="Telemetry hub publisher socket")
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
        port=args.port,
        address=args.address,
        baud=args.baud,
        qpps=args.qpps,
        drive_tuning=drive_tuning,
        loop_interval=args.loop_interval,
        retry_interval=args.retry_interval,
        telemetry_interval=args.telemetry_interval,
        idle_release_delay=args.idle_release_delay,
        telemetry_socket=args.telemetry_socket,
    )
    runner = GamepadTeleopRunner(config)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    runner.run_forever()


if __name__ == "__main__":
    main()
