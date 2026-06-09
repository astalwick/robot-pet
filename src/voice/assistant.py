from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib.log import setup_logging
from voice.conversation import ConversationHistory
from voice.turn_policy import DEFAULT_TURN_POLICY, TurnPolicy


log = setup_logging("robot-voice")


OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_VOICE_ID = "Ct9jL3ofSaf3bjiuX3cL"
ALTERNATE_VOICE_ID = "Pj4KiuLufWTFgLAn5sAM"
DEFAULT_OPERATIONAL_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "operational_system_prompt.md"
OPERATIONAL_SYSTEM_PROMPT = DEFAULT_OPERATIONAL_PROMPT_PATH.read_text().strip()
VOICE_SWITCH_TOOL_NAME = "switch_voice"
END_SESSION_TOOL_NAME = "end_session"
WIGGLE_TOOL_NAME = "wiggle"
MOVE_FORWARD_TOOL_NAME = "move_forward"
LOOK_AROUND_TOOL_NAME = "look_around"
INSPECT_ROBOT_TOOL_NAME = "inspect_robot"
FACE_ME_TOOL_NAME = "face_me"
MOTION_TOOL_NAMES = (WIGGLE_TOOL_NAME, MOVE_FORWARD_TOOL_NAME)
PLAYBACK_RMS_STALE_SECS = 0.25
ASSISTANT_TURN_TIMEOUT_SECS = 120.0
OPENAI_CREATE_RETRY_DELAY_SECS = 0.2
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
    return f"{character_prose.strip()}\n\n{OPERATIONAL_SYSTEM_PROMPT}"


def is_end_session_request(text: str, policy: TurnPolicy = DEFAULT_TURN_POLICY) -> bool:
    return policy.normalized_transcript(text) in END_SESSION_UTTERANCES


@dataclass
class AudioLevels:
    mic_rms: int = 0
    mic_peak: int = 0
    mic_last: int = 0
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
    audio_levels.mic_last = rms
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


VOICE_SWITCH_TOOL = {
    "type": "function",
    "name": VOICE_SWITCH_TOOL_NAME,
    "description": "Toggle between the default and alternate speaking voices. Only use when the user explicitly asks to switch voices.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


WIGGLE_TOOL = {
    "type": "function",
    "name": WIGGLE_TOOL_NAME,
    "description": "Wiggle the robot's body briefly. A small, playful left-right motion lasting about half a second.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


MOVE_FORWARD_TOOL = {
    "type": "function",
    "name": MOVE_FORWARD_TOOL_NAME,
    "description": "Move the robot a tiny bit forward, about half a second of slow forward motion.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
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


LOOK_AROUND_TOOL = {
    "type": "function",
    "name": LOOK_AROUND_TOOL_NAME,
    "description": "Capture a current JPEG snapshot from the robot camera so you can answer questions about what the robot sees right now.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


INSPECT_ROBOT_TOOL = {
    "type": "function",
    "name": INSPECT_ROBOT_TOOL_NAME,
    "description": (
        "Inspect the robot's current battery, motor, safety, distance sensor, face detection, "
        "and computer health status. Use this to answer questions about the robot's body or surroundings."
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


WEB_SEARCH_TOOL = {"type": "web_search"}


ASSISTANT_TOOLS = [
    VOICE_SWITCH_TOOL,
    END_SESSION_TOOL,
    WIGGLE_TOOL,
    MOVE_FORWARD_TOOL,
    LOOK_AROUND_TOOL,
    INSPECT_ROBOT_TOOL,
    FACE_ME_TOOL,
    WEB_SEARCH_TOOL,
]


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


def inspect_robot_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        return {"ok": False, "error": "telemetry_unavailable"}

    battery = snapshot.get("motor_battery")
    drive = snapshot.get("drive_status")
    motor_rail = snapshot.get("motor_rail")
    sensors = snapshot.get("sensors")
    vision = snapshot.get("vision")
    pi = snapshot.get("pi")

    result: dict[str, Any] = {
        "ok": True,
        "battery": {"available": False},
        "drive": {"available": False},
        "motor_rail": {"available": False},
        "sensors": {"available": False},
        "vision": {"available": False},
        "pi": {"available": False},
    }

    if _motion_value_available(snapshot, battery):
        result["battery"] = {
            "available": True,
            "status": battery.get("status"),
            "pack_voltage": battery.get("pack_voltage"),
            "cell_voltage": battery.get("cell_voltage"),
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
                {
                    "name": reading.get("name"),
                    "distance_mm": reading.get("distance_mm"),
                    "ok": reading.get("ok"),
                }
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


@dataclass(frozen=True)
class VoiceSwitch:
    voice_id: str
    voice_name: str


@dataclass
class VoiceState:
    default_voice_id: str
    alternate_voice_id: str = ALTERNATE_VOICE_ID
    current_voice_id: str | None = None

    def __post_init__(self) -> None:
        if self.current_voice_id is None:
            self.current_voice_id = self.default_voice_id

    def toggle(self) -> VoiceSwitch:
        if self.current_voice_id == self.default_voice_id:
            self.current_voice_id = self.alternate_voice_id
            return VoiceSwitch(voice_id=self.current_voice_id, voice_name="alternate")
        self.current_voice_id = self.default_voice_id
        return VoiceSwitch(voice_id=self.current_voice_id, voice_name="default")

    def set_voice(self, voice_id: str) -> VoiceSwitch:
        # Same machinery switch_voice uses: the next turn's TTS reads current_voice_id.
        # default tracks the new voice too, so a later toggle() stays coherent.
        self.default_voice_id = voice_id
        self.current_voice_id = voice_id
        return VoiceSwitch(voice_id=voice_id, voice_name="default")


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
    task: asyncio.Task[str]
    playback_event: asyncio.Event = field(default_factory=asyncio.Event)
    speaking_event: asyncio.Event = field(default_factory=asyncio.Event)
    playback_release_task: asyncio.Task[None] | None = None
    speech_started_at: float | None = None
    assistant_streamed_chunks: list[str] = field(default_factory=list)
    committed_text: str | None = None
    assistant_text: str | None = None
    history_committed: bool = False
    delay_playback: bool = False

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
class TurnRuntimeState:
    active_turn: ActiveTurn | None = None
    next_turn_id: int = 0
    debounce_task: asyncio.Task[None] | None = None
    last_local_speech_at: float = 0.0
    last_local_speech_rms: int = 0
    gate_open: bool = False
    gate_threshold_rms: int = 0
    gate_last_reason: str = "assistant_not_speaking"
    recent_barge_in_mic_rms: int = 0
    recent_barge_in_gate_open: bool = False
    recent_barge_in_gate_reason: str = "assistant_not_speaking"
    recent_barge_in_audio_at: float = 0.0
    utterance_barge_in_mic_rms: int = 0
    utterance_barge_in_gate_open: bool = False
    utterance_barge_in_gate_reason: str = "assistant_not_speaking"
    utterance_barge_in_audio_at: float = 0.0
    local_audio_seq: int = 0
    last_partial_text: str = ""
    last_partial_audio_seq: int = -1
    barge_in_event_count: int = 0
    barge_in_hearing_reported: bool = False


def reset_recent_barge_in_audio(state: TurnRuntimeState) -> None:
    state.recent_barge_in_mic_rms = 0
    state.recent_barge_in_gate_open = False
    state.recent_barge_in_gate_reason = "assistant_not_speaking"
    state.recent_barge_in_audio_at = 0.0


def reset_utterance_barge_in_audio(state: TurnRuntimeState) -> None:
    state.utterance_barge_in_mic_rms = 0
    state.utterance_barge_in_gate_open = False
    state.utterance_barge_in_gate_reason = "assistant_not_speaking"
    state.utterance_barge_in_audio_at = 0.0


def note_utterance_barge_in_audio(state: TurnRuntimeState, now: float) -> None:
    if state.last_local_speech_rms > state.utterance_barge_in_mic_rms:
        state.utterance_barge_in_mic_rms = state.last_local_speech_rms
    if state.gate_open:
        state.utterance_barge_in_gate_open = True
    state.utterance_barge_in_gate_reason = state.gate_last_reason
    state.utterance_barge_in_audio_at = now


def note_recent_barge_in_audio(state: TurnRuntimeState, now: float, policy: TurnPolicy) -> None:
    fresh = now - state.recent_barge_in_audio_at <= policy.local_speech_window_secs
    if not fresh or state.last_local_speech_rms > state.recent_barge_in_mic_rms:
        state.recent_barge_in_mic_rms = state.last_local_speech_rms
    if not fresh:
        state.recent_barge_in_gate_open = False
    if state.gate_open:
        state.recent_barge_in_gate_open = True
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
    active_turn: ActiveTurn,
    state: TurnRuntimeState,
    levels: AudioLevels,
    policy: TurnPolicy,
) -> BargeInOutcome:
    mic_rms = state.last_local_speech_rms
    gate_open = state.gate_open
    gate_reason = state.gate_last_reason
    if now - state.recent_barge_in_audio_at <= policy.local_speech_window_secs:
        mic_rms = state.recent_barge_in_mic_rms
        gate_open = state.recent_barge_in_gate_open
        gate_reason = state.recent_barge_in_gate_reason
    elif state.utterance_barge_in_audio_at > 0:
        mic_rms = state.utterance_barge_in_mic_rms
        gate_open = state.utterance_barge_in_gate_open
        gate_reason = state.utterance_barge_in_gate_reason
    accepted, reason = policy.barge_in_decision(
        text,
        assistant_speaking=True,
        gate_open=gate_open,
        assistant_speech_elapsed_secs=active_turn.speech_elapsed_secs(now),
        mic_rms=mic_rms,
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
    motion_intent_caller: Callable[[str], Any] | None = None,
    end_session_pending: list[bool] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    usage: Any = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
) -> AsyncIterator[str | VoiceSwitch]:
    pending = ""
    word_buffer: list[str] = []
    response_input: object = openai_input
    previous_response_id: str | None = None
    text_streamed = False
    end_session_tool_output_sent = False

    while True:
        create_kwargs: dict[str, Any] = {
            "model": OPENAI_MODEL,
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

                word_buffer.extend(pieces)
                while len(word_buffer) >= 3:
                    text_streamed = True
                    yield "".join(word_buffer[:3])
                    del word_buffer[:3]
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

                    record_openai_usage(usage, getattr(response, "usage", None))

        if pending:
            word_buffer.append(pending)
            pending = ""
        if word_buffer:
            text_streamed = True
            yield "".join(word_buffer)
            word_buffer.clear()

        if not function_calls:
            return

        tool_outputs: list[dict[str, str]] = []
        image_messages: list[dict[str, Any]] = []
        for function_call in function_calls:
            call_id = getattr(function_call, "call_id", "")
            name = getattr(function_call, "name", "")
            log.info("tool call: %s", name)
            voice_switch = None

            if name == VOICE_SWITCH_TOOL_NAME:
                voice_switch = voice_state.toggle()
                result: dict[str, Any] = {
                    "voice": voice_switch.voice_name,
                    "voice_id": voice_switch.voice_id,
                }
            elif name == END_SESSION_TOOL_NAME:
                if end_session_pending is None:
                    result = {"ok": False, "error": "session_end_unavailable"}
                else:
                    end_session_pending[0] = True
                    result = {"ok": True, "ended": True}
            elif name in MOTION_TOOL_NAMES:
                if motion_intent_caller is None:
                    result = {"ok": False, "error": "motion_caller_missing"}
                else:
                    result = await asyncio.to_thread(motion_intent_caller, name)
            elif name == LOOK_AROUND_TOOL_NAME:
                if camera_snapshot_caller is None:
                    result = {"ok": False, "error": "camera_snapshot_unavailable"}
                else:
                    try:
                        jpeg = await asyncio.to_thread(camera_snapshot_caller)
                    except Exception as exc:  # noqa: BLE001 -- camera HTTP failures vary
                        result = {"ok": False, "error": str(exc)}
                    else:
                        data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
                        image_messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Here is the current camera snapshot from the robot.",
                                    },
                                    {"type": "input_image", "image_url": data_url},
                                ],
                            }
                        )
                        result = {"ok": True, "image_attached": True}
            elif name == INSPECT_ROBOT_TOOL_NAME:
                if robot_inspection_caller is None:
                    result = {"ok": False, "error": "telemetry_unavailable"}
                else:
                    try:
                        snapshot = await asyncio.to_thread(robot_inspection_caller)
                    except Exception as exc:  # noqa: BLE001 -- telemetry transport failures vary
                        result = {"ok": False, "error": str(exc)}
                    else:
                        result = inspect_robot_snapshot(snapshot)
            elif name == FACE_ME_TOOL_NAME:
                if face_me_caller is None:
                    result = {"ok": False, "error": "face_me_unavailable"}
                else:
                    result = await asyncio.to_thread(face_me_caller)
            else:
                result = {"ok": False, "error": "unsupported tool"}

            if voice_switch is not None:
                yield voice_switch

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                }
            )

            if result.get("ok") is False:
                log.warning("tool call %s failed: %s", name, result.get("error"))
            else:
                log.info("tool call %s ok", name)

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
    motion_intent_caller: Callable[[str], Any] | None = None,
    session_end_caller: Callable[[], Any] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    usage: Any = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
) -> str:
    from voice.elevenlabs_io import speak_with_eleven_flash

    assistant_chunks: list[str] = []
    end_session_pending = [False]

    async def captured_openai_words() -> AsyncIterator[str | VoiceSwitch]:
        async for chunk in stream_openai_words(
            openai_input,
            openai_client,
            voice_state,
            motion_intent_caller,
            end_session_pending,
            camera_snapshot_caller,
            usage,
            robot_inspection_caller,
            face_me_caller,
        ):
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
    return "".join(assistant_chunks).strip()


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
    motion_intent_caller: Callable[[str], Any] | None = None,
    session_end_caller: Callable[[], Any] | None = None,
    camera_snapshot_caller: Callable[[], bytes] | None = None,
    stop_playback_now: Callable[[], Any] | None = None,
    robot_inspection_caller: Callable[[], dict[str, Any] | None] | None = None,
    face_me_caller: Callable[[], dict[str, Any]] | None = None,
) -> None:
    state = TurnRuntimeState(gate_threshold_rms=policy.barge_in_min_rms)
    history = conversation_history if conversation_history is not None else ConversationHistory()
    levels = audio_levels if audio_levels is not None else AudioLevels()
    recent_assistant_text = ""
    recent_assistant_echo_until = 0.0
    hearing_on = False
    thinking_on = False
    user_speech_on = False

    def current_system_prompt() -> str:
        return system_prompt() if callable(system_prompt) else system_prompt

    def note_user_speech() -> None:
        nonlocal user_speech_on
        if user_speech_on:
            return
        user_speech_on = True
        emit("phase", name="user_speech", on=True)

    def end_user_speech() -> None:
        nonlocal user_speech_on
        if not user_speech_on:
            return
        user_speech_on = False
        emit("phase", name="user_speech", on=False)

    def status(**values: object) -> None:
        nonlocal hearing_on, thinking_on
        if on_status:
            on_status(values)
        if not on_event or "status" not in values:
            return
        new_status = values["status"]
        if new_status not in {"hearing", "thinking", "listening"}:
            return
        new_hearing = new_status == "hearing"
        new_thinking = new_status == "thinking"
        if new_hearing != hearing_on:
            hearing_on = new_hearing
            emit("phase", name="hearing", on=hearing_on)
        if new_thinking != thinking_on:
            thinking_on = new_thinking
            emit("phase", name="thinking", on=thinking_on)

    def emit(kind: str, **payload: object) -> None:
        if not on_event:
            return
        event = {"type": kind, "t": time.monotonic(), **payload}
        on_event(event)

    def publish_barge_in_state(now: float, mic_rms: int | None = None) -> None:
        mic = state.last_local_speech_rms if mic_rms is None else mic_rms
        assistant_speaking = bool(state.active_turn and state.active_turn.is_playing_back())
        _, state.gate_open, state.gate_threshold_rms, state.gate_last_reason = refresh_barge_in_gate(
            levels,
            now,
            policy,
            assistant_speaking,
            mic,
        )
        playback_rms = effective_playback_rms(levels, now)
        status(
            **barge_in_telemetry(
                policy,
                mic,
                playback_rms,
                state.gate_threshold_rms,
                state.gate_open,
                state.gate_last_reason,
            )
        )

    def report_barge_in(source: str, outcome: BargeInOutcome) -> None:
        state.gate_last_reason = outcome.reason
        emit(
            "barge_in_considered",
            source=source,
            accepted=outcome.accepted,
            reason=outcome.reason,
            mic=outcome.mic_rms,
            playback=outcome.playback_rms,
            threshold=policy.barge_in_min_rms,
        )
        status(
            **barge_in_telemetry(
                policy,
                outcome.mic_rms,
                outcome.playback_rms,
                policy.barge_in_min_rms,
                outcome.gate_open,
                outcome.reason,
            )
        )

    def publish_barge_in_event(source: str, reason: str) -> None:
        state.barge_in_event_count += 1
        status(
            barge_in_event_count=state.barge_in_event_count,
            barge_in_last_event=f"{source}: {reason}",
        )
        emit("barge_in_fired", source=source, reason=reason)

    def publish_barge_in_hearing(source: str) -> None:
        if state.barge_in_hearing_reported:
            return
        state.barge_in_hearing_reported = True
        publish_barge_in_event(source, "hearing")

    def trigger_stop_playback_now() -> None:
        if not stop_playback_now:
            return
        try:
            result = stop_playback_now()
        except Exception:
            log.exception("stop playback failed")
            return
        if asyncio.iscoroutine(result):
            task = asyncio.create_task(result)
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def cancel_active_turn(reason: str) -> None:
        turn = state.active_turn
        state.active_turn = None
        if turn and (turn.is_active() or (turn.playback_release_task and not turn.playback_release_task.done())):
            emit("turn_cancel", turn_id=turn.turn_id, reason=reason, was_speaking=turn.is_speaking())
            streamed = turn.assistant_streamed_text().strip()
            if streamed:
                emit("assistant", turn_id=turn.turn_id, text=streamed, cancelled=True)
            if stop_playback_now and turn.is_playing_back():
                trigger_stop_playback_now()
            await turn.cancel(reason)

    async def release_speculative_playback(turn: ActiveTurn) -> None:
        await asyncio.sleep(policy.speculative_playback_delay_secs)
        while state.active_turn is turn and turn.is_active() and not turn.playback_event.is_set():
            quiet_remaining_secs = policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), state.last_local_speech_at)
            if quiet_remaining_secs <= 0:
                turn.open_playback()
                return
            await asyncio.sleep(quiet_remaining_secs)

    async def release_committed_playback(turn: ActiveTurn) -> None:
        await asyncio.sleep(policy.commit_playback_delay_secs)
        while state.active_turn is turn and not turn.playback_event.is_set():
            quiet_remaining_secs = policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), state.last_local_speech_at)
            if quiet_remaining_secs <= 0:
                turn.open_playback()
                await maybe_commit_history(turn)
                return
            await asyncio.sleep(quiet_remaining_secs)

    async def maybe_commit_history(turn: ActiveTurn) -> None:
        nonlocal recent_assistant_text, recent_assistant_echo_until
        if (
            state.active_turn is not turn
            or turn.history_committed
            or turn.speculative
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
            status(status="error", last_error=str(exc))
            return
        turn.assistant_text = assistant_text
        recent_assistant_text = assistant_text
        recent_assistant_echo_until = asyncio.get_running_loop().time() + policy.assistant_echo_memory_secs
        history.append_exchange(turn.committed_text or turn.prompt, assistant_text)
        turn.history_committed = True
        if assistant_text:
            emit("assistant", turn_id=turn.turn_id, text=assistant_text)
        status(status="listening", assistant_speaking=False, last_assistant_text=assistant_text)

    async def start_turn(prompt: str, speculative: bool) -> None:
        await cancel_active_turn("new_turn")
        state.barge_in_hearing_reported = False
        reset_recent_barge_in_audio(state)
        reset_utterance_barge_in_audio(state)
        state.next_turn_id += 1
        new_turn_id = state.next_turn_id
        playback_event = asyncio.Event()
        speaking_event = asyncio.Event()
        assistant_streamed_chunks: list[str] = []
        first_token_emitted = False

        def on_assistant_chunk(chunk: str) -> None:
            nonlocal first_token_emitted
            assistant_streamed_chunks.append(chunk)
            if not first_token_emitted:
                first_token_emitted = True
                emit("turn_first_token", turn_id=new_turn_id)

        openai_input = history.input_for(prompt, current_system_prompt())

        async def run_turn() -> str:
            return await asyncio.wait_for(
                assistant_runner(
                    new_turn_id,
                    openai_input,
                    playback_event,
                    speaking_event,
                    openai_client,
                    elevenlabs_api_key,
                    voice_state,
                    on_assistant_chunk=on_assistant_chunk,
                    motion_intent_caller=motion_intent_caller,
                    session_end_caller=session_end_caller,
                    camera_snapshot_caller=camera_snapshot_caller,
                    robot_inspection_caller=robot_inspection_caller,
                    face_me_caller=face_me_caller,
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
        task.add_done_callback(lambda _task, completed_turn=turn: scribe_events.put_nowait({"type": "assistant_done", "turn": completed_turn}))
        state.active_turn = turn
        emit("turn_start", turn_id=new_turn_id, speculative=speculative, prompt=prompt)
        if not speculative:
            emit("turn_committed", turn_id=new_turn_id, from_speculative=False)
        status(status="thinking", assistant_speaking=False)
        if speculative:
            turn.playback_release_task = asyncio.create_task(release_speculative_playback(turn))
        else:
            turn.playback_release_task = asyncio.create_task(release_committed_playback(turn))

    async def start_after_stable_partial(text: str) -> None:
        await asyncio.sleep(policy.speculative_partial_delay_secs)
        while True:
            should_start, _reason = policy.speculation_decision(text)
            if not should_start:
                return
            quiet_remaining_secs = policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), state.last_local_speech_at)
            if quiet_remaining_secs > 0:
                await asyncio.sleep(quiet_remaining_secs)
                continue
            if state.active_turn and policy.transcript_matches(text, state.active_turn.prompt):
                return
            await start_turn(text, speculative=True)
            return

    def consider_playback_barge_in(
        source: str,
        text: str,
        now: float,
        active_turn: ActiveTurn,
    ) -> BargeInOutcome:
        active_turn.mark_speech_started(now)
        publish_barge_in_state(now)
        outcome = decide_barge_in_during_playback(text, now, active_turn, state, levels, policy)
        report_barge_in(source, outcome)
        return outcome

    async def handle_partial(text: str) -> None:
        now = asyncio.get_running_loop().time()
        normalized_partial = policy.normalized_transcript(text)
        if (
            normalized_partial
            and normalized_partial == state.last_partial_text
            and state.local_audio_seq == state.last_partial_audio_seq
        ):
            return
        state.last_partial_text = normalized_partial
        state.last_partial_audio_seq = state.local_audio_seq
        emit("partial", text=text)
        note_user_speech()
        if is_recent_assistant_echo(text, now):
            await cancel_task(state.debounce_task)
            state.debounce_task = None
            emit("echo_suppressed", source="partial", text=text)
            status(status="listening")
            return

        active_turn = state.active_turn
        if active_turn and active_turn.is_playing_back():
            outcome = consider_playback_barge_in("partial", text, now, active_turn)
            if outcome.accepted:
                publish_barge_in_hearing("stt")
                status(status="hearing", partial_transcript=text)
                publish_barge_in_event("partial", outcome.reason)
                await cancel_active_turn("barge_in")
                await cancel_task(state.debounce_task)
                state.debounce_task = None
                if outcome.reason == "explicit_interrupt":
                    status(status="listening", partial_transcript=None)
                else:
                    state.debounce_task = asyncio.create_task(start_after_stable_partial(text))
            else:
                emit("barge_in_rejected", source="partial", reason=outcome.reason, text=text)
                status(status="speaking", assistant_speaking=True, partial_transcript=None)
            return

        status(status="hearing", partial_transcript=text)

        if active_turn and active_turn.speculative and text != active_turn.prompt and policy.transcript_matches(text, active_turn.prompt):
            if policy.should_replace_speculative_prompt(text, active_turn.prompt):
                await start_turn(text, speculative=True)
            else:
                if policy.looks_incomplete_partial(text) and active_turn.playback_release_task:
                    await cancel_task(active_turn.playback_release_task)
                    active_turn.playback_release_task = None
            return

        if (
            active_turn
            and not active_turn.speculative
            and not active_turn.playback_event.is_set()
            and not active_turn.is_speaking()
            and policy.normalized_transcript(text) != policy.normalized_transcript(active_turn.prompt)
        ):
            await cancel_active_turn("commit_continuation")
            await cancel_task(state.debounce_task)
            state.debounce_task = asyncio.create_task(start_after_stable_partial(text))
            return

        await cancel_task(state.debounce_task)
        state.debounce_task = asyncio.create_task(start_after_stable_partial(text))

    async def handle_commit(text: str) -> None:
        await cancel_task(state.debounce_task)
        state.debounce_task = None
        now = asyncio.get_running_loop().time()
        emit("commit", text=text)
        end_user_speech()
        try:
            if is_end_session_request(text, policy):
                status(status="listening", partial_transcript=None, last_committed_transcript=text)
                if session_end_caller:
                    session_end_caller()
                return
            if is_recent_assistant_echo(text, now):
                emit("echo_suppressed", source="commit", text=text)
                status(status="listening")
                return

            should_start_from_commit, commit_reason = policy.commit_decision(text)
            emit("commit_decision", accepted=should_start_from_commit, reason=commit_reason, text=text)

            active_turn = state.active_turn
            if (
                active_turn
                and active_turn.is_playing_back()
                and not policy.transcript_matches(text, active_turn.prompt)
            ):
                outcome = consider_playback_barge_in("commit", text, now, active_turn)
                if not outcome.accepted:
                    emit("commit_rejected", source="commit", reason=outcome.reason, text=text)
                    log.info(
                        "commit rejected during playback: reason=%s text=%r",
                        outcome.reason,
                        text,
                    )
                    return

                publish_barge_in_hearing("stt")
                publish_barge_in_event("commit", outcome.reason)
                await cancel_active_turn("barge_in_commit")
                if outcome.reason == "explicit_interrupt" or not should_start_from_commit:
                    status(status="listening", partial_transcript=None, last_committed_transcript=text)
                    return
                status(status="thinking", partial_transcript=None, last_committed_transcript=text)
                await start_turn(text, speculative=False)
                return

            status(status="thinking", partial_transcript=None, last_committed_transcript=text)

            if active_turn and not active_turn.speculative and not active_turn.playback_event.is_set():
                if policy.normalized_transcript(text) == policy.normalized_transcript(active_turn.prompt):
                    active_turn.committed_text = text
                    active_turn.prompt = text
                    await maybe_commit_history(active_turn)
                    return
                if active_turn.is_speaking():
                    return
                if not should_start_from_commit:
                    return
                await cancel_active_turn("commit_continuation")
                await start_turn(text, speculative=False)
                return

            if active_turn and active_turn.is_active():
                if policy.transcript_matches(text, active_turn.prompt):
                    await active_turn.confirm(text)
                    emit("turn_committed", turn_id=active_turn.turn_id, from_speculative=True)
                    await maybe_commit_history(active_turn)
                else:
                    if not should_start_from_commit:
                        return
                    await cancel_active_turn("commit_mismatch")
                    await start_turn(text, speculative=False)
            elif active_turn and policy.transcript_matches(text, active_turn.prompt):
                await active_turn.confirm(text)
                emit("turn_committed", turn_id=active_turn.turn_id, from_speculative=True)
                await maybe_commit_history(active_turn)
            else:
                if not should_start_from_commit:
                    return
                await start_turn(text, speculative=False)
        finally:
            reset_utterance_barge_in_audio(state)

    def is_recent_assistant_echo(text: str, now: float) -> bool:
        if policy.has_explicit_interrupt(text):
            return False
        assistant_text = ""
        if state.active_turn is not None:
            assistant_text = state.active_turn.assistant_streamed_text()
        if recent_assistant_text and now <= recent_assistant_echo_until:
            assistant_text = f"{assistant_text} {recent_assistant_text}".strip()
        return bool(assistant_text and policy.matches_assistant_echo(text, assistant_text))

    try:
        while not stop_event.is_set():
            event = await scribe_events.get()
            event_type = str(event["type"])
            if event_type == "assistant_done":
                await maybe_commit_history(event["turn"])
                continue

            text = str(event.get("text", ""))

            if event_type == "audio_activity":
                now = asyncio.get_running_loop().time()
                state.last_local_speech_rms = max(int(event.get("rms", 0)), levels.mic_peak)
                heard_local_audio = False
                if state.last_local_speech_rms >= policy.user_active_rms_threshold:
                    state.last_local_speech_at = now
                    note_user_speech()
                    heard_local_audio = True
                elif levels.scribe_gate_open:
                    note_user_speech()
                    heard_local_audio = True
                if heard_local_audio:
                    state.local_audio_seq += 1
                levels.mic_rms = state.last_local_speech_rms
                publish_barge_in_state(now, state.last_local_speech_rms)
                if state.active_turn and state.active_turn.is_playing_back():
                    if state.last_local_speech_rms >= policy.user_active_rms_threshold or state.gate_open:
                        note_utterance_barge_in_audio(state, now)
                        note_recent_barge_in_audio(state, now, policy)
                continue

            if event_type == "partial":
                await handle_partial(text)
                continue

            if event_type == "commit":
                await handle_commit(text)
    finally:
        await cancel_task(state.debounce_task)
        if state.active_turn:
            await state.active_turn.cancel("shutdown")
