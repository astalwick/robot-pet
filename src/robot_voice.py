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
from pathlib import Path

from config.voice import DEFAULT_CONFIG_PATH, VoiceConfig, VoiceConfigError, load_voice_config
from control.motion_intent import request_motion_intent
from drivers.respeaker import WAKE_MIC_QUEUE_SIZE, ReSpeakerAudio
from lib.log import setup_logging
from telemetry.messages import voice_update
from telemetry.paths import DEFAULT_CAMERA_PORT, DEFAULT_MOTION_INTENT_SOCKET, DEFAULT_PUBLISH_SOCKET, DEFAULT_VOICE_COMMAND_SOCKET
from telemetry.socket_client import publish_message
from voice.assistant import effective_playback_rms, refresh_barge_in_gate
from voice.personality import load_personalities
from voice.session import VoiceSession
from voice.wakeword import WakeWordDetector


DEFAULT_POLL_SECONDS = 1.0
TIMELINE_HORIZON_SECS = 30.0
TIMELINE_SAMPLE_HZ = 20.0
TIMELINE_MAX_SIGNAL_EVENTS = 100
TIMELINE_MAX_STATE_EVENTS = 200
TIMELINE_MAX_PARTIAL_EVENTS = 400
PLAYBACK_RMS_STALE_SECS = 0.25
ACTIVATE_FAILURE_BACKOFF_SECS = 2.0
DEFAULT_CAMERA_SNAPSHOT_URL = f"http://127.0.0.1:{DEFAULT_CAMERA_PORT}/snapshot.jpg"

log = setup_logging("robot-voice")


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
    ) -> None:
        self.config_path = config_path
        self.telemetry_socket = telemetry_socket
        self.command_socket = command_socket
        self.poll_seconds = poll_seconds
        self.motion_intent_socket = motion_intent_socket
        self.camera_url = camera_url
        self.session: VoiceSession | None = None
        self.audio: ReSpeakerAudio | None = None
        self._io_stop_event: asyncio.Event | None = None
        self.active_config: VoiceConfig | None = None
        self._mode: str | None = None
        self._wake_event = asyncio.Event()
        self._end_session_event = asyncio.Event()
        self._idle_started_at: float | None = None
        self.status: dict[str, object] = {
            "status": "disabled",
            "assistant_speaking": False,
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
        }
        self.last_logged_error: str | None = None
        self.timeline = TimelineBuffer()
        self._sampler_task: asyncio.Task[None] | None = None
        self._wake_task: asyncio.Task[None] | None = None
        self._orchestrator_task: asyncio.Task[None] | None = None
        self._detector: WakeWordDetector | None = None
        self.personalities = load_personalities()

    async def run(self, stop_event: asyncio.Event) -> None:
        command_server = await self._start_command_server()
        try:
            while not stop_event.is_set():
                try:
                    config = load_voice_config(self.config_path)
                except VoiceConfigError as exc:
                    await self.stop_all()
                    self.publish(VoiceConfig(), status="error", last_error=str(exc))
                    await asyncio.sleep(self.poll_seconds)
                    continue

                if not config.enabled or not config.wake_word_enabled:
                    await self.stop_all()
                    self.publish(config, status="disabled", assistant_speaking=False, last_error=None)
                    await asyncio.sleep(self.poll_seconds)
                    continue

                await self._run_wake_orchestrator(config)

            await self.stop_all()
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
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                log.warning("voice command socket: invalid JSON")
                return
            if not isinstance(payload, dict):
                return
            cmd = payload.get("cmd")
            if cmd == "talk_now":
                if self._mode == "active":
                    log.info("talk-now ignored: session already active")
                    return
                if self._mode != "armed":
                    log.info("talk-now ignored: voice is not armed")
                    return
                log.info("talk-now command received")
                self._wake_event.set()
            elif cmd == "end_session":
                if self._mode != "active":
                    log.info("end-session ignored: no active session")
                    return
                log.info("end-session command received")
                self.request_end_session()
            else:
                log.warning("voice command socket: unknown cmd %r", cmd)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _run_wake_orchestrator(self, config: VoiceConfig) -> None:
        if self._orchestrator_task is None or self.active_config != config:
            await self.stop_all()
            await self.start_orchestrator(config)
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
        if self._mode == "active":
            self._end_session_event.set()

    async def start_orchestrator(self, config: VoiceConfig) -> None:
        log.info(
            "starting orchestrator: wake=%s model=%s threshold=%.2f chime=%s idle=%.1fs",
            config.wake_word_enabled,
            config.wake_word_model_path,
            config.wake_threshold,
            config.wake_chime_path,
            config.session_idle_secs,
        )
        if not Path(config.wake_chime_path).is_file():
            self.publish(config, status="error", last_error=f"Chime WAV not found: {config.wake_chime_path}")
            return

        self.publish(config, status="starting", last_error=None)
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
        )
        self._io_stop_event = asyncio.Event()
        try:
            await self.audio.start_io(self._io_stop_event)
        except Exception as exc:
            log.warning("wake audio start failed: %s", exc)
            await self.stop_all()
            self.publish(config, status="error", last_error=str(exc))
            return

        try:
            detector = WakeWordDetector(
                config.wake_word_model_path,
                threshold=config.wake_threshold,
                debounce_secs=config.wake_debounce_secs,
            )
            wake_name = detector.load()
            log.info("wake model loaded: key=%r", wake_name)
        except Exception as exc:
            log.warning("wake model load failed: %s", exc)
            await self.stop_all()
            self.publish(config, status="error", last_error=str(exc))
            return
        self._detector = detector
        self._wake_task = asyncio.create_task(self._run_wake_loop(config))

        self._mode = "armed"
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

    async def _run_orchestrator(self) -> None:
        while True:
            self._mode = "armed"
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

        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            self.publish(config, status="error", last_error=f"Missing Python dependency: {exc.name}")
            return False

        self._mode = "active"
        self._idle_started_at = time.monotonic()
        self._wake_event.clear()
        self._end_session_event.clear()
        self.publish(config, status="starting", assistant_speaking=False, last_error=None)
        motion_socket = self.motion_intent_socket
        log.info("voice motion intent socket: %s", motion_socket)
        self.session = VoiceSession(
            config,
            os.environ["ELEVENLABS_API_KEY"],
            AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"]),
            lambda update: self.publish(config, **update),
            audio=self.audio,
            event_callback=self.timeline.add_event,
            motion_intent_caller=lambda tool: request_motion_intent(motion_socket, tool, timeout=2.0),
            session_end_caller=self.request_end_session,
            camera_snapshot_caller=lambda: fetch_camera_snapshot(self.camera_url),
            personalities=self.personalities,
        )
        try:
            await self.session.start()
        except Exception as exc:
            log.warning("voice session start failed: %s", exc)
            self.session = None
            self._mode = "armed"
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
        while self._mode == "active":
            await asyncio.sleep(0.5)
            if self.status.get("status") != "listening":
                continue
            if bool(self.status.get("assistant_speaking")):
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
        self._mode = "armed"
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
                partial_transcript=None,
            )

    async def _run_wake_loop(self, config: VoiceConfig) -> None:
        audio = self.audio
        detector = self._detector
        stop_event = self._io_stop_event
        if audio is None or detector is None or stop_event is None:
            return

        async for frame in audio.mic_frames(
            stop_event,
            queue_size=WAKE_MIC_QUEUE_SIZE,
            warn_on_drop=False,
        ):
            if self._mode != "armed":
                continue
            if not detector.check(frame):
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
            try:
                await audio.play_wav(config.wake_chime_path)
            except Exception as exc:
                log.warning("wake chime playback failed: %s", exc)
                self.publish(config, status="error", last_error=str(exc))
                continue
            if not self.has_credentials():
                log.info("wake detected but API keys missing: chime-only, staying armed")
                continue
            self._wake_event.set()

    async def stop_all(self) -> None:
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
        self._mode = None
        self._wake_event.clear()
        self._end_session_event.clear()
        self._idle_started_at = None
        if self._sampler_task is not None:
            self._sampler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sampler_task
            self._sampler_task = None
        if self.session is not None:
            await self.session.stop()
            self.session = None
        if self._io_stop_event is not None:
            self._io_stop_event.set()
        if self.audio is not None:
            await self.audio.stop_io()
            self.audio = None
        self._io_stop_event = None
        self.active_config = None

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
            playback_rms = levels.playback_rms if now - levels.playback_at <= PLAYBACK_RMS_STALE_SECS else 0
            self.timeline.add_sample(
                now,
                mic,
                playback_rms,
                levels.threshold_rms,
                int(levels.gate_open),
                int(levels.scribe_gate_open),
            )
            self.timeline.trim(now)

    def publish(self, config: VoiceConfig, **updates: object) -> None:
        was_idle = (
            self._mode == "active"
            and self.status.get("status") == "listening"
            and not bool(self.status.get("assistant_speaking"))
        )
        next_status = updates.get("status", self.status.get("status"))
        next_assistant_speaking = updates.get("assistant_speaking", self.status.get("assistant_speaking"))
        self.status.update(updates)
        is_idle = self._mode == "active" and next_status == "listening" and not bool(next_assistant_speaking)
        if is_idle and not was_idle:
            self._idle_started_at = time.monotonic()
        elif not is_idle:
            self._idle_started_at = None
        last_error = optional_text(self.status["last_error"])
        if last_error and last_error != self.last_logged_error:
            log.error("voice error: %s", last_error)
            self.last_logged_error = last_error
        elif last_error is None:
            self.last_logged_error = None
        now = time.monotonic()
        voice_on = config.enabled and config.wake_word_enabled
        self.timeline.trim(now)
        publish_message(
            self.telemetry_socket,
            voice_update(
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
                personality=optional_text(self.status["personality"]),
                timeline=self.timeline.snapshot(now),
            ),
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


def fetch_camera_snapshot(camera_url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(camera_url, timeout=timeout) as response:
        return response.read()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot voice assistant service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--command-socket", default=DEFAULT_VOICE_COMMAND_SOCKET)
    parser.add_argument("--motion-intent-socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    parser.add_argument("--camera-url", default=DEFAULT_CAMERA_SNAPSHOT_URL)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
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
    )
    await service.run(stop_event)


def main() -> None:
    asyncio.run(run_service(build_parser().parse_args()))


if __name__ == "__main__":
    main()
