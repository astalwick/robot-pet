"""The iterative goal runner for the voice agent.

A normal assistant turn answers in one shot. Some requests instead need the robot
to work toward a goal over several steps: move, look, check, decide, repeat. When
the assistant calls `start_goal`, the voice orchestration loop hands the goal here
and `run_agent_goal` drives its own tool loop until it reaches a terminal state.

The loop uses native OpenAI tool calls, the same mechanism the assistant path uses.
Each step is one `responses.create` call chained with `previous_response_id`, so the
model carries its reasoning forward instead of re-reading a flat transcript. The
runner executes the requested tool through the shared dispatcher (`voice/tools.py`),
sends the result back as a `function_call_output`, and loops. It ends naturally when
the model replies with no tool call: that text is the final answer. The other exits
are cancellation, the time budget, and the step budget.

Speech runs in parallel with tool execution. Assistant text that arrives alongside a
tool call is progress narration: it is spoken as a concurrent, detached task and is
never the final or part of history. Only a response with no tool call is the final.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from lib.log import setup_logging
from voice.assistant import (
    CONFIG_DIR,
    FACE_ME_TOOL_NAME,
    MOVE_TOOL_NAME,
    OPENAI_CREATE_RETRY_DELAY_SECS,
    SHARED_ROBOT_GUIDANCE,
    TURN_TOOL_NAME,
    VoiceState,
    check_surroundings_snapshot,
)
from voice.tools import (
    AGENT_TOOLS,
    RobotToolCall,
    VoiceToolContext,
    dispatch_tool,
    parse_tool_arguments,
)


log = setup_logging("robot-voice")


# The goal loop deliberates between physical actions, so it spends a little more
# thinking time than the low-latency assistant turn, which runs with no reasoning.
AGENT_REASONING_EFFORT = "medium"

# The model occasionally returns a response with no tool call and no text. We nudge
# it a few times, then give up rather than spin forever.
MAX_EMPTY_RESPONSES = 3

# Runaway guards, not a budget the goal is expected to spend. The model never sees
# them; the harness enforces them.
MAX_STEPS = 60
MAX_SECONDS = 120.0

TIMEOUT_FINAL = "I ran out of time working on that, so I stopped for now."
STEP_LIMIT_FINAL = "I tried a bunch of steps but could not finish that, so I stopped."
BLOCKED_FINAL = "I got stuck figuring out what to do next, so I stopped."

NUDGE_TEXT = (
    "You replied with no tool call and no words. Either call a tool to keep working, "
    "or say a short final sentence if you are done."
)

GOAL_MOTION_OBSERVATION_TOOLS = frozenset({MOVE_TOOL_NAME, TURN_TOOL_NAME, FACE_ME_TOOL_NAME})
GOAL_POSE_TOOLS = GOAL_MOTION_OBSERVATION_TOOLS
MAX_RECENT_ACTIONS = 5


def _motion_succeeded(output: dict[str, Any]) -> bool:
    return output.get("ok") is not False


def _turn_measured_degrees(arguments: dict[str, Any], output: dict[str, Any]) -> float | None:
    measured = output.get("measured_degrees")
    if measured is None and _motion_succeeded(output):
        measured = arguments.get("degrees", arguments.get("relative_degrees"))
    if isinstance(measured, (int, float)):
        return measured
    return None


@dataclass
class GoalPose:
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0
    recent_actions: list[str] = field(default_factory=list)

    def record_motion(self, tool: str, arguments: dict[str, Any], output: dict[str, Any]) -> None:
        line = _action_log_line(tool, arguments, output)
        if line:
            self.recent_actions.append(line)
            if len(self.recent_actions) > MAX_RECENT_ACTIONS:
                self.recent_actions.pop(0)

        if tool in (TURN_TOOL_NAME, FACE_ME_TOOL_NAME):
            measured = _turn_measured_degrees(arguments, output)
            if measured is not None:
                self.heading += measured
            return

        if tool == MOVE_TOOL_NAME:
            traveled = output.get("traveled_m")
            if not isinstance(traveled, (int, float)):
                return
            heading_rad = math.radians(self.heading)
            self.x += traveled * math.cos(heading_rad)
            self.y += traveled * math.sin(heading_rad)


def _normalize_heading_degrees(heading: float) -> float:
    normalized = heading % 360.0
    if normalized > 180.0:
        normalized -= 360.0
    return normalized


def _format_axis_distance(value: float, positive_label: str, negative_label: str) -> str | None:
    if abs(value) < 0.005:
        return None
    label = positive_label if value > 0 else negative_label
    return f"{abs(value):.2f} meters {label}"


def _position_phrase(x: float, y: float) -> str:
    forward = _format_axis_distance(x, "forward", "back")
    lateral = _format_axis_distance(y, "left", "right")
    if forward and lateral:
        return f"{forward} and {lateral}"
    if forward:
        return forward
    if lateral:
        return lateral
    return "at your starting point"


def _facing_phrase(heading: float) -> str:
    normalized = _normalize_heading_degrees(heading)
    if abs(normalized) < 0.5:
        return "facing your starting heading"
    if normalized > 0:
        return f"facing {abs(normalized):.0f} degrees left of your starting heading"
    return f"facing {abs(normalized):.0f} degrees right of your starting heading"


def goal_pose_text(pose: GoalPose) -> str:
    position = _position_phrase(pose.x, pose.y)
    facing = _facing_phrase(pose.heading)
    text = f"Position: {position} of where you started this goal, {facing}."
    if pose.recent_actions:
        text = f"{text} Recent actions: {', '.join(pose.recent_actions)}."
    return text


def _blocked_side_label(blocked_by: str) -> str:
    reason = blocked_by.lower()
    if "right" in reason:
        return "right side"
    if "left" in reason:
        return "left side"
    if "center" in reason:
        return "center"
    return blocked_by


def _action_log_line(tool: str, arguments: dict[str, Any], output: dict[str, Any]) -> str | None:
    if tool == TURN_TOOL_NAME:
        commanded = arguments.get("degrees")
        measured = _turn_measured_degrees(arguments, output)
        if not isinstance(commanded, (int, float)) or measured is None:
            return None
        side = "left" if commanded > 0 else "right"
        return f"turned {abs(commanded):.0f} {side} (measured {abs(measured):.0f})"

    if tool == FACE_ME_TOOL_NAME:
        measured = _turn_measured_degrees(arguments, output)
        if measured is None:
            return None
        return f"faced you (measured {abs(measured):.0f})"

    if tool == MOVE_TOOL_NAME:
        commanded = arguments.get("distance_meters")
        if not isinstance(commanded, (int, float)):
            return None
        direction = "forward" if commanded > 0 else "backward"
        traveled = output.get("traveled_m")
        if output.get("error") == "safety_blocked" and isinstance(traveled, (int, float)):
            blocked_by = output.get("blocked_by")
            side = _blocked_side_label(blocked_by) if isinstance(blocked_by, str) else "blocked"
            return f"moved {abs(commanded):.1f} {direction} (blocked at {abs(traveled):.2f}, {side})"
        if isinstance(traveled, (int, float)):
            move_dir = "forward" if traveled >= 0 else "backward"
            return f"moved {abs(traveled):.2f} {move_dir}"
        return None

    return None


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
    caption = f"Camera view after {action}{' (blocked)' if blocked else ''}."
    if pose_line:
        caption = f"{caption} {pose_line}"
    return caption


async def _attach_goal_motion_observation(
    call: RobotToolCall,
    output: dict[str, Any],
    context: VoiceToolContext,
    pose_text: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    enriched = dict(output)
    if context.robot_inspection_caller is not None:
        try:
            snapshot = await asyncio.to_thread(context.robot_inspection_caller)
            surroundings = check_surroundings_snapshot(snapshot)
            if surroundings.get("ok"):
                enriched["surroundings"] = surroundings
        except Exception as exc:  # noqa: BLE001 -- telemetry transport failures vary
            log.warning("goal motion surroundings fetch failed: %s", exc)

    if enriched.get("error") == "safety_blocked":
        blocked_by = enriched.get("blocked_by")
        if isinstance(blocked_by, str):
            hint = safety_blocked_hint(blocked_by)
            if hint is not None:
                enriched["hint"] = hint

    image_parts = None
    if context.camera_snapshot_caller is not None:
        try:
            jpeg = await asyncio.to_thread(context.camera_snapshot_caller)
            data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
            image_parts = [
                {
                    "type": "input_text",
                    "text": motion_camera_caption(call.name, call.arguments, enriched, pose_text),
                },
                {"type": "input_image", "image_url": data_url},
            ]
        except Exception as exc:  # noqa: BLE001 -- camera HTTP failures vary
            log.warning("goal motion camera fetch failed: %s", exc)

    return enriched, image_parts


AGENT_SYSTEM_PROMPT = (CONFIG_DIR / "agent_system_prompt.md").read_text().strip()


def compose_agent_prompt(character_prose: str) -> str:
    """Build the goal runner's developer prompt: character voice, then the goal
    runner's operational guidance, then the shared robot principles."""
    blocks = [block for block in (character_prose.strip(), AGENT_SYSTEM_PROMPT, SHARED_ROBOT_GUIDANCE) if block]
    return "\n\n".join(blocks)


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", "") in ("output_text", "text"):
                parts.append(getattr(content, "text", ""))
    return "".join(parts)


def _function_calls(response: Any) -> list[Any]:
    return [item for item in (getattr(response, "output", None) or []) if getattr(item, "type", "") == "function_call"]


def _log_narration_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("progress narration task failed: %s", exc)


async def _model_response(
    openai_client: Any, openai_model: str, input_items: list[dict[str, Any]], previous_response_id: str | None
) -> Any:
    create_kwargs: dict[str, Any] = {
        "model": openai_model,
        "input": input_items,
        "reasoning": {"effort": AGENT_REASONING_EFFORT},
        "tools": AGENT_TOOLS,
        "parallel_tool_calls": False,
    }
    if previous_response_id:
        create_kwargs["previous_response_id"] = previous_response_id
    for attempt in range(2):
        try:
            return await openai_client.responses.create(**create_kwargs)
        except Exception as exc:  # noqa: BLE001 -- transient OpenAI transport failures
            if attempt:
                raise
            log.warning("agent model call failed; retrying: %s", exc)
            await asyncio.sleep(OPENAI_CREATE_RETRY_DELAY_SECS)


async def run_agent_goal(
    *,
    goal: str,
    stop_event: asyncio.Event,
    openai_client: Any,
    openai_model: str,
    voice_state: VoiceState,
    motion_intent_caller: Callable[..., Any] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
    speaker_direction_caller: Callable[[], dict[str, Any]] | None = None,
    speak_progress: Callable[[str], Awaitable[None]] | None = None,
    is_speaking: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    character_prose: str = "",
    preamble: str = "",
    max_steps: int = MAX_STEPS,
    max_seconds: float = MAX_SECONDS,
) -> str:
    context = VoiceToolContext(
        voice_state=voice_state,
        motion_intent_caller=motion_intent_caller,
        camera_snapshot_caller=camera_snapshot_caller,
        robot_inspection_caller=robot_inspection_caller,
        face_me_caller=face_me_caller,
        speaker_direction_caller=speaker_direction_caller,
    )

    # The one detached narration task. We hold the handle so every exit path can
    # cancel or await it; a fire-and-forget task that raises is otherwise swallowed.
    narration_task: asyncio.Task[None] | None = None

    def launch_narration(text: str) -> None:
        nonlocal narration_task
        if speak_progress is None or not text:
            return
        # One narration at a time. The in-flight handle is a synchronous guard, so two
        # narrations cannot race into flight before the first registers as speaking.
        if narration_task is not None and not narration_task.done():
            return
        if is_speaking is not None and is_speaking():
            return
        narration_task = asyncio.create_task(speak_progress(text))
        narration_task.add_done_callback(_log_narration_exception)

    async def settle_narration() -> None:
        # Let an in-flight narration finish so the final or next observation never
        # overlaps a half-spoken line. A TTS or transport failure here must not sink
        # the goal's final, so we log it and move on, same as the detached path.
        nonlocal narration_task
        task, narration_task = narration_task, None
        if task is not None and not task.done():
            with suppress(asyncio.CancelledError):
                try:
                    await task
                except Exception as exc:  # noqa: BLE001 -- narration failure must not sink the final
                    log.warning("progress narration task failed: %s", exc)

    async def cancel_narration() -> None:
        nonlocal narration_task
        task, narration_task = narration_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def speak_final(text: str) -> str:
        await settle_narration()
        if speak_progress is not None and text:
            await speak_progress(text)
        return text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    input_items: list[dict[str, Any]] = [
        {"role": "developer", "content": compose_agent_prompt(character_prose)},
        {"role": "user", "content": f"Goal: {goal}"},
    ]
    previous_response_id: str | None = None
    empty_responses = 0
    pose = GoalPose()

    # The acknowledgement the assistant spoke alongside start_goal plays here, riding
    # the same narration lifecycle so it overlaps the first model call and is
    # cancelled cleanly on exit instead of blocking the first tool.
    launch_narration(preamble)

    try:
        for step in range(1, max_steps + 1):
            if stop_event.is_set():
                return ""
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.info("goal timed out after step %d: %r", step - 1, goal)
                return await speak_final(TIMEOUT_FINAL)

            try:
                response = await asyncio.wait_for(
                    _model_response(openai_client, openai_model, input_items, previous_response_id), remaining
                )
            except asyncio.TimeoutError:
                log.info("goal timed out waiting on model at step %d: %r", step, goal)
                return await speak_final(TIMEOUT_FINAL)
            if stop_event.is_set():
                return ""

            previous_response_id = getattr(response, "id", None)
            function_calls = _function_calls(response)
            assistant_text = _response_text(response).strip()

            # No tool call: the model is done. Its text is the final answer.
            if not function_calls:
                if assistant_text:
                    log.info("goal finished (natural): %r", goal)
                    return await speak_final(assistant_text)
                empty_responses += 1
                if empty_responses > MAX_EMPTY_RESPONSES:
                    log.info("goal blocked on empty responses: %r", goal)
                    return await speak_final(BLOCKED_FINAL)
                input_items = [{"role": "user", "content": NUDGE_TEXT}]
                continue
            empty_responses = 0

            # Text alongside a tool call is progress narration only: speak it (if the
            # robot is free), never history, never the final.
            if assistant_text:
                launch_narration(assistant_text)

            # parallel_tool_calls=False means at most one call. If more ever appear,
            # run the first and tell the model only one tool runs per step.
            call = RobotToolCall(
                name=getattr(function_calls[0], "name", ""),
                arguments=parse_tool_arguments(getattr(function_calls[0], "arguments", "")),
                call_id=getattr(function_calls[0], "call_id", ""),
            )
            if loop.time() >= deadline:
                log.info("goal timed out before tool at step %d: %r", step, goal)
                return await speak_final(TIMEOUT_FINAL)
            started_at = time.monotonic()
            if on_event:
                on_event(
                    {
                        "type": "tool_start",
                        "t": started_at,
                        "source": "goal",
                        "tool_id": call.call_id,
                        "name": call.name,
                        "args": call.arguments,
                    }
                )
            result = await dispatch_tool(call, context)
            finished_at = time.monotonic()
            if on_event:
                on_event(
                    {
                        "type": "tool_done",
                        "t": finished_at,
                        "source": "goal",
                        "tool_id": call.call_id,
                        "name": call.name,
                        "args": call.arguments,
                        "started_at": started_at,
                        "ok": result.ok,
                        "error": result.output.get("error"),
                        "duration_ms": round((finished_at - started_at) * 1000),
                    }
                )
            if stop_event.is_set():
                return ""
            log.info("tool call %s args=%s ok=%s", call.name, call.arguments, result.ok)

            output = dict(result.output)
            image_parts = result.image_parts
            if call.name in GOAL_POSE_TOOLS:
                pose.record_motion(call.name, call.arguments, output)
            pose_text = goal_pose_text(pose)
            if call.name in GOAL_MOTION_OBSERVATION_TOOLS:
                output, motion_image = await _attach_goal_motion_observation(
                    call, output, context, pose_text
                )
                if motion_image is not None:
                    image_parts = motion_image

            input_items = [
                {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(output)}
            ]
            for extra in function_calls[1:]:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(extra, "call_id", ""),
                        "output": json.dumps({"ok": False, "error": "only one tool runs per step"}),
                    }
                )
            if image_parts:
                input_items.append({"role": "user", "content": image_parts})
            elif call.name in GOAL_MOTION_OBSERVATION_TOOLS:
                input_items.append({"role": "user", "content": pose_text})

        log.info("goal hit step limit (%d steps): %r", max_steps, goal)
        return await speak_final(STEP_LIMIT_FINAL)
    finally:
        await cancel_narration()
