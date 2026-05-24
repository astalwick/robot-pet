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
from control.commands import MotionCommand, WheelSpeedCommand
from control.differential_drive import DifferentialDriveMixer
from control.motion_intent import MotionIntentBridge, MotionIntentExecutor
from control.teleop import GamepadTeleopPolicy
from drivers.controller import ControllerDriver
from drivers.motor import MotorDriver
from lib.log import setup_logging
from telemetry.messages import (
    controller_message,
    drive_status_message,
    gamepad_teleop_update,
    link_loop_message,
    motor_battery_message,
    wheel_message,
)
from telemetry.paths import DEFAULT_MOTION_INTENT_SOCKET, DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


log = setup_logging("gamepad-teleop")
SLOW_TELEMETRY_WARNING_SECONDS = 0.025
COMMAND_LOOP_STALL_SECONDS = 0.25
COMMAND_LOOP_STALL_LOG_INTERVAL_SECONDS = 5.0
STATUS_PUBLISH_INTERVAL = 0.5


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
    roboclaw_timeout: float = 0.5
    telemetry_socket: str = DEFAULT_PUBLISH_SOCKET
    motion_intent_socket: str = DEFAULT_MOTION_INTENT_SOCKET


class GamepadTeleopRunner:
    def __init__(
        self,
        config: TeleopConfig,
        controller_factory: Callable[[], Any] | None = None,
        motor_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        telemetry_publisher: Callable[[str, dict[str, Any]], bool] = publish_message,
        intent_bridge_factory: Callable[[], MotionIntentBridge] | None = None,
    ):
        self.config = config
        self.controller_factory = controller_factory or self._controller_factory
        self.motor_factory = motor_factory or self._motor_factory
        self.sleep = sleep
        self.clock = clock
        self.telemetry_publisher = telemetry_publisher
        self.intent_bridge_factory = intent_bridge_factory or self._intent_bridge_factory
        self.intent_executor = MotionIntentExecutor()
        self.intent_bridge: MotionIntentBridge | None = None
        self.pending_intent_complete: Callable[[dict[str, Any]], None] | None = None
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
        self.drive_state = "stopped"
        self.stop_reason: str | None = None
        self.consecutive_motor_command_failures = 0
        self.last_motor_command_ack_at: float | None = None
        self.last_motor_command_ok: bool | None = None
        self.telemetry_publish_failures = 0
        self.last_telemetry_publish_ok: bool | None = None

    def request_stop(self, *_args):
        self.stop_requested = True

    def run_forever(self):
        self._start_intent_bridge()
        try:
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
        finally:
            self._stop_intent_bridge()

    def _start_intent_bridge(self):
        try:
            bridge = self.intent_bridge_factory()
            bridge.start()
        except OSError as exc:
            log.warning("motion intent bridge unavailable: %s", exc)
            self.intent_bridge = None
            return
        self.intent_bridge = bridge
        log.info("motion intent socket at %s", self.config.motion_intent_socket)

    def _stop_intent_bridge(self):
        self._fail_pending_intent("shutdown")
        if self.intent_bridge is not None:
            self.intent_bridge.stop()
            self.intent_bridge = None

    def _intent_bridge_factory(self) -> MotionIntentBridge:
        return MotionIntentBridge(self.config.motion_intent_socket)

    def _wait_for_controller(self):
        self._set_drive_state("waiting_for_controller")
        while not self.stop_requested:
            self._publish_status_update()
            controller = self.controller_factory()
            if controller.connect():
                log.info("controller connected")
                return controller

            log.info("waiting for controller")
            self._sleep_with_status_updates(self.config.retry_interval)

        return None

    def _wait_for_roboclaw(self):
        self._set_drive_state("waiting_for_roboclaw")
        while not self.stop_requested:
            self._publish_status_update()
            try:
                motor = self.motor_factory()
            except Exception as exc:
                log.warning("waiting for RoboClaw: %s", exc)
                self._sleep_with_status_updates(self.config.retry_interval)
                continue

            if self._set_wheel_speeds(motor, 0, 0):
                log.info("RoboClaw ready")
                return motor

            log.warning("initial zero-speed command was not acknowledged")
            motor.cleanup()
            self._sleep_with_status_updates(self.config.retry_interval)

        return None

    def _sleep_with_status_updates(self, duration: float, controller_reader_alive: bool | None = None):
        deadline = self.clock() + duration
        next_publish_at = self.clock() + STATUS_PUBLISH_INTERVAL
        while self.clock() < deadline and not self.stop_requested:
            if self.clock() >= next_publish_at:
                self._publish_status_update(controller_reader_alive=controller_reader_alive)
                next_publish_at = self.clock() + STATUS_PUBLISH_INTERVAL
            remaining = min(next_publish_at, deadline) - self.clock()
            if remaining > 0:
                self.sleep(remaining)

    def _run_connected(self, controller, motor):
        disconnected = threading.Event()
        controller.start(on_disconnect=disconnected.set)
        next_telemetry = self.clock() + self.config.telemetry_interval
        closed_loop_active = False
        idle_started_at = self.clock()
        idle_released = False
        motor_max_qpps = self._read_motor_max_qpps(motor)
        last_stall_log_at = 0.0
        stop_reason = None
        self._reset_slew()
        self._set_drive_state("driving")

        while not self.stop_requested and not disconnected.is_set():
            cycle_started = self.clock()
            now = cycle_started
            self.loop_samples.append(now)
            gamepad_command = self.policy.motion_from_state(controller.state)
            gamepad_active = gamepad_command.linear_x != 0.0 or gamepad_command.angular_z != 0.0
            self._service_intent_requests(now)
            intent_command = self._tick_intent(now, gamepad_active)
            command = gamepad_command if gamepad_active or intent_command is None else intent_command
            wheels = self.mixer.mix(command)
            target = self.mixer.to_wheel_speeds(command, turbo=controller.state.lb)
            if controller.state.rb:
                target = self._slew_target(target, now)
            else:
                self._reset_slew()
            target_is_zero = target.left_qpps == 0 and target.right_qpps == 0
            if not controller.reader_alive() and (closed_loop_active or not target_is_zero):
                stop_reason = controller.disconnect_reason or "controller input reader stopped"
                log.error("%s; stopping motors", stop_reason)
                break

            motor_command_elapsed = 0.0
            telemetry_elapsed = 0.0

            if target_is_zero:
                if closed_loop_active:
                    command_started = self.clock()
                    if not self._set_wheel_speeds(motor, 0, 0, now=command_started):
                        stop_reason = "RoboClaw zero-speed command was not acknowledged"
                        log.error("%s", stop_reason)
                        break
                    motor_command_elapsed = self.clock() - command_started
                    closed_loop_active = False
                    idle_started_at = now
                    idle_released = False
                elif not idle_released and now - idle_started_at >= self.config.idle_release_delay:
                    command_started = self.clock()
                    self._release_idle(motor)
                    motor_command_elapsed = self.clock() - command_started
                    idle_released = True
            else:
                command_started = self.clock()
                if not self._set_wheel_speeds(
                    motor,
                    target.left_qpps,
                    target.right_qpps,
                    now=command_started,
                ):
                    stop_reason = "RoboClaw speed command was not acknowledged"
                    log.error("%s", stop_reason)
                    break
                motor_command_elapsed = self.clock() - command_started
                closed_loop_active = True
                idle_released = False

            if now >= next_telemetry:
                telemetry_started = self.clock()
                self._publish_telemetry(
                    controller.state,
                    wheels,
                    target,
                    motor,
                    motor_max_qpps,
                    controller_reader_alive=controller.reader_alive(),
                )
                telemetry_elapsed = self.clock() - telemetry_started
                if telemetry_elapsed > SLOW_TELEMETRY_WARNING_SECONDS:
                    log.warning("telemetry update took %.3fs", telemetry_elapsed)
                next_telemetry = now + self.config.telemetry_interval

            cycle_elapsed = self.clock() - cycle_started
            if (
                cycle_elapsed > COMMAND_LOOP_STALL_SECONDS
                and now - last_stall_log_at >= COMMAND_LOOP_STALL_LOG_INTERVAL_SECONDS
            ):
                log.warning(
                    "command loop stall %.3fs motor=%.3fs telemetry=%.3fs target=(%d,%d)",
                    cycle_elapsed,
                    motor_command_elapsed,
                    telemetry_elapsed,
                    target.left_qpps,
                    target.right_qpps,
                )
                last_stall_log_at = now

            self.sleep(self.config.loop_interval)

        self._set_wheel_speeds(motor, 0, 0, record=False)
        self._reset_slew()
        if disconnected.is_set():
            reason = controller.disconnect_reason or "controller disconnected"
            self._set_drive_state("controller_lost", reason)
            log.warning("%s; waiting for reconnect", reason)
            self._publish_status_update(controller_reader_alive=False)
        elif stop_reason is not None:
            self._set_drive_state("motor_command_failed", stop_reason)
            log.warning("%s; waiting for reconnect", stop_reason)
            self._publish_status_update(controller_reader_alive=controller.reader_alive())
        else:
            self._set_drive_state("stopped")
            self._publish_status_update(controller_reader_alive=controller.reader_alive())

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

    def _release_idle(self, motor):
        motor.stop()

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

    def _set_drive_state(self, state: str, reason: str | None = None):
        if state != self.drive_state or reason != self.stop_reason:
            log.info("drive state old=%s new=%s reason=%s", self.drive_state, state, reason or "")
        self.drive_state = state
        self.stop_reason = reason

    def _set_wheel_speeds(
        self,
        motor,
        left_qpps: int,
        right_qpps: int,
        now: float | None = None,
        record: bool = True,
    ) -> bool:
        ok = motor.set_wheel_speeds(left_qpps, right_qpps)
        if record:
            self._record_motor_command_result(ok, self.clock() if now is None else now)
        return ok

    def _record_motor_command_result(self, ok: bool, now: float):
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

    def _drive_status_payload(self, controller_reader_alive: bool | None = None) -> dict[str, Any]:
        now = self.clock()
        return drive_status_message(
            state=self.drive_state,
            stop_reason=self.stop_reason,
            controller_reader_alive=controller_reader_alive,
            motor_command_ok=self.last_motor_command_ok,
            consecutive_motor_command_failures=self.consecutive_motor_command_failures,
            last_motor_command_ack_age_seconds=self._last_motor_command_ack_age(now),
            telemetry_publish_failures=self.telemetry_publish_failures,
            last_telemetry_publish_ok=self.last_telemetry_publish_ok,
        )

    def _publish_status_update(self, controller_reader_alive: bool | None = None):
        message = gamepad_teleop_update(
            controller={"connected": False},
            wheels={"read_ok": False},
            motor_battery=motor_battery_message(None),
            link_loop=link_loop_message(
                read_success_rate=self._read_success_rate(),
                consecutive_read_failures=self.consecutive_read_failures,
                last_good_read_age_seconds=None,
                telemetry_latency_ms=None,
                command_loop_hz=self._command_loop_hz(),
            ),
            drive_tuning=self.config.drive_tuning.to_dict(),
            drive_status=self._drive_status_payload(controller_reader_alive=controller_reader_alive),
        )
        self._publish_message(message)

    def _publish_telemetry(self, state, wheels, target, motor, motor_max_qpps, controller_reader_alive: bool):
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
            drive_status=self._drive_status_payload(controller_reader_alive=controller_reader_alive),
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
            serial_timeout=self.config.roboclaw_timeout,
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
    parser.add_argument("--roboclaw-timeout", type=float, default=0.5, help="RoboClaw serial watchdog timeout in seconds")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET, help="Telemetry hub publisher socket")
    parser.add_argument("--motion-intent-socket", default=DEFAULT_MOTION_INTENT_SOCKET, help="Voice motion intent listener socket")
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
        roboclaw_timeout=args.roboclaw_timeout,
        telemetry_socket=args.telemetry_socket,
        motion_intent_socket=args.motion_intent_socket,
    )
    runner = GamepadTeleopRunner(config)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    runner.run_forever()


if __name__ == "__main__":
    main()
