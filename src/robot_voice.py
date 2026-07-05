#!/usr/bin/env python3
"""Config-driven robot voice assistant service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
import urllib.request
from collections import deque
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from config.voice import DEFAULT_CONFIG_PATH, VoiceConfig, VoiceConfigError, load_voice_config, save_voice_config
from control.motion_intent import MOTION_INTENT_REPLY_TIMEOUT_SECONDS, request_motion_intent
from drivers.respeaker import MIC_BLOCKSIZE, WAKE_MIC_QUEUE_SIZE, ReSpeakerAudio, ReSpeakerDoA
from drivers.status_leds import StatusLeds
from lib.log import setup_logging
from telemetry.messages import voice_update
from telemetry.paths import (
    DEFAULT_CAMERA_PORT,
    DEFAULT_MOTION_INTENT_SOCKET,
    DEFAULT_PUBLISH_SOCKET,
    DEFAULT_SUBSCRIBE_SOCKET,
    DEFAULT_VOICE_COMMAND_SOCKET,
)
from telemetry.messages import encode_json_line
from telemetry.socket_client import publish_message, read_telemetry_snapshot
from voice.assistant import effective_playback_rms, refresh_barge_in_gate
from voice.doa import (
    ALREADY_FACING_TOLERANCE_DEGREES,
    STABLE_CACHE_MAX_AGE_SECONDS,
    DoATracker,
    to_relative_degrees,
)
from voice.personality import load_personalities
from voice.session import VoiceSession
from voice.usage import UsageTotals, cost_snapshot
from voice.wakeword import WakeWordDetector


DEFAULT_POLL_SECONDS = 1.0
TIMELINE_HORIZON_SECS = 30.0
TIMELINE_SAMPLE_HZ = 20.0
TIMELINE_PUBLISH_HZ = 5.0
TIMELINE_MAX_SIGNAL_EVENTS = 100
TIMELINE_MAX_STATE_EVENTS = 200
TIMELINE_MAX_PARTIAL_EVENTS = 400
ACTIVATE_FAILURE_BACKOFF_SECS = 2.0
WAKE_BUFFER_SECS = 2.0
WAKE_HANDOFF_TAIL_SECS = 0.3
DOA_POLL_INTERVAL_SECONDS = 0.1
DOA_REOPEN_DELAY_SECONDS = 1.0
FACE_ME_MOTION_TIMEOUT_SECONDS = MOTION_INTENT_REPLY_TIMEOUT_SECONDS
DEFAULT_CAMERA_SNAPSHOT_URL = f"http://127.0.0.1:{DEFAULT_CAMERA_PORT}/snapshot.jpg"
VOICE_STOPPED: str | None = None
VOICE_ARMED = "armed"
VOICE_ACTIVE = "active"

log = setup_logging("robot-voice")


def wake_handoff_audio(wake_buffer: deque[bytes], sample_rate: int) -> list[bytes]:
    tail_frames = max(1, round(WAKE_HANDOFF_TAIL_SECS * sample_rate / MIC_BLOCKSIZE))
    return list(wake_buffer)[-tail_frames:]


class TimelineBuffer:
    def __init__(self) -> None:
        self.levels: deque[tuple[float, int, int, int, int, int]] = deque(maxlen=int(TIMELINE_HORIZON_SECS * TIMELINE_SAMPLE_HZ) + 10)
        # Separate deques so high-frequency partials/state can't evict rare signal events.
        self.signal_events: deque[dict[str, object]] = deque(maxlen=TIMELINE_MAX_SIGNAL_EVENTS)
        self.state_events: deque[dict[str, object]] = deque(maxlen=TIMELINE_MAX_STATE_EVENTS)
        self.partial_events: deque[dict[str, object]] = deque(maxlen=TIMELINE_MAX_PARTIAL_EVENTS)

    def add_sample(
        self,
        t: float,
        mic: int,
        playback: int,
        threshold: int,
        barge_gate_open: int,
        scribe_gate_open: int,
    ) -> None:
        self.levels.append((t, mic, playback, threshold, barge_gate_open, scribe_gate_open))

    def add_event(self, event: dict[str, object]) -> None:
        kind = event.get("type")
        if kind == "phase":
            self.state_events.append(event)
        elif kind == "partial":
            self.partial_events.append(event)
        else:
            self.signal_events.append(event)

    def trim(self, now: float) -> None:
        cutoff = now - TIMELINE_HORIZON_SECS
        while self.levels and self.levels[0][0] < cutoff:
            self.levels.popleft()
        for bucket in (self.signal_events, self.state_events, self.partial_events):
            while bucket and float(bucket[0].get("t", 0.0)) < cutoff:
                bucket.popleft()

    def snapshot(self, now: float) -> dict[str, object]:
        merged = sorted(
            (*self.signal_events, *self.state_events, *self.partial_events),
            key=lambda e: float(e.get("t", 0.0)),
        )
        return {
            "ref": now,
            "horizon_secs": TIMELINE_HORIZON_SECS,
            "levels": [list(sample) for sample in self.levels],
            "events": merged,
            "latency": self.latency_stats(merged),
        }

    def latency_stats(self, events: list[dict[str, object]]) -> dict[str, object]:
        user_events: list[dict[str, object]] = []
        records: dict[object, dict[str, object]] = {}
        for event in events:
            kind = event.get("type")
            if kind in {"partial", "commit"} and event.get("text"):
                user_events.append(event)
                continue
            if kind == "turn_start":
                turn_id = event.get("turn_id")
                if turn_id is None:
                    continue
                prompt = event.get("prompt")
                source = None
                for user_event in reversed(user_events):
                    if user_event.get("text") == prompt:
                        source = user_event
                        break
                records[turn_id] = {
                    "turn_id": turn_id,
                    "speculative": bool(event.get("speculative")),
                    "input_type": source.get("type") if source else None,
                    "input_t": float(source["t"]) if source and "t" in source else None,
                    "turn_start_t": float(event.get("t", 0.0)),
                }
                continue
            turn_id = event.get("turn_id")
            if turn_id not in records:
                continue
            if kind == "turn_first_token":
                records[turn_id]["first_token_t"] = float(event.get("t", 0.0))
            elif kind == "assistant_start":
                records[turn_id]["assistant_start_t"] = float(event.get("t", 0.0))

        turns = []
        for record in records.values():
            turn = {
                "turn_id": record["turn_id"],
                "speculative": record["speculative"],
                "input_type": record["input_type"],
            }
            start_t = record["turn_start_t"]
            input_t = record["input_t"]
            first_token_t = record.get("first_token_t")
            assistant_start_t = record.get("assistant_start_t")
            if input_t is not None:
                turn["input_to_turn_ms"] = round((start_t - input_t) * 1000)
            if first_token_t is not None:
                turn["turn_to_first_token_ms"] = round((first_token_t - start_t) * 1000)
            if assistant_start_t is not None:
                turn["turn_to_audio_ms"] = round((assistant_start_t - start_t) * 1000)
            if input_t is not None and assistant_start_t is not None:
                turn["input_to_audio_ms"] = round((assistant_start_t - input_t) * 1000)
            if first_token_t is not None and assistant_start_t is not None:
                turn["first_token_to_audio_ms"] = round((assistant_start_t - first_token_t) * 1000)
            turns.append(turn)

        turns = turns[-20:]
        input_to_audio = sorted(
            turn["input_to_audio_ms"]
            for turn in turns
            if "input_to_audio_ms" in turn
        )
        median_input_to_audio_ms = None
        if input_to_audio:
            middle = len(input_to_audio) // 2
            if len(input_to_audio) % 2:
                median_input_to_audio_ms = input_to_audio[middle]
            else:
                median_input_to_audio_ms = round((input_to_audio[middle - 1] + input_to_audio[middle]) / 2)
        return {
            "turns": turns,
            "last": turns[-1] if turns else None,
            "median_input_to_audio_ms": median_input_to_audio_ms,
        }


class RobotVoiceService:
    def __init__(
        self,
        config_path: str,
        telemetry_socket: str,
        command_socket: str = DEFAULT_VOICE_COMMAND_SOCKET,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        motion_intent_socket: str = DEFAULT_MOTION_INTENT_SOCKET,
        camera_url: str = DEFAULT_CAMERA_SNAPSHOT_URL,
        profile_every: int = 0,
        telemetry_subscribe_socket: str = DEFAULT_SUBSCRIBE_SOCKET,
    ) -> None:
        self.config_path = config_path
        self.telemetry_socket = telemetry_socket
        self.command_socket = command_socket
        self.poll_seconds = poll_seconds
        self.motion_intent_socket = motion_intent_socket
        self.camera_url = camera_url
        self.profile_every = profile_every
        self.telemetry_subscribe_socket = telemetry_subscribe_socket
        self.session: VoiceSession | None = None
        self.openai_client: Any = None
        self.usage = UsageTotals()
        self.audio: ReSpeakerAudio | None = None
        self.doa_tracker = DoATracker()
        self.doa_reader: ReSpeakerDoA | None = None
        self._doa_task: asyncio.Task[None] | None = None
        self._doa_error_logged = False
        self._io_stop_event: asyncio.Event | None = None
        self.active_config: VoiceConfig | None = None
        self._mode: str | None = None
        self._wake_event = asyncio.Event()
        self._wake_audio: list[bytes] = []
        self._end_session_event = asyncio.Event()
        self._idle_started_at: float | None = None
        self.status: dict[str, object] = {
            "status": "disabled",
            "assistant_speaking": False,
            "assistant_working": False,
            "partial_transcript": None,
            "last_committed_transcript": None,
            "last_assistant_text": None,
            "last_error": None,
            "barge_in_enabled": None,
            "barge_in_threshold_rms": None,
            "barge_in_mic_rms": None,
            "barge_in_playback_rms": None,
            "barge_in_gate_open": None,
            "barge_in_last_reason": None,
            "barge_in_event_count": None,
            "barge_in_last_event": None,
            "wake_last_score": None,
            "wake_fire_count": None,
            "wake_last_fire_at": None,
            "personality": None,
            "scribe_state": None,
            "scribe_open_count": None,
            "scribe_last_error": None,
            "false_starts": 0,
        }
        self.last_logged_error: str | None = None
        self.leds = StatusLeds()
        self.timeline = TimelineBuffer()
        self._sampler_task: asyncio.Task[None] | None = None
        self._wake_task: asyncio.Task[None] | None = None
        self._orchestrator_task: asyncio.Task[None] | None = None
        self._detector: WakeWordDetector | None = None
        self.personalities = load_personalities()
        self.selected_personality: str | None = None
        self._orchestrator_startup_latched: VoiceConfig | None = None
        self._orchestrator_startup_error: str | None = None
        self._config_load_error_text: str | None = None
        self._config_load_error_config: str | None = None
        self._wake_profile_count = 0
        self._publish_profile_count = 0
        self._last_timeline_publish_at = 0.0

    async def run(self, stop_event: asyncio.Event) -> None:
        command_server = await self._start_command_server()
        try:
            while not stop_event.is_set():
                try:
                    config = load_voice_config(self.config_path)
                except VoiceConfigError as exc:
                    error_text = str(exc)
                    config_text = None
                    with suppress(OSError):
                        config_text = Path(self.config_path).read_text()
                    if self._config_load_error_text != error_text or self._config_load_error_config != config_text:
                        await self.stop_all(
                            final_config=VoiceConfig(),
                            final_status="error",
                            final_error=error_text,
                        )
                        self._config_load_error_text = error_text
                        self._config_load_error_config = config_text
                    await asyncio.sleep(self.poll_seconds)
                    continue

                self._config_load_error_text = None
                self._config_load_error_config = None

                if not config.enabled or not config.wake_word_enabled:
                    await self.stop_all(final_config=config, final_status="disabled")
                    await asyncio.sleep(self.poll_seconds)
                    continue

                await self._run_wake_orchestrator(config)

            final_config = None
            if self.active_config is not None:
                final_config = replace(self.active_config, enabled=False, wake_word_enabled=False)
            await self.stop_all(final_config=final_config, final_status="disabled")
        finally:
            if command_server is not None:
                command_server.close()
                with suppress(Exception):
                    await command_server.wait_closed()
                with suppress(FileNotFoundError):
                    os.unlink(self.command_socket)

    async def _start_command_server(self) -> asyncio.AbstractServer | None:
        try:
            Path(self.command_socket).parent.mkdir(parents=True, exist_ok=True)
            with suppress(FileNotFoundError):
                os.unlink(self.command_socket)
            server = await asyncio.start_unix_server(self._handle_command, path=self.command_socket)
        except OSError as exc:
            log.warning("voice command socket disabled: %s", exc)
            return None
        log.info("voice command socket listening: %s", self.command_socket)
        return server

    async def _handle_command(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ack: dict[str, object] = {"ok": False, "accepted": False, "reason": "empty_request"}
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                log.warning("voice command socket: invalid JSON")
                ack = {"ok": True, "accepted": False, "reason": "invalid_json"}
                return
            ack = self._process_command(payload)
        finally:
            writer.write(encode_json_line(ack))
            await writer.drain()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    def _process_command(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            return {"ok": True, "accepted": False, "reason": "invalid_payload"}
        cmd = payload.get("cmd")
        if cmd == "talk_now":
            if self._mode == VOICE_ACTIVE:
                log.info("talk-now ignored: session already active")
                return {"ok": True, "accepted": False, "reason": "session_already_active"}
            if self._mode != VOICE_ARMED:
                log.info("talk-now ignored: voice is not armed")
                return {"ok": True, "accepted": False, "reason": "not_armed"}
            log.info("talk-now command received")
            self._wake_event.set()
            return {"ok": True, "accepted": True, "reason": None}
        if cmd == "end_session":
            if self._mode != VOICE_ACTIVE:
                log.info("end-session ignored: no active session")
                return {"ok": True, "accepted": False, "reason": "no_active_session"}
            log.info("end-session command received")
            self.request_end_session()
            return {"ok": True, "accepted": True, "reason": None}
        if cmd == "set_personality":
            name = payload.get("name")
            if not isinstance(name, str) or name not in self.personalities:
                log.warning("set-personality ignored: unknown personality %r", name)
                return {"ok": True, "accepted": False, "reason": "unknown_personality"}
            log.info("set-personality command received: %s", name)
            self.selected_personality = name
            if self.session is not None:
                self.session.set_personality(name)
            if self.active_config is not None:
                self.publish(self.active_config, personality=name)
            self._persist_personality(name)
            return {"ok": True, "accepted": True, "reason": None}
        log.warning("voice command socket: unknown cmd %r", cmd)
        return {"ok": True, "accepted": False, "reason": "unknown_cmd"}

    def _persist_personality(self, name: str) -> None:
        try:
            config = load_voice_config(self.config_path)
            if config.personality == name:
                return
            save_voice_config(VoiceConfig.from_dict({**config.to_dict(), "personality": name}), self.config_path)
        except (VoiceConfigError, OSError) as exc:
            log.warning("personality config save failed: %s", exc)

    async def _run_wake_orchestrator(self, config: VoiceConfig) -> None:
        if self._orchestrator_startup_latched is not None and same_orchestrator_config(
            self._orchestrator_startup_latched, config
        ):
            self.publish(config, status="error", last_error=self._orchestrator_startup_error)
            await asyncio.sleep(self.poll_seconds)
            return
        if self._orchestrator_startup_latched is not None:
            self._orchestrator_startup_latched = None
            self._orchestrator_startup_error = None

        if self.active_config is not None and same_orchestrator_config(self.active_config, config):
            self.active_config = config

        if self._orchestrator_task is None or not same_orchestrator_config(self.active_config, config):
            await self.stop_all()
            if not await self.start_orchestrator(config):
                await asyncio.sleep(self.poll_seconds)
                return
        elif self._orchestrator_task.done():
            exc = self._orchestrator_task.exception()
            if exc is not None:
                log.warning("wake orchestrator failed: %s", exc)
                last_error = str(exc)
            else:
                log.warning("wake orchestrator exited unexpectedly")
                last_error = "Wake orchestrator exited"
            await self.stop_all()
            self.publish(config, status="reconnecting", last_error=last_error)
            await asyncio.sleep(min(5.0, self.poll_seconds * 2))
            return

        await asyncio.sleep(self.poll_seconds)

    def request_end_session(self) -> None:
        if self._mode == VOICE_ACTIVE:
            self._end_session_event.set()

    def _latch_orchestrator_startup(self, config: VoiceConfig, error: str) -> None:
        if self._orchestrator_startup_latched is not None and same_orchestrator_config(
            self._orchestrator_startup_latched, config
        ):
            return
        self._orchestrator_startup_latched = config
        self._orchestrator_startup_error = error
        self.publish(config, status="error", last_error=error)

    async def start_orchestrator(self, config: VoiceConfig) -> bool:
        if self._orchestrator_startup_latched is not None and same_orchestrator_config(
            self._orchestrator_startup_latched, config
        ):
            return False

        log.info(
            "starting orchestrator: wake=%s model=%s threshold=%.2f rms_gate=%s chime=%s idle=%.1fs",
            config.wake_word_enabled,
            config.wake_word_model_path,
            config.wake_threshold,
            config.wake_rms_gate_min if config.wake_rms_gate_min > 0 else "off",
            config.wake_chime_path,
            config.session_idle_secs,
        )
        if not Path(config.wake_chime_path).is_file():
            error = f"Chime WAV not found: {config.wake_chime_path}"
            self._latch_orchestrator_startup(config, error)
            return False

        self._orchestrator_startup_latched = None
        self._orchestrator_startup_error = None
        self.publish(config, status="starting", last_error=None)
        if self.has_credentials():
            try:
                from openai import AsyncOpenAI
            except ModuleNotFoundError as exc:
                self._latch_orchestrator_startup(config, f"Missing Python dependency: {exc.name}")
                return False
            self.openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.active_config = config
        self.audio = ReSpeakerAudio(
            input_device=config.input_device,
            output_device=config.output_device,
            sample_rate=config.sample_rate,
            capture_channels=config.capture_channels,
            capture_channel_index=config.capture_channel_index,
            output_channels=config.output_channels,
            input_gain=config.input_gain,
            output_gain=config.output_gain,
            profile_every=self.profile_every,
        )
        self._io_stop_event = asyncio.Event()
        try:
            await self.audio.start_io(self._io_stop_event)
        except Exception as exc:
            log.warning("wake audio start failed: %s", exc)
            await self.stop_all()
            self.publish(config, status="error", last_error=str(exc))
            return False

        self._doa_error_logged = False
        self._doa_task = asyncio.create_task(self._run_doa_loop())

        try:
            detector = WakeWordDetector(
                config.wake_word_model_path,
                threshold=config.wake_threshold,
                debounce_secs=config.wake_debounce_secs,
                rms_gate_min=config.wake_rms_gate_min,
            )
            wake_name = detector.load()
            log.info("wake model loaded: key=%r", wake_name)
        except Exception as exc:
            log.warning("wake model load failed: %s", exc)
            await self.stop_all()
            self._latch_orchestrator_startup(config, str(exc))
            return False
        self._detector = detector
        self._wake_task = asyncio.create_task(self._run_wake_loop(config))

        self._mode = VOICE_ARMED
        self._idle_started_at = None
        self._orchestrator_task = asyncio.create_task(self._run_orchestrator())
        self.publish(
            config,
            status="waiting",
            last_error=None,
            assistant_speaking=False,
            partial_transcript=None,
            last_committed_transcript=None,
            last_assistant_text=None,
            wake_last_score=0.0,
            wake_fire_count=0,
            wake_last_fire_at=None,
        )
        return True

    async def _run_orchestrator(self) -> None:
        while True:
            self._mode = VOICE_ARMED
            config = self.active_config
            if config is None:
                return
            self.publish(
                config,
                status="waiting",
                assistant_speaking=False,
                partial_transcript=None,
                last_committed_transcript=None,
                last_assistant_text=None,
            )
            await self._wait_for_session_trigger()
            if self._io_stop_event is not None and self._io_stop_event.is_set():
                return
            if not await self._activate_session():
                await asyncio.sleep(ACTIVATE_FAILURE_BACKOFF_SECS)
                continue
            await self._wait_for_session_end()
            await self._deactivate_session()
            self._wake_event.clear()

    async def _wait_for_session_trigger(self) -> None:
        while True:
            if self._io_stop_event is not None and self._io_stop_event.is_set():
                return
            if self.active_config is None:
                return
            if self._wake_event.is_set():
                return
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.25)
                return
            except asyncio.TimeoutError:
                continue

    async def _activate_session(self) -> bool:
        config = self.active_config
        if config is None or self.audio is None:
            return False
        if not self.has_credentials():
            self.publish(config, status="error", last_error="Missing ELEVENLABS_API_KEY or OPENAI_API_KEY")
            self._wake_event.clear()
            return False

        self._mode = VOICE_ACTIVE
        self._idle_started_at = time.monotonic()
        self._wake_event.clear()
        self._end_session_event.clear()
        # Consume-and-clear so a later talk_now session doesn't replay old wake audio.
        wake_audio = self._wake_audio
        self._wake_audio = []
        self.publish(config, status="starting", assistant_speaking=False, last_error=None)
        motion_socket = self.motion_intent_socket
        log.info("voice motion intent socket: %s", motion_socket)
        self.personalities = load_personalities()
        self.session = VoiceSession(
            config,
            os.environ["ELEVENLABS_API_KEY"],
            self.openai_client,
            lambda update: self.publish(config, **update),
            audio=self.audio,
            event_callback=self.timeline.add_event,
            motion_intent_caller=lambda tool, **params: request_motion_intent(
                motion_socket,
                tool,
                timeout=MOTION_INTENT_REPLY_TIMEOUT_SECONDS,
                **params,
            ),
            session_end_caller=self.request_end_session,
            camera_snapshot_caller=lambda: fetch_camera_snapshot(self.camera_url),
            robot_inspection_caller=lambda: read_telemetry_snapshot(self.telemetry_subscribe_socket),
            face_me_caller=self.face_me_caller,
            speaker_direction_caller=self.doa_snapshot,
            personalities=self.personalities,
            profile_every=self.profile_every,
            usage=self.usage,
            wake_audio=wake_audio,
        )
        if self.selected_personality is not None:
            self.session.set_personality(self.selected_personality)
        try:
            await self.session.start()
        except Exception as exc:
            log.warning("voice session start failed: %s", exc)
            self.session = None
            self._mode = VOICE_ARMED
            self.publish(config, status="error", last_error=str(exc))
            return False
        self._sampler_task = asyncio.create_task(self._sample_timeline())
        return True

    async def _wait_for_session_end(self) -> None:
        if self.session is None:
            return
        config = self.active_config
        idle_task = asyncio.create_task(self._wait_for_idle())
        session_task = asyncio.create_task(self.session.wait())
        end_task = asyncio.create_task(self._end_session_event.wait())
        done, pending = await asyncio.wait(
            {idle_task, session_task, end_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if end_task in done:
            log.info("voice session ended by request")
        elif session_task in done and not session_task.cancelled():
            session_task.result()
        elif config is not None and idle_task in done:
            log.info("voice session idle after %.1fs without activity", config.session_idle_secs)

    async def _wait_for_idle(self) -> None:
        config = self.active_config
        if config is None or config.session_idle_secs <= 0 or not config.wake_word_enabled:
            await asyncio.Event().wait()
            return
        while self._mode == VOICE_ACTIVE:
            await asyncio.sleep(0.5)
            if bool(self.status.get("assistant_speaking")):
                continue
            if bool(self.status.get("assistant_working")):
                continue
            if self._idle_started_at is None:
                self._idle_started_at = time.monotonic()
                continue
            if time.monotonic() - self._idle_started_at < config.session_idle_secs:
                continue
            return

    async def _deactivate_session(self) -> None:
        config = self.active_config
        audio = self.audio
        if self._sampler_task is not None:
            self._sampler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sampler_task
            self._sampler_task = None
        if self.session is not None:
            await self.session.stop()
            self.session.history.clear()
            self.session = None
        self._idle_started_at = None
        self._end_session_event.clear()
        if self._detector is not None:
            self._detector.reset()
        self._mode = VOICE_ARMED
        self._close_timeline_phases()
        if config is not None and config.wake_word_enabled and audio is not None:
            chime_path = config.session_end_chime_path
            if Path(chime_path).is_file():
                try:
                    await audio.play_wav(chime_path)
                except Exception as exc:
                    log.warning("session end chime failed: %s", exc)
            else:
                log.warning("session end chime not found: %s", chime_path)
        if config is not None:
            self.publish(
                config,
                status="waiting",
                assistant_speaking=False,
                assistant_working=False,
                partial_transcript=None,
                force_timeline=True,
            )
            self._last_timeline_publish_at = 0.0

    def _close_timeline_phases(self) -> None:
        now = time.monotonic()
        for name in ("hearing", "thinking", "speaking", "user_speech"):
            self.timeline.add_event({"type": "phase", "t": now, "name": name, "on": False})

    async def _run_wake_loop(self, config: VoiceConfig) -> None:
        audio = self.audio
        detector = self._detector
        stop_event = self._io_stop_event
        if audio is None or detector is None or stop_event is None:
            return

        wake_buffer: deque[bytes] = deque(maxlen=int(WAKE_BUFFER_SECS * config.sample_rate / MIC_BLOCKSIZE))
        async for frame in audio.mic_frames(
            stop_event,
            queue_size=WAKE_MIC_QUEUE_SIZE,
            warn_on_drop=False,
        ):
            wake_buffer.append(frame)
            if self._mode != VOICE_ARMED:
                continue
            started = time.perf_counter()
            woke = detector.check(frame)
            self._maybe_log_wake_profile(detector, time.perf_counter() - started)
            if not woke:
                continue
            score = detector.last_score
            log.info("wake detected score=%.4f threshold=%.2f", score, config.wake_threshold)
            self.publish(
                config,
                status="waiting",
                wake_last_score=score,
                wake_fire_count=detector.fire_count,
                wake_last_fire_at=detector.last_fire_at,
            )
            if not self.has_credentials():
                log.info("wake detected but API keys missing: chime-only, staying armed")
                try:
                    await audio.play_wav(config.wake_chime_path)
                except Exception as exc:
                    log.warning("wake chime playback failed: %s", exc)
                    self.publish(config, status="error", last_error=str(exc))
                continue
            # Activate first so the session (and Scribe pre-open) come up behind the
            # chime, hiding the connect latency. The orchestrator runs in its own task.
            self._wake_audio = wake_handoff_audio(wake_buffer, config.sample_rate)
            self._wake_event.set()
            try:
                await audio.play_wav(config.wake_chime_path)
            except Exception as exc:
                log.warning("wake chime playback failed: %s", exc)
                self.publish(config, last_error=str(exc))

    def _open_doa_reader(self) -> ReSpeakerDoA:
        # Hardware boundary, kept tiny so tests can swap in a fake reader.
        return ReSpeakerDoA.open()

    async def _run_doa_loop(self) -> None:
        while True:
            if self.doa_reader is None:
                try:
                    self.doa_reader = await asyncio.to_thread(self._open_doa_reader)
                    log.info("ReSpeaker DoA reader opened")
                    self._doa_error_logged = False
                except Exception as exc:
                    if not self._doa_error_logged:
                        log.warning("ReSpeaker DoA unavailable: %s", exc)
                        self._doa_error_logged = True
                    await asyncio.sleep(DOA_REOPEN_DELAY_SECONDS)
                    continue

            try:
                reading = await asyncio.to_thread(self.doa_reader.read)
            except Exception as exc:
                if not self._doa_error_logged:
                    log.warning("ReSpeaker DoA read failed: %s", exc)
                    self._doa_error_logged = True
                self._close_doa_reader()
                await asyncio.sleep(DOA_REOPEN_DELAY_SECONDS)
                continue

            self._doa_error_logged = False
            assistant_speaking = bool(self.status.get("assistant_speaking"))
            self.doa_tracker.update(reading, time.monotonic(), assistant_speaking)
            await asyncio.sleep(DOA_POLL_INTERVAL_SECONDS)

    def _close_doa_reader(self) -> None:
        if self.doa_reader is None:
            return
        with suppress(Exception):
            self.doa_reader.close()
        self.doa_reader = None

    def face_me_caller(self) -> dict[str, object]:
        age = self.doa_tracker.age(time.monotonic())
        if age is None:
            return {"ok": False, "error": "speaker_direction_unavailable"}
        if age > STABLE_CACHE_MAX_AGE_SECONDS:
            return {"ok": False, "error": "speaker_direction_stale"}
        relative_degrees = to_relative_degrees(self.doa_tracker.stable_angle)
        if abs(relative_degrees) <= ALREADY_FACING_TOLERANCE_DEGREES:
            return {"ok": True, "result": "already_facing", "relative_degrees": relative_degrees}
        return request_motion_intent(
            self.motion_intent_socket,
            "face_me",
            timeout=FACE_ME_MOTION_TIMEOUT_SECONDS,
            relative_degrees=relative_degrees,
        )

    def doa_snapshot(self, now: float | None = None) -> dict[str, object]:
        age = self.doa_tracker.age(now if now is not None else time.monotonic())
        if age is None:
            return {"connected": self.doa_reader is not None, "relative_degrees": None, "age_seconds": None, "fresh": False}
        return {
            "connected": self.doa_reader is not None,
            "relative_degrees": to_relative_degrees(self.doa_tracker.stable_angle),
            "age_seconds": round(age, 1),
            "fresh": age <= STABLE_CACHE_MAX_AGE_SECONDS,
        }

    async def stop_all(
        self,
        *,
        final_config: VoiceConfig | None = None,
        final_status: str | None = None,
        final_error: str | None = None,
    ) -> None:
        if self._orchestrator_task is not None:
            orchestrator_task = self._orchestrator_task
            self._orchestrator_task = None
            if orchestrator_task.done():
                orchestrator_task.exception()
            else:
                orchestrator_task.cancel()
                with suppress(asyncio.CancelledError):
                    await orchestrator_task
        if self._wake_task is not None:
            wake_task = self._wake_task
            self._wake_task = None
            if wake_task.done():
                wake_task.exception()
            else:
                wake_task.cancel()
                with suppress(asyncio.CancelledError):
                    await wake_task
        self._detector = None
        self._mode = VOICE_STOPPED
        self._wake_event.clear()
        self._end_session_event.clear()
        self._idle_started_at = None
        if self._sampler_task is not None:
            self._sampler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sampler_task
            self._sampler_task = None
        if self._doa_task is not None:
            self._doa_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._doa_task
            self._doa_task = None
        self._close_doa_reader()
        if self.session is not None:
            await self.session.stop()
            self.session = None
        if self._io_stop_event is not None:
            self._io_stop_event.set()
        if self.audio is not None:
            await self.audio.stop_io()
            self.audio = None
        self.openai_client = None
        self._io_stop_event = None
        self.active_config = None
        self._orchestrator_startup_latched = None
        self._orchestrator_startup_error = None
        self._close_timeline_phases()
        self._last_timeline_publish_at = 0.0
        self.status.update(
            {
                "assistant_speaking": False,
                "assistant_working": False,
                "partial_transcript": None,
                "last_committed_transcript": None,
                "last_assistant_text": None,
                "barge_in_threshold_rms": None,
                "barge_in_mic_rms": None,
                "barge_in_playback_rms": None,
                "barge_in_gate_open": None,
                "barge_in_last_reason": None,
                "barge_in_event_count": None,
                "barge_in_last_event": None,
                "scribe_state": "closed",
                "scribe_last_error": None,
                "false_starts": 0,
            }
        )
        self.leds.update(voice_on=False, stt_active=False, llm_active=False)
        if final_config is not None and final_status is not None:
            self.publish(
                final_config,
                status=final_status,
                last_error=final_error,
                force_timeline=True,
            )

    async def _sample_timeline(self) -> None:
        interval = 1.0 / TIMELINE_SAMPLE_HZ
        while True:
            await asyncio.sleep(interval)
            session = self.session
            if session is None:
                continue
            levels = session.audio_levels
            now = time.monotonic()
            mic = levels.mic_peak
            levels.mic_peak = 0
            assistant_speaking = bool(self.status.get("assistant_speaking"))
            refresh_barge_in_gate(
                levels,
                now,
                session.policy,
                assistant_speaking,
                mic,
            )
            playback_rms = effective_playback_rms(levels, now)
            self.timeline.add_sample(
                now,
                mic,
                playback_rms,
                levels.threshold_rms,
                int(levels.gate_open),
                int(levels.scribe_gate_open),
            )
            self.timeline.trim(now)

    def publish(self, config: VoiceConfig, *, force_timeline: bool = False, **updates: object) -> None:
        started = time.perf_counter()
        next_status = updates.get("status", self.status.get("status"))
        next_assistant_speaking = updates.get("assistant_speaking", self.status.get("assistant_speaking"))
        next_assistant_working = updates.get("assistant_working", self.status.get("assistant_working"))
        user_activity = updates.get("status") == "hearing" or bool(updates.get("partial_transcript"))
        self.status.update(updates)
        if self._mode != VOICE_ACTIVE:
            self._idle_started_at = None
        elif bool(next_assistant_speaking) or bool(next_assistant_working) or user_activity:
            self._idle_started_at = None
        elif self._idle_started_at is None:
            self._idle_started_at = time.monotonic()
        last_error = optional_text(self.status["last_error"])
        if last_error and last_error != self.last_logged_error:
            log.error("voice error: %s", last_error)
            self.last_logged_error = last_error
        elif last_error is None:
            self.last_logged_error = None
        now = time.monotonic()
        voice_on = config.enabled and config.wake_word_enabled
        self.leds.update(
            voice_on=voice_on,
            stt_active=voice_on and self.status["scribe_state"] in ("uploading", "waiting_for_commit"),
            llm_active=self.status["status"] == "thinking",
        )
        timeline = None
        timeline_seconds = 0.0
        timeline_started = time.perf_counter()
        if force_timeline or now - self._last_timeline_publish_at >= 1.0 / TIMELINE_PUBLISH_HZ:
            self._last_timeline_publish_at = now
            self.timeline.trim(now)
            timeline = self.timeline.snapshot(now)
            timeline_seconds = time.perf_counter() - timeline_started
        message_started = time.perf_counter()
        message = voice_update(
            enabled=voice_on,
            status=str(self.status["status"]),
            input_device=config.input_device,
            output_device=config.output_device,
            sample_rate=config.sample_rate,
            capture_channels=config.capture_channels,
            capture_channel_index=config.capture_channel_index,
            input_gain=config.input_gain,
            output_gain=config.output_gain,
            assistant_speaking=bool(self.status["assistant_speaking"]),
            partial_transcript=optional_text(self.status["partial_transcript"]),
            last_committed_transcript=optional_text(self.status["last_committed_transcript"]),
            last_assistant_text=optional_text(self.status["last_assistant_text"]),
            last_error=last_error,
            barge_in_enabled=(
                optional_bool(self.status["barge_in_enabled"])
                if self.status["barge_in_enabled"] is not None
                else config.barge_in_enabled
            ),
            barge_in_min_rms=config.barge_in_min_rms,
            barge_in_sustain_ms=config.barge_in_sustain_ms,
            barge_in_threshold_rms=optional_int(self.status["barge_in_threshold_rms"]),
            barge_in_mic_rms=optional_int(self.status["barge_in_mic_rms"]),
            barge_in_playback_rms=optional_int(self.status["barge_in_playback_rms"]),
            barge_in_gate_open=optional_bool(self.status["barge_in_gate_open"]),
            barge_in_last_reason=optional_text(self.status["barge_in_last_reason"]),
            barge_in_event_count=optional_int(self.status["barge_in_event_count"]),
            barge_in_last_event=optional_text(self.status["barge_in_last_event"]),
            wake_word_enabled=voice_on,
            wake_threshold=config.wake_threshold if voice_on else None,
            wake_last_score=optional_float(self.status["wake_last_score"]),
            wake_fire_count=optional_int(self.status["wake_fire_count"]),
            wake_last_fire_at=optional_float(self.status["wake_last_fire_at"]),
            personality=optional_text(self.status["personality"]) or config.personality,
            doa=self.doa_snapshot(now),
            timeline=timeline,
            cost=cost_snapshot(self.usage),
            scribe_state=optional_text(self.status["scribe_state"]),
            scribe_open_count=optional_int(self.status["scribe_open_count"]),
            scribe_last_error=optional_text(self.status["scribe_last_error"]),
            false_starts=optional_int(self.status["false_starts"]),
        )
        message_seconds = time.perf_counter() - message_started
        publish_started = time.perf_counter()
        publish_message(self.telemetry_socket, message)
        publish_seconds = time.perf_counter() - publish_started
        self._maybe_log_publish_profile(
            timeline_seconds,
            message_seconds,
            publish_seconds,
            time.perf_counter() - started,
        )

    def _maybe_log_wake_profile(self, detector: WakeWordDetector, total_seconds: float) -> None:
        if self.profile_every <= 0:
            return
        self._wake_profile_count += 1
        if self._wake_profile_count % self.profile_every != 0:
            return
        log.info(
            "voice wake profile: predict=%.1fms total=%.1fms score=%.4f",
            detector.last_predict_seconds * 1000,
            total_seconds * 1000,
            detector.last_score,
        )

    def _maybe_log_publish_profile(
        self,
        timeline_seconds: float,
        message_seconds: float,
        publish_seconds: float,
        total_seconds: float,
    ) -> None:
        if self.profile_every <= 0:
            return
        self._publish_profile_count += 1
        if self._publish_profile_count % self.profile_every != 0:
            return
        log.info(
            "voice publish profile: timeline=%.1fms message=%.1fms socket=%.1fms total=%.1fms",
            timeline_seconds * 1000,
            message_seconds * 1000,
            publish_seconds * 1000,
            total_seconds * 1000,
        )

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("OPENAI_API_KEY"))


def optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def same_orchestrator_config(left: VoiceConfig | None, right: VoiceConfig | None) -> bool:
    if left is None or right is None:
        return False
    left_values = left.to_dict()
    right_values = right.to_dict()
    left_values.pop("personality", None)
    right_values.pop("personality", None)
    return left_values == right_values


def fetch_camera_snapshot(camera_url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(camera_url, timeout=timeout) as response:
        return response.read()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot voice assistant service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--telemetry-subscribe-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    parser.add_argument("--command-socket", default=DEFAULT_VOICE_COMMAND_SOCKET)
    parser.add_argument("--motion-intent-socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    parser.add_argument("--camera-url", default=DEFAULT_CAMERA_SNAPSHOT_URL)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--profile-every", type=int, default=0, help="Log voice stage timings every N frames/events")
    return parser


async def run_service(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    service = RobotVoiceService(
        args.config,
        args.telemetry_socket,
        command_socket=args.command_socket,
        poll_seconds=args.poll_seconds,
        motion_intent_socket=args.motion_intent_socket,
        camera_url=args.camera_url,
        profile_every=args.profile_every,
        telemetry_subscribe_socket=args.telemetry_subscribe_socket,
    )
    await service.run(stop_event)


def main() -> None:
    asyncio.run(run_service(build_parser().parse_args()))


if __name__ == "__main__":
    main()
