"""The iterative goal runner for the voice agent.

A normal assistant turn answers in one shot. Some requests instead need the robot
to work toward a goal over several steps: move, look, check, decide, repeat. When
the assistant calls `start_goal`, the voice orchestration loop hands the goal here
and `run_agent_goal` drives its own tool loop until it reaches a terminal state.

Each step asks the model for the next action as a small JSON object. The runner
executes the requested tools through the same shared dispatcher the assistant uses
(`voice/tools.py`), appends the results as real model input (text, or multimodal
image parts for camera snapshots), and loops. It stops only on a real terminal
condition: the model says done or blocked, the step or time budget runs out, or
the orchestrator sets the stop event.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from lib.log import setup_logging
from voice.assistant import (
    FACE_ME_TOOL,
    INSPECT_ROBOT_TOOL,
    LOOK_AROUND_TOOL,
    MOVE_FORWARD_TOOL,
    OPENAI_CREATE_RETRY_DELAY_SECS,
    WIGGLE_TOOL,
    VoiceState,
)
from voice.tools import (
    RobotToolCall,
    VoiceToolContext,
    agent_observation,
    dispatch_tool,
)


log = setup_logging("robot-voice")


AGENT_TOOLS = [
    MOVE_FORWARD_TOOL,
    WIGGLE_TOOL,
    FACE_ME_TOOL,
    INSPECT_ROBOT_TOOL,
    LOOK_AROUND_TOOL,
]
AGENT_TOOL_NAMES = frozenset(tool["name"] for tool in AGENT_TOOLS)

# The goal runner deliberates about the next action, so it spends a little more
# thinking time than the low-latency assistant turn, which runs with no reasoning.
AGENT_REASONING_EFFORT = "low"

# How many malformed model responses (bad JSON, a done/blocked with no final, or
# unknown tool names) we tolerate in a row before giving up as blocked.
MAX_DECISION_ERRORS = 3

TIMEOUT_FINAL = "I ran out of time working on that, so I stopped for now."
STEP_LIMIT_FINAL = "I tried a bunch of steps but could not finish that, so I stopped."
BLOCKED_FINAL = "I got stuck figuring out what to do next, so I stopped."


AGENT_TOOL_LINES = "\n".join(f"- {tool['name']}: {tool['description']}" for tool in AGENT_TOOLS)

AGENT_SYSTEM_PROMPT = f"""You are driving a small robot pet toward a goal over several steps.

Each step you choose the next action. Respond with ONLY a JSON object, no prose
around it, in this exact shape:

{{"narration": "short spoken update or empty", "tool_calls": [{{"name": "tool_name", "arguments": {{}}}}], "done": false, "blocked": false, "final": null}}

Rules:
- Use the tools to act and observe. You get each tool's result back as the next
  observation before you choose again.
- After moving, call inspect_robot to check sensors and drive state before deciding
  the goal is finished.
- When the goal is accomplished, set "done": true and put a short, friendly spoken
  sentence in "final".
- If a tool tells you plainly that you cannot continue, set "blocked": true and
  explain briefly in "final".
- "done" and "blocked" both require a non-empty "final".
- Keep "final" and "narration" easy to say out loud. No symbols, lists, or markdown.

Available tools:
{AGENT_TOOL_LINES}
"""


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


def _decode_decision(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _model_decision(openai_client: Any, openai_model: str, input_items: list[dict[str, Any]]) -> str:
    for attempt in range(2):
        try:
            response = await openai_client.responses.create(
                model=openai_model,
                input=input_items,
                reasoning={"effort": AGENT_REASONING_EFFORT},
            )
            break
        except Exception as exc:  # noqa: BLE001 -- transient OpenAI transport failures
            if attempt:
                raise
            log.warning("agent model call failed; retrying: %s", exc)
            await asyncio.sleep(OPENAI_CREATE_RETRY_DELAY_SECS)
    return _response_text(response)


async def run_agent_goal(
    *,
    goal: str,
    stop_event: asyncio.Event,
    openai_client: Any,
    openai_model: str,
    voice_state: VoiceState,
    motion_intent_caller: Callable[[str], Any] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
    speak_progress: Callable[[str], Awaitable[None]] | None = None,
    is_speaking: Callable[[], bool] | None = None,
    max_steps: int = 20,
    max_seconds: float = 120.0,
) -> str:
    context = VoiceToolContext(
        voice_state=voice_state,
        motion_intent_caller=motion_intent_caller,
        camera_snapshot_caller=camera_snapshot_caller,
        robot_inspection_caller=robot_inspection_caller,
        face_me_caller=face_me_caller,
    )

    async def narrate(text: str) -> None:
        if speak_progress is None or not text:
            return
        if is_speaking is not None and is_speaking():
            return
        await speak_progress(text)

    async def say_final(text: str) -> str:
        if speak_progress is not None and text:
            await speak_progress(text)
        return text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    input_items: list[dict[str, Any]] = [
        {"role": "developer", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Goal: {goal}"},
    ]
    error_count = 0

    for step in range(1, max_steps + 1):
        if stop_event.is_set():
            return ""
        remaining = deadline - loop.time()
        if remaining <= 0:
            log.info("goal timed out after step %d: %r", step - 1, goal)
            return await say_final(TIMEOUT_FINAL)

        input_items.append(
            {
                "role": "user",
                "content": f"Step {step} of {max_steps}. About {int(remaining)} seconds left. Respond with the action JSON.",
            }
        )
        try:
            decision_text = await asyncio.wait_for(
                _model_decision(openai_client, openai_model, input_items), remaining
            )
        except asyncio.TimeoutError:
            log.info("goal timed out waiting on model at step %d: %r", step, goal)
            return await say_final(TIMEOUT_FINAL)
        if stop_event.is_set():
            return ""
        input_items.append({"role": "assistant", "content": decision_text})

        decision = _decode_decision(decision_text)
        if decision is None:
            error_count += 1
            if error_count > MAX_DECISION_ERRORS:
                return await say_final(BLOCKED_FINAL)
            input_items.append(
                {"role": "user", "content": "That was not valid JSON. Respond with only the action JSON object."}
            )
            continue

        raw_final = decision.get("final")
        final = raw_final.strip() if isinstance(raw_final, str) else ""
        if decision.get("done") or decision.get("blocked"):
            if final:
                log.info("goal finished (%s): %r", "blocked" if decision.get("blocked") else "done", goal)
                return await say_final(final)
            error_count += 1
            if error_count > MAX_DECISION_ERRORS:
                return await say_final(BLOCKED_FINAL)
            input_items.append(
                {"role": "user", "content": "done or blocked requires a non-empty final spoken sentence. Provide final."}
            )
            continue

        tool_calls = decision.get("tool_calls") or []
        if not tool_calls:
            error_count = 0
            input_items.append(
                {"role": "user", "content": "No tool calls and not done. Call a tool or set done or blocked with a final."}
            )
            continue

        raw_narration = decision.get("narration")
        await narrate(raw_narration.strip() if isinstance(raw_narration, str) else "")

        observation_parts: list[dict[str, Any]] = []
        invalid_tool = False
        for raw_call in tool_calls:
            if stop_event.is_set():
                return ""
            # Don't start another physical tool once the budget is spent. We can't
            # claw back a motion intent already running in its thread, but we can
            # refuse to launch the next one.
            if loop.time() >= deadline:
                log.info("goal timed out before tool at step %d: %r", step, goal)
                return await say_final(TIMEOUT_FINAL)
            name = raw_call.get("name") if isinstance(raw_call, dict) else None
            if name not in AGENT_TOOL_NAMES:
                invalid_tool = True
                observation_parts.append(
                    {"type": "input_text", "text": json.dumps({"tool": name, "ok": False, "error": "unknown tool"})}
                )
                continue
            arguments = raw_call.get("arguments")
            call = RobotToolCall(
                name=name,
                arguments=arguments if isinstance(arguments, dict) else {},
                call_id=f"agent-{step}",
            )
            result = await dispatch_tool(call, context)
            if stop_event.is_set():
                return ""
            observation = agent_observation(result)
            observation_parts.append({"type": "input_text", "text": observation.text})
            if observation.input_parts:
                observation_parts.extend(observation.input_parts)

        if invalid_tool:
            error_count += 1
            if error_count > MAX_DECISION_ERRORS:
                return await say_final(BLOCKED_FINAL)
        else:
            error_count = 0
        input_items.append({"role": "user", "content": observation_parts})

    log.info("goal hit step limit (%d steps): %r", max_steps, goal)
    return await say_final(STEP_LIMIT_FINAL)
