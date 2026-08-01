"""Shared robot tool surface for both the normal assistant turn and the agent runner.

The normal streaming assistant path (`voice/assistant.py`) and the iterative agent
runner both need to call the same robot tools. This module owns the small data
shapes and the single dispatch function so the dispatch logic lives in one place.

Physical-motion timing (waiting for a speculative turn to actually speak) stays in
the assistant path, because that gating is about speculative turns, not about the
tool itself. The agent runner has no speculative turns, so it calls tools directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lib.log import setup_logging
from voice.camera_overlay import annotate_snapshot
from voice.model_frames import save_model_frame


log = setup_logging("robot-voice")

END_SESSION_TOOL_NAME = "end_session"
EXPRESS_TOOL_NAME = "express"
MOVE_TOOL_NAME = "move"
TURN_TOOL_NAME = "turn"
STOP_TOOL_NAME = "stop"
SCAN_TOOL_NAME = "scan"
LOOK_TOOL_NAME = "look"
CHECK_HEALTH_TOOL_NAME = "check_health"
CHECK_SURROUNDINGS_TOOL_NAME = "check_surroundings"
FACE_ME_TOOL_NAME = "face_me"
START_GOAL_TOOL_NAME = "start_goal"
MOTION_TOOL_NAMES = (EXPRESS_TOOL_NAME, MOVE_TOOL_NAME, TURN_TOOL_NAME)


EXPRESS_TOOL = {
    "type": "function",
    "name": EXPRESS_TOOL_NAME,
    "description": (
        "Express an emotion with a short body motion. The robot only has wheels, so every "
        "expression is a movement in place: 'wiggle' is a small playful left-right sway, "
        "'spin' is an excited full turn, and 'shake' is a quick back-and-forth like saying no. "
        "Pick the kind that best fits the feeling."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["wiggle", "spin", "shake"],
                "description": "Which expression to perform.",
            },
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    "strict": True,
}


MOVE_TOOL = {
    "type": "function",
    "name": MOVE_TOOL_NAME,
    "description": (
        "Drive the robot straight by distance. The distance_meters argument is signed: "
        "positive values drive forward, negative values drive backward."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "distance_meters": {
                "type": "number",
                "description": (
                    "Approximate distance to drive: positive forward, negative backward."
                ),
            },
        },
        "required": ["distance_meters"],
        "additionalProperties": False,
    },
    "strict": True,
}


TURN_TOOL = {
    "type": "function",
    "name": TURN_TOOL_NAME,
    "description": (
        "Turn the robot in place by a number of degrees. The degrees argument is "
        "signed: positive degrees turn the robot to its left, negative degrees turn "
        "it to its right. Magnitude can be from 1 up to 360 degrees."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "degrees": {
                "type": "number",
                "description": "How far to turn: positive for left, negative for right, 1 to 360 degrees.",
            },
        },
        "required": ["degrees"],
        "additionalProperties": False,
    },
    "strict": True,
}


STOP_TOOL = {
    "type": "function",
    "name": STOP_TOOL_NAME,
    "description": (
        "Immediately stop the robot's motion. Use this the moment the user says to stop, "
        "halt, or wait. It cancels any move, turn, or expression already in progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


SCAN_TOOL = {
    "type": "function",
    "name": SCAN_TOOL_NAME,
    "description": (
        "Look around by sweeping a requested number of degrees and returning observations, "
        "then facing the starting direction again. Use this when you need to survey more "
        "than what is directly ahead. Each image includes a degree ruler along the bottom "
        "(L20 means turn left 20 degrees; R20 means turn right with degrees=-20), fixed "
        "corridor lines at half a meter and one meter ahead, and an orange SENSED corridor "
        "pair at the nearest forward sensor distance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "degrees": {
                "type": "number",
                "description": "Total degrees to sweep, from a small arc up to 360 (a full turn). Pass 360 to look all the way around.",
            },
        },
        "required": ["degrees"],
        "additionalProperties": False,
    },
    "strict": True,
}


END_SESSION_TOOL = {
    "type": "function",
    "name": END_SESSION_TOOL_NAME,
    "description": (
        "End the active listening session and return to wake-word-only mode. "
        "Use when the user is done talking for now."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


LOOK_TOOL = {
    "type": "function",
    "name": LOOK_TOOL_NAME,
    "description": (
        "Look forward from the robot camera so you can answer questions about what the robot "
        "sees right now. The image includes a degree ruler along the bottom (L20 means turn "
        "left 20 degrees; R20 means turn right with degrees=-20), fixed corridor lines at "
        "half a meter and one meter ahead, and an orange SENSED corridor pair at the nearest "
        "forward sensor distance."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


CHECK_HEALTH_TOOL = {
    "type": "function",
    "name": CHECK_HEALTH_TOOL_NAME,
    "description": (
        "Check the robot's own body health: motor battery, Pi UPS battery, motor rail, drive "
        "and safety state, and computer health. Use this for questions about how the robot is "
        "doing, its power, or whether its motors are ready."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


CHECK_SURROUNDINGS_TOOL = {
    "type": "function",
    "name": CHECK_SURROUNDINGS_TOOL_NAME,
    "description": (
        "Check what is around the robot right now: distance sensor readings and how many faces "
        "are in view. This is a fast perceptual check; use it to tell whether something is close "
        "or whether anyone is nearby."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


FACE_ME_TOOL = {
    "type": "function",
    "name": FACE_ME_TOOL_NAME,
    "description": (
        "Turn the robot to face whoever is speaking, based on the most recent direction "
        "the voice came from. Use this when the user asks the robot to look at them or face them."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


START_GOAL_TOOL = {
    "type": "function",
    "name": START_GOAL_TOOL_NAME,
    "description": (
        "Start an iterative goal when the user asks for something that may require "
        "repeated tool use, observation, searching, checking progress, or working for "
        "more than one step."
    ),
    "parameters": {
        "type": "object",
        "properties": {"goal": {"type": "string"}},
        "required": ["goal"],
        "additionalProperties": False,
    },
    "strict": True,
}


WEB_SEARCH_TOOL = {"type": "web_search"}

# The camera is a wide-angle Pi Camera 3, so a coarse step still overlaps coverage
# between snapshots. A full 360 scan is four snapshots, not a fine sweep.
SCAN_STEP_DEGREES = 90.0

# A scan can never need to sweep more than one full turn: past 360 degrees you are
# just re-photographing what you already saw. Capping here also bounds how many
# blocking turns a single scan call can run, since the goal runner's time budget
# only wraps model calls, not the turns inside one dispatch.
SCAN_MAX_DEGREES = 360.0


INSPECT_SPEAKER_DIRECTION_TOOL_NAME = "inspect_speaker_direction"
POST_MOTION_CAMERA_TOOLS = frozenset({MOVE_TOOL_NAME, TURN_TOOL_NAME, FACE_ME_TOOL_NAME})


INSPECT_SPEAKER_DIRECTION_TOOL = {
    "type": "function",
    "name": INSPECT_SPEAKER_DIRECTION_TOOL_NAME,
    "description": (
        "Check the most recent direction the user's voice came from, relative to the robot. "
        "Reports whether the direction is fresh enough to act on and the relative angle if known. "
        "Does not move the robot."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


# One source of truth for the robot's tool surface. Each tool carries two flags:
# whether it is offered on the normal assistant turn, and whether it is offered to
# the iterative goal runner. ASSISTANT_TOOLS and AGENT_TOOLS are filtered views of
# this single list, so the two surfaces can never silently drift apart.
#
#                                     assistant turn   goal runner
ROBOT_TOOLS = [
    (END_SESSION_TOOL,                  True,           False),
    (EXPRESS_TOOL,                      True,           True),
    (MOVE_TOOL,                         True,           True),
    (TURN_TOOL,                         True,           True),
    (STOP_TOOL,                         True,           True),
    (SCAN_TOOL,                         False,          True),
    (LOOK_TOOL,                         True,           True),
    (CHECK_HEALTH_TOOL,                 True,           True),
    (CHECK_SURROUNDINGS_TOOL,           True,           True),
    (FACE_ME_TOOL,                      True,           True),
    (INSPECT_SPEAKER_DIRECTION_TOOL,    True,           True),
    (START_GOAL_TOOL,                   True,           False),
    (WEB_SEARCH_TOOL,                   True,           True),
]

ASSISTANT_TOOLS = [tool for tool, on_assistant_turn, _goal in ROBOT_TOOLS if on_assistant_turn]
AGENT_TOOLS = [tool for tool, _assistant, in_goal_runner in ROBOT_TOOLS if in_goal_runner]


def _telemetry_value_available(snapshot: dict[str, Any], source: str, value: object) -> bool:
    sources = snapshot.get("sources") or {}
    return (sources.get(source) or {}).get("stale") is False and isinstance(value, dict)


def _motion_value_available(snapshot: dict[str, Any], value: object) -> bool:
    """Motion owns battery and drive now; fall back to gamepad_teleop for old snapshots."""
    sources = snapshot.get("sources") or {}
    motion_source = sources.get("robot_motion") or {}
    if motion_source.get("last_seen") is not None or motion_source.get("stale") is False:
        return motion_source.get("stale") is False and isinstance(value, dict)
    return _telemetry_value_available(snapshot, "gamepad_teleop", value)


def _motor_battery_available(snapshot: dict[str, Any], battery: object) -> bool:
    if isinstance(battery, dict) and battery.get("stale") is True:
        return True
    return _motion_value_available(snapshot, battery)


def _interpret_sensor_reading(reading: dict[str, Any]) -> dict[str, Any]:
    role = reading.get("role")
    name = reading.get("name")
    ok = reading.get("ok")
    distance_mm = reading.get("distance_mm")

    if role == "forward" and ok and distance_mm is None:
        # The driver reports a valid measurement with no distance when nothing
        # is within range: infinite clearance, not a failure.
        return {"name": name, "role": "forward", "status": "no_object_in_range"}

    if role == "forward" and ok and distance_mm is not None:
        stop_below_mm = reading.get("stop_below_mm")
        tripped = stop_below_mm is not None and distance_mm < stop_below_mm
        return {
            "name": name,
            "role": "forward",
            "clearance_m": round(distance_mm / 1000, 2),
            "stops_below_m": round(stop_below_mm / 1000, 2) if stop_below_mm is not None else None,
            "tripped": tripped,
        }

    if role == "cliff" and ok and distance_mm is not None:
        trip_above_mm = reading.get("trip_above_mm")
        cliff_detected = trip_above_mm is not None and distance_mm > trip_above_mm
        return {
            "name": name,
            "role": "cliff",
            "status": "cliff_detected" if cliff_detected else "floor_normal",
        }

    result: dict[str, Any] = {
        "name": name,
        "distance_mm": distance_mm,
        "ok": ok,
    }
    if role is not None:
        result["role"] = role
    return result


def forward_clearances(surroundings: dict[str, Any] | None) -> dict[str, float | None]:
    clearances = {"left": None, "center": None, "right": None}
    if not surroundings or not surroundings.get("ok"):
        return clearances
    sensors = surroundings.get("sensors") or {}
    for reading in sensors.get("readings") or []:
        if not isinstance(reading, dict) or reading.get("role") != "forward":
            continue
        name = (reading.get("name") or "").lower()
        clearance = reading.get("clearance_m")
        if reading.get("status") == "no_object_in_range":
            slot = math.inf
        elif clearance is None or reading.get("ok") is False:
            slot = None
        else:
            slot = float(clearance)
        if "left" in name:
            clearances["left"] = slot
        elif "right" in name:
            clearances["right"] = slot
        elif "center" in name:
            clearances["center"] = slot
    return clearances


def forward_sensors_sentence(clearances: dict[str, float | None]) -> str:
    if not any(value is not None for value in clearances.values()):
        return ""
    parts: list[str] = []
    for label in ("left", "center", "right"):
        value = clearances.get(label)
        if value is None:
            parts.append(f"{label} unavailable")
        elif math.isinf(value):
            parts.append(f"{label} clear beyond range")
        else:
            parts.append(f"{label} {value:.2f} meters")
    return f" Forward sensors: {', '.join(parts)}."


def inspect_robot_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        return {"ok": False, "error": "telemetry_unavailable"}

    battery = snapshot.get("motor_battery")
    pi_battery = snapshot.get("pi_battery")
    drive = snapshot.get("drive_status")
    motor_rail = snapshot.get("motor_rail")
    sensors = snapshot.get("sensors")
    vision = snapshot.get("vision")
    pi = snapshot.get("pi")

    result: dict[str, Any] = {
        "ok": True,
        "battery": {"available": False},
        "pi_battery": {"available": False},
        "drive": {"available": False},
        "motor_rail": {"available": False},
        "sensors": {"available": False},
        "vision": {"available": False},
        "pi": {"available": False},
    }

    if _motor_battery_available(snapshot, battery):
        result["battery"] = {
            "available": True,
            "status": battery.get("status"),
            "pack_voltage": battery.get("pack_voltage"),
            "cell_voltage": battery.get("cell_voltage"),
            "percent_estimate": battery.get("percent_estimate"),
            "chemistry": battery.get("chemistry"),
            "cell_count": battery.get("cell_count"),
            "capacity_mah": battery.get("capacity_mah"),
            "stale": battery.get("stale", False),
            "stale_reason": battery.get("stale_reason"),
            "cached_at": battery.get("cached_at"),
        }
    if _telemetry_value_available(snapshot, "pi_battery", pi_battery):
        result["pi_battery"] = {
            "available": True,
            "status": pi_battery.get("status"),
            "pack_voltage": pi_battery.get("pack_voltage"),
            "percent": pi_battery.get("percent"),
            "current_amps": pi_battery.get("current_amps"),
            "power_state": pi_battery.get("power_state"),
            "runtime_minutes": pi_battery.get("runtime_minutes"),
            "warning_voltage": pi_battery.get("warning_voltage"),
            "shutdown_voltage": pi_battery.get("shutdown_voltage"),
            "shutdown_pending": pi_battery.get("shutdown_pending"),
        }
    if _motion_value_available(snapshot, drive):
        result["drive"] = {
            "available": True,
            "state": drive.get("state"),
            "stop_reason": drive.get("stop_reason"),
            "safety_blocked": drive.get("safety_blocked"),
            "safety_reason": drive.get("safety_reason"),
            "roboclaw_ready": drive.get("roboclaw_ready"),
        }
    if _telemetry_value_available(snapshot, "motor_rail", motor_rail):
        result["motor_rail"] = {
            "available": True,
            "state": motor_rail.get("state"),
            "reason": motor_rail.get("reason"),
            "last_pack_voltage": motor_rail.get("last_pack_voltage"),
        }
    if _telemetry_value_available(snapshot, "sensors", sensors):
        result["sensors"] = {
            "available": True,
            "status": sensors.get("status"),
            "readings": [
                _interpret_sensor_reading(reading)
                for reading in sensors.get("readings") or []
                if isinstance(reading, dict)
            ],
        }
    if _telemetry_value_available(snapshot, "vision", vision):
        result["vision"] = {
            "available": True,
            "status": vision.get("status"),
            "face_count": len(vision.get("faces") or []),
        }
    if _telemetry_value_available(snapshot, "system", pi):
        result["pi"] = {
            "available": True,
            "uptime_seconds": pi.get("uptime_seconds"),
            "load_1m": pi.get("load_1m"),
            "soc_temp_c": pi.get("soc_temp_c"),
            "disk_used_percent": pi.get("disk_used_percent"),
            "throttled_flags": pi.get("throttled_flags"),
        }
    return result


def check_health_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    full = inspect_robot_snapshot(snapshot)
    if not full.get("ok"):
        return full
    return {key: full[key] for key in ("ok", "battery", "pi_battery", "drive", "motor_rail", "pi")}


def check_surroundings_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    full = inspect_robot_snapshot(snapshot)
    if not full.get("ok"):
        return full
    return {key: full[key] for key in ("ok", "sensors", "vision")}


@dataclass
class RobotToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass
class RobotToolResult:
    name: str
    call_id: str
    ok: bool
    output: dict[str, Any]
    # For tools that produce images, the model-input content parts (input_text +
    # input_image) so callers can attach a real image to the next model call
    # instead of stuffing base64 into a JSON string.
    image_parts: list[dict[str, Any]] | None = None


@dataclass
class AgentObservation:
    """A tool result shaped as model input for the agent runner's next iteration."""

    text: str
    input_parts: list[dict[str, Any]] | None = None


@dataclass
class VoiceToolContext:
    voice_state: Any
    motion_intent_caller: Callable[..., Any] | None = None
    camera_snapshot_caller: Callable[[], bytes] | None = None
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None
    face_me_caller: Callable[[], dict[str, Any]] | None = None
    speaker_direction_caller: Callable[[], dict[str, Any]] | None = None
    end_session_pending: list[bool] | None = None


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def speaker_direction_output(snapshot: dict[str, Any]) -> dict[str, Any]:
    relative_degrees = snapshot.get("relative_degrees")
    if relative_degrees is None:
        return {"ok": True, "available": False, "fresh": False}
    return {
        "ok": True,
        "available": True,
        "fresh": bool(snapshot.get("fresh")),
        "relative_degrees": relative_degrees,
        "age_seconds": snapshot.get("age_seconds"),
    }


def _result(call: RobotToolCall, ok: bool, output: dict[str, Any]) -> RobotToolResult:
    return RobotToolResult(name=call.name, call_id=call.call_id, ok=ok, output=output)


async def _forward_clearances(context: VoiceToolContext) -> dict[str, float | None] | None:
    if context.robot_inspection_caller is None:
        return None
    try:
        snapshot = await asyncio.to_thread(context.robot_inspection_caller)
        surroundings = check_surroundings_snapshot(snapshot)
        if surroundings.get("ok"):
            return forward_clearances(surroundings)
    except Exception as exc:  # noqa: BLE001 -- telemetry transport failures vary
        log.warning("forward clearances fetch failed: %s", exc)
        return None
    return None


def encoder_stall_hint(distance_meters: float | None = None) -> str:
    snag = (
        "Your wheels stalled but no front sensor tripped. You are probably snagged on "
        "something beside your body at wheel height, like a doorframe edge or a furniture leg."
    )
    if isinstance(distance_meters, (int, float)) and distance_meters < 0:
        return f"{snag} Drive forward straight to free yourself. Do not turn in place while snagged."
    return f"{snag} Back up straight to free yourself. Do not turn in place while snagged."


def safety_blocked_hint(blocked_by: str) -> str | None:
    reason = blocked_by.lower()
    if "cliff" in reason:
        return "A cliff sensor tripped. Back away from the edge before anything else."
    if "right" in reason:
        return (
            "You are blocked on the right side. Back up 0.2 to 0.3 meters to create "
            "clearance before turning; turning in place will sweep your right corner into the obstacle."
        )
    if "left" in reason:
        return (
            "You are blocked on the left side. Back up 0.2 to 0.3 meters to create "
            "clearance before turning; turning in place will sweep your left corner into the obstacle."
        )
    if "center" in reason:
        return "Blocked straight ahead. Back up, then pick a direction with more clearance."
    return None


def motion_camera_caption(tool: str, arguments: dict[str, Any], output: dict[str, Any], pose_line: str = "") -> str:
    blocked = output.get("error") == "safety_blocked"
    stalled = output.get("error") == "encoder_no_progress"
    if tool == MOVE_TOOL_NAME:
        traveled = output.get("traveled_m")
        if isinstance(traveled, (int, float)):
            direction = "forward" if traveled >= 0 else "backward"
            action = f"moving {abs(traveled):.2f} meters {direction}"
        else:
            distance = arguments.get("distance_meters")
            if isinstance(distance, (int, float)):
                direction = "forward" if distance >= 0 else "backward"
                action = f"moving {abs(distance):.2f} meters {direction}"
            else:
                action = "moving"
    elif tool == TURN_TOOL_NAME:
        turn_degrees = output.get("measured_degrees", arguments.get("degrees"))
        if isinstance(turn_degrees, (int, float)) and turn_degrees != 0:
            side = "left" if turn_degrees > 0 else "right"
            action = f"turning {abs(turn_degrees):.0f} degrees {side}"
        else:
            action = "turning"
    elif tool == FACE_ME_TOOL_NAME:
        action = "facing you"
    else:
        action = tool
    caption = f"Camera view after {action}{' (blocked)' if blocked else ' (stalled)' if stalled else ''}."
    if pose_line:
        caption = f"{caption} {pose_line}"
    return caption


async def attach_motion_observation(
    call: RobotToolCall,
    output: dict[str, Any],
    context: VoiceToolContext,
    pose_text: str = "",
) -> RobotToolResult:
    enriched = dict(output)
    surroundings: dict[str, Any] | None = None
    if context.robot_inspection_caller is not None:
        try:
            snapshot = await asyncio.to_thread(context.robot_inspection_caller)
            surroundings = check_surroundings_snapshot(snapshot)
            if surroundings.get("ok"):
                enriched["surroundings"] = surroundings
        except Exception as exc:  # noqa: BLE001 -- telemetry transport failures vary
            log.warning("motion surroundings fetch failed: %s", exc)

    if enriched.get("error") == "safety_blocked":
        blocked_by = enriched.get("blocked_by")
        if isinstance(blocked_by, str):
            hint = safety_blocked_hint(blocked_by)
            if hint is not None:
                enriched["hint"] = hint
    elif enriched.get("error") == "encoder_no_progress":
        distance = call.arguments.get("distance_meters") if call.name == MOVE_TOOL_NAME else None
        enriched["hint"] = encoder_stall_hint(distance)

    image_parts = None
    clearances = forward_clearances(surroundings)
    if context.camera_snapshot_caller is not None:
        try:
            jpeg = await asyncio.to_thread(context.camera_snapshot_caller)
            caption = motion_camera_caption(call.name, call.arguments, enriched, pose_text)
            if clearances:
                caption += forward_sensors_sentence(clearances)
            jpeg = annotate_snapshot(jpeg, clearances)
            save_model_frame(jpeg, f"motion-{call.name}", caption)
            data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
            image_parts = [
                {"type": "input_text", "text": caption},
                {"type": "input_image", "image_url": data_url},
            ]
        except Exception as exc:  # noqa: BLE001 -- camera HTTP failures vary
            log.warning("motion camera fetch failed: %s", exc)

    return RobotToolResult(
        name=call.name,
        call_id=call.call_id,
        ok=enriched.get("ok") is not False,
        output=enriched,
        image_parts=image_parts,
    )


async def _scan(call: RobotToolCall, context: VoiceToolContext) -> RobotToolResult:
    """Turn in coarse steps, capturing a camera snapshot at each, and return them all.

    The wide-angle camera sees a lot per frame, so a full sweep is only a handful of
    snapshots. We snapshot first, then turn, so the first frame is the starting view.
    """
    if context.camera_snapshot_caller is None:
        return _result(call, False, {"ok": False, "error": "camera_snapshot_unavailable"})
    if context.motion_intent_caller is None:
        return _result(call, False, {"ok": False, "error": "motion_caller_missing"})

    degrees = call.arguments.get("degrees")
    if not isinstance(degrees, (int, float)) or isinstance(degrees, bool) or not math.isfinite(degrees) or degrees == 0:
        return _result(call, False, {"ok": False, "error": "invalid_degrees"})
    total = min(abs(degrees), SCAN_MAX_DEGREES)
    captures = max(1, round(total / SCAN_STEP_DEGREES))
    full_sweep = total >= SCAN_MAX_DEGREES
    step = total / captures
    headings = [index * step for index in range(captures)]
    if not full_sweep:
        headings.append(total)

    image_parts: list[dict[str, Any]] = []
    current_heading = 0.0
    for index, snapshot_heading in enumerate(headings):
        try:
            jpeg = await asyncio.to_thread(context.camera_snapshot_caller)
        except Exception as exc:  # noqa: BLE001 -- camera HTTP failures vary
            return _result(call, False, {"ok": False, "error": str(exc)})
        clearances = await _forward_clearances(context)
        jpeg = annotate_snapshot(jpeg, clearances)
        save_model_frame(jpeg, "scan")
        data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
        facing = round(snapshot_heading)
        if facing == 0:
            label = f"Scan snapshot 1 of {len(headings)}: the view straight ahead, where you started."
        else:
            label = (
                f"Scan snapshot {index + 1} of {len(headings)}: the view {facing} degrees to your left of start. "
                f"To face what you see here, turn {facing} degrees left (call turn with degrees={facing})."
            )
        if clearances is not None:
            label += forward_sensors_sentence(clearances)
        image_parts.append({"type": "input_text", "text": label})
        image_parts.append({"type": "input_image", "image_url": data_url})

        if full_sweep or index < len(headings) - 1:
            turn = await asyncio.to_thread(context.motion_intent_caller, "turn", degrees=step)
            if turn.get("ok") is False:
                # Tell the model how far the sweep got: the robot is stranded at an
                # arbitrary heading, and its mental map is wrong without this.
                return _result(
                    call,
                    False,
                    {
                        "ok": False,
                        "error": turn.get("error", "turn_failed"),
                        "snapshots": index + 1,
                        "degrees_covered": round(current_heading),
                    },
                )
            current_heading += step

    # Return to the starting heading so each snapshot's "degrees to your left" label
    # stays true from where the robot now sits. Turn back the short way; a full 360
    # sweep already lands on start, so there is nothing to undo.
    offset = current_heading % 360.0
    if offset:
        back = -offset if offset <= 180.0 else 360.0 - offset
        turn = await asyncio.to_thread(context.motion_intent_caller, "turn", degrees=back)
        if turn.get("ok") is False:
            return _result(
                call,
                False,
                {
                    "ok": False,
                    "error": turn.get("error", "turn_failed"),
                    "snapshots": len(headings),
                    "degrees_covered": round(current_heading),
                },
            )
    image_parts.append(
        {"type": "input_text", "text": "Scan complete. You are now facing your starting direction again."}
    )

    return RobotToolResult(
        name=call.name,
        call_id=call.call_id,
        ok=True,
        output={"ok": True, "snapshots": len(headings), "degrees_covered": round(current_heading)},
        image_parts=image_parts,
    )


async def dispatch_tool(call: RobotToolCall, context: VoiceToolContext) -> RobotToolResult:
    name = call.name

    if name == END_SESSION_TOOL_NAME:
        if context.end_session_pending is None:
            return _result(call, False, {"ok": False, "error": "session_end_unavailable"})
        context.end_session_pending[0] = True
        return _result(call, True, {"ok": True, "ended": True})

    if name in MOTION_TOOL_NAMES:
        if context.motion_intent_caller is None:
            return _result(call, False, {"ok": False, "error": "motion_caller_missing"})
        arguments = call.arguments
        if name == MOVE_TOOL_NAME:
            distance_meters = call.arguments.get("distance_meters")
            if (
                not isinstance(distance_meters, (int, float))
                or isinstance(distance_meters, bool)
                or not math.isfinite(distance_meters)
            ):
                return _result(call, False, {"ok": False, "error": "invalid_distance"})
            arguments = {"distance_meters": distance_meters}
        result = await asyncio.to_thread(context.motion_intent_caller, name, **arguments)
        return _result(call, result.get("ok") is not False, result)

    if name == STOP_TOOL_NAME:
        # Routed through the motion caller like any intent, but kept out of
        # MOTION_TOOL_NAMES so the assistant never gates it on playback: a stop must
        # halt the robot the instant it is requested.
        if context.motion_intent_caller is None:
            return _result(call, False, {"ok": False, "error": "motion_caller_missing"})
        result = await asyncio.to_thread(context.motion_intent_caller, name)
        return _result(call, result.get("ok") is not False, result)

    if name == LOOK_TOOL_NAME:
        if context.camera_snapshot_caller is None:
            return _result(call, False, {"ok": False, "error": "camera_snapshot_unavailable"})
        try:
            jpeg = await asyncio.to_thread(context.camera_snapshot_caller)
        except Exception as exc:  # noqa: BLE001 -- camera HTTP failures vary
            return _result(call, False, {"ok": False, "error": str(exc)})
        clearances = await _forward_clearances(context)
        jpeg = annotate_snapshot(jpeg, clearances)
        save_model_frame(jpeg, "look")
        data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
        caption = "Here is the current camera snapshot from the robot."
        if clearances is not None:
            caption += forward_sensors_sentence(clearances)
        image_parts = [
            {"type": "input_text", "text": caption},
            {"type": "input_image", "image_url": data_url},
        ]
        return RobotToolResult(
            name=name,
            call_id=call.call_id,
            ok=True,
            output={"ok": True, "image_attached": True},
            image_parts=image_parts,
        )

    if name == SCAN_TOOL_NAME:
        return await _scan(call, context)

    if name == CHECK_HEALTH_TOOL_NAME or name == CHECK_SURROUNDINGS_TOOL_NAME:
        if context.robot_inspection_caller is None:
            return _result(call, False, {"ok": False, "error": "telemetry_unavailable"})
        try:
            snapshot = await asyncio.to_thread(context.robot_inspection_caller)
        except Exception as exc:  # noqa: BLE001 -- telemetry transport failures vary
            return _result(call, False, {"ok": False, "error": str(exc)})
        build = check_health_snapshot if name == CHECK_HEALTH_TOOL_NAME else check_surroundings_snapshot
        result = build(snapshot)
        return _result(call, result.get("ok") is not False, result)

    if name == FACE_ME_TOOL_NAME:
        if context.face_me_caller is None:
            return _result(call, False, {"ok": False, "error": "face_me_unavailable"})
        result = await asyncio.to_thread(context.face_me_caller)
        return _result(call, result.get("ok") is not False, result)

    if name == INSPECT_SPEAKER_DIRECTION_TOOL_NAME:
        if context.speaker_direction_caller is None:
            return _result(call, False, {"ok": False, "error": "speaker_direction_unavailable"})
        snapshot = await asyncio.to_thread(context.speaker_direction_caller)
        return _result(call, True, speaker_direction_output(snapshot))

    return _result(call, False, {"ok": False, "error": "unsupported tool"})


def agent_observation(result: RobotToolResult) -> AgentObservation:
    """Shape a tool result as model input for the agent runner.

    Image results carry their `input_image` parts so the next model call sees a
    real image, not a base64 blob serialized into JSON.
    """
    text = json.dumps({"tool": result.name, "ok": result.ok, "output": result.output})
    return AgentObservation(text=text, input_parts=result.image_parts)
