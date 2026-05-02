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


log = setup_logging("gamepad-teleop")


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


class GamepadTeleopRunner:
    def __init__(
        self,
        config: TeleopConfig,
        controller_factory: Callable[[], Any] | None = None,
        motor_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.controller_factory = controller_factory or self._controller_factory
        self.motor_factory = motor_factory or self._motor_factory
        self.sleep = sleep
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

        while not self.stop_requested and not disconnected.is_set():
            command = self.policy.motion_from_state(controller.state)
            target = self.mixer.to_wheel_speeds(command, turbo=controller.state.lb)

            if not motor.set_wheel_speeds(target.left_qpps, target.right_qpps):
                log.error("RoboClaw speed command was not acknowledged")
                self._safe_zero_speed(motor)
                return

            self.sleep(self.config.loop_interval)

        self._safe_zero_speed(motor)
        if disconnected.is_set():
            log.warning("controller disconnected; waiting for reconnect")

    def _safe_zero_speed(self, motor):
        motor.set_wheel_speeds(0, 0)

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
    )
    runner = GamepadTeleopRunner(config)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    runner.run_forever()


if __name__ == "__main__":
    main()
