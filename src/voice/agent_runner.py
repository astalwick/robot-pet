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
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from lib.log import setup_logging
from voice.assistant import (
    FACE_ME_TOOL,
    INSPECT_ROBOT_TOOL,
    LOOK_AROUND_TOOL,
    MOVE_FORWARD_TOOL,
    OPENAI_CREATE_RETRY_DELAY_SECS,
    TURN_TOOL,
    WIGGLE_TOOL,
    VoiceState,
)
from voice.tools import (
    INSPECT_SPEAKER_DIRECTION_TOOL,
    RobotToolCall,
    VoiceToolContext,
    dispatch_tool,
    parse_tool_arguments,
)


log = setup_logging("robot-voice")


AGENT_TOOLS = [
    MOVE_FORWARD_TOOL,
    TURN_TOOL,
    WIGGLE_TOOL,
    FACE_ME_TOOL,
    INSPECT_ROBOT_TOOL,
    LOOK_AROUND_TOOL,
    INSPECT_SPEAKER_DIRECTION_TOOL,
]

# The goal loop deliberates between physical actions, so it spends a little more
# thinking time than the low-latency assistant turn, which runs with no reasoning.
AGENT_REASONING_EFFORT = "low"

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


AGENT_SYSTEM_PROMPT = """You are a small physical robot pet working toward a goal over several steps. Your motion is real, timed, and happens in the world.

Use the tools to act and observe. Call one tool at a time. You get each tool's result back before you choose again.

After any motion (move_forward, turn, face_me), call inspect_robot or look_around to observe the result before deciding the goal is finished. Do not assume a move succeeded.

Prefer the motion stack's own signals: when deciding whether you are blocked or close to something, trust drive.safety_blocked and drive.safety_reason from inspect_robot over inventing a raw distance threshold.

A failed tool is an observation, not the end. Try a different tool or finish. Do not repeat the same failing action.

You may speak short progress updates as plain text while you work. Keep spoken text easy to say out loud: no symbols, lists, or markdown.

Finish by replying with a short final sentence and no tool call. Say what happened in plain words: that you reached the goal, or that you could not and why. Do not keep calling tools once you are done."""


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
        {"role": "developer", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Goal: {goal}"},
    ]
    previous_response_id: str | None = None
    empty_responses = 0

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
            result = await dispatch_tool(call, context)
            if stop_event.is_set():
                return ""
            log.info("tool call %s ok=%s", call.name, result.ok)

            input_items = [
                {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result.output)}
            ]
            for extra in function_calls[1:]:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(extra, "call_id", ""),
                        "output": json.dumps({"ok": False, "error": "only one tool runs per step"}),
                    }
                )
            if result.image_parts:
                input_items.append({"role": "user", "content": result.image_parts})

        log.info("goal hit step limit (%d steps): %r", max_steps, goal)
        return await speak_final(STEP_LIMIT_FINAL)
    finally:
        await cancel_narration()
