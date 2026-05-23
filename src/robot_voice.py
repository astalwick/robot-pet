#!/usr/bin/env python3
"""Config-driven robot voice assistant service."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from collections import deque
from contextlib import suppress

from config.voice import DEFAULT_CONFIG_PATH, VoiceConfig, VoiceConfigError, load_voice_config
from drivers.respeaker import ReSpeakerAudio
from lib.log import setup_logging
from telemetry.messages import voice_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message
from voice.assistant import effective_playback_rms, refresh_barge_in_gate
from voice.session import VoiceSession


DEFAULT_POLL_SECONDS = 1.0
TIMELINE_HORIZON_SECS = 30.0
TIMELINE_SAMPLE_HZ = 20.0
TIMELINE_MAX_SIGNAL_EVENTS = 100
TIMELINE_MAX_STATE_EVENTS = 200
TIMELINE_MAX_PARTIAL_EVENTS = 400
PLAYBACK_RMS_STALE_SECS = 0.25

log = setup_logging("robot-voice")


class TimelineBuffer:
    def __init__(self) -> None:
        self.levels: deque[tuple[float, int, int, int, int]] = deque(maxlen=int(TIMELINE_HORIZON_SECS * TIMELINE_SAMPLE_HZ) + 10)
        # Separate deques so high-frequency partials/state can't evict rare signal events.
        self.signal_events: deque[dict[str, object]] = deque(maxlen=TIMELINE_MAX_SIGNAL_EVENTS)
        self.state_events: deque[dict[str, object]] = deque(maxlen=TIMELINE_MAX_STATE_EVENTS)
        self.partial_events: deque[dict[str, object]] = deque(maxlen=TIMELINE_MAX_PARTIAL_EVENTS)

    def add_sample(self, t: float, mic: int, playback: int, threshold: int, gate_open: int) -> None:
        self.levels.append((t, mic, playback, threshold, gate_open))

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
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.config_path = config_path
        self.telemetry_socket = telemetry_socket
        self.poll_seconds = poll_seconds
        self.session: VoiceSession | None = None
        self.audio: ReSpeakerAudio | None = None
        self._io_stop_event: asyncio.Event | None = None
        self.active_config: VoiceConfig | None = None
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
        }
        self.last_logged_error: str | None = None
        self.timeline = TimelineBuffer()
        self._sampler_task: asyncio.Task[None] | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                config = load_voice_config(self.config_path)
            except VoiceConfigError as exc:
                await self.stop_session()
                self.publish(VoiceConfig(), status="error", last_error=str(exc))
                await asyncio.sleep(self.poll_seconds)
                continue

            if not config.enabled:
                await self.stop_session()
                self.publish(config, status="disabled", assistant_speaking=False, last_error=None)
                await asyncio.sleep(self.poll_seconds)
                continue

            if not self.has_credentials():
                await self.stop_session()
                self.publish(config, status="error", last_error="Missing ELEVENLABS_API_KEY or OPENAI_API_KEY")
                await asyncio.sleep(self.poll_seconds)
                continue

            if self.session is None or self.active_config != config:
                await self.stop_session()
                await self.start_session(config)

            if self.session is not None:
                wait_task = asyncio.create_task(self.session.wait())
                sleep_task = asyncio.create_task(asyncio.sleep(self.poll_seconds))
                done, pending = await asyncio.wait({wait_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if wait_task in done:
                    try:
                        wait_task.result()
                    except Exception as exc:
                        log.warning("voice session failed: %s", exc)
                        await self.stop_session()
                        self.publish(config, status="reconnecting", last_error=str(exc))
                        await asyncio.sleep(min(5.0, self.poll_seconds * 2))
                continue

            await asyncio.sleep(self.poll_seconds)

        await self.stop_session()

    async def start_session(self, config: VoiceConfig) -> None:
        log.info(
            "starting voice: input=%s output=%s rate=%s channels=%s selected_channel=%s",
            config.input_device,
            config.output_device,
            config.sample_rate,
            config.capture_channels,
            config.capture_channel_index,
        )
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            self.publish(config, status="error", last_error=f"Missing Python dependency: {exc.name}")
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
            log.warning("voice audio start failed: %s", exc)
            await self.stop_session()
            self.publish(config, status="error", last_error=str(exc))
            return
        self.session = VoiceSession(
            config,
            os.environ["ELEVENLABS_API_KEY"],
            AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"]),
            lambda update: self.publish(config, **update),
            audio=self.audio,
            event_callback=self.timeline.add_event,
        )
        try:
            await self.session.start()
        except Exception as exc:
            log.warning("voice session start failed: %s", exc)
            await self.stop_session()
            self.publish(config, status="error", last_error=str(exc))
            return
        self._sampler_task = asyncio.create_task(self._sample_timeline())

    async def stop_session(self) -> None:
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
            mic = int(levels.get("mic_peak", 0))
            levels["mic_peak"] = 0
            assistant_speaking = bool(self.status.get("assistant_speaking"))
            refresh_barge_in_gate(
                levels,
                now,
                session.policy,
                assistant_speaking,
                mic,
            )
            playback_at = float(levels.get("playback_at", 0.0))
            playback_rms = int(levels.get("playback_rms", 0)) if now - playback_at <= PLAYBACK_RMS_STALE_SECS else 0
            self.timeline.add_sample(
                now,
                mic,
                playback_rms,
                int(levels.get("threshold_rms", 0)),
                int(levels.get("gate_open", 0)),
            )
            self.timeline.trim(now)

    def publish(self, config: VoiceConfig, **updates: object) -> None:
        self.status.update(updates)
        last_error = optional_text(self.status["last_error"])
        if last_error and last_error != self.last_logged_error:
            log.error("voice error: %s", last_error)
            self.last_logged_error = last_error
        elif last_error is None:
            self.last_logged_error = None
        now = time.monotonic()
        self.timeline.trim(now)
        publish_message(
            self.telemetry_socket,
            voice_update(
                enabled=config.enabled,
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
                barge_in_playback_leakage_ratio=config.barge_in_playback_leakage_ratio,
                barge_in_threshold_rms=optional_int(self.status["barge_in_threshold_rms"]),
                barge_in_mic_rms=optional_int(self.status["barge_in_mic_rms"]),
                barge_in_playback_rms=optional_int(self.status["barge_in_playback_rms"]),
                barge_in_gate_open=optional_bool(self.status["barge_in_gate_open"]),
                barge_in_last_reason=optional_text(self.status["barge_in_last_reason"]),
                barge_in_event_count=optional_int(self.status["barge_in_event_count"]),
                barge_in_last_event=optional_text(self.status["barge_in_last_event"]),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot voice assistant service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


async def run_service(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    await RobotVoiceService(args.config, args.telemetry_socket, args.poll_seconds).run(stop_event)


def main() -> None:
    asyncio.run(run_service(build_parser().parse_args()))


if __name__ == "__main__":
    main()
