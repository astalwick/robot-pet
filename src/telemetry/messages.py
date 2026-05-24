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
    left_max_qpps: int | None = None,
    right_max_qpps: int | None = None,
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
        "left_max_qpps": left_max_qpps,
        "right_max_qpps": right_max_qpps,
        "left_error_qpps": left_target_qpps - left_actual_qpps if left_actual_qpps is not None else None,
        "right_error_qpps": right_target_qpps - right_actual_qpps if right_actual_qpps is not None else None,
        "left_current_amps": left_current_amps,
        "right_current_amps": right_current_amps,
        "read_ok": read_ok,
    }


def link_loop_message(
    read_success_rate: float | None,
    consecutive_read_failures: int,
    last_good_read_age_seconds: float | None,
    telemetry_latency_ms: float | None,
    command_loop_hz: float | None,
) -> dict[str, Any]:
    return {
        "read_success_rate": read_success_rate,
        "consecutive_read_failures": consecutive_read_failures,
        "last_good_read_age_seconds": last_good_read_age_seconds,
        "telemetry_latency_ms": telemetry_latency_ms,
        "command_loop_hz": command_loop_hz,
    }


def drive_status_message(
    state: str,
    stop_reason: str | None,
    controller_reader_alive: bool | None,
    motor_command_ok: bool | None,
    consecutive_motor_command_failures: int,
    last_motor_command_ack_age_seconds: float | None,
    telemetry_publish_failures: int,
    last_telemetry_publish_ok: bool | None,
) -> dict[str, Any]:
    return {
        "state": state,
        "stop_reason": stop_reason,
        "controller_reader_alive": controller_reader_alive,
        "motor_command_ok": motor_command_ok,
        "consecutive_motor_command_failures": consecutive_motor_command_failures,
        "last_motor_command_ack_age_seconds": last_motor_command_ack_age_seconds,
        "telemetry_publish_failures": telemetry_publish_failures,
        "last_telemetry_publish_ok": last_telemetry_publish_ok,
    }


def vision_update(
    enabled: bool,
    status: str,
    faces: list[dict[str, float]],
    image_width: int | None,
    image_height: int | None,
    detection_rate_hz: float,
    last_detection_time: float | None,
    error: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "vision",
        "time": now if now is not None else time.time(),
        "enabled": enabled,
        "status": status,
        "faces": faces,
        "image_width": image_width,
        "image_height": image_height,
        "detection_rate_hz": detection_rate_hz,
        "last_detection_time": last_detection_time,
        "error": error,
    }


def voice_update(
    enabled: bool,
    status: str,
    input_device: str,
    output_device: str,
    sample_rate: int,
    capture_channels: int,
    capture_channel_index: int,
    input_gain: float = 1.0,
    output_gain: float = 1.0,
    assistant_speaking: bool = False,
    partial_transcript: str | None = None,
    last_committed_transcript: str | None = None,
    last_assistant_text: str | None = None,
    last_error: str | None = None,
    barge_in_enabled: bool | None = None,
    barge_in_min_rms: int | None = None,
    barge_in_sustain_ms: int | None = None,
    barge_in_playback_leakage_ratio: float | None = None,
    barge_in_threshold_rms: int | None = None,
    barge_in_mic_rms: int | None = None,
    barge_in_playback_rms: int | None = None,
    barge_in_gate_open: bool | None = None,
    barge_in_last_reason: str | None = None,
    barge_in_event_count: int | None = None,
    barge_in_last_event: str | None = None,
    wake_word_enabled: bool | None = None,
    wake_threshold: float | None = None,
    wake_last_score: float | None = None,
    wake_fire_count: int | None = None,
    wake_last_fire_at: float | None = None,
    timeline: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "source_update",
        "source": "voice",
        "time": now if now is not None else time.time(),
        "enabled": enabled,
        "status": status,
        "input_device": input_device,
        "output_device": output_device,
        "sample_rate": sample_rate,
        "capture_channels": capture_channels,
        "capture_channel_index": capture_channel_index,
        "input_gain": input_gain,
        "output_gain": output_gain,
        "assistant_speaking": assistant_speaking,
        "partial_transcript": partial_transcript,
        "last_committed_transcript": last_committed_transcript,
        "last_assistant_text": last_assistant_text,
        "last_error": last_error,
    }
    if barge_in_enabled is not None:
        payload["barge_in_enabled"] = barge_in_enabled
    if barge_in_min_rms is not None:
        payload["barge_in_min_rms"] = barge_in_min_rms
    if barge_in_sustain_ms is not None:
        payload["barge_in_sustain_ms"] = barge_in_sustain_ms
    if barge_in_playback_leakage_ratio is not None:
        payload["barge_in_playback_leakage_ratio"] = barge_in_playback_leakage_ratio
    if barge_in_threshold_rms is not None:
        payload["barge_in_threshold_rms"] = barge_in_threshold_rms
    if barge_in_mic_rms is not None:
        payload["barge_in_mic_rms"] = barge_in_mic_rms
    if barge_in_playback_rms is not None:
        payload["barge_in_playback_rms"] = barge_in_playback_rms
    if barge_in_gate_open is not None:
        payload["barge_in_gate_open"] = barge_in_gate_open
    if barge_in_last_reason is not None:
        payload["barge_in_last_reason"] = barge_in_last_reason
    if barge_in_event_count is not None:
        payload["barge_in_event_count"] = barge_in_event_count
    if barge_in_last_event is not None:
        payload["barge_in_last_event"] = barge_in_last_event
    if wake_word_enabled is not None:
        payload["wake_word_enabled"] = wake_word_enabled
    if wake_threshold is not None:
        payload["wake_threshold"] = wake_threshold
    if wake_last_score is not None:
        payload["wake_last_score"] = wake_last_score
    if wake_fire_count is not None:
        payload["wake_fire_count"] = wake_fire_count
    if wake_last_fire_at is not None:
        payload["wake_last_fire_at"] = wake_last_fire_at
    if timeline is not None:
        payload["timeline"] = timeline
    return payload


def gamepad_teleop_update(
    controller: dict[str, Any],
    wheels: dict[str, Any],
    motor_battery: dict[str, Any],
    now: float | None = None,
    link_loop: dict[str, Any] | None = None,
    drive_tuning: dict[str, Any] | None = None,
    drive_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "gamepad_teleop",
        "time": now if now is not None else time.time(),
        "controller": controller,
        "wheels": wheels,
        "motor_battery": motor_battery,
        "link_loop": link_loop,
        "drive_tuning": drive_tuning,
        "drive_status": drive_status,
    }
