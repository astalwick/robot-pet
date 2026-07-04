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


log = setup_logging("robot-voice")


OPENAI_MODEL = DEFAULT_OPENAI_MODEL
DEFAULT_VOICE_ID = "Ct9jL3ofSaf3bjiuX3cL"
ALTERNATE_VOICE_ID = "Pj4KiuLufWTFgLAn5sAM"
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
DEFAULT_OPERATIONAL_PROMPT_PATH = CONFIG_DIR / "operational_system_prompt.md"
OPERATIONAL_SYSTEM_PROMPT = DEFAULT_OPERATIONAL_PROMPT_PATH.read_text().strip()
# Shared robot principles appended to both the assistant prompt and the goal
# runner prompt, so the guidance lives in exactly one file and can't drift.
SHARED_ROBOT_GUIDANCE_PATH = CONFIG_DIR / "shared_robot_guidance.md"
SHARED_ROBOT_GUIDANCE = SHARED_ROBOT_GUIDANCE_PATH.read_text().strip()
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
    return f"{character_prose.strip()}\n\n{OPERATIONAL_SYSTEM_PROMPT}\n\n{SHARED_ROBOT_GUIDANCE}"


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
        if clearance is None or reading.get("ok") is False:
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


@dataclass(frozen=True)
class VoiceSwitch:
    voice_id: str
    voice_name: str


@dataclass(frozen=True)
class AgentGoalRequest:
    """The assistant turn decided this request needs the iterative goal runner.

    It bubbles out of the turn as the task result instead of spoken text, so the
    session can hand off to a goal instead of committing a normal exchange.
    """

    goal: str
    # Any text the assistant produced alongside the start_goal call (e.g. "On it.").
    # The goal runner speaks it as the opening acknowledgement so the handoff is not
    # silent until the goal's first narration.
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
    alternate_voice_id: str = ALTERNATE_VOICE_ID
    current_voice_id: str | None = None

    def __post_init__(self) -> None:
        if self.current_voice_id is None:
            self.current_voice_id = self.default_voice_id

    def set_voice(self, voice_id: str) -> VoiceSwitch:
        # The next turn's TTS reads current_voice_id.
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
    utterance_prefix: str = ""
    utterance_prefix_deadline: float = 0.0
    false_starts: int = 0


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
    active_turn: ActiveTurn | ProgressSpeaker,
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
) -> AsyncIterator[str | VoiceSwitch | AgentGoalRequest]:
    from voice.tools import ASSISTANT_TOOLS, RobotToolCall, VoiceToolContext, dispatch_tool, parse_tool_arguments

    pending = ""
    word_buffer: list[str] = []
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
        response_text_chunks: list[str] = []
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

                word_buffer.extend(pieces)
                while len(word_buffer) >= 3:
                    response_text_chunks.append("".join(word_buffer[:3]))
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

                    record_openai_usage(usage, getattr(response, "usage", None), openai_model)

        if pending:
            word_buffer.append(pending)
            pending = ""
        if word_buffer:
            response_text_chunks.append("".join(word_buffer))
            word_buffer.clear()

        if not function_calls:
            for chunk in response_text_chunks:
                text_streamed = True
                yield chunk
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

            # A goal handoff ends this turn: yield the request and stop. Any text the
            # model produced alongside the call rides along as the goal's opening
            # acknowledgement, so the handoff is not silent until the first narration.
            if call.name == START_GOAL_TOOL_NAME:
                log.info("goal handoff: %s", call.arguments.get("goal", ""))
                yield AgentGoalRequest(
                    goal=str(call.arguments.get("goal", "")).strip(),
                    preamble="".join(response_text_chunks).strip(),
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

    # Peek the first chunk before opening the speaker. When the model's first move
    # is a goal handoff, the AgentGoalRequest is the only thing the stream yields,
    # so we return it here without ever opening the TTS socket. (A handoff that only
    # appears after earlier tool calls is rarer and is caught in the loop below.)
    try:
        first_chunk = await words.__anext__()
    except StopAsyncIteration:
        first_chunk = None

    if isinstance(first_chunk, AgentGoalRequest):
        return first_chunk

    async def captured_openai_words() -> AsyncIterator[str | VoiceSwitch]:
        nonlocal goal_request
        if isinstance(first_chunk, str):
            assistant_chunks.append(first_chunk)
            if on_assistant_chunk:
                on_assistant_chunk(first_chunk)
        if first_chunk is not None:
            yield first_chunk
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

    def current_character_prose() -> str:
        if character_prose is None:
            return ""
        return character_prose() if callable(character_prose) else character_prose

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
        new_status = values.get("status")
        if (
            "assistant_working" not in values
            and state.active_turn is None
            and state.active_goal is None
            and state.progress is None
        ):
            values["assistant_working"] = False
        if on_status:
            on_status(values)
        if not on_event or "status" not in values:
            return
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
        assistant_speaking = bool(current_playback())
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
        nonlocal recent_assistant_text, recent_assistant_echo_until
        turn = state.active_turn
        state.active_turn = None
        if turn and (turn.is_active() or (turn.playback_release_task and not turn.playback_release_task.done())):
            was_speaking = turn.is_speaking()
            emit("turn_cancel", turn_id=turn.turn_id, reason=reason, was_speaking=was_speaking)
            if reason == "continuation_retraction" and was_speaking:
                state.false_starts += 1
                status(false_starts=state.false_starts)
            streamed = turn.assistant_streamed_text().strip()
            if streamed:
                emit("assistant", turn_id=turn.turn_id, text=streamed, cancelled=True)
            # If the turn actually reached the speaker, record what it said so
            # history reflects reality — even on barge-in/cancel. Must read
            # playback_event before turn.cancel() clears it.
            if streamed and turn.playback_event.is_set() and not turn.history_committed:
                turn.assistant_text = streamed
                recent_assistant_text = streamed
                recent_assistant_echo_until = asyncio.get_running_loop().time() + policy.assistant_echo_memory_secs
                if reason != "continuation_retraction":
                    history.append_exchange(turn.committed_text or turn.prompt, streamed)
                turn.history_committed = True
            if stop_playback_now and turn.is_playing_back():
                trigger_stop_playback_now()
            await turn.cancel(reason)

    async def cancel_unconfirmed_speculation(turn: ActiveTurn) -> None:
        await asyncio.sleep(policy.speculative_no_commit_timeout_secs)
        if state.active_turn is turn and turn.speculative and not turn.playback_event.is_set():
            turn.playback_release_task = None
            await cancel_active_turn("no_commit")
            status(status="listening", assistant_working=False, partial_transcript=None)

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
        # A speculative turn that actually spoke (playback opened) still belongs
        # in history even if no commit ever confirmed it — we record it once the
        # turn finishes, using its triggering partial as the user text.
        if (
            state.active_turn is not turn
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
            status(status="error", assistant_working=False, last_error=str(exc))
            return
        if isinstance(assistant_text, AgentGoalRequest):
            # A goal handoff is recorded only when the goal finishes, not here.
            return
        turn.assistant_text = assistant_text
        recent_assistant_text = assistant_text
        recent_assistant_echo_until = asyncio.get_running_loop().time() + policy.assistant_echo_memory_secs
        history.append_exchange(turn.committed_text or turn.prompt, assistant_text)
        turn.history_committed = True
        if assistant_text:
            emit("assistant", turn_id=turn.turn_id, text=assistant_text)
        status(status="listening", assistant_speaking=False, assistant_working=False, last_assistant_text=assistant_text)

    def completed_goal_request(turn: ActiveTurn) -> AgentGoalRequest | None:
        if not turn.task.done():
            return None
        try:
            result = turn.task.result()
        except (asyncio.CancelledError, Exception):
            return None
        return result if isinstance(result, AgentGoalRequest) else None

    async def maybe_finish_silent_turn(turn: ActiveTurn) -> None:
        if (
            state.active_turn is not turn
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
            state.active_turn = None
            await cancel_task(turn.playback_release_task)
            turn.playback_release_task = None
            log.exception("assistant turn failed: %s", exc)
            status(status="error", assistant_working=False, last_error=str(exc))
            return
        if isinstance(assistant_text, AgentGoalRequest) or assistant_text.strip():
            return
        state.active_turn = None
        await cancel_task(turn.playback_release_task)
        turn.playback_release_task = None
        status(status="listening", assistant_speaking=False, assistant_working=False)

    async def cancel_active_goal(reason: str) -> None:
        goal = state.active_goal
        state.active_goal = None
        if goal is None:
            return
        goal.terminal_reason = "cancelled"
        goal.stop_event.set()
        emit("goal_cancel", goal=goal.goal, reason=reason)
        log.info("goal cancelled: reason=%s goal=%r", reason, goal.goal)
        await cancel_task(goal.task)

    def current_playback() -> ActiveTurn | ProgressSpeaker | None:
        if state.active_turn and state.active_turn.is_playing_back():
            return state.active_turn
        if state.progress and state.progress.is_playing_back():
            return state.progress
        return None

    async def cancel_current_playback(reason: str) -> None:
        if state.progress and state.progress.is_playing_back():
            await cancel_active_goal(reason)
        else:
            await cancel_active_turn(reason)

    async def speak_progress(text: str) -> None:
        nonlocal recent_assistant_text, recent_assistant_echo_until
        text = (text or "").strip()
        if not text:
            return
        progress = ProgressSpeaker(text=text)
        progress.playback_event.set()
        progress.speaking_event.set()
        state.progress = progress
        emit("goal_speech", text=text)
        status(status="speaking", assistant_speaking=True, assistant_working=True)

        async def text_stream() -> AsyncIterator[str]:
            yield text

        speaker = progress_speaker or _speak_progress_default
        try:
            await speaker(
                text_stream(),
                elevenlabs_api_key,
                voice_state.current_voice_id,
                progress.playback_event,
                progress.speaking_event,
            )
        except asyncio.CancelledError:
            trigger_stop_playback_now()
            raise
        finally:
            if state.progress is progress:
                state.progress = None
            progress.playback_event.clear()
            progress.speaking_event.clear()

        # Narration played to completion (a cancel re-raises above and skips this).
        # Record what we just said so a delayed STT echo of this line is suppressed
        # even after state.progress clears and the goal keeps working.
        recent_assistant_text = text
        recent_assistant_echo_until = asyncio.get_running_loop().time() + policy.assistant_echo_memory_secs

        # The narration ended but the goal keeps working. The session speaker flips
        # status back to listening when speech stops, so restore thinking here or the
        # idle timer would treat an active goal as an idle session.
        if state.active_goal is not None:
            status(status="thinking", assistant_speaking=False, assistant_working=True)

    async def begin_goal_handoff(turn: ActiveTurn, goal_request: AgentGoalRequest) -> None:
        # The normal turn is finished and is handing off to an iterative goal. Drop
        # it as the active turn so it is not treated as a speaking turn, and surface
        # the handoff. We do not commit history here — the goal records its own
        # result when it reaches a terminal state.
        if state.active_turn is turn:
            state.active_turn = None
        emit("goal_handoff", turn_id=turn.turn_id, goal=goal_request.goal)
        log.info("goal handoff received: %s", goal_request.goal)
        if goal_runner is None:
            status(status="listening", assistant_speaking=False, assistant_working=False)
            return
        await cancel_active_goal("superseded")
        goal = ActiveGoal(
            goal=goal_request.goal,
            user_text=turn.committed_text or turn.prompt,
            stop_event=asyncio.Event(),
            started_at=asyncio.get_running_loop().time(),
        )

        async def run_goal() -> str:
            return await goal_runner(
                goal=goal.goal,
                stop_event=goal.stop_event,
                openai_client=openai_client,
                openai_model=openai_model,
                voice_state=voice_state,
                motion_intent_caller=motion_intent_caller,
                camera_snapshot_caller=camera_snapshot_caller,
                robot_inspection_caller=robot_inspection_caller,
                face_me_caller=face_me_caller,
                speaker_direction_caller=speaker_direction_caller,
                speak_progress=speak_progress,
                is_speaking=lambda: current_playback() is not None,
                on_event=on_event,
                character_prose=current_character_prose(),
                preamble=goal_request.preamble,
            )

        goal.task = asyncio.create_task(run_goal())
        goal.task.add_done_callback(
            lambda _done, g=goal: scribe_events.put_nowait({"type": "goal_task_done", "goal": g})
        )
        state.active_goal = goal
        emit("goal_start", goal=goal.goal)
        status(status="thinking", assistant_working=True)

    async def finish_goal(goal: ActiveGoal) -> None:
        nonlocal recent_assistant_text, recent_assistant_echo_until
        if state.active_goal is not goal or goal.task is None:
            return
        state.active_goal = None
        try:
            final_text = goal.task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("goal task failed: %s", exc)
            status(status="error", assistant_working=False, last_error=str(exc))
            return
        final_text = (final_text or "").strip()
        goal.final_text = final_text
        goal.terminal_reason = "done"
        if final_text:
            history.append_exchange(goal.user_text, final_text)
            recent_assistant_text = final_text
            recent_assistant_echo_until = asyncio.get_running_loop().time() + policy.assistant_echo_memory_secs
            emit("assistant", text=final_text)
        emit("goal_done", goal=goal.goal, text=final_text)
        status(status="listening", assistant_speaking=False, assistant_working=False, last_assistant_text=final_text)

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
                    speaker_direction_caller=speaker_direction_caller,
                    openai_model=openai_model,
                    on_event=on_event,
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
        status(status="thinking", assistant_speaking=False, assistant_working=True)
        if speculative:
            turn.playback_release_task = asyncio.create_task(cancel_unconfirmed_speculation(turn))
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
        playback: ActiveTurn | ProgressSpeaker,
    ) -> BargeInOutcome:
        playback.mark_speech_started(now)
        publish_barge_in_state(now)
        outcome = decide_barge_in_during_playback(text, now, playback, state, levels, policy)
        report_barge_in(source, outcome)
        return outcome

    def continuation_retraction_eligible(turn: ActiveTurn, text: str, now: float) -> bool:
        return (
            not policy.has_explicit_interrupt(text)
            and turn.playback_opened_at is not None
            and now - turn.playback_opened_at <= policy.continuation_grace_secs
            and len(re.findall(r"\S+", text)) >= policy.continuation_min_words
        )

    async def retract_continuation(turn: ActiveTurn, fragment_text: str, now: float) -> None:
        first_half = turn.committed_text or turn.prompt
        state.utterance_prefix = f"{state.utterance_prefix} {first_half}".strip()
        state.utterance_prefix_deadline = now + 10.0
        emit("false_start", turn_id=turn.turn_id, text=fragment_text)
        await cancel_active_turn("continuation_retraction")
        status(status="hearing", partial_transcript=fragment_text)
        await cancel_task(state.debounce_task)
        state.debounce_task = asyncio.create_task(
            start_after_stable_partial(f"{state.utterance_prefix} {fragment_text}")
        )

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
        playback = current_playback()
        if playback:
            if (
                playback is state.active_turn
                and continuation_retraction_eligible(playback, text, now)
            ):
                await retract_continuation(playback, text, now)
                return
            outcome = consider_playback_barge_in("partial", text, now, playback)
            if outcome.accepted:
                publish_barge_in_hearing("stt")
                status(status="hearing", partial_transcript=text)
                publish_barge_in_event("partial", outcome.reason)
                await cancel_current_playback("barge_in")
                await cancel_task(state.debounce_task)
                state.debounce_task = None
                if outcome.reason == "explicit_interrupt":
                    state.utterance_prefix = ""
                    status(status="listening", partial_transcript=None)
                else:
                    state.debounce_task = asyncio.create_task(start_after_stable_partial(text))
            else:
                emit("barge_in_rejected", source="partial", reason=outcome.reason, text=text)
                status(status="speaking", assistant_speaking=True, partial_transcript=None)
            return

        if policy.has_explicit_interrupt(text):
            state.utterance_prefix = ""
        elif state.utterance_prefix and now < state.utterance_prefix_deadline:
            text = f"{state.utterance_prefix} {text}"
        elif state.utterance_prefix:
            state.utterance_prefix = ""

        status(status="hearing", partial_transcript=text)

        if active_turn and active_turn.speculative and text != active_turn.prompt and policy.transcript_matches(text, active_turn.prompt):
            if policy.should_replace_speculative_prompt(text, active_turn.prompt):
                await start_turn(text, speculative=True)
            return

        if (
            active_turn
            and not active_turn.speculative
            and not active_turn.playback_event.is_set()
            and not active_turn.is_speaking()
            and policy.normalized_transcript(text) != policy.normalized_transcript(active_turn.prompt)
        ):
            first_half = active_turn.committed_text or active_turn.prompt
            state.utterance_prefix = f"{state.utterance_prefix} {first_half}".strip()
            state.utterance_prefix_deadline = now + 10.0
            await cancel_active_turn("commit_continuation")
            await cancel_task(state.debounce_task)
            state.debounce_task = asyncio.create_task(
                start_after_stable_partial(f"{state.utterance_prefix} {text}")
            )
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
                if state.active_goal is not None:
                    await cancel_active_goal("committed_speech")
                state.utterance_prefix = ""
                status(status="listening", partial_transcript=None, last_committed_transcript=text)
                if session_end_caller:
                    session_end_caller()
                return
            if is_recent_assistant_echo(text, now):
                emit("echo_suppressed", source="commit", text=text)
                status(status="listening")
                return

            raw_commit = text

            if policy.has_explicit_interrupt(text):
                state.utterance_prefix = ""
            elif state.utterance_prefix and now < state.utterance_prefix_deadline:
                text = f"{state.utterance_prefix} {text}"
                state.utterance_prefix = ""
            elif state.utterance_prefix:
                state.utterance_prefix = ""

            should_start_from_commit, commit_reason = policy.commit_decision(text)
            emit("commit_decision", accepted=should_start_from_commit, reason=commit_reason, text=text)

            if state.active_goal is not None:
                await cancel_active_goal("committed_speech")
                if should_start_from_commit and not policy.has_explicit_interrupt(text):
                    status(status="thinking", partial_transcript=None, last_committed_transcript=text)
                    await start_turn(text, speculative=False)
                else:
                    status(status="listening", partial_transcript=None, last_committed_transcript=text)
                return

            active_turn = state.active_turn
            if (
                active_turn
                and active_turn.is_playing_back()
                and not policy.has_explicit_interrupt(raw_commit)
                and continuation_retraction_eligible(active_turn, text, now)
            ):
                first_half = active_turn.committed_text or active_turn.prompt
                stitched = f"{first_half} {text}".strip()
                emit("false_start", turn_id=active_turn.turn_id, text=text)
                await cancel_active_turn("continuation_retraction")
                status(status="hearing", partial_transcript=text)
                if should_start_from_commit:
                    status(status="thinking", partial_transcript=None, last_committed_transcript=stitched)
                    await start_turn(stitched, speculative=False)
                else:
                    status(status="listening", partial_transcript=None, last_committed_transcript=text)
                return
            if (
                active_turn
                and active_turn.is_playing_back()
                and (policy.has_explicit_interrupt(text) or not policy.transcript_matches(text, active_turn.prompt))
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
                    state.utterance_prefix = ""
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
                stitched = f"{active_turn.committed_text or active_turn.prompt} {text}".strip()
                await cancel_active_turn("commit_continuation")
                await start_turn(stitched, speculative=False)
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
        elif state.progress is not None:
            assistant_text = state.progress.assistant_streamed_text()
        if recent_assistant_text and now <= recent_assistant_echo_until:
            assistant_text = f"{assistant_text} {recent_assistant_text}".strip()
        return bool(assistant_text and policy.matches_assistant_echo(text, assistant_text))

    try:
        while not stop_event.is_set():
            event = await scribe_events.get()
            event_type = str(event["type"])
            if event_type == "assistant_done":
                turn = event["turn"]
                goal_request = completed_goal_request(turn)
                if goal_request is not None:
                    await begin_goal_handoff(turn, goal_request)
                else:
                    await maybe_commit_history(turn)
                    await maybe_finish_silent_turn(turn)
                continue

            if event_type == "goal_task_done":
                await finish_goal(event["goal"])
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
                elif (
                    user_speech_on
                    and now - state.last_local_speech_at > policy.local_speech_window_secs
                ):
                    end_user_speech()
                    if (
                        hearing_on
                        and (state.active_turn is None or not state.active_turn.is_active())
                        and state.active_goal is None
                        and state.progress is None
                    ):
                        status(status="listening", partial_transcript=None)
                if heard_local_audio:
                    state.local_audio_seq += 1
                levels.mic_rms = state.last_local_speech_rms
                publish_barge_in_state(now, state.last_local_speech_rms)
                if current_playback():
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
        if state.active_goal:
            await cancel_active_goal("shutdown")
