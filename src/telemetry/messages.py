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

PI_BATTERY_WARNING_VOLTAGE = 13.3
PI_BATTERY_SHUTDOWN_VOLTAGE = 13.0
MOTOR_BATTERY_CHEMISTRY = "lipo"
MOTOR_BATTERY_CELL_COUNT = 3
MOTOR_BATTERY_CAPACITY_MAH = 2200
MOTOR_BATTERY_CUTOFF_VOLTAGE = 10.5
MOTOR_BATTERY_WARNING_VOLTAGE = 10.8
MOTOR_BATTERY_PERCENT_CURVE = (
    (4.20, 100),
    (4.10, 90),
    (4.00, 80),
    (3.90, 60),
    (3.80, 40),
    (3.70, 20),
    (3.60, 10),
    (3.50, 5),
    (3.30, 0),
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
    if pack_voltage <= MOTOR_BATTERY_CUTOFF_VOLTAGE:
        return "critical"
    if pack_voltage < MOTOR_BATTERY_WARNING_VOLTAGE:
        return "low"
    return "ok"


def motor_battery_percent_estimate(pack_voltage: float | None) -> int | None:
    if pack_voltage is None:
        return None
    cell_voltage = pack_voltage / MOTOR_BATTERY_CELL_COUNT
    if cell_voltage >= MOTOR_BATTERY_PERCENT_CURVE[0][0]:
        return 100
    for index in range(1, len(MOTOR_BATTERY_PERCENT_CURVE)):
        high_voltage, high_percent = MOTOR_BATTERY_PERCENT_CURVE[index - 1]
        low_voltage, low_percent = MOTOR_BATTERY_PERCENT_CURVE[index]
        if cell_voltage >= low_voltage:
            ratio = (cell_voltage - low_voltage) / (high_voltage - low_voltage)
            return round(low_percent + ratio * (high_percent - low_percent))
    return 0


def motor_battery_message(pack_voltage: float | None) -> dict[str, Any]:
    return {
        "pack_voltage": pack_voltage,
        "cell_voltage": pack_voltage / MOTOR_BATTERY_CELL_COUNT if pack_voltage is not None else None,
        "status": motor_battery_status(pack_voltage),
        "chemistry": MOTOR_BATTERY_CHEMISTRY,
        "cell_count": MOTOR_BATTERY_CELL_COUNT,
        "capacity_mah": MOTOR_BATTERY_CAPACITY_MAH,
        "percent_estimate": motor_battery_percent_estimate(pack_voltage),
    }


def pi_battery_status(
    pack_voltage: float | None,
    warning_voltage: float = PI_BATTERY_WARNING_VOLTAGE,
    shutdown_voltage: float = PI_BATTERY_SHUTDOWN_VOLTAGE,
) -> str:
    if pack_voltage is None:
        return "unknown"
    if pack_voltage <= shutdown_voltage:
        return "critical"
    if pack_voltage <= warning_voltage:
        return "low"
    return "ok"


def pi_battery_message(
    reading: Any | None,
    error: str | None = None,
    warning_voltage: float = PI_BATTERY_WARNING_VOLTAGE,
    shutdown_voltage: float = PI_BATTERY_SHUTDOWN_VOLTAGE,
    shutdown_pending: bool = False,
) -> dict[str, Any]:
    if reading is None:
        return {
            "pack_voltage": None,
            "current_amps": None,
            "percent": None,
            "remaining_mah": None,
            "runtime_minutes": None,
            "charge_time_minutes": None,
            "cell_voltages": [],
            "usb_c_voltage": None,
            "usb_c_current_amps": None,
            "usb_c_power_watts": None,
            "charging": False,
            "fast_charging": False,
            "vbus_present": False,
            "charge_stage": None,
            "bq4050_ok": False,
            "ip2368_ok": False,
            "power_state": "unknown",
            "status": "unknown",
            "warning_voltage": warning_voltage,
            "shutdown_voltage": shutdown_voltage,
            "shutdown_pending": shutdown_pending,
            "error": error,
        }

    if reading.charging:
        power_state = "charging"
    elif reading.battery_ma < 0:
        power_state = "discharging"
    else:
        power_state = "standby"

    pack_voltage = reading.battery_mv / 1000.0
    return {
        "pack_voltage": pack_voltage,
        "current_amps": reading.battery_ma / 1000.0,
        "percent": reading.battery_percent,
        "remaining_mah": reading.remaining_mah,
        "runtime_minutes": reading.runtime_min,
        "charge_time_minutes": reading.charge_time_min,
        "cell_voltages": [cell_mv / 1000.0 for cell_mv in reading.cells_mv],
        "usb_c_voltage": reading.vbus_mv / 1000.0,
        "usb_c_current_amps": reading.vbus_ma / 1000.0,
        "usb_c_power_watts": reading.vbus_mw / 1000.0,
        "charging": reading.charging,
        "fast_charging": reading.fast_charging,
        "vbus_present": reading.vbus_present,
        "charge_stage": reading.charge_stage,
        "bq4050_ok": reading.bq4050_ok,
        "ip2368_ok": reading.ip2368_ok,
        "power_state": power_state,
        "status": pi_battery_status(pack_voltage, warning_voltage, shutdown_voltage),
        "warning_voltage": warning_voltage,
        "shutdown_voltage": shutdown_voltage,
        "shutdown_pending": shutdown_pending,
        "error": error,
    }


def pi_battery_update(battery: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "pi_battery",
        "time": now if now is not None else time.time(),
        **battery,
    }


def motor_rail_update(
    state: str,
    mosfet_gpio: int,
    last_pack_voltage: float | None,
    reason: str | None = None,
    low_voltage_cutoff: float | None = None,
    warning_voltage: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "motor_rail",
        "time": now if now is not None else time.time(),
        "state": state,
        "mosfet_gpio": mosfet_gpio,
        "last_pack_voltage": last_pack_voltage,
        "reason": reason,
        "low_voltage_cutoff": low_voltage_cutoff,
        "warning_voltage": warning_voltage,
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


def gamepad_update(connected: bool, state: str, now: float | None = None) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "gamepad",
        "time": now if now is not None else time.time(),
        "connected": connected,
        "state": state,
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


def odometry_message(left_distance_m: float, right_distance_m: float) -> dict[str, Any]:
    """Cumulative signed wheel travel in meters since the motion service started.

    Positive means robot-forward for both wheels. The dashboard diffs consecutive
    snapshots to dead-reckon a local path, so absolute counters are deliberate:
    they survive missed telemetry frames where per-frame deltas would not.
    """
    return {
        "left_distance_m": left_distance_m,
        "right_distance_m": right_distance_m,
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
    safety_blocked: bool | None = None,
    safety_reason: str | None = None,
    motion_power_requested: bool | None = None,
    roboclaw_ready: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": state,
        "stop_reason": stop_reason,
        "controller_reader_alive": controller_reader_alive,
        "motor_command_ok": motor_command_ok,
        "consecutive_motor_command_failures": consecutive_motor_command_failures,
        "last_motor_command_ack_age_seconds": last_motor_command_ack_age_seconds,
        "telemetry_publish_failures": telemetry_publish_failures,
        "last_telemetry_publish_ok": last_telemetry_publish_ok,
    }
    if safety_blocked is not None:
        payload["safety_blocked"] = safety_blocked
    if safety_reason is not None:
        payload["safety_reason"] = safety_reason
    if motion_power_requested is not None:
        payload["motion_power_requested"] = motion_power_requested
    if roboclaw_ready is not None:
        payload["roboclaw_ready"] = roboclaw_ready
    return payload


def sensors_update(
    enabled: bool,
    status: str,
    readings: list[dict[str, Any]],
    poll_rate_hz: float,
    imu: dict[str, Any] | None = None,
    error: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    payload = {
        "type": "source_update",
        "source": "sensors",
        "time": now if now is not None else time.time(),
        "enabled": enabled,
        "status": status,
        "readings": readings,
        "poll_rate_hz": poll_rate_hz,
        "error": error,
    }
    if imu is not None:
        payload["imu"] = imu
    return payload


def reading_to_dict(reading: Any) -> dict[str, Any]:
    return {
        "name": reading.name,
        "kind": reading.kind,
        "channel": reading.channel,
        "distance_mm": reading.distance_mm,
        "ok": reading.ok,
    }


def imu_reading_to_dict(reading: Any) -> dict[str, Any]:
    return {
        "ok": reading.ok,
        "yaw_degrees": reading.yaw_degrees,
        "pitch_degrees": reading.pitch_degrees,
        "roll_degrees": reading.roll_degrees,
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
    personality: str | None = None,
    doa: dict[str, Any] | None = None,
    timeline: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    scribe_state: str | None = None,
    scribe_open_count: int | None = None,
    scribe_last_error: str | None = None,
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
    if personality is not None:
        payload["personality"] = personality
    if doa is not None:
        payload["doa"] = doa
    if timeline is not None:
        payload["timeline"] = timeline
    if cost is not None:
        payload["cost"] = cost
    if scribe_state is not None:
        payload["scribe_state"] = scribe_state
    if scribe_open_count is not None:
        payload["scribe_open_count"] = scribe_open_count
    if scribe_last_error is not None:
        payload["scribe_last_error"] = scribe_last_error
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


def robot_motion_update(
    wheels: dict[str, Any],
    motor_battery: dict[str, Any],
    now: float | None = None,
    link_loop: dict[str, Any] | None = None,
    drive_status: dict[str, Any] | None = None,
    odometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "source_update",
        "source": "robot_motion",
        "time": now if now is not None else time.time(),
        "wheels": wheels,
        "motor_battery": motor_battery,
        "link_loop": link_loop,
        "drive_status": drive_status,
        "odometry": odometry,
    }
