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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from voice.assistant import (
    END_SESSION_TOOL_NAME,
    FACE_ME_TOOL_NAME,
    INSPECT_ROBOT_TOOL_NAME,
    LOOK_AROUND_TOOL_NAME,
    MOTION_TOOL_NAMES,
    VoiceState,
    inspect_robot_snapshot,
)


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
        result = await asyncio.to_thread(context.motion_intent_caller, name, **call.arguments)
        return _result(call, result.get("ok") is not False, result)

    if name == LOOK_AROUND_TOOL_NAME:
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

    if name == INSPECT_ROBOT_TOOL_NAME:
        if context.robot_inspection_caller is None:
            return _result(call, False, {"ok": False, "error": "telemetry_unavailable"})
        try:
            snapshot = await asyncio.to_thread(context.robot_inspection_caller)
        except Exception as exc:  # noqa: BLE001 -- telemetry transport failures vary
            return _result(call, False, {"ok": False, "error": str(exc)})
        result = inspect_robot_snapshot(snapshot)
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
