from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.voice import DEFAULT_OPENAI_MODEL
from lib.log import setup_logging
from voice.conversation import ConversationHistory
from voice.turn_policy import DEFAULT_TURN_POLICY, TurnPolicy
from voice.tools import (
    ASSISTANT_TOOLS,
    FACE_ME_TOOL_NAME,
    MOTION_TOOL_NAMES,
    POST_MOTION_CAMERA_TOOLS,
    START_GOAL_TOOL_NAME,
    RobotToolCall,
    VoiceToolContext,
    attach_motion_observation,
    dispatch_tool,
    parse_tool_arguments,
)


log = setup_logging("robot-voice")


OPENAI_MODEL = DEFAULT_OPENAI_MODEL
DEFAULT_VOICE_ID = "Ct9jL3ofSaf3bjiuX3cL"
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
DEFAULT_OPERATIONAL_PROMPT_PATH = CONFIG_DIR / "operational_system_prompt.md"
OPERATIONAL_SYSTEM_PROMPT = DEFAULT_OPERATIONAL_PROMPT_PATH.read_text().strip()
# Shared robot principles appended to both the assistant prompt and the goal
# runner prompt, so the guidance lives in exactly one file and can't drift.
SHARED_ROBOT_GUIDANCE_PATH = CONFIG_DIR / "shared_robot_guidance.md"
SHARED_ROBOT_GUIDANCE = SHARED_ROBOT_GUIDANCE_PATH.read_text().strip()
PLAYBACK_RMS_STALE_SECS = 0.25
ASSISTANT_TURN_TIMEOUT_SECS = 120.0
OPENAI_CREATE_RETRY_DELAY_SECS = 0.2
BARGE_IN_TELEMETRY_INTERVAL_SECS = 0.35
END_SESSION_UTTERANCES = {
    "bye",
    "goodbye",
    "end session",
    "end the session",
    "end your session",
    "end your sessions",
    "please end session",
    "please end the session",
    "please end your session",
    "please end your sessions",
    "can you end session",
    "can you end the session",
    "can you end your session",
    "can you end your sessions",
    "can you please end session",
    "can you please end the session",
    "can you please end your session",
    "can you please end your sessions",
    "stop listening",
    "go back to sleep",
    "that is all",
    "thats all",
    "we are done",
    "were done",
}


def compose_system_prompt(character_prose: str) -> str:
    return f"{character_prose.strip()}\n\n{OPERATIONAL_SYSTEM_PROMPT}\n\n{SHARED_ROBOT_GUIDANCE}"


def is_end_session_request(text: str, policy: TurnPolicy = DEFAULT_TURN_POLICY) -> bool:
    return policy.normalized_transcript(text) in END_SESSION_UTTERANCES


@dataclass
class AudioLevels:
    mic_rms: int = 0
    mic_peak: int = 0
    playback_rms: int = 0
    playback_at: float = 0.0
    threshold_rms: int = 0
    gate_open: bool = False
    scribe_gate_open: bool = False
    gate_above_since: float | None = None


def effective_playback_rms(audio_levels: AudioLevels, now: float) -> int:
    if now - audio_levels.playback_at > PLAYBACK_RMS_STALE_SECS:
        return 0
    return audio_levels.playback_rms


def note_mic_chunk(audio_levels: AudioLevels, rms: int) -> None:
    if rms > audio_levels.mic_peak:
        audio_levels.mic_peak = rms


def refresh_barge_in_gate(
    audio_levels: AudioLevels,
    now: float,
    policy: TurnPolicy,
    assistant_speaking: bool,
    mic_rms: int,
) -> tuple[float | None, bool, int, str]:
    if assistant_speaking:
        gate_above_since, gate_open, threshold_rms, reason = update_near_end_gate(
            policy,
            audio_levels.gate_above_since,
            now,
            mic_rms,
        )
    else:
        gate_above_since = None
        gate_open = False
        reason = "assistant_not_speaking"
        threshold_rms = policy.barge_in_min_rms
    audio_levels.gate_above_since = gate_above_since
    audio_levels.threshold_rms = threshold_rms
    audio_levels.gate_open = gate_open
    return gate_above_since, gate_open, threshold_rms, reason


def update_near_end_gate(
    policy: TurnPolicy,
    gate_above_since: float | None,
    now: float,
    mic_rms: int,
) -> tuple[float | None, bool, int, str]:
    threshold_rms = policy.barge_in_min_rms
    if mic_rms < threshold_rms:
        return None, False, threshold_rms, "low_rms"

    if gate_above_since is None:
        gate_above_since = now

    sustained_ms = (now - gate_above_since) * 1000
    if sustained_ms >= policy.barge_in_sustain_ms:
        return gate_above_since, True, threshold_rms, "substantial_partial"
    return gate_above_since, False, threshold_rms, "not_sustained"


def barge_in_telemetry(
    policy: TurnPolicy,
    mic_rms: int,
    playback_rms: int,
    threshold_rms: int,
    gate_open: bool,
    last_reason: str,
) -> dict[str, object]:
    return {
        "barge_in_enabled": policy.barge_in_enabled,
        "barge_in_threshold_rms": threshold_rms,
        "barge_in_mic_rms": mic_rms,
        "barge_in_playback_rms": playback_rms,
        "barge_in_gate_open": gate_open,
        "barge_in_last_reason": last_reason,
    }


@dataclass(frozen=True)
class AgentGoalRequest:
    """The assistant turn decided this request needs the iterative goal runner.

    It bubbles out of the turn as the task result instead of spoken text, so the
    session can hand off to a goal instead of committing a normal exchange.
    """

    goal: str
    # Optional opening acknowledgement for callers that hand a goal directly to the
    # runner. The normal assistant stream speaks start_goal-adjacent text itself.
    preamble: str = ""


@dataclass
class ActiveGoal:
    """An iterative goal running outside the normal turn lifecycle.

    The goal runner drives its own tool loop with no speculative turns and no
    per-turn timeout. This is the orchestration state that lets a committed
    utterance cancel the goal and lets a terminal result commit one history
    exchange, using the original request as the user text.
    """

    goal: str
    user_text: str
    stop_event: asyncio.Event
    started_at: float
    task: asyncio.Task[str] | None = None
    final_text: str | None = None
    terminal_reason: str | None = None


@dataclass
class VoiceState:
    default_voice_id: str
    current_voice_id: str | None = None

    def __post_init__(self) -> None:
        if self.current_voice_id is None:
            self.current_voice_id = self.default_voice_id

    def set_voice(self, voice_id: str) -> None:
        # The next turn's TTS reads current_voice_id.
        self.default_voice_id = voice_id
        self.current_voice_id = voice_id


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@dataclass
class ActiveTurn:
    turn_id: int
    prompt: str
    speculative: bool
    task: asyncio.Task[str | AgentGoalRequest]
    playback_event: asyncio.Event = field(default_factory=asyncio.Event)
    speaking_event: asyncio.Event = field(default_factory=asyncio.Event)
    playback_release_task: asyncio.Task[None] | None = None
    speech_started_at: float | None = None
    assistant_streamed_chunks: list[str] = field(default_factory=list)
    committed_text: str | None = None
    assistant_text: str | None = None
    history_committed: bool = False
    delay_playback: bool = False
    playback_opened_at: float | None = None

    def __post_init__(self) -> None:
        if not self.speculative:
            self.committed_text = self.prompt
            if not self.delay_playback:
                self.open_playback()

    def is_active(self) -> bool:
        return not self.task.done()

    def is_speaking(self) -> bool:
        return self.speaking_event.is_set()

    def is_playing_back(self) -> bool:
        return self.is_active() and self.playback_event.is_set()

    def mark_speech_started(self, now: float) -> None:
        if self.speech_started_at is None:
            self.speech_started_at = now

    def speech_elapsed_secs(self, now: float) -> float | None:
        if self.speech_started_at is None:
            return None
        return now - self.speech_started_at

    def open_playback(self) -> None:
        if not self.playback_event.is_set():
            self.playback_opened_at = asyncio.get_running_loop().time()
        self.playback_event.set()

    def assistant_streamed_text(self) -> str:
        return "".join(self.assistant_streamed_chunks)

    async def confirm(self, commit_text: str) -> None:
        self.committed_text = commit_text
        self.prompt = commit_text
        self.speculative = False
        self.delay_playback = False
        self.open_playback()
        await cancel_task(self.playback_release_task)
        self.playback_release_task = None

    def request_cancel(self, reason: str) -> None:
        if self.playback_release_task and not self.playback_release_task.done():
            self.playback_release_task.cancel()
        self.playback_release_task = None
        self.playback_event.clear()
        self.speaking_event.clear()
        if not self.task.done():
            self.task.cancel()

    async def cancel(self, reason: str) -> None:
        playback_release_task = self.playback_release_task
        self.request_cancel(reason)
        await cancel_task(playback_release_task)
        await cancel_task(self.task)


@dataclass
class ProgressSpeaker:
    """Playback for a goal's progress narration and final result.

    Goal narration is spoken outside the normal turn, but barge-in still has to
    interrupt it the same way it interrupts an assistant turn. This mirrors the
    handful of ActiveTurn methods the barge-in checks read, so the same accessor
    can treat either source as the current interruptible playback.
    """

    text: str
    playback_event: asyncio.Event = field(default_factory=asyncio.Event)
    speaking_event: asyncio.Event = field(default_factory=asyncio.Event)
    speech_started_at: float | None = None

    def is_speaking(self) -> bool:
        return self.speaking_event.is_set()

    def is_playing_back(self) -> bool:
        return self.playback_event.is_set()

    def mark_speech_started(self, now: float) -> None:
        if self.speech_started_at is None:
            self.speech_started_at = now

    def speech_elapsed_secs(self, now: float) -> float | None:
        if self.speech_started_at is None:
            return None
        return now - self.speech_started_at

    def assistant_streamed_text(self) -> str:
        return self.text


@dataclass
class TurnRuntimeState:
    active_turn: ActiveTurn | None = None
    active_goal: ActiveGoal | None = None
    progress: ProgressSpeaker | None = None
    next_turn_id: int = 0
    debounce_task: asyncio.Task[None] | None = None
    last_local_speech_at: float = 0.0
    last_local_speech_rms: int = 0
    gate_open: bool = False
    gate_threshold_rms: int = 0
    gate_last_reason: str = "assistant_not_speaking"
    recent_barge_in_mic_rms: int = 0
    recent_barge_in_gate_open: bool = False
    recent_barge_in_scribe_gate_open: bool = False
    recent_barge_in_gate_reason: str = "assistant_not_speaking"
    recent_barge_in_audio_at: float = 0.0
    utterance_barge_in_mic_rms: int = 0
    utterance_barge_in_gate_open: bool = False
    utterance_barge_in_scribe_gate_open: bool = False
    utterance_barge_in_gate_reason: str = "assistant_not_speaking"
    utterance_barge_in_audio_at: float = 0.0
    local_audio_seq: int = 0
    last_partial_text: str = ""
    last_partial_audio_seq: int = -1
    barge_in_event_count: int = 0
    barge_in_hearing_reported: bool = False
    utterance_prefix: str = ""
    utterance_prefix_deadline: float = 0.0
    false_starts: int = 0


def reset_recent_barge_in_audio(state: TurnRuntimeState) -> None:
    state.recent_barge_in_mic_rms = 0
    state.recent_barge_in_gate_open = False
    state.recent_barge_in_scribe_gate_open = False
    state.recent_barge_in_gate_reason = "assistant_not_speaking"
    state.recent_barge_in_audio_at = 0.0


def reset_utterance_barge_in_audio(state: TurnRuntimeState) -> None:
    state.utterance_barge_in_mic_rms = 0
    state.utterance_barge_in_gate_open = False
    state.utterance_barge_in_scribe_gate_open = False
    state.utterance_barge_in_gate_reason = "assistant_not_speaking"
    state.utterance_barge_in_audio_at = 0.0


def note_utterance_barge_in_audio(
    state: TurnRuntimeState,
    now: float,
    *,
    scribe_gate_open: bool = False,
) -> None:
    if state.last_local_speech_rms > state.utterance_barge_in_mic_rms:
        state.utterance_barge_in_mic_rms = state.last_local_speech_rms
    if state.gate_open:
        state.utterance_barge_in_gate_open = True
    if scribe_gate_open:
        state.utterance_barge_in_scribe_gate_open = True
    state.utterance_barge_in_gate_reason = state.gate_last_reason
    state.utterance_barge_in_audio_at = now


def note_recent_barge_in_audio(
    state: TurnRuntimeState,
    now: float,
    policy: TurnPolicy,
    *,
    scribe_gate_open: bool = False,
) -> None:
    fresh = now - state.recent_barge_in_audio_at <= policy.local_speech_window_secs
    if not fresh or state.last_local_speech_rms > state.recent_barge_in_mic_rms:
        state.recent_barge_in_mic_rms = state.last_local_speech_rms
    if not fresh:
        state.recent_barge_in_gate_open = False
        state.recent_barge_in_scribe_gate_open = False
    if state.gate_open:
        state.recent_barge_in_gate_open = True
    if scribe_gate_open:
        state.recent_barge_in_scribe_gate_open = True
    state.recent_barge_in_gate_reason = state.gate_last_reason
    state.recent_barge_in_audio_at = now


@dataclass
class BargeInOutcome:
    accepted: bool
    reason: str
    mic_rms: int
    gate_open: bool
    playback_rms: int


def decide_barge_in_during_playback(
    text: str,
    now: float,
    active_turn: ActiveTurn | ProgressSpeaker,
    state: TurnRuntimeState,
    levels: AudioLevels,
    policy: TurnPolicy,
) -> BargeInOutcome:
    mic_rms = state.last_local_speech_rms
    gate_open = state.gate_open
    gate_reason = state.gate_last_reason
    scribe_gate_open = levels.scribe_gate_open
    if now - state.recent_barge_in_audio_at <= policy.local_speech_window_secs:
        mic_rms = state.recent_barge_in_mic_rms
        gate_open = state.recent_barge_in_gate_open
        gate_reason = state.recent_barge_in_gate_reason
        scribe_gate_open = state.recent_barge_in_scribe_gate_open
    elif state.utterance_barge_in_audio_at > 0:
        mic_rms = state.utterance_barge_in_mic_rms
        gate_open = state.utterance_barge_in_gate_open
        gate_reason = state.utterance_barge_in_gate_reason
        scribe_gate_open = state.utterance_barge_in_scribe_gate_open
    decision_mic_rms = None if scribe_gate_open and policy.has_explicit_interrupt(text) else mic_rms
    accepted, reason = policy.barge_in_decision(
        text,
        assistant_speaking=True,
        gate_open=gate_open,
        assistant_speech_elapsed_secs=active_turn.speech_elapsed_secs(now),
        mic_rms=decision_mic_rms,
        gate_reason=gate_reason,
        assistant_text=active_turn.assistant_streamed_text(),
    )
    return BargeInOutcome(
        accepted=accepted,
        reason=reason,
        mic_rms=mic_rms,
        gate_open=gate_open,
        playback_rms=effective_playback_rms(levels, now),
    )


async def stream_openai_words(
    openai_input: list[dict[str, Any]],
    openai_client: Any,
    voice_state: VoiceState,
    motion_intent_caller: Callable[..., Any] | None = None,
    end_session_pending: list[bool] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    usage: Any = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
    speaker_direction_caller: Callable[[], dict[str, Any]] | None = None,
    playback_event: asyncio.Event | None = None,
    openai_model: str = OPENAI_MODEL,
    on_event: Callable[[dict[str, object]], None] | None = None,
) -> AsyncIterator[str | AgentGoalRequest]:
    pending = ""
    response_input: object = openai_input
    previous_response_id: str | None = None
    text_streamed = False
    end_session_tool_output_sent = False
    tool_context = VoiceToolContext(
        voice_state=voice_state,
        motion_intent_caller=motion_intent_caller,
        camera_snapshot_caller=camera_snapshot_caller,
        robot_inspection_caller=robot_inspection_caller,
        face_me_caller=face_me_caller,
        speaker_direction_caller=speaker_direction_caller,
        end_session_pending=end_session_pending,
    )

    while True:
        create_kwargs: dict[str, Any] = {
            "model": openai_model,
            "input": response_input,
            "reasoning": {"effort": "none"},
            "tools": ASSISTANT_TOOLS,
            "stream": True,
        }
        if previous_response_id:
            create_kwargs["previous_response_id"] = previous_response_id

        for attempt in range(2):
            try:
                stream = await openai_client.responses.create(**create_kwargs)
                break
            except Exception as exc:
                if attempt:
                    raise
                log.warning("openai response create failed; retrying: %s", exc)
                await asyncio.sleep(OPENAI_CREATE_RETRY_DELAY_SECS)
        function_calls: list[Any] = []
        response_id = previous_response_id

        async for event in stream:
            event_type = getattr(event, "type", "")
            delta = getattr(event, "delta", "")

            if event_type in {"response.text_delta", "response.output_text.delta"}:
                if not delta:
                    continue

                pending += delta
                pieces = re.findall(r"\S+\s*", pending)

                if pending and not pending[-1].isspace() and pieces:
                    pending = pieces.pop()
                else:
                    pending = ""

                for piece in pieces:
                    text_streamed = True
                    yield piece
                continue

            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "function_call":
                    function_calls.append(item)
                continue

            if event_type == "response.completed":
                response = getattr(event, "response", None)
                response_id = getattr(response, "id", response_id)
                if usage is not None:
                    from voice.usage import record_openai_usage

                    record_openai_usage(usage, getattr(response, "usage", None), openai_model)

        if pending:
            text_streamed = True
            yield pending
            pending = ""

        if not function_calls:
            return

        tool_outputs: list[dict[str, str]] = []
        image_messages: list[dict[str, Any]] = []
        for function_call in function_calls:
            call = RobotToolCall(
                name=getattr(function_call, "name", ""),
                arguments=parse_tool_arguments(getattr(function_call, "arguments", "")),
                call_id=getattr(function_call, "call_id", ""),
            )
            log.info("tool call: %s args=%s", call.name, call.arguments)

            # A goal handoff ends this turn. Any text produced alongside the call
            # was already yielded into the normal TTS path.
            if call.name == START_GOAL_TOOL_NAME:
                log.info("goal handoff: %s", call.arguments.get("goal", ""))
                yield AgentGoalRequest(
                    goal=str(call.arguments.get("goal", "")).strip(),
                    preamble="",
                )
                return

            # Physical motion must not fire for a speculative turn that gets
            # discarded. Wait until this turn is actually speaking; if it's
            # cancelled first, this await is cancelled and the motion never
            # happens. The agent runner has no speculative turns, so this gating
            # lives here in the assistant path, not in the shared dispatcher.
            # stop is the exception: it must halt motion immediately, never wait
            # on playback.
            if call.name in MOTION_TOOL_NAMES or call.name == FACE_ME_TOOL_NAME:
                if playback_event is not None:
                    await playback_event.wait()

            started_at = time.monotonic()
            if on_event:
                on_event(
                    {
                        "type": "tool_start",
                        "t": started_at,
                        "source": "assistant",
                        "tool_id": call.call_id,
                        "name": call.name,
                        "args": call.arguments,
                    }
                )
            result = await dispatch_tool(call, tool_context)
            finished_at = time.monotonic()
            if on_event:
                on_event(
                    {
                        "type": "tool_done",
                        "t": finished_at,
                        "source": "assistant",
                        "tool_id": call.call_id,
                        "name": call.name,
                        "args": call.arguments,
                        "started_at": started_at,
                        "ok": result.ok,
                        "error": result.output.get("error"),
                        "duration_ms": round((finished_at - started_at) * 1000),
                    }
                )

            if call.name in POST_MOTION_CAMERA_TOOLS:
                result = await attach_motion_observation(call, result.output, tool_context)

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": json.dumps(result.output),
                }
            )
            if result.image_parts:
                image_messages.append({"role": "user", "content": result.image_parts})

            if result.ok:
                log.info("tool call %s args=%s ok", call.name, call.arguments)
            else:
                log.warning(
                    "tool call %s args=%s failed: %s",
                    call.name,
                    call.arguments,
                    result.output.get("error"),
                )

        if end_session_pending and end_session_pending[0] and (text_streamed or end_session_tool_output_sent):
            return

        if end_session_pending and end_session_pending[0]:
            end_session_tool_output_sent = True

        previous_response_id = response_id
        response_input = [*tool_outputs, *image_messages]


async def run_assistant_turn(
    turn_id: int,
    openai_input: list[dict[str, Any]],
    playback_event: asyncio.Event,
    speaking_event: asyncio.Event,
    openai_client: Any,
    elevenlabs_api_key: str,
    voice_state: VoiceState,
    on_assistant_chunk: Callable[[str], None] | None = None,
    tts_speaker: Callable[..., Any] | None = None,
    motion_intent_caller: Callable[..., Any] | None = None,
    session_end_caller: Callable[[], Any] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    usage: Any = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
    speaker_direction_caller: Callable[[], dict[str, Any]] | None = None,
    openai_model: str = OPENAI_MODEL,
    on_event: Callable[[dict[str, object]], None] | None = None,
) -> str | AgentGoalRequest:
    from voice.elevenlabs_io import speak_with_eleven_flash

    assistant_chunks: list[str] = []
    end_session_pending = [False]
    goal_request: AgentGoalRequest | None = None

    words = stream_openai_words(
        openai_input,
        openai_client,
        voice_state,
        motion_intent_caller,
        end_session_pending,
        camera_snapshot_caller,
        usage,
        robot_inspection_caller,
        face_me_caller,
        speaker_direction_caller,
        playback_event,
        openai_model,
        on_event,
    )

    async def captured_openai_words() -> AsyncIterator[str]:
        nonlocal goal_request
        async for chunk in words:
            # A goal handoff is a control signal, not speech — keep it out of the
            # speaker so nothing is spoken, and return it as the turn result.
            if isinstance(chunk, AgentGoalRequest):
                goal_request = chunk
                continue
            if isinstance(chunk, str):
                assistant_chunks.append(chunk)
                if on_assistant_chunk:
                    on_assistant_chunk(chunk)
            yield chunk

    speaker = tts_speaker or speak_with_eleven_flash
    await speaker(
        captured_openai_words(),
        elevenlabs_api_key,
        voice_state.current_voice_id,
        playback_event,
        speaking_event,
    )
    if end_session_pending[0] and session_end_caller is not None:
        session_end_caller()
    if goal_request is not None:
        return goal_request
    return "".join(assistant_chunks).strip()


async def _speak_progress_default(
    text_chunks: AsyncIterator[str],
    elevenlabs_api_key: str,
    voice_id: str,
    playback_event: asyncio.Event,
    speaking_event: asyncio.Event,
) -> None:
    from voice.elevenlabs_io import speak_with_eleven_flash

    await speak_with_eleven_flash(text_chunks, elevenlabs_api_key, voice_id, playback_event, speaking_event)


class TurnOrchestrator:
    """Owns one voice session's turn state and consumes its scribe-event queue."""

    def __init__(
        self,
        scribe_events: asyncio.Queue[dict[str, object]],
        openai_client: Any,
        elevenlabs_api_key: str,
        voice_state: VoiceState,
        stop_event: asyncio.Event,
        system_prompt: str | Callable[[], str],
        policy: TurnPolicy = DEFAULT_TURN_POLICY,
        audio_levels: AudioLevels | None = None,
        conversation_history: ConversationHistory | None = None,
        on_status: Callable[[dict[str, object]], None] | None = None,
        on_event: Callable[[dict[str, object]], None] | None = None,
        assistant_runner: Callable[..., Any] = run_assistant_turn,
        goal_runner: Callable[..., Any] | None = None,
        motion_intent_caller: Callable[..., Any] | None = None,
        session_end_caller: Callable[[], Any] | None = None,
        camera_snapshot_caller: Callable[[], bytes] | None = None,
        stop_playback_now: Callable[[], Any] | None = None,
        robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
        face_me_caller: Callable[[], dict[str, Any]] | None = None,
        speaker_direction_caller: Callable[[], dict[str, Any]] | None = None,
        progress_speaker: Callable[..., Any] | None = None,
        character_prose: str | Callable[[], str] | None = None,
        openai_model: str = OPENAI_MODEL,
    ) -> None:
        self.scribe_events = scribe_events
        self.openai_client = openai_client
        self.elevenlabs_api_key = elevenlabs_api_key
        self.voice_state = voice_state
        self.stop_event = stop_event
        self.system_prompt = system_prompt
        self.policy = policy
        self.on_status = on_status
        self.on_event = on_event
        self.assistant_runner = assistant_runner
        self.goal_runner = goal_runner
        self.motion_intent_caller = motion_intent_caller
        self.session_end_caller = session_end_caller
        self.camera_snapshot_caller = camera_snapshot_caller
        self.stop_playback_now = stop_playback_now
        self.robot_inspection_caller = robot_inspection_caller
        self.face_me_caller = face_me_caller
        self.speaker_direction_caller = speaker_direction_caller
        self.progress_speaker = progress_speaker
        self.character_prose = character_prose
        self.openai_model = openai_model
        self.state = TurnRuntimeState(gate_threshold_rms=policy.barge_in_min_rms)
        self.history = conversation_history if conversation_history is not None else ConversationHistory()
        self.levels = audio_levels if audio_levels is not None else AudioLevels()
        self.recent_assistant_text = ""
        self.recent_assistant_echo_until = 0.0
        self.barge_in_telemetry_published_at: float | None = None
        self.hearing_on = False
        self.thinking_on = False
        self.user_speech_on = False

    def current_system_prompt(self) -> str:
        return self.system_prompt() if callable(self.system_prompt) else self.system_prompt

    def current_character_prose(self) -> str:
        if self.character_prose is None:
            return ""
        return self.character_prose() if callable(self.character_prose) else self.character_prose

    def note_user_speech(self) -> None:
        if self.user_speech_on:
            return
        self.user_speech_on = True
        self.emit("phase", name="user_speech", on=True)

    def end_user_speech(self) -> None:
        if not self.user_speech_on:
            return
        self.user_speech_on = False
        self.emit("phase", name="user_speech", on=False)

    def status(self, **values: object) -> None:
        new_status = values.get("status")
        if (
            "assistant_working" not in values
            and self.state.active_turn is None
            and self.state.active_goal is None
            and self.state.progress is None
        ):
            values["assistant_working"] = False
        if self.on_status:
            self.on_status(values)
        if not self.on_event or "status" not in values:
            return
        if new_status not in {"hearing", "thinking", "listening"}:
            return
        new_hearing = new_status == "hearing"
        new_thinking = new_status == "thinking"
        if new_hearing != self.hearing_on:
            self.hearing_on = new_hearing
            self.emit("phase", name="hearing", on=self.hearing_on)
        if new_thinking != self.thinking_on:
            self.thinking_on = new_thinking
            self.emit("phase", name="thinking", on=self.thinking_on)

    def emit(self, kind: str, **payload: object) -> None:
        if not self.on_event:
            return
        event = {"type": kind, "t": time.monotonic(), **payload}
        self.on_event(event)

    def publish_barge_in_state(self, now: float, mic_rms: int | None = None, *, force: bool = False) -> None:
        mic = self.state.last_local_speech_rms if mic_rms is None else mic_rms
        assistant_speaking = bool(self.current_playback())
        _, self.state.gate_open, self.state.gate_threshold_rms, self.state.gate_last_reason = refresh_barge_in_gate(
            self.levels,
            now,
            self.policy,
            assistant_speaking,
            mic,
        )
        if (
            not force
            and self.barge_in_telemetry_published_at is not None
            and now - self.barge_in_telemetry_published_at < BARGE_IN_TELEMETRY_INTERVAL_SECS
        ):
            return
        self.barge_in_telemetry_published_at = now
        playback_rms = effective_playback_rms(self.levels, now)
        self.status(
            **barge_in_telemetry(
                self.policy,
                mic,
                playback_rms,
                self.state.gate_threshold_rms,
                self.state.gate_open,
                self.state.gate_last_reason,
            )
        )

    def report_barge_in(self, source: str, outcome: BargeInOutcome) -> None:
        self.state.gate_last_reason = outcome.reason
        self.emit(
            "barge_in_considered",
            source=source,
            accepted=outcome.accepted,
            reason=outcome.reason,
            mic=outcome.mic_rms,
            playback=outcome.playback_rms,
            threshold=self.policy.barge_in_min_rms,
        )
        self.status(
            **barge_in_telemetry(
                self.policy,
                outcome.mic_rms,
                outcome.playback_rms,
                self.policy.barge_in_min_rms,
                outcome.gate_open,
                outcome.reason,
            )
        )

    def publish_barge_in_event(self, source: str, reason: str) -> None:
        self.state.barge_in_event_count += 1
        self.status(
            barge_in_event_count=self.state.barge_in_event_count,
            barge_in_last_event=f"{source}: {reason}",
        )
        self.emit("barge_in_fired", source=source, reason=reason)

    def publish_barge_in_hearing(self, source: str) -> None:
        if self.state.barge_in_hearing_reported:
            return
        self.state.barge_in_hearing_reported = True
        self.publish_barge_in_event(source, "hearing")

    def trigger_stop_playback_now(self) -> None:
        if not self.stop_playback_now:
            return
        try:
            result = self.stop_playback_now()
        except Exception:
            log.exception("stop playback failed")
            return
        if asyncio.iscoroutine(result):
            task = asyncio.create_task(result)
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def cancel_active_turn(self, reason: str) -> None:
        turn = self.state.active_turn
        self.state.active_turn = None
        if turn and (turn.is_active() or (turn.playback_release_task and not turn.playback_release_task.done())):
            was_speaking = turn.is_speaking()
            self.emit("turn_cancel", turn_id=turn.turn_id, reason=reason, was_speaking=was_speaking)
            if reason == "continuation_retraction" and was_speaking:
                self.state.false_starts += 1
                self.status(false_starts=self.state.false_starts)
            streamed = turn.assistant_streamed_text().strip()
            if streamed:
                self.emit("assistant", turn_id=turn.turn_id, text=streamed, cancelled=True)
            # If the turn actually reached the speaker, record what it said so
            # history reflects reality — even on barge-in/cancel. Must read
            # playback_event before turn.cancel() clears it.
            if streamed and turn.playback_event.is_set() and not turn.history_committed:
                turn.assistant_text = streamed
                self.recent_assistant_text = streamed
                self.recent_assistant_echo_until = asyncio.get_running_loop().time() + self.policy.assistant_echo_memory_secs
                if reason != "continuation_retraction":
                    self.history.append_exchange(turn.committed_text or turn.prompt, streamed)
                turn.history_committed = True
            if self.stop_playback_now and turn.is_playing_back():
                self.trigger_stop_playback_now()
            await turn.cancel(reason)

    async def cancel_unconfirmed_speculation(self, turn: ActiveTurn) -> None:
        await asyncio.sleep(self.policy.speculative_no_commit_timeout_secs)
        if self.state.active_turn is turn and turn.speculative and not turn.playback_event.is_set():
            turn.playback_release_task = None
            await self.cancel_active_turn("no_commit")
            self.status(status="listening", assistant_working=False, partial_transcript=None)

    async def release_committed_playback(self, turn: ActiveTurn) -> None:
        await asyncio.sleep(self.policy.commit_playback_delay_secs)
        while self.state.active_turn is turn and not turn.playback_event.is_set():
            quiet_remaining_secs = self.policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), self.state.last_local_speech_at)
            if quiet_remaining_secs <= 0:
                turn.open_playback()
                await self.maybe_commit_history(turn)
                return
            await asyncio.sleep(quiet_remaining_secs)

    async def maybe_commit_history(self, turn: ActiveTurn) -> None:
        # A speculative turn that actually spoke (playback opened) still belongs
        # in history even if no commit ever confirmed it — we record it once the
        # turn finishes, using its triggering partial as the user text.
        if (
            self.state.active_turn is not turn
            or turn.history_committed
            or not turn.playback_event.is_set()
            or not turn.task.done()
        ):
            return
        try:
            assistant_text = turn.task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("assistant turn failed: %s", exc)
            self.status(status="error", assistant_working=False, last_error=str(exc))
            return
        if isinstance(assistant_text, AgentGoalRequest):
            # A goal handoff is recorded only when the goal finishes, not here.
            return
        turn.assistant_text = assistant_text
        self.recent_assistant_text = assistant_text
        self.recent_assistant_echo_until = asyncio.get_running_loop().time() + self.policy.assistant_echo_memory_secs
        self.history.append_exchange(turn.committed_text or turn.prompt, assistant_text)
        turn.history_committed = True
        if assistant_text:
            self.emit("assistant", turn_id=turn.turn_id, text=assistant_text)
        self.status(status="listening", assistant_speaking=False, assistant_working=False, last_assistant_text=assistant_text)

    def completed_goal_request(self, turn: ActiveTurn) -> AgentGoalRequest | None:
        if not turn.task.done():
            return None
        try:
            result = turn.task.result()
        except (asyncio.CancelledError, Exception):
            return None
        return result if isinstance(result, AgentGoalRequest) else None

    async def maybe_finish_silent_turn(self, turn: ActiveTurn) -> None:
        if (
            self.state.active_turn is not turn
            or turn.history_committed
            or turn.playback_event.is_set()
            or not turn.task.done()
        ):
            return
        try:
            assistant_text = turn.task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.state.active_turn = None
            await cancel_task(turn.playback_release_task)
            turn.playback_release_task = None
            log.exception("assistant turn failed: %s", exc)
            self.status(status="error", assistant_working=False, last_error=str(exc))
            return
        if isinstance(assistant_text, AgentGoalRequest) or assistant_text.strip():
            return
        self.state.active_turn = None
        await cancel_task(turn.playback_release_task)
        turn.playback_release_task = None
        self.status(status="listening", assistant_speaking=False, assistant_working=False)

    async def cancel_active_goal(self, reason: str) -> None:
        goal = self.state.active_goal
        self.state.active_goal = None
        if goal is None:
            return
        goal.terminal_reason = "cancelled"
        goal.stop_event.set()
        self.emit("goal_cancel", goal=goal.goal, reason=reason)
        log.info("goal cancelled: reason=%s goal=%r", reason, goal.goal)
        await cancel_task(goal.task)

    def current_playback(self) -> ActiveTurn | ProgressSpeaker | None:
        if self.state.active_turn and self.state.active_turn.is_playing_back():
            return self.state.active_turn
        if self.state.progress and self.state.progress.is_playing_back():
            return self.state.progress
        return None

    async def cancel_current_playback(self, reason: str) -> None:
        if self.state.progress and self.state.progress.is_playing_back():
            await self.cancel_active_goal(reason)
        else:
            await self.cancel_active_turn(reason)

    async def speak_progress(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        progress = ProgressSpeaker(text=text)
        progress.playback_event.set()
        progress.speaking_event.set()
        self.state.progress = progress
        self.emit("goal_speech", text=text)
        self.status(status="speaking", assistant_speaking=True, assistant_working=True)

        async def text_stream() -> AsyncIterator[str]:
            yield text

        speaker = self.progress_speaker or _speak_progress_default
        try:
            await speaker(
                text_stream(),
                self.elevenlabs_api_key,
                self.voice_state.current_voice_id,
                progress.playback_event,
                progress.speaking_event,
            )
        except asyncio.CancelledError:
            self.trigger_stop_playback_now()
            raise
        finally:
            if self.state.progress is progress:
                self.state.progress = None
            progress.playback_event.clear()
            progress.speaking_event.clear()

        # Narration played to completion (a cancel re-raises above and skips this).
        # Record what we just said so a delayed STT echo of this line is suppressed
        # even after state.progress clears and the goal keeps working.
        self.recent_assistant_text = text
        self.recent_assistant_echo_until = asyncio.get_running_loop().time() + self.policy.assistant_echo_memory_secs

        # The narration ended but the goal keeps working. The session speaker flips
        # status back to listening when speech stops, so restore thinking here or the
        # idle timer would treat an active goal as an idle session.
        if self.state.active_goal is not None:
            self.status(status="thinking", assistant_speaking=False, assistant_working=True)

    async def begin_goal_handoff(self, turn: ActiveTurn, goal_request: AgentGoalRequest) -> None:
        # The normal turn is finished and is handing off to an iterative goal. Drop
        # it as the active turn so it is not treated as a speaking turn, and surface
        # the handoff. We do not commit history here — the goal records its own
        # result when it reaches a terminal state.
        streamed = turn.assistant_streamed_text().strip()
        if streamed:
            self.recent_assistant_text = streamed
            self.recent_assistant_echo_until = asyncio.get_running_loop().time() + self.policy.assistant_echo_memory_secs
        if self.state.active_turn is turn:
            self.state.active_turn = None
        self.emit("goal_handoff", turn_id=turn.turn_id, goal=goal_request.goal)
        log.info("goal handoff received: %s", goal_request.goal)
        if self.goal_runner is None:
            self.status(status="listening", assistant_speaking=False, assistant_working=False)
            return
        await self.cancel_active_goal("superseded")
        goal = ActiveGoal(
            goal=goal_request.goal,
            user_text=turn.committed_text or turn.prompt,
            stop_event=asyncio.Event(),
            started_at=asyncio.get_running_loop().time(),
        )

        async def run_goal() -> str:
            return await self.goal_runner(
                goal=goal.goal,
                stop_event=goal.stop_event,
                openai_client=self.openai_client,
                openai_model=self.openai_model,
                voice_state=self.voice_state,
                motion_intent_caller=self.motion_intent_caller,
                camera_snapshot_caller=self.camera_snapshot_caller,
                robot_inspection_caller=self.robot_inspection_caller,
                face_me_caller=self.face_me_caller,
                speaker_direction_caller=self.speaker_direction_caller,
                speak_progress=self.speak_progress,
                is_speaking=lambda: self.current_playback() is not None,
                on_event=self.on_event,
                character_prose=self.current_character_prose(),
                preamble=goal_request.preamble,
            )

        goal.task = asyncio.create_task(run_goal())
        goal.task.add_done_callback(
            lambda _done, g=goal: self.scribe_events.put_nowait({"type": "goal_task_done", "goal": g})
        )
        self.state.active_goal = goal
        self.emit("goal_start", goal=goal.goal)
        self.status(status="thinking", assistant_working=True)

    async def finish_goal(self, goal: ActiveGoal) -> None:
        if self.state.active_goal is not goal or goal.task is None:
            return
        self.state.active_goal = None
        try:
            final_text = goal.task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("goal task failed: %s", exc)
            self.status(status="error", assistant_working=False, last_error=str(exc))
            return
        final_text = (final_text or "").strip()
        goal.final_text = final_text
        goal.terminal_reason = "done"
        if final_text:
            self.history.append_exchange(goal.user_text, final_text)
            self.recent_assistant_text = final_text
            self.recent_assistant_echo_until = asyncio.get_running_loop().time() + self.policy.assistant_echo_memory_secs
            self.emit("assistant", text=final_text)
        self.emit("goal_done", goal=goal.goal, text=final_text)
        self.status(status="listening", assistant_speaking=False, assistant_working=False, last_assistant_text=final_text)

    async def start_turn(self, prompt: str, speculative: bool) -> None:
        await self.cancel_active_turn("new_turn")
        self.state.barge_in_hearing_reported = False
        reset_recent_barge_in_audio(self.state)
        reset_utterance_barge_in_audio(self.state)
        self.state.next_turn_id += 1
        new_turn_id = self.state.next_turn_id
        playback_event = asyncio.Event()
        speaking_event = asyncio.Event()
        assistant_streamed_chunks: list[str] = []
        first_token_emitted = False

        def on_assistant_chunk(chunk: str) -> None:
            nonlocal first_token_emitted
            assistant_streamed_chunks.append(chunk)
            if not first_token_emitted:
                first_token_emitted = True
                self.emit("turn_first_token", turn_id=new_turn_id)

        openai_input = self.history.input_for(prompt, self.current_system_prompt())

        async def run_turn() -> str:
            return await asyncio.wait_for(
                self.assistant_runner(
                    new_turn_id,
                    openai_input,
                    playback_event,
                    speaking_event,
                    self.openai_client,
                    self.elevenlabs_api_key,
                    self.voice_state,
                    on_assistant_chunk=on_assistant_chunk,
                    motion_intent_caller=self.motion_intent_caller,
                    session_end_caller=self.session_end_caller,
                    camera_snapshot_caller=self.camera_snapshot_caller,
                    robot_inspection_caller=self.robot_inspection_caller,
                    face_me_caller=self.face_me_caller,
                    speaker_direction_caller=self.speaker_direction_caller,
                    openai_model=self.openai_model,
                    on_event=self.on_event,
                ),
                timeout=ASSISTANT_TURN_TIMEOUT_SECS,
            )

        task = asyncio.create_task(run_turn())
        turn = ActiveTurn(
            turn_id=new_turn_id,
            prompt=prompt,
            speculative=speculative,
            task=task,
            playback_event=playback_event,
            speaking_event=speaking_event,
            assistant_streamed_chunks=assistant_streamed_chunks,
            delay_playback=not speculative,
        )
        task.add_done_callback(lambda _task, completed_turn=turn: self.scribe_events.put_nowait({"type": "assistant_done", "turn": completed_turn}))
        self.state.active_turn = turn
        self.emit("turn_start", turn_id=new_turn_id, speculative=speculative, prompt=prompt)
        if not speculative:
            self.emit("turn_committed", turn_id=new_turn_id, from_speculative=False)
        self.status(status="thinking", assistant_speaking=False, assistant_working=True)
        if speculative:
            turn.playback_release_task = asyncio.create_task(self.cancel_unconfirmed_speculation(turn))
        else:
            turn.playback_release_task = asyncio.create_task(self.release_committed_playback(turn))

    async def start_after_stable_partial(self, text: str) -> None:
        await asyncio.sleep(self.policy.speculative_partial_delay_secs)
        while True:
            should_start, _reason = self.policy.speculation_decision(text)
            if not should_start:
                return
            quiet_remaining_secs = self.policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), self.state.last_local_speech_at)
            if quiet_remaining_secs > 0:
                await asyncio.sleep(quiet_remaining_secs)
                continue
            if self.state.active_turn and self.policy.transcript_matches(text, self.state.active_turn.prompt):
                return
            await self.start_turn(text, speculative=True)
            return

    def consider_playback_barge_in(
        self,
        source: str,
        text: str,
        now: float,
        playback: ActiveTurn | ProgressSpeaker,
    ) -> BargeInOutcome:
        playback.mark_speech_started(now)
        self.publish_barge_in_state(now, force=True)
        outcome = decide_barge_in_during_playback(text, now, playback, self.state, self.levels, self.policy)
        self.report_barge_in(source, outcome)
        return outcome

    def continuation_retraction_eligible(self, turn: ActiveTurn, text: str, now: float) -> bool:
        return (
            not self.policy.has_explicit_interrupt(text)
            and turn.playback_opened_at is not None
            and now - turn.playback_opened_at <= self.policy.continuation_grace_secs
            and len(re.findall(r"\S+", text)) >= self.policy.continuation_min_words
        )

    async def retract_continuation(self, turn: ActiveTurn, fragment_text: str, now: float) -> None:
        first_half = turn.committed_text or turn.prompt
        self.state.utterance_prefix = f"{self.state.utterance_prefix} {first_half}".strip()
        self.state.utterance_prefix_deadline = now + 10.0
        self.emit("false_start", turn_id=turn.turn_id, text=fragment_text)
        await self.cancel_active_turn("continuation_retraction")
        self.status(status="hearing", partial_transcript=fragment_text)
        await cancel_task(self.state.debounce_task)
        self.state.debounce_task = asyncio.create_task(
            self.start_after_stable_partial(f"{self.state.utterance_prefix} {fragment_text}")
        )

    async def handle_partial(self, text: str) -> None:
        now = asyncio.get_running_loop().time()
        normalized_partial = self.policy.normalized_transcript(text)
        if (
            normalized_partial
            and normalized_partial == self.state.last_partial_text
            and self.state.local_audio_seq == self.state.last_partial_audio_seq
        ):
            return
        self.state.last_partial_text = normalized_partial
        self.state.last_partial_audio_seq = self.state.local_audio_seq
        self.emit("partial", text=text)
        self.note_user_speech()
        if self.is_recent_assistant_echo(text, now):
            await cancel_task(self.state.debounce_task)
            self.state.debounce_task = None
            self.emit("echo_suppressed", source="partial", text=text)
            self.status(status="listening")
            return

        active_turn = self.state.active_turn
        playback = self.current_playback()
        if playback:
            if (
                playback is self.state.active_turn
                and self.continuation_retraction_eligible(playback, text, now)
            ):
                await self.retract_continuation(playback, text, now)
                return
            outcome = self.consider_playback_barge_in("partial", text, now, playback)
            if outcome.accepted:
                self.publish_barge_in_hearing("stt")
                self.status(status="hearing", partial_transcript=text)
                self.publish_barge_in_event("partial", outcome.reason)
                await self.cancel_current_playback("barge_in")
                await cancel_task(self.state.debounce_task)
                self.state.debounce_task = None
                if outcome.reason == "explicit_interrupt":
                    self.state.utterance_prefix = ""
                    self.status(status="listening", partial_transcript=None)
                else:
                    self.state.debounce_task = asyncio.create_task(self.start_after_stable_partial(text))
            else:
                self.emit("barge_in_rejected", source="partial", reason=outcome.reason, text=text)
                self.status(status="speaking", assistant_speaking=True, partial_transcript=None)
            return

        if self.policy.has_explicit_interrupt(text):
            self.state.utterance_prefix = ""
        elif self.state.utterance_prefix and now < self.state.utterance_prefix_deadline:
            text = f"{self.state.utterance_prefix} {text}"
        elif self.state.utterance_prefix:
            self.state.utterance_prefix = ""

        self.status(status="hearing", partial_transcript=text)

        if active_turn and active_turn.speculative and text != active_turn.prompt and self.policy.transcript_matches(text, active_turn.prompt):
            if self.policy.should_replace_speculative_prompt(text, active_turn.prompt):
                await self.start_turn(text, speculative=True)
            return

        if (
            active_turn
            and not active_turn.speculative
            and not active_turn.playback_event.is_set()
            and not active_turn.is_speaking()
            and self.policy.normalized_transcript(text) != self.policy.normalized_transcript(active_turn.prompt)
        ):
            first_half = active_turn.committed_text or active_turn.prompt
            self.state.utterance_prefix = f"{self.state.utterance_prefix} {first_half}".strip()
            self.state.utterance_prefix_deadline = now + 10.0
            await self.cancel_active_turn("commit_continuation")
            await cancel_task(self.state.debounce_task)
            self.state.debounce_task = asyncio.create_task(
                self.start_after_stable_partial(f"{self.state.utterance_prefix} {text}")
            )
            return

        await cancel_task(self.state.debounce_task)
        self.state.debounce_task = asyncio.create_task(self.start_after_stable_partial(text))

    async def handle_commit(self, text: str) -> None:
        await cancel_task(self.state.debounce_task)
        self.state.debounce_task = None
        now = asyncio.get_running_loop().time()
        self.emit("commit", text=text)
        self.end_user_speech()
        try:
            if is_end_session_request(text, self.policy):
                if self.state.active_goal is not None:
                    await self.cancel_active_goal("committed_speech")
                self.state.utterance_prefix = ""
                self.status(status="listening", partial_transcript=None, last_committed_transcript=text)
                if self.session_end_caller:
                    self.session_end_caller()
                return
            if self.is_recent_assistant_echo(text, now):
                self.emit("echo_suppressed", source="commit", text=text)
                self.status(status="listening")
                return

            raw_commit = text

            if self.policy.has_explicit_interrupt(text):
                self.state.utterance_prefix = ""
            elif self.state.utterance_prefix and now < self.state.utterance_prefix_deadline:
                text = f"{self.state.utterance_prefix} {text}"
                self.state.utterance_prefix = ""
            elif self.state.utterance_prefix:
                self.state.utterance_prefix = ""

            should_start_from_commit, commit_reason = self.policy.commit_decision(text)
            self.emit("commit_decision", accepted=should_start_from_commit, reason=commit_reason, text=text)

            if self.state.active_goal is not None:
                await self.cancel_active_goal("committed_speech")
                if should_start_from_commit and not self.policy.has_explicit_interrupt(text):
                    self.status(status="thinking", partial_transcript=None, last_committed_transcript=text)
                    await self.start_turn(text, speculative=False)
                else:
                    self.status(status="listening", partial_transcript=None, last_committed_transcript=text)
                return

            active_turn = self.state.active_turn
            if (
                active_turn
                and active_turn.is_playing_back()
                and not self.policy.has_explicit_interrupt(raw_commit)
                and self.continuation_retraction_eligible(active_turn, text, now)
            ):
                first_half = active_turn.committed_text or active_turn.prompt
                stitched = f"{first_half} {text}".strip()
                self.emit("false_start", turn_id=active_turn.turn_id, text=text)
                await self.cancel_active_turn("continuation_retraction")
                self.status(status="hearing", partial_transcript=text)
                if should_start_from_commit:
                    self.status(status="thinking", partial_transcript=None, last_committed_transcript=stitched)
                    await self.start_turn(stitched, speculative=False)
                else:
                    self.status(status="listening", partial_transcript=None, last_committed_transcript=text)
                return
            if (
                active_turn
                and active_turn.is_playing_back()
                and (self.policy.has_explicit_interrupt(text) or not self.policy.transcript_matches(text, active_turn.prompt))
            ):
                outcome = self.consider_playback_barge_in("commit", text, now, active_turn)
                if not outcome.accepted:
                    self.emit("commit_rejected", source="commit", reason=outcome.reason, text=text)
                    log.info(
                        "commit rejected during playback: reason=%s text=%r",
                        outcome.reason,
                        text,
                    )
                    return

                self.publish_barge_in_hearing("stt")
                self.publish_barge_in_event("commit", outcome.reason)
                await self.cancel_active_turn("barge_in_commit")
                if outcome.reason == "explicit_interrupt" or not should_start_from_commit:
                    self.state.utterance_prefix = ""
                    self.status(status="listening", partial_transcript=None, last_committed_transcript=text)
                    return
                self.status(status="thinking", partial_transcript=None, last_committed_transcript=text)
                await self.start_turn(text, speculative=False)
                return

            self.status(status="thinking", partial_transcript=None, last_committed_transcript=text)

            if active_turn and not active_turn.speculative and not active_turn.playback_event.is_set():
                if self.policy.normalized_transcript(text) == self.policy.normalized_transcript(active_turn.prompt):
                    active_turn.committed_text = text
                    active_turn.prompt = text
                    await self.maybe_commit_history(active_turn)
                    return
                if active_turn.is_speaking():
                    return
                if not should_start_from_commit:
                    return
                stitched = f"{active_turn.committed_text or active_turn.prompt} {text}".strip()
                await self.cancel_active_turn("commit_continuation")
                await self.start_turn(stitched, speculative=False)
                return

            if active_turn and active_turn.is_active():
                if self.policy.transcript_matches(text, active_turn.prompt):
                    await active_turn.confirm(text)
                    self.emit("turn_committed", turn_id=active_turn.turn_id, from_speculative=True)
                    await self.maybe_commit_history(active_turn)
                else:
                    if not should_start_from_commit:
                        return
                    await self.cancel_active_turn("commit_mismatch")
                    await self.start_turn(text, speculative=False)
            elif active_turn and self.policy.transcript_matches(text, active_turn.prompt):
                await active_turn.confirm(text)
                self.emit("turn_committed", turn_id=active_turn.turn_id, from_speculative=True)
                await self.maybe_commit_history(active_turn)
            else:
                if not should_start_from_commit:
                    return
                await self.start_turn(text, speculative=False)
        finally:
            reset_utterance_barge_in_audio(self.state)

    def is_recent_assistant_echo(self, text: str, now: float) -> bool:
        if self.policy.has_explicit_interrupt(text):
            return False
        assistant_text = ""
        if self.state.active_turn is not None:
            assistant_text = self.state.active_turn.assistant_streamed_text()
        elif self.state.progress is not None:
            assistant_text = self.state.progress.assistant_streamed_text()
        if self.recent_assistant_text and now <= self.recent_assistant_echo_until:
            assistant_text = f"{assistant_text} {self.recent_assistant_text}".strip()
        return bool(assistant_text and self.policy.matches_assistant_echo(text, assistant_text))

    async def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                event = await self.scribe_events.get()
                event_type = str(event["type"])
                if event_type == "assistant_done":
                    turn = event["turn"]
                    goal_request = self.completed_goal_request(turn)
                    if goal_request is not None:
                        await self.begin_goal_handoff(turn, goal_request)
                    else:
                        await self.maybe_commit_history(turn)
                        await self.maybe_finish_silent_turn(turn)
                    continue

                if event_type == "goal_task_done":
                    await self.finish_goal(event["goal"])
                    continue

                text = str(event.get("text", ""))

                if event_type == "audio_activity":
                    now = asyncio.get_running_loop().time()
                    self.state.last_local_speech_rms = int(event.get("rms", 0))
                    heard_local_audio = False
                    if self.state.last_local_speech_rms >= self.policy.user_active_rms_threshold:
                        self.state.last_local_speech_at = now
                        self.note_user_speech()
                        heard_local_audio = True
                    elif self.levels.scribe_gate_open:
                        self.note_user_speech()
                        heard_local_audio = True
                    elif (
                        self.user_speech_on
                        and now - self.state.last_local_speech_at > self.policy.local_speech_window_secs
                    ):
                        self.end_user_speech()
                        if (
                            self.hearing_on
                            and (self.state.active_turn is None or not self.state.active_turn.is_active())
                            and self.state.active_goal is None
                            and self.state.progress is None
                        ):
                            self.status(status="listening", partial_transcript=None)
                    if heard_local_audio:
                        self.state.local_audio_seq += 1
                    self.levels.mic_rms = self.state.last_local_speech_rms
                    self.publish_barge_in_state(now, self.state.last_local_speech_rms)
                    if self.current_playback():
                        if (
                            self.state.last_local_speech_rms >= self.policy.user_active_rms_threshold
                            or self.state.gate_open
                            or self.levels.scribe_gate_open
                        ):
                            note_utterance_barge_in_audio(
                                self.state,
                                now,
                                scribe_gate_open=self.levels.scribe_gate_open,
                            )
                            note_recent_barge_in_audio(
                                self.state,
                                now,
                                self.policy,
                                scribe_gate_open=self.levels.scribe_gate_open,
                            )
                    continue

                if event_type == "partial":
                    await self.handle_partial(text)
                    continue

                if event_type == "commit":
                    await self.handle_commit(text)
        finally:
            await cancel_task(self.state.debounce_task)
            if self.state.active_turn:
                await self.state.active_turn.cancel("shutdown")
            if self.state.active_goal:
                await self.cancel_active_goal("shutdown")


async def handle_scribe_events(
    scribe_events: asyncio.Queue[dict[str, object]],
    openai_client: Any,
    elevenlabs_api_key: str,
    voice_state: VoiceState,
    stop_event: asyncio.Event,
    system_prompt: str | Callable[[], str],
    policy: TurnPolicy = DEFAULT_TURN_POLICY,
    audio_levels: AudioLevels | None = None,
    conversation_history: ConversationHistory | None = None,
    on_status: Callable[[dict[str, object]], None] | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    assistant_runner: Callable[..., Any] = run_assistant_turn,
    goal_runner: Callable[..., Any] | None = None,
    motion_intent_caller: Callable[..., Any] | None = None,
    session_end_caller: Callable[[], Any] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    stop_playback_now: Callable[[], Any] | None = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
    speaker_direction_caller: Callable[[], dict[str, Any]] | None = None,
    progress_speaker: Callable[..., Any] | None = None,
    character_prose: str | Callable[[], str] | None = None,
    openai_model: str = OPENAI_MODEL,
) -> None:
    await TurnOrchestrator(
        scribe_events,
        openai_client,
        elevenlabs_api_key,
        voice_state,
        stop_event,
        system_prompt,
        policy=policy,
        audio_levels=audio_levels,
        conversation_history=conversation_history,
        on_status=on_status,
        on_event=on_event,
        assistant_runner=assistant_runner,
        goal_runner=goal_runner,
        motion_intent_caller=motion_intent_caller,
        session_end_caller=session_end_caller,
        camera_snapshot_caller=camera_snapshot_caller,
        stop_playback_now=stop_playback_now,
        robot_inspection_caller=robot_inspection_caller,
        face_me_caller=face_me_caller,
        speaker_direction_caller=speaker_direction_caller,
        progress_speaker=progress_speaker,
        character_prose=character_prose,
        openai_model=openai_model,
    ).run()
