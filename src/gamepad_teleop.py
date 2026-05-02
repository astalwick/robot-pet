#!/usr/bin/env python3
"""Boot-ready gamepad teleop service for RoboClaw closed-loop speed control."""

import argparse
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from control.differential_drive import DifferentialDriveMixer
from control.teleop import GamepadTeleopPolicy
from drivers.controller import ControllerDriver
from drivers.motor import MotorDriver
from lib.log import setup_logging
from telemetry.messages import controller_message, gamepad_teleop_update, motor_battery_message, wheel_message
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
    speed_scale: float = 0.25
    turbo_scale: float = 0.75
    deadzone: float = 0.15
    loop_interval: float = 0.05
    retry_interval: float = 1.0
    telemetry_interval: float = 0.2
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
        self.policy = GamepadTeleopPolicy(deadzone=config.deadzone)
        self.mixer = DifferentialDriveMixer(
            qpps=config.qpps,
            speed_scale=config.speed_scale,
            turbo_scale=config.turbo_scale,
        )

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

        while not self.stop_requested and not disconnected.is_set():
            command = self.policy.motion_from_state(controller.state)
            wheels = self.mixer.mix(command)
            target = self.mixer.to_wheel_speeds(command, turbo=controller.state.lb)

            if not motor.set_wheel_speeds(target.left_qpps, target.right_qpps):
                log.error("RoboClaw speed command was not acknowledged")
                self._safe_zero_speed(motor)
                return

            now = self.clock()
            if now >= next_telemetry:
                telemetry_started = self.clock()
                self._publish_telemetry(controller.state, wheels, target, motor)
                telemetry_elapsed = self.clock() - telemetry_started
                if telemetry_elapsed > SLOW_TELEMETRY_WARNING_SECONDS:
                    log.warning("telemetry update took %.3fs", telemetry_elapsed)
                next_telemetry = now + self.config.telemetry_interval

            self.sleep(self.config.loop_interval)

        self._safe_zero_speed(motor)
        if disconnected.is_set():
            log.warning("controller disconnected; waiting for reconnect")

    def _safe_zero_speed(self, motor):
        motor.set_wheel_speeds(0, 0)

    def _publish_telemetry(self, state, wheels, target, motor):
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

        message = gamepad_teleop_update(
            controller=controller_message(state),
            wheels=wheel_message(
                left_command=wheels.left,
                right_command=wheels.right,
                left_target_qpps=target.left_qpps,
                right_target_qpps=target.right_qpps,
                left_actual_qpps=left_actual,
                right_actual_qpps=right_actual,
                left_current_amps=left_current,
                right_current_amps=right_current,
                read_ok=read_ok,
            ),
            motor_battery=motor_battery_message(pack_voltage),
        )
        try:
            self.telemetry_publisher(self.config.telemetry_socket, message)
        except Exception as exc:
            log.warning("telemetry publish failed: %s", exc)

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
    parser.add_argument("--device", help="Read a specific /dev/input/event* controller device")
    parser.add_argument("--port", default="/dev/serial0", help="RoboClaw serial port")
    parser.add_argument("--address", type=parse_address, default=0x80, help="RoboClaw packet serial address")
    parser.add_argument("--baud", type=int, default=38400, help="RoboClaw serial baud rate")
    parser.add_argument("--qpps", type=int, default=2425, help="Configured RoboClaw max speed in encoder counts/sec")
    parser.add_argument("--speed-scale", type=float, default=0.25, help="Normal-mode fraction of --qpps")
    parser.add_argument("--turbo-scale", type=float, default=0.75, help="Turbo-mode fraction of --qpps while LB is held")
    parser.add_argument("--deadzone", type=float, default=0.15, help="Stick deadzone from 0.0 to 1.0")
    parser.add_argument("--loop-interval", type=float, default=0.05, help="Main control loop interval in seconds")
    parser.add_argument("--retry-interval", type=float, default=1.0, help="Hardware reconnect retry interval in seconds")
    parser.add_argument("--telemetry-interval", type=float, default=0.2, help="Telemetry publish interval in seconds")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET, help="Telemetry hub publisher socket")
    return parser


def main():
    args = build_parser().parse_args()
    config = TeleopConfig(
        device=args.device,
        port=args.port,
        address=args.address,
        baud=args.baud,
        qpps=args.qpps,
        speed_scale=args.speed_scale,
        turbo_scale=args.turbo_scale,
        deadzone=args.deadzone,
        loop_interval=args.loop_interval,
        retry_interval=args.retry_interval,
        telemetry_interval=args.telemetry_interval,
        telemetry_socket=args.telemetry_socket,
    )
    runner = GamepadTeleopRunner(config)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    runner.run_forever()


if __name__ == "__main__":
    main()
