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

from control.motion_intent import MOVE_METERS_PER_SECOND
from voice.assistant import (
    CHECK_HEALTH_TOOL,
    CHECK_HEALTH_TOOL_NAME,
    CHECK_SURROUNDINGS_TOOL,
    CHECK_SURROUNDINGS_TOOL_NAME,
    END_SESSION_TOOL,
    END_SESSION_TOOL_NAME,
    EXPRESS_TOOL,
    FACE_ME_TOOL,
    FACE_ME_TOOL_NAME,
    LOOK_TOOL,
    LOOK_TOOL_NAME,
    MOTION_TOOL_NAMES,
    MOVE_TOOL,
    MOVE_TOOL_NAME,
    SCAN_TOOL,
    SCAN_TOOL_NAME,
    START_GOAL_TOOL,
    STOP_TOOL,
    STOP_TOOL_NAME,
    TURN_TOOL,
    VoiceState,
    WEB_SEARCH_TOOL,
    check_health_snapshot,
    check_surroundings_snapshot,
)


# The camera is a wide-angle Pi Camera 3, so a coarse step still overlaps coverage
# between snapshots. A full 360 scan is four snapshots, not a fine sweep.
SCAN_STEP_DEGREES = 90.0

# A scan can never need to sweep more than one full turn: past 360 degrees you are
# just re-photographing what you already saw. Capping here also bounds how many
# blocking turns a single scan call can run, since the goal runner's time budget
# only wraps model calls, not the turns inside one dispatch.
SCAN_MAX_DEGREES = 360.0


INSPECT_SPEAKER_DIRECTION_TOOL_NAME = "inspect_speaker_direction"


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
    voice_state: VoiceState
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


async def _scan(call: RobotToolCall, context: VoiceToolContext) -> RobotToolResult:
    """Turn in coarse steps, capturing a camera snapshot at each, and return them all.

    The wide-angle camera sees a lot per frame, so a full sweep is only a handful of
    snapshots. We snapshot first, then turn, so the first frame is the starting view.
    """
    if context.camera_snapshot_caller is None:
        return _result(call, False, {"ok": False, "error": "camera_snapshot_unavailable"})
    if context.motion_intent_caller is None:
        return _result(call, False, {"ok": False, "error": "motion_caller_missing"})

    total = min(abs(call.arguments.get("degrees") or 360.0), SCAN_MAX_DEGREES)
    captures = max(1, round(total / SCAN_STEP_DEGREES))
    step = total / captures

    image_parts: list[dict[str, Any]] = []
    heading = 0.0
    for index in range(captures):
        try:
            jpeg = await asyncio.to_thread(context.camera_snapshot_caller)
        except Exception as exc:  # noqa: BLE001 -- camera HTTP failures vary
            return _result(call, False, {"ok": False, "error": str(exc)})
        data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
        facing = round(heading)
        if facing == 0:
            label = f"Scan snapshot 1 of {captures}: the view straight ahead, where you started."
        else:
            label = (
                f"Scan snapshot {index + 1} of {captures}: the view {facing} degrees to your left of start. "
                f"To face what you see here, turn {facing} degrees left (call turn with degrees={facing})."
            )
        image_parts.append({"type": "input_text", "text": label})
        image_parts.append({"type": "input_image", "image_url": data_url})

        turn = await asyncio.to_thread(context.motion_intent_caller, "turn", degrees=step)
        if turn.get("ok") is False:
            return _result(call, False, {"ok": False, "error": turn.get("error", "turn_failed")})
        heading += step

    # Return to the starting heading so each snapshot's "degrees to your left" label
    # stays true from where the robot now sits. Turn back the short way; a full 360
    # sweep already lands on start, so there is nothing to undo.
    offset = heading % 360.0
    if offset:
        back = -offset if offset <= 180.0 else 360.0 - offset
        turn = await asyncio.to_thread(context.motion_intent_caller, "turn", degrees=back)
        if turn.get("ok") is False:
            return _result(call, False, {"ok": False, "error": turn.get("error", "turn_failed")})
    image_parts.append(
        {"type": "input_text", "text": "Scan complete. You are now facing your starting direction again."}
    )

    return RobotToolResult(
        name=call.name,
        call_id=call.call_id,
        ok=True,
        output={"ok": True, "snapshots": captures, "degrees_covered": round(heading)},
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
        if name == MOVE_TOOL_NAME and "distance_meters" in call.arguments:
            distance_meters = call.arguments["distance_meters"]
            if (
                not isinstance(distance_meters, (int, float))
                or isinstance(distance_meters, bool)
                or not math.isfinite(distance_meters)
            ):
                return _result(call, False, {"ok": False, "error": "invalid_distance"})
            arguments = {"duration_seconds": distance_meters / MOVE_METERS_PER_SECOND}
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
        data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
        image_parts = [
            {"type": "input_text", "text": "Here is the current camera snapshot from the robot."},
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
