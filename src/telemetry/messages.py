"""Small helpers for robot telemetry JSON messages."""

from __future__ import annotations

import json
import time
from typing import Any


BUTTON_FIELDS = (
    "a",
    "b",
    "x",
    "y",
    "lb",
    "rb",
    "back",
    "start",
    "guide",
    "left_stick",
    "right_stick",
)


def encode_json_line(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode_json_line(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def motor_battery_status(pack_voltage: float | None) -> str:
    if pack_voltage is None:
        return "unknown"
    if pack_voltage <= 9.6:
        return "critical"
    if pack_voltage < 10.5:
        return "low"
    return "ok"


def motor_battery_message(pack_voltage: float | None) -> dict[str, Any]:
    return {
        "pack_voltage": pack_voltage,
        "cell_voltage": pack_voltage / 3.0 if pack_voltage is not None else None,
        "status": motor_battery_status(pack_voltage),
    }


def is_stale(last_seen: float | None, now: float | None = None, timeout: float = 1.0) -> bool:
    if last_seen is None:
        return True
    return ((now if now is not None else time.monotonic()) - last_seen) > timeout


def stale_label(stale: bool, connected: bool = True) -> str:
    if not connected:
        return "disconnected"
    return "stale" if stale else "live"


def controller_message(state: Any, connected: bool = True) -> dict[str, Any]:
    return {
        "connected": connected,
        "left_stick_x": state.left_stick_x,
        "left_stick_y": state.left_stick_y,
        "right_stick_x": state.right_stick_x,
        "right_stick_y": state.right_stick_y,
        "left_trigger": state.left_trigger,
        "right_trigger": state.right_trigger,
        "dpad_x": state.dpad_x,
        "dpad_y": state.dpad_y,
        "buttons": {
            "a": state.a,
            "b": state.b,
            "x": state.x,
            "y": state.y,
            "lb": state.lb,
            "rb": state.rb,
            "back": state.back,
            "start": state.start,
            "guide": state.guide,
            "left_stick": state.left_stick_click,
            "right_stick": state.right_stick_click,
        },
    }


def wheel_message(
    left_command: float,
    right_command: float,
    left_target_qpps: int,
    right_target_qpps: int,
    left_actual_qpps: int | None = None,
    right_actual_qpps: int | None = None,
    left_current_amps: float | None = None,
    right_current_amps: float | None = None,
    read_ok: bool = True,
) -> dict[str, Any]:
    return {
        "left_command": left_command,
        "right_command": right_command,
        "left_target_qpps": left_target_qpps,
        "right_target_qpps": right_target_qpps,
        "left_actual_qpps": left_actual_qpps,
        "right_actual_qpps": right_actual_qpps,
        "left_error_qpps": left_target_qpps - left_actual_qpps if left_actual_qpps is not None else None,
        "right_error_qpps": right_target_qpps - right_actual_qpps if right_actual_qpps is not None else None,
        "left_current_amps": left_current_amps,
        "right_current_amps": right_current_amps,
        "read_ok": read_ok,
    }


def gamepad_teleop_update(
    controller: dict[str, Any],
    wheels: dict[str, Any],
    motor_battery: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "gamepad_teleop",
        "time": now if now is not None else time.time(),
        "controller": controller,
        "wheels": wheels,
        "motor_battery": motor_battery,
    }
